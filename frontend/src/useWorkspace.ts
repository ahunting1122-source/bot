import { useCallback, useEffect, useRef, useState } from "react";
import { api, ensureSession, type Config, type Conversation, type FileScope, type Message, type Task, type TaskEvent } from "./api";

export type LiveReply = { id: string; responseId: string; content: string };
export type TokenMeter = {
  tokensPerSecond: number;
  completionTokens: number;
  promptTokens: number;
  totalTokens: number;
};
const activeStatuses = ["PENDING", "RUNNING"];

export function useWorkspace() {
  const [config, setConfig] = useState<Config | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [selected, setSelectedState] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [task, setTask] = useState<Task | null>(null);
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const [liveReply, setLiveReply] = useState<LiveReply | null>(null);
  const [tokenMeter, setTokenMeter] = useState<TokenMeter | null>(null);
  const [background, setBackground] = useState<{ report: string } | null>(null);
  const [error, setError] = useState("");
  const [connectionNote, setConnectionNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const selectedRef = useRef<string | null>(null);
  const busyRef = useRef(false);
  const cursor = useRef({ task: "", id: 0 });

  const setSelected = useCallback((id: string | null) => {
    if (selectedRef.current === id) return;
    selectedRef.current = id;
    setSelectedState(id);
    setMessages([]);
    setTask(null);
    setEvents([]);
    setLiveReply(null);
    setTokenMeter(null);
    setBackground(null);
    setLoading(!!id);
    setError("");
    setConnectionNote("");
    cursor.current = { task: "", id: 0 };
  }, []);

  const refreshConversations = useCallback(async () => {
    setConversations(await api<Conversation[]>("/conversations"));
  }, []);

  const refreshConfig = useCallback(async () => {
    setConfig(await api<Config>("/connectors"));
  }, []);

  useEffect(() => {
    let alive = true;
    (async () => {
      await ensureSession();
      const [settings, chats] = await Promise.all([api<Config>("/connectors"), api<Conversation[]>("/conversations")]);
      if (alive) { setConfig(settings); setConversations(chats); }
    })().catch((e) => { if (alive) setError(`无法连接工作台：${e.message}`); });
    return () => { alive = false; };
  }, []);

  const refresh = useCallback(async () => {
    const id = selectedRef.current;
    if (!id) return;
    const [history, tasks] = await Promise.all([
      api<Message[]>(`/conversations/${id}/messages`),
      api<Task[]>(`/tasks?conversation_id=${id}`),
    ]);
    const latest = tasks[0] ? await api<Task>(`/tasks/${tasks[0].id}`) : null;
    if (selectedRef.current === id) {
      setMessages(history);
      setTask(latest);
      setLoading(false);
      if (!latest || !activeStatuses.includes(latest.status)) setLiveReply(null);
    }
  }, []);

  useEffect(() => {
    if (selected) void refresh().catch((e) => { setError(e.message); setLoading(false); });
  }, [selected, refresh]);

  const taskId = task?.id;
  const taskStatus = task?.status;
  useEffect(() => {
    if (!taskId) return;
    if (cursor.current.task !== taskId) {
      cursor.current = { task: taskId, id: 0 };
      setEvents([]);
      setLiveReply(null);
      setTokenMeter(null);
      setBackground(null);
    }
    const conversationId = selectedRef.current;
    let source: EventSource | null = null;
    let stopped = false;
    let timer = 0;
    let heartbeatTimer = 0;
    let failures = 0;
    let lastHeartbeat = Date.now();
    let settled = false;
    const current = () => !stopped && !settled && selectedRef.current === conversationId;
    function disconnected() {
      window.clearInterval(heartbeatTimer);
      window.clearTimeout(timer);
      source?.close();
      if (!current()) return;
      setConnectionNote("连接暂时中断，正在重新连接…");
      timer = window.setTimeout(() => {
        void ensureSession().then(() => { if (current()) connect(); })
          .catch(() => { if (current()) disconnected(); });
      }, Math.min(1000 * 2 ** failures++, 10000));
    }
    function connect() {
      if (!current()) return;
      source = new EventSource(`/api/tasks/${taskId}/events?after=${cursor.current.id}`, { withCredentials: true });
      lastHeartbeat = Date.now();
      source.onopen = () => { if (current()) { setConnectionNote(""); failures = 0; lastHeartbeat = Date.now(); } };
      source.addEventListener("heartbeat", () => { lastHeartbeat = Date.now(); });
      source.onmessage = (event) => {
        if (!current()) return;
        lastHeartbeat = Date.now();
        const item: TaskEvent = JSON.parse(event.data);
        if (item.id <= cursor.current.id) return;
        cursor.current.id = item.id;
        const payload = item.payload;
        const receiving = taskStatus === "PENDING" || taskStatus === "RUNNING";
        if (receiving && item.event_type === "message_started") {
          setLiveReply({ id: String(payload.message_id), responseId: String(payload.response_id), content: "" });
        } else if (receiving && item.event_type === "message_delta") {
          setLiveReply((previous) => previous && previous.responseId === payload.response_id
            ? { id: previous.id, responseId: previous.responseId, content: previous.content + String(payload.text ?? "") } : previous);
          if (payload.tokens_per_second != null || payload.tokens != null) {
            setTokenMeter({
              tokensPerSecond: Number(payload.tokens_per_second ?? 0),
              completionTokens: Number(payload.tokens ?? 0),
              promptTokens: 0,
              totalTokens: Number(payload.tokens ?? 0),
            });
          }
        } else if (item.event_type === "message_finished" && payload.response_kind === "activity") {
          setLiveReply(null);
        } else if (item.event_type === "backgrounded") {
          setBackground({ report: `后台运行中（已 ${payload.elapsed ?? 0} 秒），会定期汇报进度` });
        } else if (item.event_type === "background_report") {
          setBackground({ report: `后台进度：${payload.actions ?? 0} 个动作 · ${payload.elapsed ?? 0} 秒` });
        } else if (item.event_type === "task_completed") {
          const usage = payload.token_usage as Record<string, unknown> | undefined;
          if (usage) {
            const prompt = Number(usage.prompt_tokens ?? 0);
            const completion = Number(usage.completion_tokens ?? 0);
            setTokenMeter({
              tokensPerSecond: Number(usage.elapsed ?? 0) > 0 ? Math.round((completion / Number(usage.elapsed)) * 10) / 10 : 0,
              completionTokens: completion,
              promptTokens: prompt,
              totalTokens: prompt + completion,
            });
          }
        }
        if (!item.event_type.startsWith("message_")) setEvents((previous) => [...previous, item]);
        if (["task_completed", "task_failed", "waiting_user", "task_cancelled", "artifact_created"].includes(item.event_type)) {
          void refresh().then(refreshConversations).catch((e) => { if (current()) setError(e.message); });
        }
      };
      source.addEventListener("settled", () => {
        source?.close();
        window.clearInterval(heartbeatTimer);
        window.clearTimeout(timer);
        if (!current()) return;
        settled = true;
        setConnectionNote("");
        void refresh().catch((e) => {
          if (!stopped && selectedRef.current === conversationId) setError(e.message);
        });
      });
      source.onerror = () => {
        window.clearInterval(heartbeatTimer);
        disconnected();
      };
      heartbeatTimer = window.setInterval(() => {
        if (current() && Date.now() - lastHeartbeat > 3500) {
          window.clearInterval(heartbeatTimer);
          disconnected();
        }
      }, 1000);
    }
    connect();
    return () => {
      stopped = true;
      source?.close();
      window.clearTimeout(timer);
      window.clearInterval(heartbeatTimer);
    };
  }, [taskId, taskStatus, refresh, refreshConversations]);

  async function operate(action: () => Promise<void>) {
    if (busyRef.current) return false;
    busyRef.current = true;
    setBusy(true);
    setError("");
    try { await action(); return true; }
    catch (e) { setError((e as Error).message); return false; }
    finally { busyRef.current = false; setBusy(false); }
  }

  async function resume(answer: string, approved = true, remember = false) {
    if (!task || task.status !== "WAITING_USER") return false;
    const id = selectedRef.current;
    return operate(async () => {
      await api(`/tasks/${task.id}/resume`, "POST", { answer, approved, remember });
      if (selectedRef.current === id) await refresh();
      if (remember) await refreshConfig();
    });
  }

  async function revokeGrant(host: string) {
    return operate(async () => {
      const result = await api<{ grants: Record<string, string[]> }>(
        "/grants/revoke",
        "POST",
        { host },
      );
      setConfig((previous) => (previous ? { ...previous, grants: result.grants } : previous));
    });
  }

  /** 切换文件访问级别。后端在回复进行中会拒绝（409），这里把原因原样抛给用户。 */
  async function setFileScope(scope: FileScope) {
    return operate(async () => {
      setConfig(await api<Config>("/connectors", "PUT", { file_scope: scope }));
    });
  }

  async function submit(content: string) {
    if (!content.trim()) return false;
    if (task?.status === "WAITING_USER") {
      if (task.waiting.reason === "approval") {
        setError("请先选择允许或拒绝这项操作。");
        return false;
      }
      return resume(content);
    }
    if (task && activeStatuses.includes(task.status)) return false;
    return operate(async () => {
      let id = selectedRef.current;
      if (!id) {
        const conversation = await api<Conversation>("/conversations", "POST");
        id = conversation.id;
        setSelected(id);
      }
      const reply = await api<Task>(`/conversations/${id}/messages`, "POST", { content });
      if (selectedRef.current === id) setTask(reply);
      await refreshConversations();
      if (selectedRef.current === id) await refresh();
    });
  }

  async function stop() {
    if (!task) return;
    await operate(async () => { await api(`/tasks/${task.id}/cancel`, "POST"); await refresh(); });
  }
  async function retry() {
    if (!task) return;
    await operate(async () => { await api(`/tasks/${task.id}/retry`, "POST"); await refresh(); });
  }

  return { config, setConfig, conversations, selected, setSelected, messages, task, events, liveReply, tokenMeter, background,
    error, setError, busy, loading, connectionNote, submit, resume, stop, retry, refresh, refreshConversations,
    revokeGrant, setFileScope };
}
