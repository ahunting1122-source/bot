import {
  ArrowUpRight,
  FileText,
  Github,
  NotebookPen,
  FolderOpen,
  Globe2,
  MessagesSquare,
  Unlock,
} from "lucide-react";
import type { ReactNode } from "react";
import type { Config } from "./api";
import "./connection-pages.css";

export function Welcome({
  children,
  onChoose,
  config,
  onSettings,
}: {
  children: ReactNode;
  onChoose: (text: string) => void;
  config: Config | null;
  onSettings?: () => void;
}) {
  const hour = new Date().getHours();
  const greeting =
    hour < 6
      ? "夜深了，慢慢来。"
      : hour < 12
        ? "早上好，今天从哪里开始？"
        : hour < 18
          ? "下午好，今天从哪里开始？"
          : "晚上好，今天从哪里开始？";
  const suggestions = [
    {
      icon: FileText,
      title: "读一份资料，整理成笔记",
      detail: "本地文件",
      text: "先看看工作目录里有哪些资料，和我一起梳理其中的重点。",
    },
    {
      icon: Github,
      title: "看看项目最近在讨论什么",
      detail: config?.github_repo ? "GitHub" : "需配置仓库",
      text: "查询已配置 GitHub 仓库中与文档相关的 Issue，整理摘要并附上来源链接。",
      needsConfig: !config?.github_repo,
    },
    {
      icon: NotebookPen,
      title: "把整理好的内容同步到 Notion",
      detail: config?.notion_url ? "Notion" : "需配置页面",
      text: "先和我一起整理笔记内容，确认后再追加到已配置的 Notion 页面。",
      needsConfig: !config?.notion_url,
    },
  ];
  const modelState = !config
    ? "正在读取配置…"
    : {
        unconfigured: "尚未配置",
        configured: "已保存 · 尚未验证",
        verified: "最近一次连接检查通过",
        failed: "最近一次连接检查失败",
      }[config.model_status || "unconfigured"];

  return (
    <section className="welcome">
      <div className="welcome-heading">
        <p className="greeting">你的个人工作台</p>
        <h1>{greeting}</h1>
        <p className="welcome-description">
          陪你读资料、整理信息，也聊聊正在思考的事。
        </p>
      </div>
      {children}
      <section className="suggestions" aria-label="对话建议">
        <h2>从这里开始</h2>
        {suggestions.map((item) => (
          <button
            type="button"
            key={item.title}
            onClick={() =>
              item.needsConfig && onSettings
                ? onSettings()
                : onChoose(item.text)
            }
          >
            <item.icon size={18} strokeWidth={1.6} />
            <span className="suggestion-title">{item.title}</span>
            <span className="suggestion-source">{item.detail}</span>
            <ArrowUpRight className="suggestion-arrow" size={15} />
          </button>
        ))}
      </section>
      <section className="workspace-glance" aria-label="连接概览">
        <div className="glance-heading">
          <span>连接概览</span>
          {onSettings && (
            <button type="button" className="text-action" onClick={onSettings}>
              管理连接
              <ArrowUpRight size={13} />
            </button>
          )}
        </div>
        <div className="glance-grid">
          <div className="glance-card">
            <span className="glance-icon">
              {config?.file_scope === "machine" ? (
                <Unlock size={17} />
              ) : (
                <FolderOpen size={17} />
              )}
            </span>
            <div>
              <strong>
                {config?.file_scope === "machine" ? "完全访问权限" : "本地工作目录"}
              </strong>
              <small className="glance-path" title={config?.workspace}>
                {config?.file_scope === "machine"
                  ? "可读写任意文件、执行任意命令"
                  : config?.workspace || "正在读取配置…"}
              </small>
            </div>
          </div>
          <div className="glance-card">
            <span className="glance-icon">
              <MessagesSquare size={17} />
            </span>
            <div>
              <strong>{config?.model || "模型服务"}</strong>
              <small>{modelState}</small>
            </div>
          </div>
          <div className="glance-card">
            <span className="glance-icon">
              <Globe2 size={17} />
            </span>
            <div>
              <strong>网页浏览</strong>
              <small>
                {config?.browser_url
                  ? "已保存网址 · 可打开浏览器"
                  : "在连接设置中打开网页"}
              </small>
            </div>
          </div>
        </div>
      </section>
    </section>
  );
}
