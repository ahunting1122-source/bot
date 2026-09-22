import { useState } from "react";
import {
  Activity,
  ArrowUpRight,
  Check,
  FileText,
  Pause,
  Square,
} from "lucide-react";
import { api, labels, type Task, type TaskEvent } from "./api";

const eventLabels: Record<string, string> = {
  task_created: "任务已接收",
  task_started: "开始执行",
  plan_updated: "行动说明",
  tool_started: "调用工具",
  tool_finished: "工具返回",
  waiting_user: "需要你处理",
  artifact_created: "文件已生成",
  task_completed: "任务已结束",
  task_failed: "执行失败",
  task_cancelled: "任务已取消",
};

export function TaskPanel({
  task,
  events,
  onChange,
  onError,
}: {
  task: Task | null;
  events: TaskEvent[];
  onChange: () => Promise<void>;
  onError: (error: string) => void;
}) {
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  async function action(kind: string, approved = true) {
    if (!task) return;
    setBusy(true);
    try {
      await api(
        `/tasks/${task.id}/${kind}`,
        "POST",
        kind === "resume"
          ? { answer: answer || "继续，请重新确认页面状态", approved }
          : undefined,
      );
      setAnswer("");
      await onChange();
    } catch (error) {
      onError((error as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <aside className="task-panel" aria-label="任务详情">
      <div className="panel-heading">
        <Activity size={16} />
        <h2>执行过程</h2>
      </div>
      {!task ? (
        <div className="panel-empty">
          <FileText size={24} strokeWidth={1.3} />
          <h3>还没有进行中的任务</h3>
          <p>任务开始后，执行步骤和生成的文件会出现在这里。</p>
        </div>
      ) : (
        <>
          <div className="task-status">
            <span className={`status ${task.status.toLowerCase()}`}>
              {labels[task.status]}
            </span>
            {["RUNNING", "PENDING", "WAITING_USER"].includes(task.status) && (
              <button
                className="text-button"
                disabled={busy}
                onClick={() => void action("cancel")}
              >
                <Square size={12} />
                取消
              </button>
            )}
          </div>
          {task.error && (
            <div className="notice error" role="alert">
              {task.error}
            </div>
          )}
          {task.status === "WAITING_USER" && (
            <section className="handoff">
              <h3>
                <Pause size={15} /> 轮到你了
              </h3>
              <p>{task.waiting.question}</p>
              {task.waiting.args && (
                <pre>{JSON.stringify(task.waiting.args, null, 2)}</pre>
              )}
              <label htmlFor="resume-answer">补充说明（可选）</label>
              <textarea
                id="resume-answer"
                rows={2}
                value={answer}
                onChange={(e) => setAnswer(e.target.value)}
              />
              <div className="button-row">
                <button
                  className="primary"
                  disabled={busy}
                  onClick={() => void action("resume")}
                >
                  确认并继续
                </button>
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() => void action("resume", false)}
                >
                  拒绝此步
                </button>
              </div>
            </section>
          )}
          <ol className="timeline">
            {events.map((event) => (
              <li key={event.id}>
                <span className="timeline-dot">
                  {event.event_type === "task_completed" ? (
                    <Check size={10} />
                  ) : null}
                </span>
                <div className="event-title">
                  {eventLabels[event.event_type] || event.event_type}
                  <time>
                    {new Date(event.created_at).toLocaleTimeString("zh-CN", {
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                  </time>
                </div>
                {typeof event.payload.tool === "string" && (
                  <code>{event.payload.tool}</code>
                )}
                {typeof event.payload.text === "string" && (
                  <p>{event.payload.text}</p>
                )}
                {event.payload.result !== undefined && (
                  <details>
                    <summary>查看返回结果</summary>
                    <pre>{JSON.stringify(event.payload.result, null, 2)}</pre>
                  </details>
                )}
              </li>
            ))}
          </ol>
          {!!task.artifacts?.length && (
            <section className="artifacts">
              <h3>任务产物</h3>
              {task.artifacts.map((item) => (
                <a
                  href={`/api/artifacts/${item.id}`}
                  key={item.id}
                  target="_blank"
                  rel="noreferrer"
                >
                  <FileText size={16} />
                  <span>{item.label}</span>
                  <ArrowUpRight size={14} />
                </a>
              ))}
            </section>
          )}
        </>
      )}
      <div className="panel-footnote">过程与结果，保存在当前对话。</div>
    </aside>
  );
}
