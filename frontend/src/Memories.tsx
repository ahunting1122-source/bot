import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  Brain,
  Clock3,
  Eye,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Trash2,
  UserRound,
  X,
} from "lucide-react";
import { api, type Memory, type Profile } from "./api";
import "./connection-pages.css";

/** `hit_count` 为 0 且写入已超过这个天数，基本可以断定是死数据。 */
const DEAD_DAYS = 30;
const DAY_MS = 24 * 60 * 60 * 1000;

function parseTime(value: string) {
  if (!value) return 0;
  const stamp = new Date(value.includes("T") ? value : value.replace(" ", "T")).getTime();
  return Number.isFinite(stamp) ? stamp : 0;
}

function isDead(item: Memory) {
  if ((item.hit_count || 0) > 0) return false;
  const stamp = parseTime(item.updated_at);
  return !!stamp && Date.now() - stamp > DEAD_DAYS * DAY_MS;
}

function usage(item: Memory) {
  const hits = item.hit_count || 0;
  if (!hits) return item.last_used_at ? "从未被用过" : "还没有被用过";
  return `用过 ${hits} 次`;
}

export function Memories({
  onError,
  onBack,
}: {
  onError: (message: string) => void;
  onBack?: () => void;
}) {
  const [items, setItems] = useState<Memory[]>([]);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [draftKey, setDraftKey] = useState("");
  const [draftValue, setDraftValue] = useState("");
  const [creating, setCreating] = useState(false);
  const [profileDraft, setProfileDraft] = useState("");
  const [profileOpen, setProfileOpen] = useState(false);
  const [deadOnly, setDeadOnly] = useState(false);
  const mounted = useRef(false);
  const sequence = useRef(0);

  const load = useCallback(async (quiet = false) => {
    const request = ++sequence.current;
    if (!quiet) setLoading(true);
    setError("");
    try {
      const [rows, document] = await Promise.all([
        api<Memory[]>("/memories"),
        api<Profile>("/profile"),
      ]);
      if (!mounted.current || request !== sequence.current) return;
      setItems(rows);
      setProfile(document);
      setProfileDraft(document.body || "");
    } catch (cause) {
      if (mounted.current && request === sequence.current) {
        setError(cause instanceof Error ? cause.message : "无法读取长期记忆，请重试。");
      }
    } finally {
      if (mounted.current && request === sequence.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void load();
    return () => {
      mounted.current = false;
      sequence.current += 1;
    };
  }, [load]);

  function startCreate() {
    setEditing(null);
    setCreating(true);
    setDraftKey("");
    setDraftValue("");
    setNotice("");
    onError("");
  }

  function startEdit(item: Memory) {
    setCreating(false);
    setEditing(item.id);
    setDraftKey(item.key);
    setDraftValue(item.value);
    setNotice("");
    onError("");
  }

  function cancelForm() {
    setEditing(null);
    setCreating(false);
    setDraftKey("");
    setDraftValue("");
  }

  async function submitForm(event: React.FormEvent) {
    event.preventDefault();
    const key = draftKey.trim();
    const value = draftValue.trim();
    if (!key || !value) {
      setNotice("名称和内容都要填写。");
      return;
    }
    setBusy(true);
    setNotice("");
    onError("");
    try {
      if (editing) {
        await api(`/memories/${encodeURIComponent(editing)}`, "PATCH", { key, value });
        setNotice("已更新这条记忆。");
      } else {
        await api("/memories", "POST", { key, value });
        setNotice("已写入长期记忆。");
      }
      cancelForm();
      await load(true);
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "保存失败，请重试。");
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  async function forget(item: Memory) {
    if (!window.confirm(`删除记忆「${item.key}」？删除后模型不会再看到它。`)) return;
    setBusy(true);
    setNotice("");
    onError("");
    try {
      await api(`/memories/${encodeURIComponent(item.id)}`, "DELETE");
      setNotice(`已删除「${item.key}」。`);
      if (editing === item.id) cancelForm();
      await load(true);
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "删除失败，请重试。");
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  async function purgeDead() {
    const dead = items.filter(isDead);
    if (!dead.length) return;
    if (
      !window.confirm(
        `将删除 ${dead.length} 条超过 ${DEAD_DAYS} 天未被使用的记忆：\n` +
          dead.map((item) => `· ${item.key}`).join("\n"),
      )
    )
      return;
    setBusy(true);
    setNotice("");
    onError("");
    let removed = 0;
    try {
      for (const item of dead) {
        await api(`/memories/${encodeURIComponent(item.id)}`, "DELETE");
        removed += 1;
      }
      setNotice(`已清理 ${removed} 条从未被使用的记忆。`);
      await load(true);
    } catch (cause) {
      setNotice(
        `清理到第 ${removed + 1} 条时失败：${
          cause instanceof Error ? cause.message : "请重试。"
        }`,
      );
      await load(true);
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  async function saveProfile(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setNotice("");
    onError("");
    try {
      const saved = await api<Profile>("/profile", "PUT", { body: profileDraft });
      setProfile(saved);
      setProfileDraft(saved.body || "");
      const dropped = saved.dropped?.length || 0;
      setNotice(
        dropped
          ? `已保存。超出长度上限，最旧的 ${dropped} 条被丢弃。`
          : "用户画像已保存。下次对话生效。",
      );
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "保存失败，请重试。");
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  const term = query.trim().toLocaleLowerCase();
  const visible = items.filter((item) => {
    if (deadOnly && !isDead(item)) return false;
    if (!term) return true;
    return `${item.key} ${item.value}`.toLocaleLowerCase().includes(term);
  });
  const deadCount = items.filter(isDead).length;

  return (
    <main className="settings-page memory-page">
      <div className="page-intro">
        {onBack && (
          <button type="button" className="page-back" onClick={onBack}>
            <ArrowLeft size={15} />
            返回对话
          </button>
        )}
        <p className="eyebrow">跨对话保留</p>
        <h1>长期记忆</h1>
        <p>
          这里的内容会随每轮对话注入给模型。模型只在你明确说「记住……」时才会新增，
          其余的清理和修改都由你在这里决定。
        </p>
      </div>

      {notice && (
        <div className="notice" role="status">
          {notice}
        </div>
      )}
      {error && (
        <div className="notice error" role="alert">
          {error}
          <button type="button" className="text-action" onClick={() => void load()}>
            重试
          </button>
        </div>
      )}

      <div className="summary-toolbar">
        <label className="summary-search">
          <Search size={16} />
          <span className="sr-only">搜索长期记忆</span>
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索名称或内容"
          />
        </label>
        {query && (
          <button
            type="button"
            className="icon-button"
            aria-label="清空搜索"
            onClick={() => setQuery("")}
          >
            <X size={16} />
          </button>
        )}
        <button
          type="button"
          className="secondary"
          disabled={busy || loading}
          onClick={startCreate}
        >
          <Plus size={15} />
          新增记忆
        </button>
        <button
          type="button"
          className="secondary"
          disabled={loading}
          onClick={() => {
            onError("");
            setNotice("");
            void load();
          }}
        >
          <RefreshCw size={15} />
          {loading ? "读取中…" : "刷新"}
        </button>
      </div>

      {(creating || editing) && (
        <form className="memory-form" onSubmit={submitForm}>
          <div className="memory-form-head">
            <strong>{editing ? "修改这条记忆" : "新增一条记忆"}</strong>
            <button
              type="button"
              className="icon-button"
              aria-label="关闭编辑"
              onClick={cancelForm}
            >
              <X size={15} />
            </button>
          </div>
          <label>
            <span>名称</span>
            <input
              value={draftKey}
              maxLength={160}
              disabled={!!editing}
              placeholder="例如：代码风格偏好"
              onChange={(event) => setDraftKey(event.target.value)}
            />
          </label>
          <label>
            <span>内容</span>
            <textarea
              value={draftValue}
              maxLength={4000}
              rows={3}
              placeholder="例如：提交信息用中文，不加大写前缀"
              onChange={(event) => setDraftValue(event.target.value)}
            />
          </label>
          <div className="button-row">
            <button type="submit" className="primary" disabled={busy}>
              {busy ? "保存中…" : editing ? "保存修改" : "写入记忆"}
            </button>
            <button type="button" className="secondary" disabled={busy} onClick={cancelForm}>
              取消
            </button>
          </div>
        </form>
      )}

      <div className="memory-stats">
        <span>
          <Brain size={14} />
          共 {items.length} 条
        </span>
        <span className={deadCount ? "memory-stat-warn" : undefined}>
          <Eye size={14} />
          {deadCount} 条从未被用过
        </span>
        {deadCount > 0 && (
          <button type="button" className="text-action" disabled={busy} onClick={() => void purgeDead()}>
            <Trash2 size={13} />
            清理未使用
          </button>
        )}
        <label className="memory-filter">
          <input
            type="checkbox"
            checked={deadOnly}
            onChange={(event) => setDeadOnly(event.target.checked)}
          />
          只看未使用的
        </label>
      </div>

      {loading && !items.length ? (
        <div className="summary-loading" role="status">
          正在读取长期记忆…
        </div>
      ) : !items.length && !error ? (
        <div className="memory-empty">
          <Brain size={24} />
          <p>还没有长期记忆。</p>
          <p>在对话里说「记住……」就会出现在这里，也可以点上方手动新增。</p>
        </div>
      ) : !visible.length ? (
        <p className="summary-loading">
          {deadOnly ? "没有从未被使用过的记忆。" : "没有找到匹配的记忆，可以换个关键词。"}
        </p>
      ) : (
        <div className="memory-list">
          {visible.map((item) => (
            <article
              className={`memory-row ${isDead(item) ? "memory-row-dead" : ""}`}
              key={item.id}
            >
              <div className="memory-main">
                <strong className="memory-key">{item.key}</strong>
                <p className="memory-value">{item.value}</p>
                <div className="memory-meta">
                  <span className={isDead(item) ? "memory-stat-warn" : undefined}>
                    <Eye size={12} />
                    {usage(item)}
                  </span>
                  <span>
                    <Clock3 size={12} />
                    更新于 {new Date(parseTime(item.updated_at) || Date.now()).toLocaleDateString("zh-CN")}
                  </span>
                  {item.source && item.source !== "user" && <span className="memory-source">来自任务</span>}
                </div>
              </div>
              <div className="memory-actions">
                <button
                  type="button"
                  className="text-action"
                  disabled={busy}
                  onClick={() => startEdit(item)}
                >
                  <Pencil size={13} />
                  修改
                </button>
                <button
                  type="button"
                  className="text-action"
                  disabled={busy}
                  onClick={() => void forget(item)}
                >
                  <Trash2 size={13} />
                  删除
                </button>
              </div>
            </article>
          ))}
        </div>
      )}

      <section className="profile-panel">
        <div className="profile-head">
          <div>
            <p className="eyebrow">用户画像</p>
            <h2>
              <UserRound size={17} />
              关于你的长期设定
            </h2>
            <p>
              和上面的记忆不同：这里是一整段文档，每次对话开头整段带上，用来调整表达方式和默认假设。
              记忆是「可以按条查的结论」，画像是「你是谁」。
            </p>
          </div>
          <button
            type="button"
            className="text-action"
            onClick={() => setProfileOpen((open) => !open)}
          >
            {profileOpen ? "收起" : "编辑画像"}
          </button>
        </div>
        {profile && !profileOpen && (
          <pre className="profile-preview">
            {profile.exists && profile.body?.trim()
              ? profile.body.trim()
              : "（还没有内容，点「编辑画像」写几条）"}
          </pre>
        )}
        {profile && profileOpen && (
          <form className="memory-form" onSubmit={saveProfile}>
            <p className="profile-hint">
              每行一条，或以「§」分隔。上限 {profile.limit || 1600} 字，超出时丢弃最旧的条目。
            </p>
            <textarea
              value={profileDraft}
              rows={8}
              onChange={(event) => setProfileDraft(event.target.value)}
              placeholder={"习惯用中文沟通\n代码项目在 E:\\bbbooottt\n§\n偏好简洁说明，不要铺垫"}
            />
            <div className="button-row">
              <button type="submit" className="primary" disabled={busy}>
                {busy ? "保存中…" : "保存画像"}
              </button>
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={() => {
                  setProfileDraft(profile.body || "");
                  setProfileOpen(false);
                }}
              >
                放弃修改
              </button>
            </div>
          </form>
        )}
      </section>
    </main>
  );
}
