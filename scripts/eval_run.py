"""Run the fixed task set against the local API and write a metrics report.

Usage (from backend/):

    uv run python ../scripts/eval_run.py                 # all cases
    uv run python ../scripts/eval_run.py bili_page_two   # one case by id

Assertions check behaviour and structure only — site content changes daily, so a case must
never depend on a specific video title. Every confirmation is approved automatically; the
result records how many were needed, which is itself a metric.
"""

import argparse
import http.cookiejar
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

import agent_probe as probe

ROOT = pathlib.Path(__file__).resolve().parents[1]
CASES = pathlib.Path(__file__).with_name("eval_cases.json")


def run_case(opener, case, deadline_scale=1.0):
    result = {
        "id": case["id"],
        "title": case["title"],
        "tools": [],
        "confirmations": 0,
        "turns": [],
        "answer": "",
        "last_url": "",
        "status": "RUNNING",
        "seconds": 0.0,
        "waits": [],
    }
    started = time.time()
    conversation_id = ""
    for turn in case["turns"]:
        if turn.get("fresh") or not conversation_id:
            conversation_id = probe.call(opener, "/api/conversations", "POST")["id"]
        task = probe.call(
            opener,
            f"/api/conversations/{conversation_id}/messages",
            "POST",
            {"content": turn["goal"]},
        )
        turn_result = drain(opener, task["id"], deadline_scale)
        result["tools"].extend(turn_result["tools"])
        result["waits"].extend(turn_result["waits"])
        result["confirmations"] += turn_result["confirmations"]
        result["turns"].append(
            {"goal": turn["goal"], "status": turn_result["status"], "seconds": turn_result["seconds"]}
        )
        # Checks look at the whole run, so keep the latest answer and page.
        result["answer"] = turn_result["answer"] or result["answer"]
        result["status"] = turn_result["status"]
        if turn_result.get("last_url"):
            result["last_url"] = turn_result["last_url"]
        if turn_result["status"] != "SUCCEEDED":
            break
    result["seconds"] = round(time.time() - started, 1)
    return result


def drain(opener, task_id, deadline_scale):
    """Follow one task until it settles, approving every pending confirmation."""
    out = {
        "tools": [],
        "waits": [],
        "confirmations": 0,
        "answer": "",
        "last_url": "",
        "status": "RUNNING",
        "seconds": 0.0,
    }
    started = time.time()
    cursor = 0
    limit = 420 * deadline_scale
    while time.time() - started < limit:
        try:
            stream = opener.open(
                f"{probe.BASE}/api/tasks/{task_id}/events?after={cursor}", timeout=20
            )
        except (urllib.error.URLError, TimeoutError) as exc:
            out["status"] = f"STREAM_ERROR {exc}"
            return out
        settled = False
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
                payload = event.get("payload") or {}
                kind = event.get("event_type")
                if kind == "tool_started":
                    out["tools"].append(
                        {
                            "name": payload.get("tool"),
                            "status": "started",
                            "url": "",
                            "args": payload.get("args"),
                        }
                    )
                elif kind == "tool_finished":
                    data = payload.get("result", {}).get("data")
                    url = str(data.get("url") or "") if isinstance(data, dict) else ""
                    for record in reversed(out["tools"]):
                        if record["name"] == payload.get("tool") and record["status"] == "started":
                            record["status"] = payload.get("result", {}).get("status")
                            record["url"] = url
                            record["error"] = payload.get("result", {}).get("error", "")
                            record["message"] = payload.get("result", {}).get("message", "")
                            break
                    if url:
                        out["last_url"] = url
                elif kind == "waiting_user":
                    out["waits"].append(payload.get("reason"))
                    if payload.get("reason") not in {"approval", "site"}:
                        out["status"] = f"NEEDS_HUMAN {payload.get('reason')}"
                        out["answer"] = str(payload.get("question") or "")
                        out["tools"].append(
                            {
                                "name": f"ask_user:{payload.get('reason')}",
                                "status": "needs_user",
                                "url": "",
                                "args": payload.get("args"),
                            }
                        )
                        out["seconds"] = round(time.time() - started, 1)
                        return out
                    out["confirmations"] += 1
                    try:
                        probe.call(
                            opener,
                            f"/api/tasks/{task_id}/resume",
                            "POST",
                            {"answer": "确认", "approved": True},
                        )
                    except urllib.error.HTTPError as exc:
                        out["status"] = f"RESUME_FAILED {exc.code}"
                        return out
                elif kind in {"task_completed", "task_failed", "task_cancelled"}:
                    out["status"] = {
                        "task_completed": "SUCCEEDED",
                        "task_failed": "FAILED",
                        "task_cancelled": "CANCELLED",
                    }[kind]
                    out["answer"] = str(payload.get("text") or payload.get("error") or "")
                    out["seconds"] = round(time.time() - started, 1)
                    return out
        if not settled:
            continue
    out["status"] = "TIMEOUT"
    out["seconds"] = round(time.time() - started, 1)
    return out


def hosts(result):
    found = set()
    for tool in result["tools"]:
        host = urlparse(tool["url"]).hostname
        if host:
            found.add(host.lower())
    return found


