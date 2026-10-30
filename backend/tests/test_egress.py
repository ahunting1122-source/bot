"""`app/egress.py` 的契约测试（MATRIX 4.8 / 14.x）。

三组：

1. **三态判定**：allow / deny / ask 各自的触发条件，规则匹配的三种写法
   （精确 / 子域通配 / `*`），以及 deny 优先于 allow。
2. **注入只在内存里**：`inject_headers` 返回新 dict、不改入参、**不落盘**、
   **不回显**；取不到密钥就不注入（绝不塞占位假值）。
3. **如实说明**：`describe()` 必须说清"没有代理进程"，而且说清缺什么。
   这一条是本次交付的核心承诺——宁可写"做不到"，也不要让人以为有 iron-proxy。

另有一条**接线**用例：`run_command` 里 `curl` 的目标 host 真的会过这道闸。
"""

from __future__ import annotations

import json

import pytest

import app.terminal as terminal_module
from app import egress, terminal
from app.config import Settings
from app.main import migrate
from app.storage import Conversation, Store, Task
from app.tools import ToolRegistry

SECRET_VALUE = "sk-orbit-super-secret-value"


class _FakeResolver:
    """假密钥解析器：只回一个已知值，用来断言"注入的是它、落盘的不是它"。"""

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self.mapping = mapping or {}

    def resolve(self, key: str) -> str | None:
        return self.mapping.get(key)


@pytest.fixture()
def data_dir(tmp_path):
    path = tmp_path / "data"
    path.mkdir()
    return path


def _policy(data_dir, **overrides) -> egress.EgressPolicy:
    policy = egress.EgressPolicy.load(data_dir)
    for key, value in overrides.items():
        setattr(policy, key, value)
    return policy


# ── 1. 三态判定 ────────────────────────────────────────────────────────────


def test_no_policy_means_allow_and_that_is_deliberate(data_dir):
    """没有策略文件时一律放行——引入这一层不能改变既有行为。"""
    decision = egress.EgressPolicy.load(data_dir).check("https://api.example.com/v1")
    assert decision.action == egress.ALLOW
    assert decision.host == "api.example.com"
    assert "default=allow" in decision.reason


def test_allow_rule_matches_exactly(data_dir):
    policy = _policy(data_dir, allow=["api.example.com"], default=egress.DENY)
    decision = policy.check("https://api.example.com/v1")
    assert decision.action == egress.ALLOW
    assert decision.rule == "api.example.com"
    # 精确规则**不**连带子域：写清楚一条就是一条，别让一条规则悄悄放行一片。
    assert policy.check("https://sub.api.example.com/v1").action == egress.DENY


def test_wildcard_rule_covers_subdomains_but_not_the_bare_domain(data_dir):
    policy = _policy(data_dir, allow=["*.example.com"], default=egress.DENY)
    assert policy.check("https://sub.example.com/x").action == egress.ALLOW
    assert policy.check("https://a.b.example.com/x").action == egress.ALLOW
    # `*.example.com` 不含 example.com 本身（与 shell glob 一致）
    assert policy.check("https://example.com/x").action == egress.DENY


def test_deny_wins_over_allow(data_dir):
    """deny 是安全决策、allow 是便利决策，冲突时选保守的那个。"""
    policy = _policy(
        data_dir, allow=["*.example.com"], deny=["api.example.com"], default=egress.ALLOW
    )
    decision = policy.check("https://api.example.com/x")
    assert decision.action == egress.DENY
    assert decision.rule == "api.example.com"
    assert "deny" in decision.reason


def test_default_deny_and_default_ask(data_dir):
    denied = _policy(data_dir, default=egress.DENY).check("https://any.example/x")
    assert denied.action == egress.DENY and "default=deny" in denied.reason
    asked = _policy(data_dir, default=egress.ASK).check("https://any.example/x")
    assert asked.action == egress.ASK and "default=ask" in asked.reason


def test_unparseable_target_is_ask_not_allow(data_dir):
    """解析不出主机名时不能放行：那等于把"我看不清"当成"没问题"。"""
    decision = egress.EgressPolicy.load(data_dir).check("")
    assert decision.action == egress.ASK
    assert "解析不出主机名" in decision.reason


