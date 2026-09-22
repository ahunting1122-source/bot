import { useCallback, useEffect, useState, type ReactNode } from "react";
import {
  ArrowLeft,
  ExternalLink,
  Folder,
  Github,
  Globe2,
  KeyRound,
  Link2,
  Plug,
  ShieldCheck,
} from "lucide-react";
import { api, type Config, type McpServer } from "./api";
import "./connection-pages.css";

type SectionName = "model" | "github" | "browser" | "notion";
type Feedback = { kind: "success" | "error"; text: string };
type FormValues = {
  model: string;
  model_base_url: string;
  model_api_key: string;
  github_repo: string;
  github_token: string;
  browser_url: string;
  notion_url: string;
};
const sectionFields: Record<SectionName, (keyof FormValues)[]> = {
  model: ["model", "model_base_url", "model_api_key"],
  github: ["github_repo", "github_token"],
  browser: ["browser_url"],
  notion: ["notion_url"],
};
const modelLabels = {
  unconfigured: "尚未配置",
  configured: "已保存 · 尚未验证",
  verified: "连接检查通过",
  failed: "最近一次连接检查失败",
};

function SettingsSection({
  title,
  icon,
  children,
  feedback,
  busy,
  onSave,
}: {
  title: string;
  icon: ReactNode;
  children: ReactNode;
  feedback?: Feedback;
  busy: boolean;
  onSave: () => void;
}) {
  return (
    <form
      className="settings-section"
      onSubmit={(event) => {
        event.preventDefault();
        onSave();
      }}
      aria-busy={busy}
    >
      <h2>
        {icon}
        {title}
      </h2>
      {children}
      {feedback && (
        <p
          className={`notice ${feedback.kind}`}
          role={feedback.kind === "error" ? "alert" : "status"}
        >
          {feedback.text}
        </p>
      )}
    </form>
  );
}

const mcpLabels: Record<McpServer["state"], string> = {
  ready: "已连接",
  error: "连接失败",
  disabled: "已停用",
  idle: "等待连接",
};

function McpSection({ onError }: { onError: (e: string) => void }) {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");

  const load = useCallback(async () => {
    const data = await api<{ servers: McpServer[]; config: unknown[] }>("/mcp");
    setServers(data.servers);
    setText(JSON.stringify(data.config ?? [], null, 2));
  }, []);

  useEffect(() => {
    void load().catch((e: Error) => onError(e.message));
  }, [load, onError]);

  async function run(label: string, action: () => Promise<void>) {
    if (busy) return;
    setBusy(label);
    setNote("");
    try {
      await action();
      await load();
    } catch (e) {
      onError(e instanceof Error ? e.message : "MCP 操作失败。");
    } finally {
      setBusy("");
    }
  }

  return (
    <SettingsSection
      title="MCP 服务"
      icon={<Plug size={18} />}
      busy={!!busy}
      onSave={() => undefined}
    >
      {servers.length === 0 ? (
        <p className="hint">
          还没有配置 MCP 服务。在下面填入服务列表后保存，助手就能调用它们提供的工具。
        </p>
      ) : (
        <ul className="grant-list">
          {servers.map((server) => (
            <li key={server.name}>
              <span>
                <strong>{server.name}</strong>
                <span className="hint">
                  {" "}
                  {mcpLabels[server.state]}
                  {server.state === "ready" ? ` · ${server.tools} 个工具` : ""}
                  {server.auto_approve ? " · 免确认" : ""}
                </span>
                {server.error && (
                  <span className="hint"> {server.error.slice(0, 120)}</span>
                )}
              </span>
              <span className="hint">{server.target.slice(0, 60)}</span>
            </li>
          ))}
        </ul>
      )}
      <label className="field">
        <span>服务配置（JSON 数组）</span>
        <textarea
          rows={8}
          spellCheck={false}
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder={
            '[{"name": "filesystem", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "E:\\\\notes"]}]'
          }
        />
      </label>
      <p className="hint">
        <code>command</code> + <code>args</code> 启动本地进程，或填 <code>url</code> 连远程服务。
        默认每个工具都要确认；确认无误的服务可以加 <code>"auto_approve": true</code>。
        密钥不要写在文件里：把值写成 <code>"keyring:名称"</code> 再用下面的接口单独保存。
      </p>
      <div className="connection-actions">
        <button
          type="button"
          className="secondary"
          disabled={!!busy}
          onClick={() =>
            void run("save", async () => {
              const parsed = JSON.parse(text) as unknown;
              if (!Array.isArray(parsed)) throw new Error("配置必须是 JSON 数组");
              const result = await api<{ servers: McpServer[] }>("/mcp", "PUT", {
                servers: parsed,
              });
              setServers(result.servers);
              setNote("已保存并重新连接。");
            })
          }
        >
          {busy === "save" ? "保存中…" : "保存并重连"}
        </button>
        <button
          type="button"
          className="secondary"
          disabled={!!busy}
          onClick={() =>
            void run("reload", async () => {
              await api("/mcp/reload", "POST");
              setNote("已重新连接。");
            })
          }
        >
          {busy === "reload" ? "重连中…" : "重新连接"}
        </button>
      </div>
      {note && (
        <p className="notice success" role="status">
          {note}
        </p>
      )}
    </SettingsSection>
  );
}

