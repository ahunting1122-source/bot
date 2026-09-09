export type Conversation = { id: string; title: string; created_at: string };
export type Message = { id: string; role: string; content: string };
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
export type Memory = {
  id: string;
  key: string;
  value: string;
  updated_at: string;
};
export type Config = {
  model: string;
  model_base_url: string;
  github_repo: string;
  notion_url: string;
  workspace: string;
  model_key_set: boolean;
  github_token_set: boolean;
};

export async function api<T>(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method,
    credentials: "include",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
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