def evaluate(case, result):
    """Return a list of failure reasons. Empty means the case passed."""
    checks = case.get("checks", {})
    failures = []
    names = [tool["name"] for tool in result["tools"]]

    for required in checks.get("require_tools", []):
        if required not in names:
            failures.append(f"没有调用 {required}")
    for group in checks.get("require_any_tools", []):
        if not any(name in names for name in group):
            failures.append(f"{'、'.join(group)} 都没调用")
    for forbidden in checks.get("forbid_tools", []):
        if forbidden in names:
            failures.append(f"调用了不该用的 {forbidden}")
    errors = [tool for tool in result["tools"] if tool["status"] != "ok"]
    if len(errors) > checks.get("max_tool_errors", 10**6):
        failures.append(f"{len(errors)} 次工具失败：{[t['name'] for t in errors][:3]}")
    expected_url = checks.get("final_url_contains")
    if expected_url and expected_url not in result["last_url"]:
        failures.append(f"最终地址不含 {expected_url}（实际 {result['last_url'][:60]}）")
    if len(hosts(result)) < checks.get("min_distinct_hosts", 0):
        failures.append(f"只访问了 {len(hosts(result))} 个站点")
    for keyword in checks.get("answer_contains", []):
        if keyword not in result["answer"]:
            failures.append(f"回答里没有「{keyword}」")
    for keyword in checks.get("answer_excludes", []):
        if keyword in result["answer"]:
            failures.append(f"回答里不该有「{keyword}」")
    if result["seconds"] > checks.get("max_seconds", 10**6):
        failures.append(f"耗时 {result['seconds']}s 超过上限")
    if result["status"].startswith("NEEDS_HUMAN"):
        failures.append(f"需要人工接管（{result['status'].split(' ', 1)[-1]}），无人值守跑不完")
    elif result["status"] != "SUCCEEDED":
        failures.append(f"任务状态 {result['status']}")
    return failures


def report(results, started):
    passed = [r for r in results if not r["failures"]]
    lines = [
        f"# 固定任务集实测报告（{started}）",
        "",
        f"- 用例：{len(results)} 个，通过 **{len(passed)}**，失败 {len(results) - len(passed)}",
        f"- 完成率：{round(100 * len(passed) / max(len(results), 1))}%",
        f"- 平均耗时：{round(sum(r['seconds'] for r in results) / max(len(results), 1), 1)} 秒",
        f"- 平均工具调用：{round(sum(len(r['tools']) for r in results) / max(len(results), 1), 1)} 次",
        f"- 确认次数合计：{sum(r['confirmations'] for r in results)}",
        "",
        "| 用例 | 结果 | 耗时 | 工具 | 确认 | 失败原因 |",
        "|---|---|---|---|---|---|",
    ]
    for item in results:
        lines.append(
            "| {title} | {mark} | {seconds}s | {tools} | {confirmations} | {reason} |".format(
                title=item["title"],
                mark="通过" if not item["failures"] else "失败",
                seconds=item["seconds"],
                tools=len(item["tools"]),
                confirmations=item["confirmations"],
                reason="；".join(item["failures"])[:160] or "—",
            )
        )
    statistics = {}
    for item in results:
        for reason in item["failures"]:
            key = reason.split("（")[0].split("：")[0]
            statistics[key] = statistics.get(key, 0) + 1
    if statistics:
        lines += ["", "## 错误归因", ""]
        lines += [f"- {key}：{count} 次" for key, count in sorted(statistics.items(), key=lambda x: -x[1])]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("ids", nargs="*")
    parser.add_argument("--json", action="store_true", help="只打印 JSON")
    args = parser.parse_args()

    spec = json.loads(CASES.read_text(encoding="utf-8"))
    selected = [c for c in spec["cases"] if not args.ids or c["id"] in args.ids]
    if not selected:
        print("没有匹配的用例")
        return 1

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    probe.call(opener, "/api/session")

    results = []
    for case in selected:
        print(f"[{case['id']}] {case['title']} …", flush=True)
        result = run_case(opener, case)
        result["failures"] = evaluate(case, result)
        results.append(result)
        mark = "通过" if not result["failures"] else "失败"
        print(
            f"    {mark} {result['seconds']}s 工具 {len(result['tools'])} 次 "
            f"确认 {result['confirmations']} 次 {('；'.join(result['failures']))[:120]}",
            flush=True,
        )
        if result["failures"]:
            for tool in result["tools"]:
                detail = tool.get("error") or tool.get("message") or ""
                print(
                    f"      · {tool['name']} [{tool['status']}] {str(tool.get('args'))[:80]}"
                    f"{(' → ' + str(detail)[:110]) if detail else ''}",
                    flush=True,
                )

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    started = time.strftime("%Y-%m-%d %H:%M")
    text = report(results, started)
    out_dir = ROOT / "test-results" / f"eval-{time.strftime('%Y-%m-%d')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(text, encoding="utf-8")
    (out_dir / "report.json").write_text(
        json.dumps({"started": started, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print()
    print(text)
    print(f"报告已写入 {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
