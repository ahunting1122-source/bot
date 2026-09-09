import { useState } from "react";
import { ExternalLink, Folder, Github, KeyRound, Link2 } from "lucide-react";
import { api, type Config } from "./api";

export function Settings({
  config,
  onSaved,
  onError,
}: {
  config: Config;
  onSaved: (c: Config) => void;
  onError: (e: string) => void;
}) {
  const [form, setForm] = useState({
    model: config.model,
    model_base_url: config.model_base_url,
    github_repo: config.github_repo,
    notion_url: config.notion_url,
    model_api_key: "",
    github_token: "",
  });
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setNotice("");
    try {
      const saved = await api<Config>("/connectors", "PUT", form);
      onSaved(saved);
      setForm({ ...form, model_api_key: "", github_token: "" });
      setNotice("设置已保存。可以返回工作台开始任务。");
    } catch (error) {
      onError((error as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function openBrowser() {
    setBusy(true);
    try {
      await api("/browser/session", "POST");
      setNotice("已打开专用浏览器，请在浏览器中登录。");
    } catch (error) {
      onError((error as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function field(
    key: keyof typeof form,
    label: string,
    placeholder: string,
    secret = false,
  ) {
    return (
      <label className="field">
        {label}
        <input
          type={secret ? "password" : "text"}
          value={form[key]}
          onChange={(e) => setForm({ ...form, [key]: e.target.value })}
          placeholder={placeholder}
          autoComplete="off"
        />
      </label>
    );
  }
  return (
    <main className="settings-page">
      <div className="page-intro">
        <span className="eyebrow">CONNECTIONS</span>
        <h1>连接你的工作空间</h1>
        <p>配置模型、网页平台，以及助手可以使用的本地资料。</p>
      </div>
      <form onSubmit={save}>
        <section className="settings-section">
          <h2>
            <KeyRound size={18} /> 模型服务
          </h2>
          <div className="field-grid">
            {field("model", "模型名称", "例如 gpt-4.1-mini")}
            {field("model_base_url", "API 地址", "https://api.openai.com/v1")}
          </div>
          {field(
            "model_api_key",
            config.model_key_set ? "API Key · 已保存，留空保持原值" : "API Key",
            "输入你的模型服务密钥",
            true,
          )}
          <p className="hint">
            使用支持工具调用的 OpenAI 兼容模型。选入上下文的资料会发送给该模型。
          </p>
        </section>
        <section className="settings-section">
          <h2>
            <Github size={18} /> GitHub
          </h2>
          {field("github_repo", "仓库", "owner/repository")}
          {field(
            "github_token",
            config.github_token_set
              ? "访问 Token · 已保存"
              : "访问 Token（公开仓库可留空）",
            "使用你有权限的仓库凭证",
            true,
          )}
        </section>
        <section className="settings-section">
          <h2>
            <Link2 size={18} /> Notion 网页
          </h2>
          {field("notion_url", "可编辑页面链接", "https://www.notion.so/...")}
          <p className="hint">
            先保存链接，再打开专用浏览器登录。助手会在这个页面读取、追加摘要并核对结果。
          </p>
          <button
            type="button"
            className="secondary"
            disabled={busy}
            onClick={() => void openBrowser()}
          >
            <ExternalLink size={15} />
            打开浏览器登录
          </button>
        </section>
        <section className="settings-section">
          <h2>
            <Folder size={18} /> 本地工作目录
          </h2>
          <code className="workspace-path">{config.workspace}</code>
          <p className="hint">
            将 TXT、Markdown 或文本型 PDF 放入此目录。需要更换目录时，在
            backend/.env 设置 ORBIT_WORKSPACE 后重启。
          </p>
        </section>
        {notice && (
          <p className="notice success" role="status">
            {notice}
          </p>
        )}
        <button type="submit" className="primary" disabled={busy}>
          {busy ? "处理中…" : "保存设置"}
        </button>
      </form>
    </main>
  );
}
