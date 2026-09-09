import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  type Config,
  type Conversation,
  type Message,
  type Task,
  type TaskEvent,
} from "./api";

export function useWorkspace() {
  const [config, setConfig] = useState<Config | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [task, setTask] = useState<Task | null>(null);
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const cursor = useRef({ task: "", id: 0 });

  useEffect(() => {
    let alive = true;
    (async () => {
      await api("/session");
      const [settings, chats] = await Promise.all([
        api<Config>("/connectors"),
        api<Conversation[]>("/conversations"),
      ]);
      if (alive) {
        setConfig(settings);
        setConversations(chats);
      }
    })().catch((e) => {
      if (alive) setError(`无法连接工作台：${e.message}`);
    });
    return () => {
      alive = false;
    };
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
    }
  }, []);

  useEffect(() => {
    setMessages([]);
    setTask(null);
    setEvents([]);
    cursor.current = { task: "", id: 0 };
    if (selected) void refresh().catch((e) => setError(e.message));
  }, [selected, refresh]);

  const taskId = task?.id;
  const taskStatus = task?.status;
  useEffect(() => {
    if (!taskId) return;
    if (cursor.current.task !== taskId) {
      cursor.current = { task: taskId, id: 0 };
      setEvents([]);
    }
    const id = selectedRef.current;
    const source = new EventSource(
      `/api/tasks/${taskId}/events?after=${cursor.current.id}`,
      { withCredentials: true },
    );
    source.onmessage = (event) => {
      if (selectedRef.current !== id) return;
      const item: TaskEvent = JSON.parse(event.data);
      cursor.current.id = Math.max(cursor.current.id, item.id);
      setEvents((previous) =>
        previous.some((e) => e.id === item.id) ? previous : [...previous, item],
      );
      if (
        [
          "task_completed",
          "task_failed",
          "waiting_user",
          "task_cancelled",
          "artifact_created",
          "task_started",
        ].includes(item.event_type)
      )
        void refresh().catch((e) => setError(e.message));
    };
    source.addEventListener("settled", () => {
      source.close();
      void refresh().catch((e) => setError(e.message));
    });
    return () => source.close();
  }, [taskId, taskStatus, refresh]);

  async function submit(content: string) {
    if (!content.trim() || busy) return;
    setBusy(true);
    setError("");
    try {
      let id = selectedRef.current;
      if (!id) {
        const c = await api<Conversation>("/conversations", "POST");
        id = c.id;
        selectedRef.current = id;
        setSelected(id);
      }
      await api<Task>(`/conversations/${id}/messages`, "POST", { content });
      setConversations(await api<Conversation[]>("/conversations"));
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return {
    config,
    setConfig,
    conversations,
    selected,
    setSelected,
    messages,
    task,
    events,
    error,
    setError,
    busy,
    submit,
    refresh,
  };
}
