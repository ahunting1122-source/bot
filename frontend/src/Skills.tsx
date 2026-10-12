import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  ArrowUpRight,
  BookOpenCheck,
  Check,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Trash2,
  X,
} from "lucide-react";
import { api, type Skill, type SkillDetail } from "./api";
import "./connection-pages.css";

const STATUS_LABEL: Record<Skill["status"], string> = {
  unreviewed: "待确认",
  active: "启用中",
  stale: "已降级",
  archived: "已归档",
};

function parseTime(value: string) {
  if (!value) return 0;
  const stamp = new Date(value.includes("T") ? value : value.replace(" ", "T")).getTime();
  return Number.isFinite(stamp) ? stamp : 0;
}

export function Skills({
  onError,
  onBack,
}: {
  onError: (message: string) => void;
  onBack?: () => void;
}) {
  const [items, setItems] = useState<Skill[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState({ name: "", scope: "site", triggers: "", body: "" });
  const mounted = useRef(false);
  const sequence = useRef(0);

  const load = useCallback(async (quiet = false) => {
    const request = ++sequence.current;
    if (!quiet) setLoading(true);
    setError("");
    try {
      const rows = await api<Skill[]>("/skills");
      if (!mounted.current || request !== sequence.current) return;
      setItems(rows);
    } catch (cause) {
      if (mounted.current && request === sequence.current) {
        setError(cause instanceof Error ? cause.message : "无法读取技能，请重试。");
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

  async function startEdit(name: string) {
    setNotice("");
    onError("");
    try {
      const detail = await api<SkillDetail>(`/skills/${encodeURIComponent(name)}`);
      setEditing(name);
      setCreating(false);
      setDraft({
        name,
        scope: detail.scope,
        triggers: detail.triggers.join("\n"),
        body: detail.body,
      });
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "读取技能失败，请重试。");
    }
  }

  function startCreate() {
    setEditing(null);
    setCreating(true);
    setDraft({ name: "", scope: "site", triggers: "", body: "" });
    setNotice("");
    onError("");
  }

  function cancelForm() {
    setEditing(null);
    setCreating(false);
    setDraft({ name: "", scope: "site", triggers: "", body: "" });
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    const name = draft.name.trim();
    const body = draft.body.trim();
    if (!name || !body) {
      setNotice("技能名和正文都要填写。");
      return;
    }
    const triggers = draft.triggers
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    setBusy(true);
    setNotice("");
    onError("");
    try {
      const saved = await api<SkillDetail>(`/skills/${encodeURIComponent(name)}`, "PUT", {
        body,
        scope: draft.scope,
        triggers,
      });
      setNotice((saved as unknown as { notice?: string }).notice || "已保存。");
      cancelForm();
      await load(true);
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "保存失败，请重试。");
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  async function setStatus(name: string, status: Skill["status"], label: string) {
    setBusy(true);
    setNotice("");
    onError("");
    try {
      await api(`/skills/${encodeURIComponent(name)}/status`, "POST", { status });
      setNotice(label);
      await load(true);
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "状态更新失败，请重试。");
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  async function remove(name: string) {
    if (!window.confirm(`移除技能「${name}」？正文会被删掉，但使用统计会保留，之后可以恢复。`)) return;
    setBusy(true);
    setNotice("");
    onError("");
    try {
      await api(`/skills/${encodeURIComponent(name)}`, "DELETE");
      setNotice(`已移除「${name}」（保留统计，可恢复）。`);
      if (editing === name) cancelForm();
      await load(true);
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "移除失败，请重试。");
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  const term = query.trim().toLocaleLowerCase();
  const visible = items.filter((item) => {
    if (!term) return true;
    return `${item.name} ${item.triggers.join(" ")}`.toLocaleLowerCase().includes(term);
  });
  const activeCount = items.filter((item) => item.status === "active").length;
  const unusedCount = items.filter((item) => (item.usage_count || 0) === 0).length;

  return (
    <main className="settings-page memory-page skills-page">
      <div className="page-intro">
        {onBack && (
          <button type="button" className="page-back" onClick={onBack}>
            <ArrowLeft size={15} />
            返回对话
          </button>
        )}
        <p className="eyebrow">流程知识</p>
        <h1>技能</h1>
        <p>
          技能是模型沉淀下来的「怎么做」流程，遇到相关任务时会被自动注入。
          新沉淀的技能默认「待确认」，确认前不会生效。
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
          <span className="sr-only">搜索技能</span>
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索技能名或触发词"
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
        <button type="button" className="secondary" disabled={busy || loading} onClick={startCreate}>
          <Plus size={15} />
          新增技能
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

      <div className="memory-stats">
        <span>
          <BookOpenCheck size={14} />
          共 {items.length} 条
        </span>
        <span>
          <Check size={14} />
          {activeCount} 条启用中
        </span>
        <span className={unusedCount ? "memory-stat-warn" : undefined}>
          <ArrowUpRight size={14} />
          {unusedCount} 条还没被用过
        </span>
      </div>

      {(creating || editing) && (
        <form className="memory-form skill-form" onSubmit={save}>
          <div className="memory-form-head">
            <strong>{editing ? `编辑「${editing}」` : "新增一个技能"}</strong>
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
            <span>技能名（唯一标识）</span>
            <input
              value={draft.name}
              maxLength={80}
              disabled={!!editing}
              placeholder="例如：web_search"
              onChange={(event) => setDraft((d) => ({ ...d, name: event.target.value }))}
            />
          </label>
          <div className="skill-row">
            <label>
              <span>作用域</span>
              <select
                value={draft.scope}
                onChange={(event) => setDraft((d) => ({ ...d, scope: event.target.value }))}
              >
                <option value="site">site · 站点级</option>
                <option value="project">project · 项目级</option>
                <option value="global">global · 全局</option>
              </select>
            </label>
            <label>
              <span>触发词（每行一条）</span>
              <textarea
                value={draft.triggers}
                rows={3}
                placeholder={"站内搜索\n筛选排序"}
                onChange={(event) => setDraft((d) => ({ ...d, triggers: event.target.value }))}
              />
            </label>
          </div>
          <label>
            <span>正文</span>
            <textarea
              className="skill-body"
              value={draft.body}
              rows={10}
              spellCheck={false}
              placeholder="触发条件 + 步骤 + 坑 + 绕法"
              onChange={(event) => setDraft((d) => ({ ...d, body: event.target.value }))}
            />
          </label>
          <div className="button-row">
            <button type="submit" className="primary" disabled={busy}>
              {busy ? "保存中…" : editing ? "保存修改" : "创建技能"}
            </button>
            <button type="button" className="secondary" disabled={busy} onClick={cancelForm}>
              取消
            </button>
          </div>
        </form>
      )}

      {loading && !items.length ? (
        <div className="summary-loading" role="status">
          正在读取技能…
        </div>
      ) : !items.length && !error ? (
        <div className="memory-empty">
          <BookOpenCheck size={24} />
          <p>还没有技能。</p>
          <p>模型在任务里说「沉淀这个流程」时会生成，也可以点上方手动新增。</p>
        </div>
      ) : !visible.length ? (
        <p className="summary-loading">没有找到匹配的技能，可以换个关键词。</p>
      ) : (
        <div className="memory-list">
          {visible.map((item) => (
            <article
              className={`memory-row ${item.status === "archived" ? "memory-row-dead" : ""}`}
              key={item.name}
            >
              <div className="memory-main">
                <div className="skill-title">
                  <strong className="memory-key">{item.name}</strong>
                  {item.source && item.source !== "local" && (
                    <span className="skill-scope skill-scope-mcp">外部 · {item.source}</span>
                  )}
                  <span className={`skill-scope skill-scope-${item.scope}`}>{item.scope}</span>
                  <span className={`skill-status skill-status-${item.status}`}>
                    {STATUS_LABEL[item.status]}
                  </span>
                </div>
                {item.triggers.length > 0 && (
                  <p className="memory-value">{item.triggers.join(" · ")}</p>
                )}
                <div className="memory-meta">
                  <span>
                    <ArrowUpRight size={12} />
                    {item.usage_count > 0 ? `用过 ${item.usage_count} 次` : "还没被用过"}
                  </span>
                  <span>
                    成功率 {Math.round((item.success_rate || 0) * 100)}%
                  </span>
                  <span>
                    更新于{" "}
                    {new Date(parseTime(item.updated_at) || Date.now()).toLocaleDateString("zh-CN")}
                  </span>
                </div>
              </div>
              <div className="memory-actions">
                <button
                  type="button"
                  className="text-action"
                  disabled={busy}
                  onClick={() => void startEdit(item.name)}
                >
                  <Pencil size={13} />
                  编辑
                </button>
                {item.status === "unreviewed" && (
                  <button
                    type="button"
                    className="text-action"
                    disabled={busy}
                    onClick={() => void setStatus(item.name, "active", `已启用「${item.name}」。`)}
                  >
                    <Check size={13} />
                    确认启用
                  </button>
                )}
                {item.status === "archived" && (
                  <button
                    type="button"
                    className="text-action"
                    disabled={busy}
                    onClick={() => void setStatus(item.name, "active", `已恢复「${item.name}」。`)}
                  >
                    <Check size={13} />
                    恢复
                  </button>
                )}
                {item.status === "active" && (
                  <button
                    type="button"
                    className="text-action"
                    disabled={busy}
                    onClick={() => void setStatus(item.name, "archived", `已归档「${item.name}」。`)}
                  >
                    归档
                  </button>
                )}
                <button
                  type="button"
                  className="text-action"
                  disabled={busy}
                  onClick={() => void remove(item.name)}
                >
                  <Trash2 size={13} />
                  移除
                </button>
              </div>
            </article>
          ))}
        </div>
      )}
    </main>
  );
}
