import { useState } from "react";
import {
  ArrowRight,
  ArrowUp,
  Brain,
  Check,
  Circle,
  FileText,
  FolderOpen,
  Github,
  Link2,
  Menu,
  MessageSquare,
  Plus,
  Settings2,
  Sparkles,
  X,
} from "lucide-react";
import { useWorkspace } from "./useWorkspace";
import { TaskPanel } from "./TaskPanel";
import { Settings } from "./Settings";
import { Memories } from "./Memories";

export function App() {
  const workspace = useWorkspace();
  const [view, setView] = useState<"chat" | "settings" | "memories">("chat");
  const [draft, setDraft] = useState("");
  const [menu, setMenu] = useState(false);
  const {
    config,
    conversations,
    selected,
    messages,
    task,
    events,
    error,
    busy,
  } = workspace;
  const running =
    task && ["PENDING", "RUNNING", "WAITING_USER"].includes(task.status);
  function newChat() {
    workspace.setSelected(null);
    setView("chat");
    setMenu(false);
    setDraft("");
  }
  function choose(id: string) {
    workspace.setSelected(id);
    setView("chat");
    setMenu(false);
  }
  async function send(e: React.FormEvent) {
    e.preventDefault();
    const content = draft;
    setDraft("");
    await workspace.submit(content);
  }
  const suggestions = [
    {
      icon: FolderOpen,
      title: "整理本地资料",
      text: "列出工作目录的文件，阅读项目笔记，生成一份中文 Markdown 摘要，保存到 reports。",
    },
    {
      icon: Github,
      title: "了解项目进展",
      text: "查询已配置 GitHub 仓库中与文档相关的 Issue，按重要性整理摘要并附上来源链接。",
    },
    {
      icon: Link2,
      title: "同步到 Notion",
      text: "阅读工作目录中的笔记，整理重点，然后追加到已配置的 Notion 页面并保存一份本地报告。",
    },
  ];
  return (
    <div className="shell">
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
          onClick={(e) => {
            e.preventDefault();
            newChat();
          }}
        >
          <span className="brand-mark">
            O<span />
          </span>
          <strong>
            orbit<span>个人工作台</span>
          </strong>
        </a>
        <button className="new-chat" onClick={newChat}>
          <Plus size={17} />
          开启新对话<span>＋</span>
        </button>
        <div className="nav-group">
          <button
            className={view === "chat" ? "nav-item active" : "nav-item"}
            onClick={() => {
              setView("chat");
              setMenu(false);
            }}
          >
            <MessageSquare size={17} />
            工作台
          </button>
          <button
            className={view === "memories" ? "nav-item active" : "nav-item"}
            onClick={() => {
              setView("memories");
              setMenu(false);
            }}
          >
            <Brain size={17} />
            长期记忆
          </button>
          <button
            className={view === "settings" ? "nav-item active" : "nav-item"}
            onClick={() => {
              setView("settings");
              setMenu(false);
            }}
          >
            <Settings2 size={17} />
            连接与设置
          </button>
        </div>
        <div className="history-heading">
          最近对话 <span>{conversations.length}</span>
        </div>
        <div className="conversation-list">
          {conversations.length ? (
            conversations.map((c) => (
              <button
                key={c.id}
                className={
                  selected === c.id ? "conversation selected" : "conversation"
                }
                title={c.title}
                onClick={() => choose(c.id)}
              >
                <MessageSquare size={14} />
                <span>{c.title}</span>
              </button>
            ))
          ) : (
            <p className="history-empty">你的任务，从一段对话开始。</p>
          )}
        </div>
        <div className="sidebar-bottom">
          <div className="local-indicator">
            <Circle size={7} fill="currentColor" /> 本地工作空间
          </div>
          <div className="user-row">
            <span className="avatar">我</span>
            <div>
              个人助手<small>文件 · 网页 · 记忆</small>
            </div>
            <Check size={14} />
          </div>
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
            <span className="breadcrumb">/</span>
            <strong>
              {view === "settings"
                ? "连接与设置"
                : view === "memories"
                  ? "长期记忆"
                  : "工作台"}
            </strong>
          </div>
          <button className="model-pill" onClick={() => setView("settings")}>
            <span
              className={`connection-dot ${config?.model_key_set ? "connected" : ""}`}
            />
            {config?.model_key_set ? config.model || "选择模型" : "配置模型"}
            <Settings2 size={12} />
          </button>
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
        {!config ? (
          <main className="loading-state" role="status">
            <div className="empty-orbit" />
            <p>
              {error
                ? "请确认后端已启动，再刷新页面。"
                : "正在连接你的本地工作空间…"}
            </p>
          </main>
        ) : view === "settings" ? (
          <Settings
            config={config}
            onSaved={workspace.setConfig}
            onError={workspace.setError}
          />
        ) : view === "memories" ? (
          <Memories onError={workspace.setError} />
        ) : (
          <div className="workspace-body">
            <main className="chat-main">
              {!messages.length ? (
                <section className="welcome">
                  <span className="eyebrow">
                    <Sparkles size={14} /> YOUR PERSONAL ASSISTANT
                  </span>
                  <h1>
                    把想法交给我，
                    <br />
                    <span>一起完成下一件事。</span>
                  </h1>
                  <p className="welcome-description">
                    从本地的一份笔记，到网页上的一个任务。
                    <br />
                    连接资料、理解目标，把结果留在你需要的地方。
                  </p>
                  <div className="capability-strip">
                    <span>
                      <FolderOpen size={14} />
                      本地文件
                    </span>
                    <span>
                      <Link2 size={14} />
                      网页连接
                    </span>
                    <span>
                      <Brain size={14} />
                      跨对话记忆
                    </span>
                  </div>
                  <div className="suggestions">
                    {suggestions.map((s) => (
                      <button key={s.title} onClick={() => setDraft(s.text)}>
                        <s.icon size={19} />
                        <strong>{s.title}</strong>
                        <p>{s.text}</p>
                        <ArrowRight size={15} />
                      </button>
                    ))}
                  </div>
                  {!config.model_key_set && (
                    <button
                      className="setup-nudge"
                      onClick={() => setView("settings")}
                    >
                      <span>
                        <Settings2 size={16} />
                        首次使用，先连接一个模型服务
                      </span>
                      <ArrowRight size={16} />
                    </button>
                  )}
                </section>
              ) : (
                <section className="messages" aria-label="对话内容">
                  {messages.map((m) => (
                    <article className={`message ${m.role}`} key={m.id}>
                      <div className="message-avatar">
                        {m.role === "user" ? "我" : <Sparkles size={16} />}
                      </div>
                      <div>
                        <h2>{m.role === "user" ? "你" : "Orbit"}</h2>
                        <div className="message-content">{m.content}</div>
                      </div>
                    </article>
                  ))}
                  {running && (
                    <div className="working-note" role="status">
                      <Circle size={8} />
                      {task.status === "WAITING_USER"
                        ? "需要你在右侧完成接管或确认"
                        : "正在处理任务，执行详情会出现在右侧…"}
                    </div>
                  )}
                </section>
              )}
              <div className="composer-wrap">
                <form className="composer" onSubmit={(e) => void send(e)}>
                  <label htmlFor="task-input" className="sr-only">
                    给助手一个任务
                  </label>
                  <textarea
                    id="task-input"
                    placeholder="告诉我你想完成什么…"
                    rows={3}
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                        e.preventDefault();
                        if (draft.trim() && !running && !busy) void send(e);
                      }
                    }}
                  />
                  <div className="composer-bottom">
                    <span>
                      <FileText size={14} />
                      工作目录中的资料可直接读取
                    </span>
                    <button
                      className="send-button"
                      type="submit"
                      aria-label="发送任务"
                      disabled={!draft.trim() || busy || !!running}
                    >
                      <ArrowUp size={20} />
                    </button>
                  </div>
                </form>
                <p className="composer-hint">
                  Ctrl + Enter 发送 · 登录或验证需要时，可在浏览器中接管
                </p>
              </div>
            </main>
            <TaskPanel
              task={task}
              events={events}
              onChange={workspace.refresh}
              onError={workspace.setError}
            />
          </div>
        )}
      </div>
    </div>
  );
}
