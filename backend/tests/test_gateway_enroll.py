"""MATRIX 12.10：`gateway enroll` 的本地预检 + `install`/`uninstall` 真能装 + `setup` 向导。

这份文件守三条诚实要求：

1. **预检是真的**：`enroll_preflight()` 查的五件事（gateway id / peer token /
   wake secret / wake URL / connector URL）全部离线可验，失败分支逐条断言。
   预检全过之后**真注册那一步如实抛 `NetworkUnavailable`** —— 不假装注册成功。
2. **install 默认 dry-run**：`apply=False` 时**一条命令都不跑**，只把将执行的完整命令
   交出去；`apply=True` 用**注入的假执行器**断言命令行参数，绝不真的注册一个计划任务
   （那会改系统状态，也会让测试依赖机器）。
3. **setup 向导一次问一个问题、输入输出可注入**：用预置答案把整条向导跑通，
   并断言**机密答案不落盘**。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import gateway as gw


@pytest.fixture(autouse=True)
def _has_schtasks(monkeypatch):
    """把 `schtasks` 的存在性钉成常量。

    真机上它本来就在（`C:\\Windows\\system32\\schtasks.exe`），但这条测试要守的是
    "参数拼对了没"，不该因为跑在别的系统上就变成 skip —— 所以显式钉住。
    """
    monkeypatch.setattr(gw.shutil, "which", lambda name: rf"C:\Windows\system32\{name}.exe")


def _good_env() -> dict[str, str]:
    return {
        "ORBIT_PEER_TOKEN": "tok-123456",
        "ORBIT_WAKE_SECRET": "s" * 64,
        "ORBIT_WAKE_URL": "https://orbit.example.com/peer/wake",
        "ORBIT_CONNECTOR_URL": "https://relay.example.com",
    }


class FakeRunner:
    """假的命令执行器：只记录 argv，永远成功（除非显式让它失败）。"""

    def __init__(self, *, fail_on: tuple[str, ...] = ()) -> None:
        self.calls: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, argv):  # noqa: ANN001 - 与 Callable[[Iterable[str]], CommandOutcome] 对齐
        items = [str(item) for item in argv]
        self.calls.append(items)
        code = 1 if any(token in items for token in self.fail_on) else 0
        return gw.CommandOutcome(
            argv=tuple(items),
            returncode=code,
            stdout="SUCCESS" if code == 0 else "",
            stderr="" if code == 0 else "Access is denied.",
        )


# ══ 1. enroll 的本地预检 ═══════════════════════════════════════════════════


def test_preflight_passes_when_everything_is_configured(tmp_path):
    """五项齐备 → `ok=True`，并把注册端点算出来（路径里带网关 id）。"""
    preflight = gw.enroll_preflight(data_dir=tmp_path, env=_good_env())
    assert preflight.ok is True
    assert preflight.failures() == ()
    assert preflight.gateway_id.startswith("gw-")
    assert preflight.register_url == (
        f"https://relay.example.com/v1/gateways/{preflight.gateway_id}/register"
    )
    payload = preflight.as_dict()
    assert payload["implemented"] is True
    assert {check["id"] for check in payload["checks"]} == {
        "gateway-id",
        "peer-token",
        "wake-secret",
        "wake-url",
        "connector-url",
    }


def test_preflight_generates_and_persists_the_gateway_id(tmp_path):
    """gateway id 是**真写文件**的：第一次生成，第二次读到同一个（换 id = 换身份）。"""
    assert gw.read_gateway_id(tmp_path) == ""
    first = gw.enroll_preflight(data_dir=tmp_path, env=_good_env()).gateway_id
    path = gw.GatewayPaths(Path(tmp_path)).gateway_id
    assert path.exists()
    assert path.read_text(encoding="utf-8").strip() == first
    second = gw.enroll_preflight(data_dir=tmp_path, env=_good_env()).gateway_id
    assert second == first, "重跑预检不能换身份，否则注册过的 id 就找不回来了"


def test_preflight_reports_a_missing_peer_token(tmp_path):
    env = _good_env()
    env.pop("ORBIT_PEER_TOKEN")
    preflight = gw.enroll_preflight(data_dir=tmp_path, env=env)
    assert preflight.ok is False
    assert [check.id for check in preflight.failures()] == ["peer-token"]
    check = next(item for item in preflight.checks if item.id == "peer-token")
    assert "未配置" in check.detail
    assert "connector" in check.fix


def test_preflight_reports_a_missing_wake_secret(tmp_path):
    env = _good_env()
    env.pop("ORBIT_WAKE_SECRET")
    preflight = gw.enroll_preflight(data_dir=tmp_path, env=env)
    assert [check.id for check in preflight.failures()] == ["wake-secret"]
    assert "token_hex" in next(
        item for item in preflight.checks if item.id == "wake-secret"
    ).fix


@pytest.mark.parametrize(
    ("url", "needle"),
    [
        ("", "是空的"),
        ("tcp://orbit.example.com/peer/wake", "必须是 http/https"),
        ("https:///peer/wake", "没有主机名"),
        ("http://orbit.example.com/peer/wake", "明文 http"),
        ("https://orbit.example.com", "根路径"),
        ("https://orbit.example.com/", "根路径"),
    ],
)
def test_wake_url_validation_covers_every_failure_mode(tmp_path, url, needle):
    env = _good_env()
    env["ORBIT_WAKE_URL"] = url
    preflight = gw.enroll_preflight(data_dir=tmp_path, env=env)
    assert preflight.ok is False
    check = next(item for item in preflight.checks if item.id == "wake-url")
    assert check.ok is False
    assert needle in check.detail


def test_wake_url_allows_http_on_loopback_only(tmp_path):
    """本机自测用 http://127.0.0.1/... 是允许的（不发公网、不经反代）。"""
    env = _good_env()
    env["ORBIT_WAKE_URL"] = "http://127.0.0.1:8377/peer/wake"
    preflight = gw.enroll_preflight(data_dir=tmp_path, env=env)
    assert preflight.ok is True


