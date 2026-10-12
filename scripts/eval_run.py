"""Run the fixed task set against the local API and write a metrics report.

Usage (from backend/):

    uv run python ../scripts/eval_run.py                 # all cases
    uv run python ../scripts/eval_run.py bili_page_two   # one case by id

Assertions check behaviour and structure only — site content changes daily, so a case must
never depend on a specific video title.

v1 是「事前零确认」：只有模型主动 ask_user 与工具返回 needs_user 会停下。因此这里**不再
自动批准**任何 approval——approval 一旦再次出现就判为权限回归并显式失败，而不是悄悄放行。
「对外动作数」改为读 finalize_completed 的 outward_actions，是审计口径而非人工介入次数。
"""

import argparse
import http.cookiejar
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlparse

import agent_probe as probe

ROOT = pathlib.Path(__file__).resolve().parents[1]
CASES = pathlib.Path(__file__).with_name("eval_cases.json")


def substitute(text, run_id):
    """把用例里的 {run_id} 换成本次运行的唯一串。

    长期记忆是全局的（`memories.scope=local`），写死的内容第二次运行就"已经存在"，
    模型会**正确地**不再重复保存，用例却因此判失败——那是用例不干净，不是行为退化。
    每轮换一个唯一值，才能把"真的存下来了"测出来。
    """
    return str(text).replace("{run_id}", run_id)


def cleanup_case(opener, case):
    """收尾清理：把用例声明为副产物的数据删掉，让评测可重复运行。

    目前只有 `memory_across_conversations` 需要——它每轮用新 run_id 写长期记忆，
    不清就会无限累积（真库曾因此叠出 5 条同义 key 的垃圾记忆，见
    `scripts/cleanup_eval_data.py` 的背景说明）。
    """
    cleanup = case.get("cleanup") or {}
    keywords = [k for k in (cleanup.get("memory_keys") or []) if str(k).strip()]
    if not keywords:
        return
    try:
        memories = probe.call(opener, "/api/memories")
    except Exception as exc:
        print(f"    清理失败（忽略）：{exc}", flush=True)
        return
    removed = []
    for memory in memories:
        key = str(memory.get("key") or "")
        if any(word in key for word in keywords):
            probe.call(opener, f"/api/memories/{memory['id']}", "DELETE")
            removed.append(key)
    if removed:
        print(f"    清理：删除 {len(removed)} 条用例记忆 {removed}", flush=True)



def normalize_origin(value):
    """召回与引用比对用：去 scheme / www. / query，保留 host+path 以便前缀比对。"""
    text = str(value or "").strip().lower()
    text = text.split("://", 1)[-1]
    if text.startswith("www."):
        text = text[4:]
    return text.split("?", 1)[0].rstrip("/")


def origin_matches(got, target):
    """归一化后比对：同 host、path 前缀，或 target 是 got 的域名后缀。

    模型常把 `search.bilibili.com/all` 引用成 `bilibili.com`，严格全等会误判成未引用。
    """
    if not got or not target:
        return False
    if got == target or got.startswith(target + "/"):
        return True
    return f".{target}" in got


def recall_at_5(result, expected):
    """prepare 预取（≤5 片段）命中了多少期望来源；没有期望来源时返回 None。"""
    wanted = [normalize_origin(item) for item in (expected or []) if str(item).strip()]
    if not wanted:
        return None
    injected = [
        normalize_origin(origin)
        for item in result["retrieval"]
        if item.get("trigger") == "prepare"
        for origin in item.get("origins") or []
    ]
    if not injected:
        return 0.0
    hits = sum(1 for target in wanted if any(origin_matches(got, target) for got in injected))
    return hits / len(wanted)


def citation_coverage(result):
    """引用覆盖率 = 命中 /（命中 + 缺失）；从未校验过就返回 None。"""
    matched = sum(item["matched"] for item in result["citations"])
    missing = sum(item["missing"] for item in result["citations"])
    total = matched + missing
    return None if total == 0 else matched / total


def percent(value):
    return "—" if value is None else f"{round(100 * value)}%"