def test_round_trip_keeps_rules_and_stores_no_secret(data_dir):
    policy = _policy(
        data_dir,
        allow=["api.example.com"],
        deny=["*.evil.example"],
        default=egress.DENY,
        credentials=[
            egress.CredentialRule(
                host="api.example.com", header="Authorization", scheme="Bearer", key="EGRESS_KEY"
            )
        ],
    )
    path = policy.save()
    raw = path.read_text(encoding="utf-8")
    reloaded = egress.EgressPolicy.load(data_dir)
    assert reloaded.allow == ["api.example.com"]
    assert reloaded.deny == ["*.evil.example"]
    assert reloaded.default == egress.DENY
    assert reloaded.credentials[0].key == "EGRESS_KEY"
    # 文件里只有**键名**，没有任何密钥值——这份文件可以进备份、进版本库。
    assert "EGRESS_KEY" in raw and SECRET_VALUE not in raw
    assert json.loads(raw)["_note"]


# ── 2. 注入只在内存里 ──────────────────────────────────────────────────────


def _credential_policy(data_dir) -> egress.EgressPolicy:
    policy = egress.EgressPolicy.load(data_dir)
    policy.credentials = [
        egress.CredentialRule(
            host="api.example.com", header="Authorization", scheme="Bearer", key="EGRESS_KEY"
        )
    ]
    return policy


def test_inject_headers_returns_a_copy_and_never_touches_the_input(data_dir):
    policy = _credential_policy(data_dir)
    original = {"Accept": "application/json"}
    merged = policy.inject_headers(
        "https://api.example.com/v1", original, resolver=_FakeResolver({"EGRESS_KEY": SECRET_VALUE})
    )
    assert original == {"Accept": "application/json"}, "入参被就地改了"
    assert merged["Accept"] == "application/json"
    assert merged["Authorization"] == f"Bearer {SECRET_VALUE}"
    assert "Authorization" not in original


def test_inject_headers_writes_nothing_to_disk(data_dir):
    """注入是**内存**动作：策略文件里不能出现密钥值。"""
    policy = _credential_policy(data_dir)
    path = policy.save()
    policy.inject_headers(
        "https://api.example.com/v1", {}, resolver=_FakeResolver({"EGRESS_KEY": SECRET_VALUE})
    )
    assert SECRET_VALUE not in path.read_text(encoding="utf-8")


def test_inject_headers_skips_the_host_when_no_rule_matches(data_dir):
    policy = _credential_policy(data_dir)
    merged = policy.inject_headers(
        "https://other.example/v1", {}, resolver=_FakeResolver({"EGRESS_KEY": SECRET_VALUE})
    )
    assert merged == {}


def test_inject_headers_never_injects_a_placeholder_when_the_secret_is_missing(data_dir):
    """取不到密钥就不注入。塞一个假值会让服务端 401，而排查方向会跑到"令牌过期"。"""
    policy = _credential_policy(data_dir)
    merged = policy.inject_headers(
        "https://api.example.com/v1", {}, resolver=_FakeResolver({})
    )
    assert merged == {}
    plan = policy.injection_plan("https://api.example.com/v1", resolver=_FakeResolver({}))
    assert plan == [{"host": "api.example.com", "header": "Authorization", "key": "EGRESS_KEY", "resolved": False}]


def test_inject_headers_does_not_override_the_callers_header(data_dir):
    policy = _credential_policy(data_dir)
    merged = policy.inject_headers(
        "https://api.example.com/v1",
        {"Authorization": "Bearer manual"},
        resolver=_FakeResolver({"EGRESS_KEY": SECRET_VALUE}),
    )
    assert merged["Authorization"] == "Bearer manual"


def test_injection_plan_never_echoes_the_value(data_dir):
    policy = _credential_policy(data_dir)
    plan = policy.injection_plan(
        "https://api.example.com/v1", resolver=_FakeResolver({"EGRESS_KEY": SECRET_VALUE})
    )
    assert plan[0]["resolved"] is True
    assert SECRET_VALUE not in json.dumps(plan, ensure_ascii=False)


# ── 3. 如实说明 ────────────────────────────────────────────────────────────


def test_describe_admits_there_is_no_proxy_process(data_dir):
    """本次交付的核心承诺：**不许假装有 iron-proxy**。"""
    text = egress.EgressPolicy.load(data_dir).describe()
    assert "没有代理进程" in text
    assert "iron-proxy" in text
    # 缺的东西要逐条列出来，否则读者不知道该补什么
    for missing in ("CA", "令牌", "mappings.json", "proxy.yaml"):
        assert missing in text
    # 以及它挡不住什么
    assert "拿得到真密钥" in text
    assert "挡不住绕过" in text