@pytest.mark.parametrize(
    ("url", "needle"),
    [
        ("http://relay.example.com", "必须是 https"),
        ("https://relay.example.com/", "末尾不要带斜杠"),
        ("https:///x", "没有主机名"),
    ],
)
def test_connector_url_validation(tmp_path, url, needle):
    env = _good_env()
    env["ORBIT_CONNECTOR_URL"] = url
    preflight = gw.enroll_preflight(data_dir=tmp_path, env=env)
    assert preflight.ok is False
    check = next(item for item in preflight.checks if item.id == "connector-url")
    assert needle in check.detail


def test_connector_url_is_optional_for_the_preflight(tmp_path):
    """没给 connector 地址不拦预检（它属于注册那一步），但要说清"未提供"。"""
    env = _good_env()
    env.pop("ORBIT_CONNECTOR_URL")
    preflight = gw.enroll_preflight(data_dir=tmp_path, env=env)
    assert preflight.ok is True
    assert preflight.register_url == ""
    check = next(item for item in preflight.checks if item.id == "connector-url")
    assert "未提供" in check.detail


def test_explicit_arguments_win_over_the_environment(tmp_path):
    """显式参数优先于环境变量：测试/脚本不该被迫改进程环境。"""
    preflight = gw.enroll_preflight(
        data_dir=tmp_path,
        peer_token="from-arg",
        wake_url="https://a.example.com/peer/wake",
        wake_secret="k" * 32,
        gateway_id="gw-fixed",
        connector_url="https://relay.example.com",
        env={"ORBIT_PEER_TOKEN": "from-env"},
    )
    assert preflight.ok is True
    assert preflight.gateway_id == "gw-fixed"
    check = next(item for item in preflight.checks if item.id == "peer-token")
    assert "8 字符" in check.detail  # len("from-arg")


# ══ 2. enroll：预检过了就如实报"注册要网络" ════════════════════════════════


def test_enroll_stops_at_preflight_when_something_is_missing(tmp_path):
    result = gw.enroll(data_dir=tmp_path, env={})
    assert result.ok is False
    assert result.stage == "preflight"
    assert "预检没过" in result.reason
    assert "peer-token" in result.reason
    assert result.preflight.ok is False


def test_enroll_raises_network_unavailable_after_a_clean_preflight(tmp_path):
    """预检全过 → 真注册那一步**抛** `NetworkUnavailable`（不是返回 ok=True）。"""
    with pytest.raises(gw.NetworkUnavailable) as excinfo:
        gw.enroll(data_dir=tmp_path, env=_good_env())
    message = str(excinfo.value)
    assert "真实网络" in message
    assert "/v1/gateways/" in message
    assert "ORBIT_PEER_KEY" in message


# ══ 3. install：默认 dry-run，`--apply` 才真跑 ═════════════════════════════


def test_install_dry_run_executes_nothing_and_prints_the_command(tmp_path):
    """默认 dry-run：**一条命令都不跑**，只交出将执行的完整命令与 XML 全文。"""
    runner = FakeRunner()
    plan = gw.install_service(data_dir=tmp_path, apply=False, runner=runner)
    assert runner.calls == [], "dry-run 不许执行任何命令"
    assert plan.supported is True
    assert plan.dry_run is True
    assert plan.installed is False
    assert plan.executed == ()
    assert len(plan.commands) == 1
    argv = plan.commands[0]
    assert argv[:4] == ("schtasks", "/Create", "/TN", gw.WINDOWS_TASK_NAME)
    assert "/XML" in argv and "/F" in argv
    lines = plan.format_commands()
    assert lines[0].startswith("schtasks /Create /TN OrbitGateway /XML")
    assert "--apply" in plan.note
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in plan.xml_content
    # dry-run 不写文件（零副作用）
    assert not Path(plan.xml_path).exists()


