"""真实 Agent 跑一轮命令执行（需要后端在 6185 跑着，且模型已配置）。

和 `e2e_file_scope.py` 的分工：
  - 那个是**确定性**的——直接驱动工具层，不打模型，可在 CI 里反复跑；
  - 这个是**端到端**的——真的让模型自己决定调用 `run_command`，验证"工具进了模型可见列表、
    模型会用它、结果能回到正文"这条链没有断。会花一次模型调用，所以单独成篇、手动跑。
"""

import asyncio
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:6185"
MARKER = "ORBIT-CMD-OK-7421"
PROMPT = f"请用 run_command 工具在本机执行这条命令：echo {MARKER}，然后把它的标准输出原样告诉我。"
FAILS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + label + (f"   [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(label)


class Client:
    def __init__(self) -> None:
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def call(self, method: str, path: str, payload: object | None = None):
        data = None
        headers: dict[str, str] = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8")
            try:
                parsed: object = json.loads(raw)
            except json.JSONDecodeError:
                parsed = raw
            return error.code, parsed

    def events(self, task_id: str) -> list[dict]:
        """`/events` 是 SSE：`after=0` 回放全部事件，任务终态后补一帧 settled 并关闭。"""
        request = urllib.request.Request(BASE + f"/api/tasks/{task_id}/events?after=0")
        with self.opener.open(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
        rows = []
        for line in raw.splitlines():
            if not line.startswith("data: "):
                continue
            body = line[6:]
            if body in {"", "{}"}:
                continue
            try:
                rows.append(json.loads(body))
            except json.JSONDecodeError:
                continue
        return rows


async def main() -> None:
    client = Client()
    client.call("GET", "/api/session")

    status, tools = client.call("GET", "/api/tools")
    names = {tool["name"] for tool in tools or []}
    check("run_command 出现在模型可见的工具列表里", "run_command" in names, f"{len(names)} 个工具")

    status, conversation = client.call("POST", "/api/conversations")
    conversation_id = conversation["id"]
    status, task = client.call("POST", f"/api/conversations/{conversation_id}/messages", {"content": PROMPT})
    check("任务已受理", status == 202, f"status={status}")
    task_id = task["id"]

    deadline = time.time() + 180
    state = task
    while time.time() < deadline:
        _, state = client.call("GET", f"/api/tasks/{task_id}")
        if state["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            break
        await asyncio.sleep(1.5)
    check("任务到达终态", state["status"] == "SUCCEEDED", f"status={state['status']} error={state.get('error')}")

    events = client.events(task_id)
    started = [
        event
        for event in events
        if event["event_type"] == "tool_started" and event["payload"].get("tool") == "run_command"
    ]
    check("模型确实调用了 run_command", bool(started), f"{len(started)} 次")

    _, messages = client.call("GET", f"/api/conversations/{conversation_id}/messages")
    replies = [m for m in messages or [] if m["role"] == "assistant"]
    content = replies[-1]["content"] if replies else ""
    check("正文里带回了命令的真实输出", MARKER in content, content[:120].replace("\n", " "))

    print("\n" + "=" * 48)
    if FAILS:
        print(f"结果：{len(FAILS)} 项失败")
        for name in FAILS:
            print("  - " + name)
        sys.exit(1)
    print("结果：全部通过（真实模型 · 真实 shell）")


if __name__ == "__main__":
    asyncio.run(main())