# ── 4. 命令文本里的目标 host ───────────────────────────────────────────────


def test_command_hosts_only_looks_at_fetchers():
    assert egress.command_hosts("curl -s https://api.example.com/v1") == ["api.example.com"]
    assert egress.command_hosts("wget -q https://a.example/x https://b.example/y") == [
        "a.example",
        "b.example",
    ]
    # 不是取网器就不猜：`python build.py` 里恰好有 URL 不该被当成出站意图。
    assert egress.command_hosts("python build.py --url https://x.example/a") == []
    assert egress.command_hosts("") == []


def test_command_hosts_deduplicates_and_strips_punctuation():
    hosts = egress.command_hosts("curl 'https://a.example/x', https://a.example/y")
    assert hosts == ["a.example"]


# ── 5. 接线：run_command 里 curl 的目标 host 会过闸 ────────────────────────


class _StubBackend:
    name = "local"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def available(self) -> bool:
        return True

    def probe(self) -> tuple[bool, str]:
        return True, ""

    def describe(self) -> str:
        return "stub"

    def run(self, command, *, cwd, timeout, env=None):
        self.calls.append(command)
        return terminal.TerminalResult(
            backend=self.name,
            command=command,
            cwd=str(cwd),
            exit_code=0,
            timed_out=False,
            stdout="stub-ok",
            stderr="",
            truncated=False,
            seconds=0.01,
        )


@pytest.fixture()
async def env(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="出站检查"))
    registry = ToolRegistry(settings, store)
    stub = _StubBackend()
    monkeypatch.setattr(terminal_module, "resolve_backend", lambda name=None, settings=None: stub)
    try:
        yield settings, registry, stub, task.id
    finally:
        await store.engine.dispose()


async def test_run_command_refuses_a_denied_host(env):
    settings, registry, stub, task_id = env
    policy = egress.EgressPolicy.load(settings.data_dir)
    policy.deny = ["evil.example"]
    policy.save()
    result = await registry.execute(
        task_id, "e1", "run_command", {"command": "curl -s https://evil.example/collect"}
    )
    assert result["status"] == "refused", result
    assert "evil.example" in result["error"]
    assert "orbit egress allow" in result["error"]  # 拒绝要给出下一步怎么做
    assert stub.calls == [], "被拦下的命令不能被执行"


async def test_run_command_still_runs_when_no_policy_is_configured(env):
    """没有策略文件 = 全部放行，`curl` 照旧执行（回归闸）。"""
    _settings, registry, stub, task_id = env
    command = "curl -s https://api.example.com/v1"
    result = await registry.execute(task_id, "e2", "run_command", {"command": command})
    assert result["status"] == "ok", result
    assert stub.calls == [command]


# ── 6. 命令面：`egress {allow,deny,policy,check}` 与"没有代理进程"的那几条 ──
#
# 接线由另一处统一做（`commands/__init__.py`），这里显式导入本波次新增的命令模块。

import app.cli.commands.tools_extra2  # noqa: E402,F401 —— 导入即挂上 egress 的新子命令
from app.cli.main import main  # noqa: E402


def test_cli_egress_allow_writes_the_rule(tmp_path, capsys):
    data_dir = str(tmp_path / "data")
    assert main(["--data-dir", data_dir, "egress", "allow", "api.example.com"]) == 0
    capsys.readouterr()
    saved = egress.EgressPolicy.load(tmp_path / "data")
    assert saved.allow == ["api.example.com"]
    # 落盘的是策略，不是密钥
    assert saved.credentials == []