def test_install_apply_runs_schtasks_with_the_exact_arguments(tmp_path):
    """`apply=True` + 注入执行器：断言命令行参数逐字正确，并断言 XML 真的落了盘。"""
    runner = FakeRunner()
    plan = gw.install_service(data_dir=tmp_path, apply=True, runner=runner)
    assert plan.dry_run is False
    assert plan.installed is True
    assert runner.calls == [
        [
            "schtasks",
            "/Create",
            "/TN",
            "OrbitGateway",
            "/XML",
            plan.xml_path,
            "/F",
        ]
    ]
    # XML 必须是 UTF-16（schtasks 只吃 Unicode；用 UTF-8 会被判"XML 无效"）
    written = Path(plan.xml_path)
    assert written.exists()
    raw = written.read_bytes()
    assert raw[:2] in {b"\xff\xfe", b"\xfe\xff"}, "schtasks 要求带 BOM 的 UTF-16"
    assert written.read_text(encoding="utf-16") == plan.xml_content
    assert plan.unit is not None and plan.unit.installed is True


def test_install_apply_failure_is_reported_not_swallowed(tmp_path):
    runner = FakeRunner(fail_on=("/Create",))
    plan = gw.install_service(data_dir=tmp_path, apply=True, runner=runner)
    assert plan.installed is False
    assert plan.executed and plan.executed[0].returncode == 1
    assert "安装失败" in plan.note
    assert plan.unit is not None and plan.unit.installed is False


def test_install_refuses_non_windows_kinds_instead_of_pretending(tmp_path):
    """systemd / launchd 要写系统目录，超出写入范围 → `supported=False` + 手工步骤。"""
    plan = gw.install_service(data_dir=tmp_path, kind="systemd-user", apply=True)
    assert plan.supported is False
    assert plan.installed is False
    assert plan.executed == ()
    assert plan.unit is not None
    assert any("systemctl --user enable --now" in step for step in plan.unit.install_steps)
    assert "超出 Orbit 的写入范围" in plan.note


def test_install_without_schtasks_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(gw.shutil, "which", lambda name: None)
    plan = gw.install_service(data_dir=tmp_path, apply=True)
    assert plan.supported is False
    assert "schtasks" in plan.note


def test_windows_task_xml_has_the_settings_that_matter(tmp_path):
    """几处不能省的细节：不限时、单实例、崩了才重启、登录触发、当前用户权限。"""
    xml = gw.windows_task_xml(data_dir=tmp_path, executable="orbit", user=r"PC\alice")
    assert xml.startswith('<?xml version="1.0" encoding="UTF-16"?>')
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml, "默认 72 小时会静默杀进程"
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml
    assert "<RestartOnFailure>" in xml and "<Count>3</Count>" in xml
    assert "<LogonTrigger>" in xml
    assert "<RunLevel>LeastPrivilege</RunLevel>" in xml
    assert "<UserId>PC\\alice</UserId>" in xml
    assert "<Command>orbit</Command>" in xml
    assert f'<Arguments>gateway run --data-dir "{Path(tmp_path).resolve()}"</Arguments>' in xml
    assert "<WorkingDirectory>" in xml


def test_windows_task_xml_escapes_awkward_paths(tmp_path):
    """路径里有 `&` / `<` 时必须转义，否则 XML 直接非法。"""
    weird = tmp_path / "a&b"
    weird.mkdir()
    xml = gw.windows_task_xml(data_dir=weird, user="")
    assert "&amp;" in xml
    assert "a&b" not in xml


# ══ 4. uninstall ═══════════════════════════════════════════════════════════


def test_uninstall_dry_run_prints_end_and_delete(tmp_path):
    plan = gw.uninstall_service(data_dir=tmp_path, apply=False)
    assert plan.dry_run is True
    assert plan.action == "uninstall"
    lines = plan.format_commands()
    assert lines[0] == "schtasks /End /TN OrbitGateway"
    assert lines[1] == "schtasks /Delete /TN OrbitGateway /F"
    assert "--apply" in plan.note


def test_uninstall_apply_tolerates_a_failing_end(tmp_path):
    """任务没在跑时 `/End` 必然返回非零 —— 判据只看 `/Delete`。"""
    runner = FakeRunner(fail_on=("/End",))
    plan = gw.uninstall_service(data_dir=tmp_path, apply=True, runner=runner)
    assert len(runner.calls) == 2
    assert runner.calls[1] == ["schtasks", "/Delete", "/TN", "OrbitGateway", "/F"]
    assert plan.installed is False, "Delete 成功 = 已经不在系统里了"
    assert "已执行卸载命令" in plan.note


