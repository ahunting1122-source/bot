"""Drive one real task through the local API and print the tool trace.

Usage (from backend/, so that the venv is available is not required — stdlib only):

    uv run python ../scripts/agent_probe.py "在 B 站搜索机械键盘并翻到第 2 页"
    uv run python ../scripts/agent_probe.py "..." --no-approve

With --approve (default) every pending confirmation is approved automatically, so a
multi-step run does not need a human clicking between steps. The trace shows which tool
ran, with which arguments, and what came back.
"""

import argparse
import http.cookiejar
import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:6185"


def local_opener(jar=None):
    """Build an opener for the local API that never goes through a system proxy.

    urllib 会读 HTTP_PROXY/系统代理设置；在有代理的机器上，127.0.0.1:6185 会被
    代理接管并返回 502 Bad Gateway，看起来像后端挂了。显式装一个空 ProxyHandler
    把这个变量消掉：本地回环地址不需要也不该走代理。
    """
    handlers = [urllib.request.ProxyHandler({})]
    if jar is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(jar))
    return urllib.request.build_opener(*handlers)


def call(opener, path, method="GET", payload=None, timeout=30):
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with opener.open(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def brief(value, limit=150):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text[:limit]


def show(event):
    kind = event.get("event_type")
    payload = event.get("payload") or {}
    if kind == "tool_started":
        print(f"  → {payload.get('tool')}")
    elif kind == "tool_finished":
        result = payload.get("result") or {}
        data = result.get("data") or {}
        extra = ""
        if data.get("url"):
            extra = f" url={str(data['url'])[:70]}"
        elements = data.get("elements") or []
        if elements and payload.get("tool") != "vault_login":
            named = [f"{e.get('ref')}:{str(e.get('name'))[:18]}" for e in elements[:6]]
            extra += f" elements={named}"
        if result.get("error"):
            extra += f" error={brief(result['error'], 90)}"
        print(f"    {result.get('status')}{extra}")
    elif kind == "waiting_user":
        print(f"  ! waiting ({payload.get('reason')}) tool={payload.get('tool')}")
    elif kind == "activity_summary":
        print(f"  · {brief(payload.get('text'), 110)}")
    return kind


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("goal")
    parser.add_argument("--no-approve", dest="approve", action="store_false")
    parser.add_argument("--deadline", type=int, default=300)
    args = parser.parse_args()

    jar = http.cookiejar.CookieJar()
    opener = local_opener(jar)
    call(opener, "/api/session")

    conversation = call(opener, "/api/conversations", "POST")
    task = call(
        opener,
        f"/api/conversations/{conversation['id']}/messages",
        "POST",
        {"content": args.goal},
    )
    task_id = task["id"]
    print(f"goal: {args.goal}\ntask: {task_id}\n")

    cursor = 0
    deadline = time.time() + args.deadline
    approvals = 0
    while time.time() < deadline:
        url = f"{BASE}/api/tasks/{task_id}/events?after={cursor}"
        settled = False
        try:
            stream = opener.open(url, timeout=20)
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"stream error: {exc}")
            break
        with stream:
            for raw in stream:
                line = raw.decode("utf-8", "replace").strip()
                if line == "event: settled":
                    settled = True
                    break
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                cursor = max(cursor, int(event.get("id", 0)))
                kind = show(event)
                if kind == "waiting_user":
                    payload = event.get("payload") or {}
                    if not args.approve or payload.get("reason") not in {"approval", "site"}:
                        print("\nstopped: needs a human decision")
                        return
                    approvals += 1
                    print(f"    (auto-approving #{approvals})")
                    try:
                        call(
                            opener,
                            f"/api/tasks/{task_id}/resume",
                            "POST",
                            {"answer": "确认", "approved": True},
                        )
                    except urllib.error.HTTPError as exc:
                        print(f"    resume failed: {exc.code} {exc.read()[:120]!r}")
                elif kind in {"task_completed", "task_failed", "task_cancelled"}:
                    text = (event.get("payload") or {})
                    print(f"\n===== {kind} =====")
                    print(brief(text.get("text") or text.get("error") or "", 1500))
                    print(f"\napprovals: {approvals}")
                    return
        if not settled:
            continue
        final = call(opener, f"/api/tasks/{task_id}", timeout=20)
        print(f"\n===== {final.get('status')} =====")
        print(f"approvals: {approvals}")
        return
    print("\nstopped: deadline reached")


if __name__ == "__main__":
    main()