def test_cli_egress_deny_then_check_reports_deny(tmp_path, capsys):
    data_dir = str(tmp_path / "data")
    assert main(["--data-dir", data_dir, "egress", "deny", "*.evil.example"]) == 0
    capsys.readouterr()
    assert main(["--data-dir", data_dir, "--json", "egress", "check", "https://a.evil.example/x"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision"]["action"] == egress.DENY
    assert payload["decision"]["rule"] == "*.evil.example"


def test_cli_egress_policy_states_there_is_no_proxy(tmp_path, capsys):
    assert main(["--data-dir", str(tmp_path / "data"), "egress", "policy"]) == 0
    out = capsys.readouterr().out
    assert "没有代理进程" in out
    assert "iron-proxy" in out


def test_cli_egress_proxy_lifecycle_commands_report_honestly(tmp_path):
    """`egress start` 一类必须**非零退出**：返回 0 会被脚本读成"代理起来了"。"""
    # **egress start 现在真的起代理（前台阻塞）**，不能再调它来断言退出码。
    # 改成性质断言：它的 handler 已经不是那个桩了。
    from app.cli.commands.egress_lifecycle import run_egress_start
    from app.cli.commands.tools_extra2 import run_proxy_unavailable
    from app.cli.spec import REGISTRY

    start_cmd = REGISTRY.get("egress").child("start")
    assert start_cmd.handler is run_egress_start
    assert start_cmd.handler is not run_proxy_unavailable


# ── 7. 重定向：每一跳都要过闸 ──────────────────────────────────────────────
#
# `check()` 只看调用方显式写出来的那个 host。但 `curl -L` 会跟着 `Location` 一路走，
# 对方只要在第二跳回一个内网地址，第一跳的 allow 规则就成了攻击者的跳板。


def test_redirect_to_a_denied_host_is_refused(data_dir):
    policy = _policy(data_dir, allow=["ok.example"], deny=["evil.example"], default=egress.DENY)
    decision = policy.check("https://ok.example/x", redirects=["https://evil.example/collect"])
    assert decision.action == egress.DENY
    assert decision.host == "evil.example"
    assert decision.rule == "evil.example"
    # 报错要指向**被拒的那一跳**，否则人会去查一个完全正常的主机
    assert "第 1 跳" in decision.reason
    assert "ok.example" in decision.reason


def test_redirect_to_a_host_with_no_rule_is_refused_under_default_deny(data_dir):
    policy = _policy(data_dir, allow=["ok.example"], default=egress.DENY)
    decision = policy.check_chain(["https://ok.example/x", "http://169.254.169.254/latest"])
    assert decision.action == egress.DENY
    assert "169.254.169.254" in decision.host


def test_redirect_chain_that_stays_allowed_is_allowed(data_dir):
    policy = _policy(data_dir, allow=["*.example.com"], default=egress.DENY)
    decision = policy.check_chain(
        ["https://a.example.com/x", "https://b.example.com/y", "https://c.example.com/z"]
    )
    assert decision.action == egress.ALLOW
    assert "3 跳全部通过" in decision.reason


def test_first_hop_denial_is_reported_without_redirect_wording(data_dir):
    policy = _policy(data_dir, deny=["evil.example"])
    decision = policy.check_chain(["https://evil.example/x", "https://ok.example/y"])
    assert decision.action == egress.DENY
    assert "跳" not in decision.reason


def test_empty_redirect_list_behaves_like_a_plain_check(data_dir):
    policy = _policy(data_dir, allow=["ok.example"], default=egress.DENY)
    plain = policy.check("https://ok.example/x")
    with_empty = policy.check("https://ok.example/x", redirects=[])
    assert plain.action == with_empty.action == egress.ALLOW
    assert plain.reason == with_empty.reason


# ── 8. 多供应商头注入 ──────────────────────────────────────────────────────


def test_anthropic_style_uses_x_api_key_without_a_bearer_prefix(data_dir):
    policy = egress.EgressPolicy.load(data_dir)
    policy.credentials = [
        egress.CredentialRule(host="api.anthropic.com", vendor="anthropic", key="ANTHROPIC_KEY")
    ]
    merged = policy.inject_headers(
        "https://api.anthropic.com/v1/messages",
        {},
        resolver=_FakeResolver({"ANTHROPIC_KEY": SECRET_VALUE}),
    )
    assert merged["x-api-key"] == SECRET_VALUE  # 没有 `Bearer ` 前缀
    assert merged["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in merged


def test_azure_and_gemini_shapes(data_dir):
    policy = egress.EgressPolicy.load(data_dir)
    policy.credentials = [
        egress.CredentialRule(host="*.openai.azure.com", vendor="azure-openai", key="AZ"),
        egress.CredentialRule(host="generativelanguage.googleapis.com", vendor="gemini", key="GG"),
    ]
    resolver = _FakeResolver({"AZ": "azure-value", "GG": "gemini-value"})
    azure = policy.inject_headers("https://x.openai.azure.com/v1", {}, resolver=resolver)
    assert azure["api-key"] == "azure-value"
    gemini = policy.inject_headers(
        "https://generativelanguage.googleapis.com/v1", {}, resolver=resolver
    )
    assert gemini["x-goog-api-key"] == "gemini-value"


def test_explicit_header_and_scheme_win_over_the_preset(data_dir):
    policy = egress.EgressPolicy.load(data_dir)
    policy.credentials = [
        egress.CredentialRule(
            host="api.example.com",
            vendor="anthropic",
            header="X-Custom-Key",
            scheme="Token",
            key="K",
        )
    ]
    merged = policy.inject_headers(
        "https://api.example.com/v1", {}, resolver=_FakeResolver({"K": SECRET_VALUE})
    )
    assert merged["X-Custom-Key"] == f"Token {SECRET_VALUE}"
    assert "x-api-key" not in merged


def test_several_rules_for_one_host_inject_several_headers(data_dir):
    """多供应商共存：同一个 host 需要不止一个头时，不该只注入第一条。"""
    policy = egress.EgressPolicy.load(data_dir)
    policy.credentials = [
        egress.CredentialRule(host="gw.example.com", header="Authorization", key="A"),
        egress.CredentialRule(host="gw.example.com", header="X-Api-Key", key="B"),
    ]
    merged = policy.inject_headers(
        "https://gw.example.com/v1",
        {},
        resolver=_FakeResolver({"A": "one", "B": "two"}),
    )
    assert merged["Authorization"] == "Bearer one"
    assert merged["X-Api-Key"] == "Bearer two"


def test_two_rules_fighting_over_one_header_keep_the_first(data_dir):
    """同一个头名以第一条命中的规则为准：顺序即优先级，否则先后就变成随机的。"""
    policy = egress.EgressPolicy.load(data_dir)
    policy.credentials = [
        egress.CredentialRule(host="gw.example.com", header="Authorization", key="A"),
        egress.CredentialRule(host="gw.example.com", header="Authorization", key="B"),
    ]
    merged = policy.inject_headers(
        "https://gw.example.com/v1", {}, resolver=_FakeResolver({"A": "one", "B": "two"})
    )
    assert merged["Authorization"] == "Bearer one"


def test_static_headers_are_not_injected_when_the_secret_is_missing(data_dir):
    """取不到密钥就整条规则跳过——不能只改一半请求。"""
    policy = egress.EgressPolicy.load(data_dir)
    policy.credentials = [
        egress.CredentialRule(host="api.anthropic.com", vendor="anthropic", key="MISSING")
    ]
    merged = policy.inject_headers(
        "https://api.anthropic.com/v1", {}, resolver=_FakeResolver({})
    )
    assert merged == {}


def test_vendor_survives_a_save_and_reload(data_dir):
    policy = egress.EgressPolicy.load(data_dir)
    policy.credentials = [
        egress.CredentialRule(host="api.anthropic.com", vendor="anthropic", key="ANTHROPIC_KEY")
    ]
    path = policy.save()
    raw = path.read_text(encoding="utf-8")
    reloaded = egress.EgressPolicy.load(data_dir)
    assert reloaded.credentials[0].vendor == "anthropic"
    assert reloaded.credentials[0].resolved_header == "x-api-key"
    # 文件里仍然只有键名，没有值
    assert "ANTHROPIC_KEY" in raw and SECRET_VALUE not in raw


def test_injection_plan_detailed_reports_the_vendor_shape_without_values(data_dir):
    policy = egress.EgressPolicy.load(data_dir)
    policy.credentials = [
        egress.CredentialRule(host="api.anthropic.com", vendor="anthropic", key="ANTHROPIC_KEY")
    ]
    plan = policy.injection_plan(
        "https://api.anthropic.com/v1",
        resolver=_FakeResolver({"ANTHROPIC_KEY": SECRET_VALUE}),
        detailed=True,
    )
    assert plan[0]["header"] == "x-api-key"
    assert plan[0]["vendor"] == "anthropic"
    assert plan[0]["static_headers"] == ["anthropic-version"]
    assert SECRET_VALUE not in json.dumps(plan, ensure_ascii=False)


def test_describe_lists_the_six_missing_pieces_with_what_each_needs(data_dir):
    """缺的东西要逐条写清"要真做需要什么"，而不是只列名词。"""
    text = egress.EgressPolicy.load(data_dir).describe()
    for missing in ("独立守护进程", "自签 CA", "代理令牌", "proxy.yaml", "mappings.json", "强制出站改道"):
        assert missing in text, missing
    assert "要真做需要什么" in text
    # 以及它现在能做到的第三层：重定向再审
    assert "check_chain" in text