export function Settings({
  config,
  onSaved,
  onError,
  onBack,
  onRevoke,
}: {
  config: Config;
  onSaved: (c: Config) => void;
  onError: (e: string) => void;
  onBack?: () => void;
  onRevoke: (host: string) => Promise<boolean>;
}) {
  const [form, setForm] = useState<FormValues>({
    model: config.model,
    model_base_url: config.model_base_url,
    github_repo: config.github_repo,
    notion_url: config.notion_url,
    browser_url: config.browser_url || "",
    model_api_key: "",
    github_token: "",
  });
  const [busy, setBusy] = useState<{
    section: SectionName;
    action: string;
  } | null>(null);
  const [feedback, setFeedback] = useState<
    Partial<Record<SectionName, Feedback>>
  >({});
  const modelDirty =
    form.model.trim() !== config.model ||
    form.model_base_url.trim() !== config.model_base_url ||
    !!form.model_api_key.trim();

  async function run(
    section: SectionName,
    action: string,
    operation: () => Promise<string>,
  ) {
    if (busy) return;
    setBusy({ section, action });
    setFeedback((previous) => ({ ...previous, [section]: undefined }));
    onError("");
    try {
      const text = await operation();
      setFeedback((previous) => ({
        ...previous,
        [section]: { kind: "success", text },
      }));
    } catch (error) {
      const text =
        error instanceof Error ? error.message : "操作失败，请重试。";
      setFeedback((previous) => ({
        ...previous,
        [section]: { kind: "error", text },
      }));
    } finally {
      setBusy(null);
    }
  }

  async function persist(section: SectionName) {
    const values = Object.fromEntries(
      sectionFields[section].map((key) => [key, form[key].trim()]),
    );
    const saved = await api<Config>("/connectors", "PUT", values);
    onSaved(saved);
    setForm((previous) => {
      const next = { ...previous };
      for (const key of sectionFields[section]) {
        next[key] =
          key === "model_api_key" || key === "github_token" ? "" : saved[key];
      }
      return next;
    });
    return saved;
  }

  function save(section: SectionName) {
    void run(section, "save", async () => {
      await persist(section);
      return section === "model"
        ? "模型设置已保存。可以检查连接，确认模型能够响应。"
        : "设置已保存。";
    });
  }

  function checkModel() {
    void run("model", "check", async () => {
      const checked = await api<Config>("/connectors/model/check", "POST");
      onSaved(checked);
      if (checked.model_status !== "verified") {
        throw new Error(
          checked.model_check_error || "模型连接检查未通过，请检查配置后重试。",
        );
      }
      return "模型已成功响应，连接检查通过。";
    });
  }

  function openBrowser(
    section: "browser" | "notion",
    element: HTMLFormElement | null,
  ) {
    if (element && !element.reportValidity()) return;
    void run(section, "open", async () => {
      const changed = sectionFields[section].some(
        (key) =>
          form[key].trim() !== config[key as "browser_url" | "notion_url"],
      );
      const saved = changed ? await persist(section) : config;
      const url = section === "browser" ? saved.browser_url : saved.notion_url;
      const session = await api<{
        url: string;
        title: string;
        mode: string;
        visible: boolean;
      }>("/browser/session", "POST", { url });
      if (session.mode !== "headed" || session.visible !== true) {
        throw new Error("未能打开可见浏览器，请重试。");
      }
      return `已打开可见浏览器：${session.title || session.url}。需要登录时，请在浏览器窗口中完成。`;
    });
  }

  function field(
    key: keyof FormValues,
    label: string,
    placeholder: string,
    type = "text",
    required = false,
  ) {
    return (
      <label className="field">
        {label}
        <input
          name={key}
          type={type}
          value={form[key]}
          disabled={!!busy}
          required={required}
          pattern={type === "url" ? "https?://.+" : undefined}
          title={
            type === "url"
              ? "请输入以 http:// 或 https:// 开头的网址"
              : undefined
          }
          onChange={(event) => {
            setForm((previous) => ({ ...previous, [key]: event.target.value }));
            const section = (Object.keys(sectionFields) as SectionName[]).find(
              (name) => sectionFields[name].includes(key),
            );
            if (section)
              setFeedback((previous) => ({
                ...previous,
                [section]: undefined,
              }));
          }}
          placeholder={placeholder}
          autoComplete="off"
        />
      </label>
    );
  }
  function saveButton(section: SectionName, label: string) {
    return (
      <button type="submit" className="primary" disabled={!!busy}>
        {busy?.section === section && busy.action === "save"
          ? "保存中…"
          : label}
      </button>
    );
  }
  function browserButton(section: "browser" | "notion", label: string) {
    return (
      <button
        type="button"
        className="secondary"
        disabled={
          !!busy ||
          (section === "browser"
            ? !form.browser_url.trim()
            : !form.notion_url.trim())
        }
        onClick={(event) => openBrowser(section, event.currentTarget.form)}
      >
        <ExternalLink size={15} />
        {busy?.section === section && busy.action === "open"
          ? "正在打开…"
          : label}
      </button>
    );
  }
  function sectionProps(section: SectionName) {
    return {
      feedback: feedback[section],
      busy: busy?.section === section,
      onSave: () => save(section),
    };
  }

  return (
    <main className="settings-page connection-settings">
      <div className="page-intro">
        {onBack && (
          <button type="button" className="page-back" onClick={onBack}>
            <ArrowLeft size={15} />
            返回对话
          </button>
        )}
        <h1>连接与设置</h1>
        <p>配置常用的模型、网页和资料来源。每个区域单独保存。</p>
      </div>
      <SettingsSection
        title="模型服务"
        icon={<KeyRound size={18} />}
        {...sectionProps("model")}
      >
        <p
          className={`connection-state ${config.model_status === "failed" ? "connection-state-error" : ""}`}
        >
          {modelLabels[config.model_status || "unconfigured"]}
          {config.model_checked_at && (
            <span>
              {" "}
              · {new Date(config.model_checked_at).toLocaleString("zh-CN")}
            </span>
          )}
        </p>
        <div className="field-grid">
          {field("model", "模型名称", "例如 deepseek-chat", "text", true)}
          {field(
            "model_base_url",
            "API 地址",
            "https://api.deepseek.com",
            "url",
            true,
          )}
        </div>
        {field(
          "model_api_key",
          config.model_key_set ? "API Key · 已保存，留空保持原值" : "API Key",
          "输入模型服务密钥",
          "password",
        )}
        <p className="hint">
          使用支持工具调用的 OpenAI 兼容模型。检查连接会发送一次简短请求。
        </p>
        {modelDirty && (
          <p className="hint">当前有未保存的修改，请先保存再检查连接。</p>
        )}
        <div className="connection-actions">
          {saveButton("model", "保存模型设置")}
          <button
            type="button"
            className="secondary"
            disabled={
              !!busy || modelDirty || !config.model_key_set || !config.model
            }
            onClick={checkModel}
          >
            {busy?.action === "check" ? "检查中…" : "检查模型连接"}
          </button>
        </div>
      </SettingsSection>
      <SettingsSection
        title="通用网页"
        icon={<Globe2 size={18} />}
        {...sectionProps("browser")}
      >
        {field("browser_url", "打开的网址", "https://www.bilibili.com/", "url")}
        <p className="hint">
          对话中可以让它打开任意网址，不受这里限制。打开时会先保存本区域的修改，再启动可见浏览器；登录后可继续使用同一个会话。
        </p>
        <div className="connection-actions">
          {saveButton("browser", "保存网页设置")}
          {browserButton("browser", "打开浏览器登录")}
        </div>
      </SettingsSection>
      <SettingsSection
        title="已记住的站点操作"
        icon={<ShieldCheck size={18} />}
        busy={false}
        onSave={() => undefined}
      >
        {Object.keys(config.grants ?? {}).length === 0 ? (
          <p className="hint">
            还没有记住任何操作。在聊天里点「允许，这个站点以后不再问」即可记住。
          </p>
        ) : (
          <ul className="grant-list">
            {Object.entries(config.grants ?? {}).map(([host, actions]) => (
              <li key={host}>
                <span>
                  <strong>{host}</strong>
                  <span className="hint"> {actions.join("、")}</span>
                </span>
                <button
                  type="button"
                  className="secondary"
                  disabled={!!busy}
                  onClick={() => {
                    void onRevoke(host).catch((e: Error) => onError(e.message));
                  }}
                >
                  撤销
                </button>
              </li>
            ))}
          </ul>
        )}
        <p className="hint">
          只对点击、填写、按键、滚动这类站内动作生效；写入、删除等操作始终会逐次询问。
        </p>
      </SettingsSection>
      <SettingsSection
        title="GitHub"
        icon={<Github size={18} />}
        {...sectionProps("github")}
      >
        {field("github_repo", "仓库", "owner/repository")}
        {field(
          "github_token",
          config.github_token_set
            ? "访问 Token · 已保存，留空保持原值"
            : "访问 Token（公开仓库可留空）",
          "使用你有权限的仓库凭证",
          "password",
        )}
        <p className="hint">
          保存仓库后可在对话中查询 Issue。保存配置不代表已经验证仓库访问权限。
        </p>
        {saveButton("github", "保存 GitHub 设置")}
      </SettingsSection>
      <SettingsSection
        title="Notion 页面"
        icon={<Link2 size={18} />}
        {...sectionProps("notion")}
      >
        {field(
          "notion_url",
          "可编辑页面链接",
          "https://www.notion.so/...",
          "url",
        )}
        <p className="hint">
          用于读取和追加笔记。登录按钮只打开这里填写的页面，不影响通用网页设置。
        </p>
        <div className="connection-actions">
          {saveButton("notion", "保存 Notion 设置")}
          <button
            type="button"
            className="secondary"
            disabled={!!busy || !form.notion_url.trim()}
            onClick={(event) => openBrowser("notion", event.currentTarget.form)}
          >
            <ExternalLink size={15} />
            {busy?.section === "notion" && busy.action === "open"
              ? "正在打开…"
              : "打开 Notion 登录"}
          </button>
        </div>
      </SettingsSection>
      <McpSection onError={(message) => onError(message)} />
      <section className="settings-section">
        <h2>
          <Folder size={18} />
          本地工作目录
        </h2>
        <code className="workspace-path">{config.workspace}</code>
        <p className="hint">
          将 TXT、Markdown 或文本型 PDF
          放入此目录，即可在对话中让助手阅读和整理。
        </p>
      </section>
    </main>
  );
}