def test_uninstall_apply_failure_is_reported(tmp_path):
    runner = FakeRunner(fail_on=("/Delete",))
    plan = gw.uninstall_service(data_dir=tmp_path, apply=True, runner=runner)
    assert "卸载失败" in plan.note
    assert plan.installed is True, "没删掉 = 它还装着"


# ══ 5. setup 向导：一次一问、可注入、机密不落盘 ═════════════════════════════


def test_setup_wizard_replays_preset_answers(tmp_path):
    """用预置答案跑通整条向导，并断言 setup.json 里**没有机密原文**。"""
    told: list[str] = []
    answers = {
        "wake_url": "https://orbit.example.com/peer/wake",
        "peer_token": "SUPER-SECRET-TOKEN",
        "wake_secret": "SUPER-SECRET-KEY",
        "install_task": "no",
    }
    result = gw.run_setup_wizard(
        data_dir=tmp_path, ask=lambda q: "", tell=told.append, answers=answers
    )
    assert result.ok is True
    assert result.answer("peer_token") == "SUPER-SECRET-TOKEN"
    assert result.answer("wake_url") == "https://orbit.example.com/peer/wake"
    written = Path(result.written)
    assert written.exists()
    text = written.read_text(encoding="utf-8")
    assert "SUPER-SECRET-TOKEN" not in text, "机密答案不许落盘"
    assert "SUPER-SECRET-KEY" not in text
    payload = json.loads(text)
    secrets = [item for item in payload["answers"] if item.get("secret")]
    assert len(secrets) == 2
    assert all(item["provided"] is True for item in secrets)
    assert payload["preflight"]["ok"] is True


def test_setup_wizard_asks_one_question_at_a_time_in_order(tmp_path):
    """`ask` 被逐个调用、顺序固定，且每题都先 tell 出去（用户知道自己在答第几题）。"""
    asked: list[str] = []
    told: list[str] = []
    scripted = {
        "gateway_id": "",
        "wake_url": "https://orbit.example.com/peer/wake",
        "peer_token": "tok",
        "wake_secret": "sec",
        "install_task": "yes",
    }

    def ask(question):  # noqa: ANN001 - 与 Callable[[SetupQuestion], str] 对齐
        asked.append(question.id)
        return scripted[question.id]

    result = gw.run_setup_wizard(data_dir=tmp_path, ask=ask, tell=told.append)
    assert asked == ["gateway_id", "wake_url", "peer_token", "wake_secret", "install_task"]
    assert len(told) >= 5
    # 选 yes 也只是**打印**命令（向导不装）
    assert any("schtasks /Create" in step for step in result.steps)
    assert any("向导**不装**" in step for step in result.steps)


def test_setup_wizard_reports_what_is_still_missing(tmp_path):
    """答案不完整时向导**照样跑完**，但结果里必须列出还差什么。"""
    told: list[str] = []
    result = gw.run_setup_wizard(
        data_dir=tmp_path,
        ask=lambda q: "",
        tell=told.append,
        answers={"wake_url": "", "peer_token": "", "wake_secret": "", "install_task": "no"},
    )
    assert result.ok is False
    assert result.preflight.failures()
    assert any("还差这些才能注册" in step for step in result.steps)


def test_setup_wizard_falls_back_on_an_invalid_choice(tmp_path):
    """`install_task` 填了 yes/no 之外的值 → 退回默认 no，并告诉用户为什么。"""
    told: list[str] = []
    result = gw.run_setup_wizard(
        data_dir=tmp_path,
        ask=lambda q: "maybe",
        tell=told.append,
        answers={"peer_token": "t", "wake_secret": "s", "wake_url": ""},
    )
    assert result.answer("install_task") == "no"
    assert any("只能是 yes/no" in line for line in told)


def test_setup_questions_are_declared_with_secrets_flagged():
    questions = gw.setup_questions(gateway_id="gw-x")
    ids = [question.id for question in questions]
    assert ids == ["gateway_id", "wake_url", "peer_token", "wake_secret", "install_task"]
    assert questions[0].default == "gw-x"
    assert questions[2].secret is True and questions[3].secret is True
    assert questions[4].choices == ("yes", "no")


# ══ 6. 命令行的可打印形式 ══════════════════════════════════════════════════


def test_format_argv_quotes_paths_with_spaces():
    """带空格的路径必须加引号，否则"打印出来的命令"粘进终端会变成两条参数。"""
    line = gw.format_argv(["schtasks", "/XML", r"C:\my data\OrbitGateway.xml"])
    assert "my data" in line
    if gw.os.name == "nt":
        assert '"C:\\my data\\OrbitGateway.xml"' in line