def run_case(opener, case, run_id, deadline_scale=1.0):
    result = {
        "id": case["id"],
        "title": case["title"],
        "tools": [],
        "audit_actions": 0,
        "retrieval": [],
        "citations": [],
        "turns": [],
        "answer": "",
        "last_url": "",
        "status": "RUNNING",
        "seconds": 0.0,
        "waits": [],
        "reflections": [],
        "retries": [],
        "budget_stops": [],
        "notices": [],
        "blocked": {},
        "effective": {},
        "cost": {},
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
            {"content": substitute(turn["goal"], run_id)},
        )
        turn_result = drain(opener, task["id"], deadline_scale)
        result["tools"].extend(turn_result["tools"])
        result["waits"].extend(turn_result["waits"])
        result["audit_actions"] += turn_result["audit_actions"]
        result["retrieval"].extend(turn_result["retrieval"])
        result["citations"].extend(turn_result["citations"])
        result["reflections"].extend(turn_result["reflections"])
        result["retries"].extend(turn_result["retries"])
        result["budget_stops"].extend(turn_result["budget_stops"])
        result["notices"].extend(turn_result["notices"])
        for key, value in turn_result["blocked"].items():
            result["blocked"][key] = result["blocked"].get(key, 0) + value
        for key, value in turn_result["effective"].items():
            result["effective"][key] = result["effective"].get(key, 0) + value
        # 成本按轮累加（多轮用例的总账）
        total = dict(result["cost"])
        for key, value in (turn_result["cost"] or {}).items():
            total[key] = round(float(total.get(key) or 0) + float(value or 0), 2)
        result["cost"] = total
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
    """Follow one task until it settles.

    v1 事前零确认：approval 不该再出现，因此这里不再自动批准——出现即判权限回归。
    只有模型主动 ask_user / 工具 needs_user 会停，停下即 NEEDS_HUMAN（无人值守跑不完）。
    """
    out = {
        "tools": [],
        "waits": [],
        "audit_actions": 0,
        "retrieval": [],
        "citations": [],
        "answer": "",
        "last_url": "",
        "status": "RUNNING",
        "seconds": 0.0,
        "reflections": [],
        "retries": [],
        "budget_stops": [],
        "notices": [],
        "blocked": {},
        "effective": {},
        "cost": {},
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
                    # 工具自带的结构化提示（例如检索次数已达上限）单独留证，
                    # 让"上限真的生效"这件事可以被断言，而不是靠模型转述。
                    tool_name = str(payload.get("tool") or "")
                    blocked = isinstance(data, dict) and bool(data.get("notice"))
                    if blocked:
                        out["notices"].append(str(data["notice"]))
                        out["blocked"][tool_name] = out["blocked"].get(tool_name, 0) + 1
                    else:
                        out["effective"][tool_name] = out["effective"].get(tool_name, 0) + 1
                    for record in reversed(out["tools"]):
                        if record["name"] == payload.get("tool") and record["status"] == "started":
                            record["status"] = payload.get("result", {}).get("status")
                            record["url"] = url
                            record["error"] = payload.get("result", {}).get("error", "")
                            record["message"] = payload.get("result", {}).get("message", "")
                            record["blocked"] = blocked
                            break
                    if url:
                        out["last_url"] = url
                elif kind == "waiting_user":
                    reason = str(payload.get("reason") or "")
                    out["waits"].append(reason)
                    if reason == "approval":
                        # v1 应事前零确认；再出现就是权限回归，必须失败而不是悄悄放行。
                        out["status"] = "REGRESSION approval 重新出现（v1 应零事前确认）"
                        out["seconds"] = round(time.time() - started, 1)
                        return out
                    out["status"] = f"NEEDS_HUMAN {reason}"
                    out["answer"] = str(payload.get("question") or "")
                    out["tools"].append(
                        {
                            "name": f"ask_user:{reason}",
                            "status": "needs_user",
                            "url": "",
                            "args": payload.get("args"),
                        }
                    )
                    out["seconds"] = round(time.time() - started, 1)
                    return out
                elif kind == "retrieval_injected":
                    out["retrieval"].append(
                        {
                            "trigger": payload.get("trigger"),
                            "count": payload.get("count"),
                            "origins": list(payload.get("origins") or []),
                            "scores": list(payload.get("scores") or []),
                        }
                    )
                elif kind == "citation_checked":
                    out["citations"].append(
                        {
                            "checked": bool(payload.get("checked")),
                            "matched": int(payload.get("matched") or 0),
                            "missing": int(payload.get("missing") or 0),
                        }
                    )
                elif kind == "finalize_completed":
                    out["audit_actions"] += int(payload.get("outward_actions") or 0)
                    # 成本聚合的落地证据：这些字段必须真的出现在事件里
                    out["cost"] = {
                        "tokens": int(payload.get("tokens") or 0),
                        "bytes": int(payload.get("bytes") or 0),
                        "actions": int(payload.get("actions") or 0),
                        "refs": int(payload.get("refs") or 0),
                        "elapsed": float(payload.get("elapsed") or 0),
                        "stop_reason": payload.get("stop_reason") or "",
                    }
                elif kind == "reflection":
                    out["reflections"].append(
                        {"step": payload.get("step"), "notes": list(payload.get("notes") or [])}
                    )
                elif kind == "model_retry":
                    out["retries"].append(
                        {"attempt": payload.get("attempt"), "error": payload.get("error")}
                    )
                elif kind == "budget_stopped":
                    out["budget_stops"].append(payload.get("reason"))
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


def evaluate(case, result, run_id):
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
        wanted = substitute(keyword, run_id)
        if wanted not in result["answer"]:
            failures.append(f"回答里没有「{wanted}」")
    for keyword in checks.get("answer_excludes", []):
        unwanted = substitute(keyword, run_id)
        if unwanted in result["answer"]:
            failures.append(f"回答里不该有「{unwanted}」")
    if result["seconds"] > checks.get("max_seconds", 10**6):
        failures.append(f"耗时 {result['seconds']}s 超过上限")

    # ── 护栏断言（2026-09-30 新增：把"实现了"变成可被评测证明的事） ──
    # max_tool_calls 只数**真正执行**的调用；被护栏拦下、只回提示的调用不算，
    # 它们由 require_tool_notice / min_blocked_calls 单独断言。
    for tool_name, limit in (checks.get("max_tool_calls") or {}).items():
        used = int(result["effective"].get(tool_name, 0))
        if used > int(limit):
            failures.append(f"{tool_name} 真正执行了 {used} 次，超过上限 {limit}")
    for tool_name, floor in (checks.get("min_blocked_calls") or {}).items():
        blocked = int(result["blocked"].get(tool_name, 0))
        if blocked < int(floor):
            failures.append(f"{tool_name} 只被拦截 {blocked} 次，护栏没被触发（预期至少 {floor} 次）")
    if checks.get("require_tool_notice"):
        if not result["notices"]:
            failures.append("没有任何工具返回结构化 notice（预期触发上限提示）")
    for event_name, label in (
        ("reflections", "reflection"),
        ("retries", "model_retry"),
        ("budget_stops", "budget_stopped"),
    ):
        if event_name in (checks.get("forbid_events") or []):
            if result[event_name]:
                failures.append(f"不该出现的 {label} 事件出现了 {len(result[event_name])} 次")
    if checks.get("require_events"):
        for event_name in checks["require_events"]:
            if not result.get(event_name):
                failures.append(f"没有出现 {event_name} 事件")
    floor_cover = checks.get("min_citation_coverage")
    coverage = None
    if floor_cover is not None:
        coverage = citation_coverage(result)
        if coverage is None or coverage + 1e-9 < float(floor_cover):
            shown = "—" if coverage is None else f"{coverage:.2f}"
            failures.append(f"引用覆盖率 {shown} 低于 {floor_cover}")

    # 结构断言：只要任务走到了 finalize，成本字段就必须齐全
    # （防止"成本聚合"变成只写在文档里的功能）
    if result["status"] == "SUCCEEDED" and not checks.get("skip_cost_check"):
        cost = result.get("cost") or {}
        missing = [key for key in ("tokens", "bytes", "actions", "elapsed") if key not in cost]
        if missing:
            failures.append(f"finalize 事件缺少成本字段 {missing}")

    expected_origins = checks.get("expect_origins", [])
    floor = checks.get("min_recall_at_5")
    # 只写 expect_origins 时仅出数（计划 §9.3：先出数、不定 KPI）；
    # 需要门禁的用例再显式加 min_recall_at_5。
    if expected_origins and floor is not None:
        recalled = recall_at_5(result, expected_origins)
        if recalled is None or recalled + 1e-9 < float(floor):
            shown = "—" if recalled is None else f"{recalled:.2f}"
            failures.append(f"recall@5 {shown} 低于 {floor}")
    if result["status"].startswith("NEEDS_HUMAN"):
        failures.append(f"需要人工接管（{result['status'].split(' ', 1)[-1]}），无人值守跑不完")
    elif result["status"] != "SUCCEEDED":
        failures.append(f"任务状态 {result['status']}")
    return failures


def report(results, started):
    passed = [r for r in results if not r["failures"]]
    recalls = [r["recall_at_5"] for r in results if r.get("recall_at_5") is not None]
    covers = [r["citation_coverage"] for r in results if r.get("citation_coverage") is not None]
    tokens = [int((r.get("cost") or {}).get("tokens") or 0) for r in results]
    lines = [
        f"# 固定任务集实测报告（{started}）",
        "",
        f"- 用例：{len(results)} 个，通过 **{len(passed)}**，失败 {len(results) - len(passed)}",
        f"- 完成率：{round(100 * len(passed) / max(len(results), 1))}%",
        f"- 平均耗时：{round(sum(r['seconds'] for r in results) / max(len(results), 1), 1)} 秒",
        f"- 平均工具调用：{round(sum(len(r['tools']) for r in results) / max(len(results), 1), 1)} 次",
        f"- 对外动作数合计：{sum(r['audit_actions'] for r in results)}（审计口径，v1 事前零确认）",
        f"- 工具结果 token 估算合计：{sum(tokens)}（tool_results 口径，不含对话历史）",
        f"- 护栏事件：reflection {sum(len(r['reflections']) for r in results)} 次 / "
        f"model_retry {sum(len(r['retries']) for r in results)} 次 / "
        f"budget_stopped {sum(len(r['budget_stops']) for r in results)} 次",
        f"- 平均 recall@5：{percent(sum(recalls) / len(recalls)) if recalls else '—'}"
        f"（{len(recalls)} 个用例有期望来源）",
        f"- 引用覆盖率：{percent(sum(covers) / len(covers)) if covers else '—'}"
        f"（{len(covers)} 个用例校验过引用）",
        "",
        "| 用例 | 结果 | 耗时 | 工具 | 对外动作 | 召回@5 | 引用覆盖 | 护栏(自省/重试/预算) | tokens | 失败原因 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for item in results:
        lines.append(
            "| {title} | {mark} | {seconds}s | {tools} | {actions} | {recall} | {cover}"
            " | {guards} | {tokens} | {reason} |".format(
                title=item["title"],
                mark="通过" if not item["failures"] else "失败",
                seconds=item["seconds"],
                tools=len(item["tools"]),
                actions=item["audit_actions"],
                recall=percent(item.get("recall_at_5")),
                cover=percent(item.get("citation_coverage")),
                guards="{}/{}/{}".format(
                    len(item["reflections"]), len(item["retries"]), len(item["budget_stops"])
                ),
                tokens=int((item.get("cost") or {}).get("tokens") or 0),
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
    opener = probe.local_opener(jar)
    probe.call(opener, "/api/session")

    results = []
    run_id = f"{time.strftime('%m%d%H%M')}-{uuid.uuid4().hex[:4]}"
    print(f"本次运行标识 run_id={run_id}（用例里的 {{run_id}} 会替换成它）", flush=True)
    for case in selected:
        print(f"[{case['id']}] {case['title']} …", flush=True)
        result = run_case(opener, case, run_id)
        result["failures"] = evaluate(case, result, run_id)
        cleanup_case(opener, case)
        result["recall_at_5"] = recall_at_5(
            result, case.get("checks", {}).get("expect_origins", [])
        )
        result["citation_coverage"] = citation_coverage(result)
        results.append(result)
        mark = "通过" if not result["failures"] else "失败"
        print(
            f"    {mark} {result['seconds']}s 工具 {len(result['tools'])} 次 "
            f"对外动作 {result['audit_actions']} 次 召回@5 {percent(result['recall_at_5'])} "
            f"引用覆盖 {percent(result['citation_coverage'])} "
            f"{('；'.join(result['failures']))[:100]}",
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
