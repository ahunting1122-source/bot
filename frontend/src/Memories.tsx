import { useEffect, useState } from "react";
import { Brain, Pencil, Trash2 } from "lucide-react";
import { api, type Memory } from "./api";

export function Memories({ onError }: { onError: (e: string) => void }) {
  const [items, setItems] = useState<Memory[]>([]);
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [ready, setReady] = useState(false);
  useEffect(() => {
    let alive = true;
    api<Memory[]>("/memories")
      .then((data) => {
        if (alive) {
          setItems(data);
          setReady(true);
        }
      })
      .catch((e) => onError(e.message));
    return () => {
      alive = false;
    };
  }, [onError]);
  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await api("/memories", "POST", { key, value });
      setItems(await api("/memories"));
      setKey("");
      setValue("");
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function remove(id: string) {
    setBusy(true);
    try {
      await api(`/memories/${id}`, "DELETE");
      setItems(items.filter((m) => m.id !== id));
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="settings-page">
      <div className="page-intro">
        <span className="eyebrow">PERSONAL MEMORY</span>
        <h1>让助手更了解你</h1>
        <p>这些偏好可用于新的对话。聊天历史仍按会话分别保存。</p>
      </div>
      <form className="memory-form" onSubmit={save}>
        <label className="field">
          记忆名称
          <input
            required
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder="例如：报告格式"
            maxLength={160}
          />
        </label>
        <label className="field">
          内容
          <textarea
            required
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="报告使用中文 Markdown，保存到 reports 目录。"
            rows={3}
            maxLength={4000}
          />
        </label>
        <button className="primary" disabled={busy}>
          保存记忆
        </button>
      </form>
      <h2 className="list-heading">
        已保存的记忆 <span>{items.length}</span>
      </h2>
      {!ready ? (
        <p role="status">正在读取记忆…</p>
      ) : !items.length ? (
        <div className="memory-empty">
          <Brain size={24} />
          <p>还没有长期记忆。可以在这里添加，或在对话中说“记住……” 。</p>
        </div>
      ) : (
        <ul className="memory-list">
          {items.map((item) => (
            <li key={item.id}>
              <div>
                <h3>{item.key}</h3>
                <p>{item.value}</p>
                <small>
                  {new Date(item.updated_at).toLocaleDateString("zh-CN")}
                </small>
              </div>
              <button
                className="icon-button"
                aria-label={`编辑 ${item.key}`}
                onClick={() => {
                  setKey(item.key);
                  setValue(item.value);
                }}
              >
                <Pencil size={16} />
              </button>
              <button
                className="icon-button"
                aria-label={`删除 ${item.key}`}
                disabled={busy}
                onClick={() => void remove(item.id)}
              >
                <Trash2 size={16} />
              </button>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
