import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  Clock3,
  MessageSquare,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import { api, type Conversation } from "./api";
import "./connection-pages.css";

function summaryText(item: Conversation) {
  if (item.summary) return item.summary;
  if (item.summary_status === "pending") return "正在根据对话内容整理摘要…";
  if (item.summary_status === "failed")
    return "摘要生成失败，可以点击下方按钮重试。";
  return item.message_count
    ? "还没有摘要。点击生成后，回顾这段对话的主题和结论。"
    : "还没有消息，可以打开对话继续聊。";
}

export function Memories({
  onError,
  onSelect,
  onRefresh,
  onBack,
}: {
  onError: (e: string) => void;
  onSelect: (id: string) => void;
  onRefresh?: () => void;
  onBack?: () => void;
}) {
  const [items, setItems] = useState<Conversation[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [requesting, setRequesting] = useState<string[]>([]);
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const mounted = useRef(false);
  const sequence = useRef(0);

  const load = useCallback(async (quiet = false) => {
    const request = ++sequence.current;
    if (!quiet) setLoading(true);
    setError("");
    try {
      const rows = await api<Conversation[]>("/conversations");
      if (mounted.current && request === sequence.current) setItems(rows);
    } catch (cause) {
      if (mounted.current && request === sequence.current) {
        setError(
          cause instanceof Error ? cause.message : "无法读取对话，请重试。",
        );
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

  useEffect(() => {
    if (error || !items.some((item) => item.summary_status === "pending"))
      return;
    const timer = window.setTimeout(() => void load(true), 1800);
    return () => window.clearTimeout(timer);
  }, [items, error, load]);

  async function regenerate(id: string) {
    setRequesting((previous) => [...previous, id]);
    setRowErrors((previous) => ({ ...previous, [id]: "" }));
    onError("");
    try {
      await api(`/conversations/${encodeURIComponent(id)}/summary`, "POST");
      if (!mounted.current) return;
      setItems((previous) =>
        previous.map((item) =>
          item.id === id ? { ...item, summary_status: "pending" } : item,
        ),
      );
      await load(true);
      onRefresh?.();
    } catch (cause) {
      if (mounted.current)
        setRowErrors((previous) => ({
          ...previous,
          [id]:
            cause instanceof Error ? cause.message : "无法生成摘要，请重试。",
        }));
    } finally {
      if (mounted.current)
        setRequesting((previous) => previous.filter((value) => value !== id));
    }
  }

  const term = query.trim().toLocaleLowerCase();
  const visibleItems = items.filter((item) =>
    `${item.title} ${item.summary || ""}`.toLocaleLowerCase().includes(term),
  );

  return (
    <main className="settings-page memory-page">
      <div className="page-intro">
        {onBack && (
          <button type="button" className="page-back" onClick={onBack}>
            <ArrowLeft size={15} />
            返回对话
          </button>
        )}
        <p className="eyebrow">回顾与整理</p>
        <h1>对话摘要</h1>
        <p>回顾每段对话讨论过什么、得出了什么结论。点击摘要即可继续聊。</p>
      </div>
      <div className="summary-toolbar">
        <label className="summary-search">
          <Search size={16} />
          <span className="sr-only">搜索对话摘要</span>
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索标题或摘要"
          />
        </label>
        {query && (
          <button
            type="button"
            className="icon-button"
            aria-label="清空摘要搜索"
            onClick={() => setQuery("")}
          >
            <X size={16} />
          </button>
        )}
        <button
          type="button"
          className="secondary"
          disabled={loading}
          onClick={() => {
            onError("");
            void load();
          }}
        >
          <RefreshCw size={15} />
          {loading ? "读取中…" : "刷新列表"}
        </button>
      </div>
      {error && (
        <div className="notice error" role="alert">
          {error}
          <button
            type="button"
            className="text-action"
            onClick={() => void load()}
          >
            重试
          </button>
        </div>
      )}
      {loading && !items.length ? (
        <div className="summary-loading" role="status">
          正在读取对话摘要…
        </div>
      ) : !items.length && !error ? (
        <div className="memory-empty">
          <MessageSquare size={24} />
          <p>还没有可以回顾的对话。</p>
          <p>开始聊天后，对话会出现在这里。</p>
        </div>
      ) : !visibleItems.length && items.length > 0 ? (
        <p className="summary-loading">没有找到匹配的对话，可以换个关键词。</p>
      ) : (
        <div className="conversation-summaries">
          {visibleItems.map((item) => {
            const pending =
              item.summary_status === "pending" || requesting.includes(item.id);
            return (
              <article className="conversation-summary" key={item.id}>
                <button
                  type="button"
                  className="summary-open"
                  aria-label={`打开对话：${item.title}`}
                  onClick={() => {
                    onError("");
                    onSelect(item.id);
                  }}
                >
                  <span className="summary-icon">
                    <MessageSquare size={17} />
                  </span>
                  <span className="summary-body">
                    <strong className="summary-title">{item.title}</strong>
                    <span className="summary-copy">{summaryText(item)}</span>
                    <span className="summary-meta">
                      <span>
                        <Clock3 size={13} />
                        {new Date(
                          item.updated_at || item.created_at,
                        ).toLocaleDateString("zh-CN")}
                      </span>
                      <span>{item.message_count || 0} 条消息</span>
                    </span>
                  </span>
                </button>
                <div className="summary-card-actions">
                  <span
                    className={`summary-status ${item.summary_status === "failed" ? "summary-status-error" : ""}`}
                    role={pending ? "status" : undefined}
                  >
                    {pending
                      ? "摘要生成中…"
                      : item.summary_status === "failed"
                        ? "生成失败"
                        : item.summary_updated_at
                          ? `摘要更新于 ${new Date(item.summary_updated_at).toLocaleString("zh-CN")}`
                          : "尚未生成摘要"}
                  </span>
                  <button
                    type="button"
                    className="text-action"
                    disabled={pending || !item.message_count}
                    aria-label={`${item.summary ? "重新生成" : "生成"}摘要：${item.title}`}
                    onClick={() => void regenerate(item.id)}
                  >
                    <RefreshCw size={13} />
                    {pending
                      ? "生成中…"
                      : item.summary
                        ? "重新生成摘要"
                        : item.summary_status === "failed"
                          ? "重试生成"
                          : "生成摘要"}
                  </button>
                </div>
                {rowErrors[item.id] && (
                  <p className="notice error" role="alert">
                    {rowErrors[item.id]}
                  </p>
                )}
              </article>
            );
          })}
        </div>
      )}
    </main>
  );
}
