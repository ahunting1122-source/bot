import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import { flushSync } from "react-dom";
import {
  ArrowUpRight,
  BookOpen,
  Brain,
  ChevronRight,
  Folder,
  KeyRound,
  Menu,
  MessageSquare,
  Plus,
  Settings2,
  Sparkles,
  X,
} from "lucide-react";
import { useWorkspace } from "./useWorkspace";
import { Settings } from "./Settings";
import { Summaries } from "./Summaries";
import { Memories } from "./Memories";
import { Skills } from "./Skills";
import { Vault } from "./Vault";
import { Composer } from "./Composer";
import { Welcome } from "./Welcome";
import { MarkdownContent } from "./MarkdownContent";

type View = "chat" | "summaries" | "memories" | "skills" | "vault" | "settings";
// Mirrors GRANTABLE_TOOLS in backend/app/config.py: only in-page actions can be remembered.
const GRANTABLE_ACTIONS = [
  "browser_click",
  "browser_fill",
  "browser_press",
  "browser_scroll",
];
function StreamReply({ content }: { content: string }) {
  return (
    <>
      <MarkdownContent content={content} />
      <span className="stream-cursor" aria-label="正在输出" />
    </>
  );
}
const navigation = [
  { id: "chat" as const, label: "工作台", icon: MessageSquare },
  { id: "summaries" as const, label: "对话摘要", icon: BookOpen },
  { id: "memories" as const, label: "长期记忆", icon: Brain },
  { id: "skills" as const, label: "技能", icon: Sparkles },
  { id: "vault" as const, label: "密钥存储", icon: KeyRound },
  { id: "settings" as const, label: "连接与设置", icon: Settings2 },
];

