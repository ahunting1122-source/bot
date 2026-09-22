import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  Eye,
  EyeOff,
  KeyRound,
  Plus,
  RefreshCw,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import {
  api,
  type LoginState,
  type SecretsPayload,
  type SecretKey,
  type SiteSecret,
} from "./api";
import "./connection-pages.css";

const loginLabels: Record<LoginState["status"], string> = {
  logged_in: "疑似已登录",
  unknown: "有 Cookie，未识别到会话",
  none: "没有该站点的 Cookie",
};

function masked(value: string) {
  return value ? "•".repeat(Math.min(Math.max(value.length, 6), 24)) : "";
}

function stamp(value: string) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString("zh-CN", { hour12: false });
}

export function Vault({
  onError,
  onBack,
}: {
  onError: (e: string) => void;
  onBack?: () => void;
}) {
  const [data, setData] = useState<SecretsPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [hidden, setHidden] = useState(false);
  const [draftKeys, setDraftKeys] = useState<Record<string, string>>({});
  const [newSite, setNewSite] = useState({ host: "", username: "", password: "" });
  const [notice, setNotice] = useState("");
  const mounted = useRef(false);

  const load = useCallback(
    async (quiet = false) => {
      if (!quiet) setLoading(true);
      try {
        const payload = await api<SecretsPayload>("/secrets");
        if (mounted.current) setData(payload);
      } catch (cause) {
        if (mounted.current)
          onError(cause instanceof Error ? cause.message : "无法读取密钥存储。");
      } finally {
        if (mounted.current) setLoading(false);
      }
    },
    [onError],
  );

  useEffect(() => {
    mounted.current = true;
    void load();
    return () => {
      mounted.current = false;
    };
  }, [load]);

  async function run(label: string, action: () => Promise<void>) {
    if (busy) return;
    setBusy(label);
    setNotice("");
    try {
      await action();
      await load(true);
    } catch (cause) {
      onError(cause instanceof Error ? cause.message : "操作失败，请重试。");
    } finally {
      setBusy("");
    }
  }

  const saveKey = (item: SecretKey) => {
    const value = (draftKeys[item.key] || "").trim();
    if (!value) {
      setNotice(`请先填写 ${item.label} 的新值。`);
      return;
    }
    void run(item.key, async () => {
      await api(`/secrets`, "POST", { key: item.key, value });
      setDraftKeys((previous) => ({ ...previous, [item.key]: "" }));
      setNotice(`${item.label} 已保存。`);
    });
  };

  const removeKey = (item: SecretKey) => {
    void run(item.key, async () => {
      await api(`/secrets/${encodeURIComponent(item.key)}`, "DELETE");
      setNotice(`${item.label} 已删除。`);
    });
  };

  const saveSite = () => {
    void run("new-site", async () => {
      await api("/secrets/sites", "POST", newSite);
      setNewSite({ host: "", username: "", password: "" });
      setNotice(`已保存 ${newSite.host} 的账号。`);
    });
  };

  const removeSite = (item: SiteSecret) => {
    void run(item.host, async () => {
      await api("/secrets/sites/remove", "POST", { host: item.host });
      setNotice(`已删除 ${item.host} 的账号。`);
    });
  };

  return (
    <main className="settings-page vault-page">
      <div className="page-intro">
        {onBack && (
          <button type="button" className="page-back" onClick={onBack}>
            <ArrowLeft size={15} />
            返回对话
          </button>
        )}
        <p className="eyebrow">凭证与登录</p>
        <h1>密钥存储</h1>
        <p>
          查看已保存的密钥和站点账号，检查各个站点的登录状态。明文只在本机展示，不会进入对话或日志。
        </p>
        <div className="connection-actions">
          <button
            type="button"
            className="secondary"
            onClick={() => setHidden((value) => !value)}
          >
            {hidden ? <Eye size={15} /> : <EyeOff size={15} />}
            {hidden ? "显示全部明文" : "全部隐藏"}
          </button>
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
            {loading ? "读取中…" : "刷新"}
          </button>
        </div>
        {notice && (
          <p className="notice success" role="status">
            {notice}
          </p>
        )}
      </div>

      <section className="settings-section">
        <h2>
          <KeyRound size={18} />
          密钥
        </h2>
        {!data ? (
          <p className="hint">正在读取…</p>
        ) : (
          data.keys.map((item) => (
            <div className="vault-row" key={item.key}>
              <div className="vault-main">
                <span className="vault-label">{item.label}</span>
                <code className="vault-value">
                  {item.set
                    ? hidden
                      ? masked(item.value)
                      : item.value
                    : "尚未设置"}
                </code>
                <span className="hint">更新于 {stamp(item.updated_at)}</span>
              </div>
              <div className="vault-actions">
                <input
                  type="password"
                  aria-label={`${item.label} 新值`}
                  placeholder="输入新值"
                  value={draftKeys[item.key] || ""}
                  onChange={(event) =>
                    setDraftKeys((previous) => ({
                      ...previous,
                      [item.key]: event.target.value,
                    }))
                  }
                />
                <button
                  type="button"
                  className="secondary"
                  disabled={!!busy}
                  onClick={() => saveKey(item)}
                >
                  {busy === item.key ? "保存中…" : "保存"}
                </button>
                {item.set && (
                  <button
                    type="button"
                    className="icon-button"
                    aria-label={`删除 ${item.label}`}
                    disabled={!!busy}
                    onClick={() => removeKey(item)}
                  >
                    <Trash2 size={15} />
                  </button>
                )}
              </div>
            </div>
          ))
        )}
        <p className="hint">
          密钥存放在系统凭据管理器（Windows 凭据管理器）里，本文件不保存它们的值。
        </p>
      </section>

      <section className="settings-section">
        <h2>
          <ShieldCheck size={18} />
          站点账号
        </h2>
        {!data || data.sites.length === 0 ? (
          <p className="hint">还没有保存站点账号。可以在下面添加。</p>
        ) : (
          data.sites.map((item) => (
            <div className="vault-row" key={item.host}>
              <div className="vault-main">
                <span className="vault-label">{item.host}</span>
                <code className="vault-value">
                  {hidden
                    ? `${masked(item.username)} / ${masked(item.password)}`
                    : `${item.username || "（未填用户名）"} / ${item.password || "（未填密码）"}`}
                </code>
                <span className="hint">更新于 {stamp(item.updated_at)}</span>
              </div>
              <div className="vault-actions">
                <button
                  type="button"
                  className="secondary"
                  disabled={!!busy}
                  onClick={() => removeSite(item)}
                >
                  删除
                </button>
              </div>
            </div>
          ))
        )}
        <div className="vault-row">
          <div className="vault-actions vault-actions--form">
            <input
              aria-label="站点主机名"
              placeholder="站点主机名，例如 www.bilibili.com"
              value={newSite.host}
              onChange={(event) =>
                setNewSite((previous) => ({ ...previous, host: event.target.value }))
              }
            />
            <input
              aria-label="站点用户名"
              placeholder="用户名 / 手机号 / 邮箱"
              value={newSite.username}
              onChange={(event) =>
                setNewSite((previous) => ({ ...previous, username: event.target.value }))
              }
            />
            <input
              type="password"
              aria-label="站点密码"
              placeholder="密码"
              value={newSite.password}
              onChange={(event) =>
                setNewSite((previous) => ({ ...previous, password: event.target.value }))
              }
            />
            <button
              type="button"
              className="secondary"
              disabled={!!busy || !newSite.host.trim()}
              onClick={saveSite}
            >
              <Plus size={15} />
              {busy === "new-site" ? "保存中…" : "添加"}
            </button>
          </div>
        </div>
        <p className="hint">
          这些账号可用于「用保存的账号填写登录框」，但每次填写都会单独向你确认。
        </p>
      </section>

      <section className="settings-section">
        <h2>
          <ShieldCheck size={18} />
          登录状态
        </h2>
        {!data ? (
          <p className="hint">正在读取…</p>
        ) : !data.logins_ready ? (
          <p className="hint">
            {data.logins_reason ||
              "需要先打开浏览器才能读取登录状态。"}
            {" "}在「连接与设置」里点「打开浏览器登录」后再刷新。
          </p>
        ) : (
          data.logins.map((item) => (
            <div className="vault-row" key={item.host}>
              <div className="vault-main">
                <span className="vault-label">
                  {item.host}
                  <span
                    className={`glance-badge glance-badge--${item.status}`}
                  >
                    {item.expiring_soon ? "即将过期" : loginLabels[item.status]}
                  </span>
                </span>
                <code className="vault-value">
                  {item.cookies} 个 Cookie
                  {item.evidence.length ? ` · 依据 ${item.evidence.join(", ")}` : ""}
                </code>
                <span className="hint">
                  {item.expires_at
                    ? `会话凭证最晚到期 ${stamp(item.expires_at)}`
                    : "无明确到期时间"}
                </span>
              </div>
            </div>
          ))
        )}
        <p className="hint">
          登录状态是根据本机浏览器 Profile 里的 Cookie 推测的，仅供参考；「即将过期」表示会话 Cookie 三天内到期。
        </p>
      </section>
    </main>
  );
}
