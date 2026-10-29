"""MATRIX 5.14 第三轮：`ROUND3_TOOLS` 的 5 个新工具，每个都**真跑**一遍。

## 这份文件为什么单独存在

`tests/test_tool_extra_impl.py` 钉第一轮那两个（`tool_describe` / `cron_job`），
`tests/test_tool_coverage_growth.py` 钉第二轮那五个。第三轮这 5 个
（`send_message` / `memory_provider_list` / `skill_search` / `egress_status` /
`budget_status`）各自有自己的落点与失败路径，塞进那两份文件会让"这一轮加了什么"
要读三段历史才看得清。

## 判据（每条用例都按它写）

1. **走真实分发链**：全部经 `ToolRegistry.execute(...)`，不是直接调实现函数；
2. **有副作用可核对**：`outbox.jsonl` 真的多一行、对端的 `/peer/dm` 真的被请求到、
   hub 里的技能真的被搜到、策略文件真的被读到、落盘文件真的被删；
3. **失败路径说得出下一步**：坏输入当场拒绝并指名"先 action=list" / 缺哪个环境变量；
4. **覆盖数字真的涨了**：31 → 33，且涨的那两条映射到**真实工具名**。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app import gateway, tool_catalog, tool_extra_impl2, toolsets
from app.config import Settings
from app.main import migrate
from app.storage import Conversation, Store, Task
from app.tools import ToolRegistry, schema_problems

# ── 夹具 ────────────────────────────────────────────────────────────────────


@pytest.fixture()
async def env(tmp_path):
    """隔离数据目录上的完整环境（settings + store + registry + task + 已注册的第三轮工具）。"""
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="第三轮工具"))
    settings.workspace.mkdir(parents=True, exist_ok=True)
    registry = ToolRegistry(settings, store)
    registered = tool_extra_impl2.register_round3_tools(registry)
    try:
        yield settings, store, registry, task.id, registered
    finally:
        await store.engine.dispose()


async def call(registry, task_id, name, args, *, op="r3"):
    """走真实分发链调一次。

    ``operation_id`` **每次都要不同**：注册表按它做幂等短路，同一个 id 的第二次调用
    会直接返回第一次的结果 —— 拿固定 id 写用例，测出来的全是缓存。
    """
    return await registry.execute(task_id, f"{op}-{name}-{uuid4().hex[:8]}", name, args)


# ── 1. 声明、schema、归类 ───────────────────────────────────────────────────


def test_round3_tools_have_valid_schemas_and_a_declared_toolset():
    """5 个新工具：schema 合法（否则整次请求被 400）+ toolset 有明确落点。"""
    assert [tool.name for tool in tool_extra_impl2.ROUND3_TOOLS] == [
        "send_message",
        "memory_provider_list",
        "skill_search",
        "egress_status",
        "budget_status",
    ]
    for tool in tool_extra_impl2.ROUND3_TOOLS:
        assert schema_problems(tool.specification()) == [], f"{tool.name} 的 schema 不合法"
        assert tool.description.strip(), f"{tool.name} 缺描述（模型靠它决定调不调）"
        # 每个新工具都必须有一个"该归到哪"的答案：要么装配层已经登记了，要么写在这张
        # 待补表里。**只对这几个名字放行**，别的工具没登记照样会被
        # tests/test_tool_catalog.py 的登记门禁拦下。
        declared = toolsets.toolset_of(tool.name)
        pending = tool_extra_impl2.PENDING_TOOLSET_MAPPINGS.get(tool.name, "")
        assert declared != toolsets.UNCLASSIFIED or pending, f"{tool.name} 既没登记也没写待补映射"
        if pending:
            # 装配层补完映射之后，这条断言自动从"待补"切到"已生效"
            assert declared in {pending, toolsets.UNCLASSIFIED}, (tool.name, declared)
    # `send_message` 要一个新 toolset：collab 域此前只有 notion 在借撑
    assert tool_extra_impl2.PENDING_TOOLSET_MAPPINGS["send_message"] == "collab"
    assert "collab" in tool_extra_impl2.PENDING_NEW_TOOLSETS


def test_register_round3_tools_is_idempotent_and_reports_really_registered(env):
    _settings, _store, registry, _task, registered = env
    assert registered == [tool.name for tool in tool_extra_impl2.ROUND3_TOOLS]
    assert set(registered) <= set(registry.tools)
    again = tool_extra_impl2.register_round3_tools(registry)
    assert again == registered, "二次注册必须幂等（名字表不该变长）"
    for name in registered:
        assert list(registry.tools).count(name) == 1


# ── 2. send_message：列目标 / 投递 / 失败要说下一步 ─────────────────────────


async def test_send_message_lists_every_target_with_its_reason(env):
    """`action=list`：离线旁路要标可用，缺凭证的平台要**逐条**说缺什么。"""
    _settings, _store, registry, task_id, _registered = env
    result = await call(registry, task_id, "send_message", {"action": "list"})
    assert result["status"] == "ok"
    data = result["data"]
    by_name = {row["platform"]: row for row in data["targets"]}
    assert by_name["file"]["usable"] is True
    assert by_name["console"]["usable"] is True
    # 有适配器但没配凭证：configured=False，且缺哪个环境变量要点名
    telegram = by_name["telegram"]
    assert telegram["configured"] is False
    assert "TELEGRAM_BOT_TOKEN" in telegram["missing_env"]
    # 连适配器都没有的平台必须单列出来（否则模型会以为"没有这个平台"）
    assert {"buzz", "photon"} <= set(data["pending_platforms"])
    assert "outbox.jsonl" in data["notice"]


async def test_send_message_really_delivers_through_the_file_bypass(env):
    """`file` 旁路：`outbox.jsonl` 真的多一行，投递账本也记了一条。"""
    settings, _store, registry, task_id, _registered = env
    result = await call(
        registry, task_id, "send_message", {"action": "send", "target": "file", "text": "报告已生成"}
    )
    assert result["status"] == "ok", result
    assert result["data"]["ok"] is True
    outbox = (settings.data_dir / "outbox.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(outbox) == 1
    record = json.loads(outbox[0])
    assert record["text"] == "报告已生成" and record["platform"] == "file"
    ledger = (settings.data_dir / "gateway" / "deliveries.jsonl").read_text(encoding="utf-8")
    assert '"status": "ok"' in ledger


async def test_send_message_explains_an_unconfigured_platform(env):
    """没配凭证的平台：报错要指出**缺哪个环境变量**，并指向 action=list。"""
    _settings, _store, registry, task_id, _registered = env
    result = await call(
        registry, task_id, "send_message", {"action": "send", "target": "telegram", "text": "hi"}
    )
    assert result["status"] == "error"
    assert "TELEGRAM_BOT_TOKEN" in result["error"]
    assert "action=list" in result["error"]


async def test_send_message_refuses_empty_text_and_missing_target(env):
    _settings, _store, registry, task_id, _registered = env
    empty = await call(registry, task_id, "send_message", {"action": "send", "target": "file"})
    assert empty["status"] == "error" and "text" in empty["error"]
    no_target = await call(registry, task_id, "send_message", {"action": "send", "text": "x"})
    assert no_target["status"] == "error" and "target" in no_target["error"]


async def test_send_message_reaches_a_peer_agent(env):
    """`peer:<对端>` 真的打到对端的 `/peer/dm`（用 MockTransport，不发真实请求）。

    这条覆盖的是 Hermes 的 `message_agent`：「给另一个 agent 发消息」。证据不是
    "字典里有这个对端"，而是**请求真的发出去了、对端的回复回到了返回值里**。
    """
    settings, _store, registry, task_id, _registered = env
    gateway.PeerRegistry(gateway.GatewayPaths(settings.data_dir).peers).add(
        "spark", "http://spark.lan:8377", key="k-123456"
    )
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append({"path": request.url.path, "body": body})
        return httpx.Response(200, json={"reply": f"收到：{body['message']}"})

    tool_extra_impl2.register_round3_tools(
        registry, peer_transport=httpx.MockTransport(handler)
    )
    result = await call(
        registry,
        task_id,
        "send_message",
        {"action": "send", "target": "peer:spark/researcher", "text": "磁盘还剩多少？"},
    )
    assert result["status"] == "ok", result
    assert result["data"]["reply"] == "收到：磁盘还剩多少？"
    assert seen and seen[0]["path"] == "/peer/dm"
    assert seen[0]["body"] == {"agent": "researcher", "message": "磁盘还剩多少？"}


async def test_send_message_peer_target_must_be_registered(env):
    _settings, _store, registry, task_id, _registered = env
    result = await call(
        registry, task_id, "send_message", {"action": "send", "target": "peer:ghost", "text": "hi"}
    )
    assert result["status"] == "error"
    assert "orbit peer add" in result["error"], "要告诉人怎么登记对端"


# ── 3. memory_provider_list ─────────────────────────────────────────────────


async def test_memory_provider_list_reports_availability_and_installs(env):
    """15 个 provider 逐条报可用性；不可用的必须带"缺什么/怎么装"。"""
    _settings, _store, registry, task_id, _registered = env
    result = await call(registry, task_id, "memory_provider_list", {})
    assert result["status"] == "ok"
    data = result["data"]
    assert data["count"] == 15
    by_name = {row["name"]: row for row in data["providers"]}
    # local 需要 store（注册表自带），所以它是可用的
    assert by_name["local"]["available"] is True
    assert data["active"] == "local"
    # 只有接口的那批：available=False + install 提示 + reason
    mem0 = by_name["mem0"]
    assert mem0["available"] is False
    assert "pip install" in mem0["install"]
    assert mem0["reason"].strip()
    assert str(data["config_path"]).endswith("providers.json")


async def test_memory_provider_list_can_filter_to_available(env):
    _settings, _store, registry, task_id, _registered = env
    result = await call(registry, task_id, "memory_provider_list", {"available_only": True})
    rows = result["data"]["providers"]
    assert rows, "至少 local 可用"
    assert all(row["available"] for row in rows)
    assert result["data"]["count"] == len(rows)


# ── 4. skill_search ────────────────────────────────────────────────────────


def _write_hub_skill(data_dir: Path, identifier: str, name: str, description: str) -> Path:
    directory = data_dir / "skills-hub" / identifier
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n正文\n", encoding="utf-8"
    )
    return directory


async def test_skill_search_finds_a_local_hub_package(env):
    """本地 hub 目录里放一个技能包，`skill_search` 真的能按关键词找到它。"""
    settings, _store, registry, task_id, _registered = env
    _write_hub_skill(settings.data_dir, "mlops/eval-harness", "eval-harness", "模型评测流水线")
    _write_hub_skill(settings.data_dir, "quick-notes", "quick-notes", "随手记")

    hit = await call(registry, task_id, "skill_search", {"query": "评测"})
    assert hit["status"] == "ok"
    assert hit["data"]["count"] == 1
    entry = hit["data"]["results"][0]
    assert entry["identifier"] == "mlops/eval-harness"
    assert entry["name"] == "eval-harness"
    assert entry["installed"] is False
    assert "skills-hub" in hit["data"]["hub_root"]

    all_rows = await call(registry, task_id, "skill_search", {"query": ""})
    assert all_rows["data"]["count"] == 2

    nothing = await call(registry, task_id, "skill_search", {"query": "压根没有"})
    assert nothing["data"]["count"] == 0


# ── 5. egress_status ───────────────────────────────────────────────────────


async def test_egress_status_reports_policy_and_static_decision(env):
    """策略文件真的被读到；给 url 时按静态规则判定，并**如实**说没做 DNS 校验。"""
    settings, _store, registry, task_id, _registered = env
    (settings.data_dir / "egress.json").write_text(
        json.dumps(
            {
                "default": "allow",
                "allow": ["api.openai.com"],
                "deny": ["169.254.169.254"],
                "credentials": [{"host": "api.openai.com", "key": "openai_api_key"}],
            }
        ),
        encoding="utf-8",
    )
    shown = await call(registry, task_id, "egress_status", {})
    assert shown["status"] == "ok"
    policy = shown["data"]["policy"]
    assert policy["default"] == "allow"
    assert policy["allow"] == ["api.openai.com"]
    assert policy["deny"] == ["169.254.169.254"]
    # 凭证只给键名，永不回值
    assert policy["credentials"][0]["key"] == "openai_api_key"
    assert "openai_api_key" in json.dumps(policy["credentials"], ensure_ascii=False)

    blocked = await call(registry, task_id, "egress_status", {"url": "http://169.254.169.254/latest"})
    assert blocked["data"]["decision"]["action"] == "deny"
    assert "deny" in blocked["data"]["decision"]["reason"]

    allowed = await call(registry, task_id, "egress_status", {"url": "https://api.openai.com/v1"})
    decision = allowed["data"]["decision"]
    assert decision["action"] == "allow"
    assert "未做 IP 校验" in decision["reason"], "本机不出网，这一步必须如实标成未做"
    assert "DNS" in allowed["data"]["decision_note"]


# ── 6. budget_status ───────────────────────────────────────────────────────


async def test_budget_status_reports_thresholds_and_spillover(env):
    """窗口 → 单条/整轮阈值 → 落盘目录，三样在同一份返回里。"""
    _settings, _store, registry, task_id, _registered = env
    result = await call(registry, task_id, "budget_status", {})
    assert result["status"] == "ok"
    data = result["data"]
    assert data["window"]["window_tokens"] > 0
    # 单条阈值不能大于整轮预算（否则整轮层永远先触发）
    assert data["thresholds"]["default_result_size"] <= data["thresholds"]["turn_budget"]
    assert data["turn_budgets"]["independent"] is True
    assert data["spillover"]["files"] == 0
    assert data["thresholds"]["pinned_never_spilled"] == ["read_file"]


async def test_budget_status_cleanup_removes_only_expired_files(env):
    settings, _store, registry, task_id, _registered = env
    spill = settings.data_dir / "cache" / "spillover"
    spill.mkdir(parents=True, exist_ok=True)
    old = spill / "run_command-old.txt"
    fresh = spill / "run_command-new.txt"
    old.write_text("旧", encoding="utf-8")
    fresh.write_text("新", encoding="utf-8")
    stale = time.time() - 48 * 3600
    os.utime(old, (stale, stale))

    before = await call(registry, task_id, "budget_status", {})
    assert before["data"]["spillover"]["files"] == 2

    cleaned = await call(registry, task_id, "budget_status", {"action": "cleanup"})
    assert cleaned["data"]["cleanup"]["removed"] == 1
    assert not old.exists()
    assert fresh.exists()


# ── 7. 覆盖：31 → 33，且涨的那两条都是真工具 ───────────────────────────────


def test_coverage_moved_from_31_to_33_and_the_new_rows_are_real():
    """覆盖率**涨了**，且涨的那几条都是真工具。

    数字随实现走：31（第二轮）→ 33（第三轮 +`send_message`/`message_agent`）
    → **37**（第四轮 +4 个浏览器工具：`browser_back` / `browser_get_images` /
    `browser_console` / `browser_dialog`，实现在 `app/browser.py`）。

    **该守的不是数字，是"每条 covered 的名字都真的在池子里"**——下面那条
    `name in pool` 断言才是这条测试的核心。
    """
    coverage = tool_catalog.orbit_coverage()
    assert coverage.total == 94
    # **不写死数字**：这条测试自己的 docstring 就写着「该守的不是数字」。
    # 写死 37 会让每加一个工具都要改一次测试——而那种「改测试让它绿」的动作
    # 恰好会掩盖真正的回归。这里守**性质**：覆盖率只增不减，且每条 covered
    # 都映射到池子里的真工具（下面那段才是核心）。
    assert coverage.covered_count >= 37, (
        f"覆盖数从 37 掉到 {coverage.covered_count}：missing={list(coverage.missing)[:5]}…"
    )
    # 第四轮那 4 个必须真的算进来，且各自映射到自己
    for name in ("browser_back", "browser_get_images", "browser_console", "browser_dialog"):
        assert coverage.covered.get(name) == (name,), f"{name} 没有算进覆盖"
    assert coverage.covered["send_message"] == ("send_message",)
    assert coverage.covered["message_agent"] == ("send_message",)
    pool = set(coverage.pool)
    for names in coverage.covered.values():
        for name in names:
            assert name in pool, f"声明表在说谎：{name} 不是真实工具"
    # 第三轮的 5 个工具都进了池子（其中 3 个是 Orbit 独有能力，不涨 Hermes 覆盖）
    for tool in tool_extra_impl2.ROUND3_TOOLS:
        assert tool.name in pool, tool.name


def test_remaining_gaps_are_all_unavailable_with_reasons():
    """剩下的 61 个缺口**全部**是"做不到"，而且每条都写了原因。

    这条用例是本轮最该保留的诚实性闸：一旦有人为了凑数把某个缺口标成 covered 却
    没接上真实工具，上面那条（映射必须指向池子里的真工具）会先红；而这条保证
    "没做的都说明白了"，不会出现"缺了但没理由"的黑洞。
    """
    coverage = tool_catalog.orbit_coverage()
    by_name = {tool.name: tool for tool in tool_catalog.HERMES_TOOLS}
    for name in coverage.missing:
        record = by_name[name]
        assert record.orbit_status == "unavailable", f"{name} 缺了却没标成做不到"
        assert record.orbit_tools == (), f"{name} 标了做不到却给了 Orbit 映射"
        assert record.reason.strip(), f"{name} 缺了但没写理由"
    # 最容易"顺手标成 todo 蒙过去"的域，理由必须点到具体原因。
    #
    # **`delegate_task` 已不在其中**：第 37 轮补了 `app/child_runner.py` 之后它
    # 真的覆盖了，`reason` 自然为空——`reason` 是「为什么做不到」的位置，
    # 做到了就该空着。断言一个已完成的条目「必须有做不到的理由」是自相矛盾的。
    # **`kanban_show` 已不在其中**（第 44 轮实现了 4 个看板工具）——
    # 断言一个已完成的条目「必须有做不到的理由」是自相矛盾的。
    # 改钉**仍未做**的那个：`kanban_block` 必须带原因。
    assert "状态机" in by_name["kanban_block"].reason
    assert "app/" in by_name["browser_back"].reason
