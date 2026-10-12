export type Conversation = {
  id: string; title: string; created_at: string; updated_at: string;
  summary: string; message_count: number; summary_updated_at: string;
  summary_status: "empty" | "pending" | "ready" | "failed";
  summary_error: string;
};
export type Message = {
  id: string; role: string; content: string; reply_status?: string;
};
export type Artifact = {
  id: string;
  kind: string;
  label: string;
  location: string;
};
export type Task = {
  id: string;
  conversation_id: string;
  goal: string;
  status: string;
  error: string;
  waiting: {
    question?: string;
    reason?: string;
    tool?: string;
    args?: Record<string, unknown>;
  };
  artifacts?: Artifact[];
};
export type TaskEvent = {
  id: number;
  task_id: string;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
};
export type SecretKey = {
  key: string;
  label: string;
  value: string;
  set: boolean;
  updated_at: string;
};
export type SiteSecret = {
  host: string;
  username: string;
  password: string;
  updated_at: string;
};
export type LoginState = {
  host: string;
  status: "logged_in" | "unknown" | "none";
  cookies: number;
  evidence: string[];
  expires_at: string;
  expiring_soon: boolean;
};
export type SecretsPayload = {
  keys: SecretKey[];
  sites: SiteSecret[];
  logins: LoginState[];
  logins_ready: boolean;
  logins_reason: string;
  hosts: string[];
};
export type McpServer = {
  name: string;
  target: string;
  enabled: boolean;
  auto_approve: boolean;
  secrets: string[];
  state: "idle" | "ready" | "reconnecting" | "error" | "disabled";
  tools: number;
  error: string;
};
export type Memory = {
  id: string;
  key: string;
  value: string;
  source: string;
  hit_count: number;
  last_used_at: string;
  updated_at: string;
};
export type Profile = {
  path: string;
  body: string;
  entries: string[];
  chars: number;
  limit: number;
  exists: boolean;
  dropped?: string[];
  added?: boolean;
};
export type Skill = {
  name: string;
  scope: string;
  version: number;
  triggers: string[];
  status: "unreviewed" | "active" | "stale" | "archived";
  usage_count: number;
  success_rate: number;
  consecutive_failures: number;
  last_used_at: string;
  updated_at: string;
  source?: string;
};
export type SkillDetail = Skill & {
  body: string;
};
export type Config = {
  model: string;
  model_base_url: string;
  github_repo: string;
  notion_url: string;
  browser_url: string;
  grants: Record<string, string[]>;
  workspace: string;
  model_key_set: boolean;
  github_token_set: boolean;
  model_status: "unconfigured" | "configured" | "verified" | "failed";
  model_checked_at: string;
  model_check_error: string;
};

let sessionRequest: Promise<void> | null = null;
export function ensureSession(): Promise<void> {
  if (!sessionRequest) {
    sessionRequest = fetch("/api/session", { credentials: "include" })
      .then((response) => {
        if (!response.ok) throw new Error("无法建立本地连接，请确认服务已经启动。");
      })
      .finally(() => { sessionRequest = null; });
  }
  return sessionRequest;
}

export async function api<T>(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  const options: RequestInit = {
    method,
    credentials: "include",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  };
  let response = await fetch(`/api${path}`, options);
  // A rejected session request has not reached the route, so one retry cannot duplicate a write.
  if (response.status === 401 && path !== "/session") {
    await ensureSession();
    response = await fetch(`/api${path}`, options);
  }
  if (!response.ok) {
    const text = await response.text();
    let message = text;
    try {
      const error = JSON.parse(text);
      message =
        typeof error.detail === "string"
          ? error.detail
          : "输入格式有误，请检查后重试";
    } catch {
      /* Keep plain text responses. */
    }
    throw new Error(message || `请求失败 (${response.status})`);
  }
  return response.status === 204 ? (undefined as T) : response.json();
}

export const labels: Record<string, string> = {
  PENDING: "等待执行",
  RUNNING: "正在执行",
  WAITING_USER: "等待你处理",
  SUCCEEDED: "已结束",
  FAILED: "执行失败",
  CANCELLED: "已取消",
};