export function App() {
  const workspace = useWorkspace();
  const [view, setView] = useState<View>("chat");
  const [draft, setDraft] = useState("");
  const [menu, setMenu] = useState(false);
  const messagePane = useRef<HTMLElement>(null);
  const followReply = useRef(true);
  const {
    config,
    conversations,
    selected,
    messages,
    task,
    events,
    error,
    busy,
    loading,
    connectionNote,
    refreshConversations,
    setError,
  } = workspace;
  const replyActive = task?.status === "PENDING" || task?.status === "RUNNING";
  const waitingApproval =
    task?.status === "WAITING_USER" && task.waiting.reason === "approval";
  useLayoutEffect(() => {
    followReply.current = true;
  }, [selected, view]);
  useLayoutEffect(() => {
    const pane = messagePane.current;
    if (pane && followReply.current) pane.scrollTop = pane.scrollHeight;
  }, [
    messages,
    workspace.liveReply?.content,
    task?.status,
    events.length,
    loading,
    selected,
    view,
  ]);
  const refreshSidebar = useCallback(() => {
    void refreshConversations().catch((cause: unknown) => {
      setError(
        cause instanceof Error ? cause.message : "无法更新对话列表，请重试。",
      );
    });
  }, [refreshConversations, setError]);
  useEffect(() => {
    if (
      !conversations.some(
        (conversation) => conversation.summary_status === "pending",
      )
    )
      return;
    const timer = window.setTimeout(refreshSidebar, 1800);
    return () => window.clearTimeout(timer);
  }, [conversations, refreshSidebar]);
  const modelConnectionLabel = config
    ? {
        unconfigured: "未配置",
        configured: "待验证",
        verified: "连接检查通过",
        failed: "连接检查失败",
      }[config.model_status]
    : "正在读取配置";

  function switchView(next: View) {
    setMenu(false);
    if (next === view) return;
    if (view === "summaries") refreshSidebar();
    const change = () => flushSync(() => setView(next));
    const documentWithTransition = document as Document & {
      startViewTransition?: (change: () => void) => {
        finished?: Promise<unknown>;
        ready?: Promise<unknown>;
      };
    };
    if (
      documentWithTransition.startViewTransition &&
      !matchMedia("(prefers-reduced-motion: reduce)").matches
    ) {
      try {
        const transition = documentWithTransition.startViewTransition(change);
        // Rapid navigation can abort a previous transition. Consume that
        // expected rejection so it never becomes a page-level unhandled error.
        transition.finished?.catch(() => undefined);
        transition.ready?.catch(() => undefined);
      } catch {
        change();
      }
    } else change();
  }
  function newChat() {
    workspace.setSelected(null);
    switchView("chat");
    setMenu(false);
    setDraft("");
  }
  function choose(id: string) {
    workspace.setSelected(id);
    switchView("chat");
    setMenu(false);
  }
  async function send(event: React.FormEvent) {
    event.preventDefault();
    const content = draft;
    followReply.current = true;
    const accepted = await workspace.submit(content);
    if (accepted) setDraft((current) => (current === content ? "" : current));
  }
  function selectSuggestion(text: string) {
    setDraft(text);
    document.getElementById("task-input")?.focus();
  }
  const composer = (
    <Composer
      draft={draft}
      onDraft={setDraft}
      onSend={send}
      disabled={busy || loading || replyActive || waitingApproval}
    />
  );

  return (
    <div
      className={`shell ${view === "chat" && messages.length ? "chat-layout" : ""}`}
    >
      {menu && (
        <button
          className="nav-backdrop"
          aria-label="关闭导航"
          onClick={() => setMenu(false)}
        />
      )}
      <nav className={`sidebar ${menu ? "open" : ""}`} aria-label="主导航">
        <a
          className="brand"
          href="#"
          onClick={(event) => {
            event.preventDefault();
            newChat();
          }}
        >
          <span className="brand-mark" aria-hidden="true" />
          <strong>
            orbit<span>个人空间</span>
          </strong>
        </a>
        <button className="new-chat" onClick={newChat}>
          <Plus size={17} />
          新对话
        </button>
        <div
          className="nav-group"
          style={
            {
              "--nav-index": navigation.findIndex((item) => item.id === view),
            } as CSSProperties
          }
        >
          <span className="nav-indicator" aria-hidden="true" />
          {navigation.map((item) => (
            <button
              key={item.id}
              className={`nav-item ${view === item.id ? "active" : ""}`}
              aria-current={view === item.id ? "page" : undefined}
              onClick={() => switchView(item.id)}
            >
              <item.icon size={17} strokeWidth={1.7} />
              {item.label}
            </button>
          ))}
        </div>
        <div className="history-heading">最近对话</div>
        <div className="conversation-list">
          {conversations.length ? (
            conversations.map((item) => (
              <button
                key={item.id}
                className={`conversation ${selected === item.id ? "selected" : ""}`}
                title={item.title}
                onClick={() => choose(item.id)}
              >
                <span>{item.title}</span>
              </button>
            ))
          ) : (
            <p className="history-empty">新的对话会收纳在这里。</p>
          )}
        </div>
        <div className="sidebar-bottom">
          <button className="space-link" onClick={() => switchView("settings")}>
            <span className="space-icon">
              <Folder size={17} />
            </span>
            <span>
              本地工作空间<small>资料保存在这台电脑</small>
            </span>
            <ChevronRight size={14} />
          </button>
          <p className="sidebar-caption">留一点空间，专注眼前的事。</p>
        </div>
      </nav>
      <div className="main-shell">
        <header className="topbar">
          <div>
            <button
              className="icon-button mobile-menu"
              aria-label="打开导航"
              onClick={() => setMenu(true)}
            >
              <Menu size={19} />
            </button>
            <span>个人空间</span>
            <ChevronRight size={12} />
            <strong>
              {navigation.find((item) => item.id === view)?.label}
            </strong>
          </div>
          <div className="topbar-actions">
            <button
              className="model-button"
              onClick={() => switchView("settings")}
              title={`${config?.model || "模型服务"} · ${modelConnectionLabel}`}
              aria-label={`${config?.model || "模型服务"}，${modelConnectionLabel}，打开连接设置`}
            >
              <span
                className={`connection-dot ${config?.model_status === "verified" ? "connected" : ""}`}
                aria-hidden="true"
              />
              <span>
                {config?.model_status === "unconfigured" || !config
                  ? "连接模型"
                  : `${config.model} · ${modelConnectionLabel}`}
              </span>
              <ChevronRight size={12} />
            </button>
          </div>
        </header>
        {error && (
          <div className="global-error" role="alert">
            {error}
            <button
              className="icon-button"
              aria-label="关闭错误提示"
              onClick={() => workspace.setError("")}
            >
              <X size={16} />
            </button>
          </div>
        )}
        <div className="view-content" key={view}>
          {!config ? (
            <main className="loading-state" role="status">
              <p>
                {error ? "请确认后端已启动，再刷新页面。" : "正在打开工作空间…"}
              </p>
            </main>
          ) : view === "settings" ? (
            <Settings
              config={config}
              onSaved={workspace.setConfig}
              onError={workspace.setError}
              onBack={() => switchView("chat")}
              onRevoke={workspace.revokeGrant}
            />
          ) : view === "summaries" ? (
            <Summaries
              onError={workspace.setError}
              onSelect={(id) => {
                workspace.setSelected(id);
                switchView("chat");
              }}
              onBack={() => switchView("chat")}
              onRefresh={refreshSidebar}
            />
          ) : view === "memories" ? (
            <Memories
              onError={workspace.setError}
              onBack={() => switchView("chat")}
            />
          ) : view === "skills" ? (
            <Skills
              onError={workspace.setError}
              onBack={() => switchView("chat")}
            />
          ) : view === "vault" ? (
            <Vault
              onError={workspace.setError}
              onBack={() => switchView("chat")}
            />
          ) : (
            <div className="workspace-body">
              <main
                className={`chat-main ${messages.length ? "has-messages" : ""}`}
                aria-busy={loading}
              >
                {connectionNote && (
                  <p className="notice" role="status" aria-live="polite">
                    {connectionNote}
                  </p>
                )}
                {loading ? (
                  <section
                    className="loading-state"
                    role="status"
                    aria-live="polite"
                  >
                    <p>正在读取这段对话…</p>
                  </section>
                ) : !messages.length ? (
                  <>
                    <Welcome
                      config={config}
                      onSettings={() => switchView("settings")}
                      onChoose={selectSuggestion}
                    >
                      {composer}
                    </Welcome>
                    <div className="workspace-footer">
                      <span>已接入工作目录</span>
                      <span className="footer-divider" />
                      <button onClick={() => switchView("settings")}>
                        {config.github_repo ? "GitHub 已配置" : "连接 GitHub"}
                        <ArrowUpRight size={12} />
                      </button>
                      <button onClick={() => switchView("settings")}>
                        {config.notion_url ? "Notion 已配置" : "连接 Notion"}
                        <ArrowUpRight size={12} />
                      </button>
                    </div>
                  </>
                ) : (
                  <>
                    <section
                      className="messages"
                      aria-label="对话内容"
                      ref={messagePane}
                      onScroll={(event) => {
                        const pane = event.currentTarget;
                        followReply.current =
                          pane.scrollHeight -
                            pane.scrollTop -
                            pane.clientHeight <
                          96;
                      }}
                      onWheel={(event) => {
                        if (event.deltaY < 0) followReply.current = false;
                      }}
                      onKeyDown={(event) => {
                        if (["ArrowUp", "PageUp", "Home"].includes(event.key))
                          followReply.current = false;
                      }}
                    >
                      {messages.map((message) => (
                        <article
                          className={`message ${message.role}`}
                          key={message.id}
                        >
                          <div className="message-avatar">
                            {message.role === "user" ? (
                              "我"
                            ) : (
                              <span className="mini-mark" />
                            )}
                          </div>
                          <div>
                            <h2>{message.role === "user" ? "你" : "Orbit"}</h2>
                            <div className="message-content">
                              {message.role === "assistant" ? (
                                <MarkdownContent content={message.content} />
                              ) : (
                                message.content
                              )}
                            </div>
                            {message.role === "assistant" &&
                              (message.reply_status === "CANCELLED" ||
                                message.reply_status === "FAILED") && (
                                <p className="hint">
                                  {message.reply_status === "CANCELLED"
                                    ? "回复已停止"
                                    : "回复未完成"}
                                </p>
                              )}
                          </div>
                        </article>
                      ))}
                      {workspace.liveReply && (
                        <article className="message assistant live-reply">
                          <div className="message-avatar">
                            <span className="mini-mark" />
                          </div>
                          <div>
                            <h2>Orbit</h2>
                            <div className="message-content">
                              <StreamReply
                                content={workspace.liveReply.content}
                              />
                            </div>
                          </div>
                        </article>
                      )}
                      {task?.status === "WAITING_USER" && (
                        <div className="chat-handoff">
                          <strong>需要你补充一点信息</strong>
                          <p>{task.waiting.question}</p>
                          {task.waiting.reason === "approval" ? (
                            <div className="button-row">
                              <button
                                className="primary"
                                disabled={busy}
                                onClick={() =>
                                  void workspace.resume("确认", true)
                                }
                              >
                                允许并继续
                              </button>
                              {GRANTABLE_ACTIONS.includes(
                                task.waiting.tool ?? "",
                              ) && (
                                <button
                                  className="secondary"
                                  disabled={busy}
                                  onClick={() =>
                                    void workspace.resume("确认", true, true)
                                  }
                                >
                                  允许，这个站点以后不再问
                                </button>
                              )}
                              <button
                                className="secondary"
                                disabled={busy}
                                onClick={() =>
                                  void workspace.resume("拒绝", false)
                                }
                              >
                                拒绝
                              </button>
                            </div>
                          ) : (
                            <p className="hint">直接在下方回复即可继续。</p>
                          )}
                        </div>
                      )}
                      {task?.status === "FAILED" && (
                        <div className="chat-failure">
                          <p>{task.error}</p>
                          <button
                            className="secondary"
                            disabled={busy}
                            onClick={() => void workspace.retry()}
                          >
                            重试这次回复
                          </button>
                        </div>
                      )}
                      {task?.status === "CANCELLED" && (
                        <div className="chat-handoff">
                          <p>已停止回复。</p>
                          <button
                            className="secondary"
                            disabled={busy}
                            onClick={() => void workspace.retry()}
                          >
                            重新回复
                          </button>
                        </div>
                      )}
                      {!!events
                        .filter(
                          (event) => event.event_type === "activity_summary",
                        )
                        .at(-1)?.payload.text && (
                        <details className="thinking-summary">
                          <summary>查看过程说明</summary>
                          <p>
                            {String(
                              events
                                .filter(
                                  (event) =>
                                    event.event_type === "activity_summary",
                                )
                                .at(-1)?.payload.text,
                            )}
                          </p>
                        </details>
                      )}
                    </section>
                    <div className="composer-row">
                      {composer}
                      {(replyActive || task?.status === "WAITING_USER") && (
                        <button
                          className="stop-reply"
                          disabled={busy}
                          onClick={() => void workspace.stop()}
                        >
                          停止回复
                        </button>
                      )}
                    </div>
                  </>
                )}
              </main>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
