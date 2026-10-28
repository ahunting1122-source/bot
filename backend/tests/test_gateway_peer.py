"""``app/gateway.py`` 的 peer / 剩余状态 + 两个标准库平台适配器 + clarify 按钮。

四块，各自盯一类"最容易假绿"的东西：

1. **MATRIX 12.10 的 gateway 三件事**：``enroll`` 只出清单、``install/uninstall``
   只**生成**单元文件（内容必须是真的，逐行核对）、``list`` 能读多实例登记表。
   "未实现"必须连同"要什么"一起交出去，否则等于什么都没做。
2. **MATRIX 12.5 的 email / irc**：连接那一步是可注入的，所以"构造 MIME → 连接 →
   发信"和"连接 → 注册 → PRIVMSG"三条都能在**完全离线**的机器上逐步断言。
   未配置时 ``ok=False`` 且**点名自己的**环境变量。
3. **MATRIX 5.13 的 clarify 按钮**：``render_choices`` 超过 5 个选项降级为编号文本；
   ``send_clarify`` 有/无 ``send_buttons`` 两条路都要测。
4. **CLI 接线**：新路径真的在注册表里、状态是诚实的（``peer run`` / ``stop`` 被
   ``platform_extra`` 占位时记为 ``occupied``，不抢别人的实现）。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app import gateway as gw
from app import platforms
from app.cli.context import CliError, Context
from app.cli.main import main
from app.cli.spec import REGISTRY

#: 显式导入即接线（生产里由 ``app/cli/commands/__init__.py`` 统一接线）。
import app.cli.commands.peer_extra as peer_extra  # noqa: E402,F401  isort: skip


def _run(tmp_path: Path, *argv: str) -> int:
    return main(["--data-dir", str(tmp_path / "data"), *argv])


def _context(tmp_path: Path) -> Context:
    """直接调 handler 用的 Context（不经 argparse，便于注入 transport）。"""
    return Context(data_dir=tmp_path / "data")


# ══ 1. gateway enroll：如实未实现 + 可执行的清单 ═════════════════════════════


def test_enroll_plan_is_honest_and_actionable():
    """``enroll`` 没实现，但清单必须**可执行**：relay 地址、wake 契约、凭证、轮换。"""
    plan = gw.enroll_requirements(
        connector_url="https://relay.example.com",
        gateway_id="gw-1",
        wake_url="https://orbit.example.com/peer/wake",
    )
    assert plan.ok is False
    assert "未实现" in plan.reason
    ids = {item.id for item in plan.requirements}
    assert ids == {"connector-url", "wake-url", "credentials", "rotation", "inbound"}
    how = {item.id: item.how for item in plan.requirements}
    requirement = {item.id: item.requirement for item in plan.requirements}
    # relay 地址格式：必须是 HTTPS、路径里带网关 id
    assert "https://relay.example.com/v1/gateways/gw-1/register" in how["connector-url"]
    assert "HTTPS" in requirement["connector-url"]
    # wake URL 契约：方法、body 三个字段、签名头、幂等
    assert "https://orbit.example.com/peer/wake" in how["wake-url"]
    assert "POST" in how["wake-url"]
    assert "X-Orbit-Signature" in how["wake-url"]
    assert "nonce" in how["wake-url"]
    # 需要哪些凭证
    for name in ("ORBIT_PEER_TOKEN", "ORBIT_PEER_KEY", "ORBIT_WAKE_SECRET"):
        assert name in how["credentials"]
    assert plan.steps and all(step.strip() for step in plan.steps)
    payload = plan.as_dict()
    assert payload["implemented"] is False
    assert payload["ok"] is False


def test_enroll_plan_uses_placeholders_when_nothing_given():
    """什么都不给也要能打印（否则用户连"该填什么"都看不到）。"""
    plan = gw.enroll_requirements()
    how = {item.id: item.how for item in plan.requirements}
    assert "<gateway-id>" in how["connector-url"]
    assert "<公网可达主机>" in how["wake-url"]


# ══ 2. gateway install / uninstall：生成的单元文件内容是真的 ═════════════════


def test_systemd_user_unit_content_is_real(tmp_path):
    unit = gw.render_service_unit("systemd-user", data_dir=tmp_path, executable="/usr/bin/orbit")
    assert unit.path == "~/.config/systemd/user/orbit-gateway.service"
    assert unit.as_dict()["installed"] is False  # 只生成，不安装
    content = unit.content
    assert "[Unit]" in content and "[Service]" in content and "[Install]" in content
    # ExecStart 必须指向**前台运行**（gateway run），不是 start
    assert f"ExecStart=/usr/bin/orbit gateway run --data-dir {Path(tmp_path).resolve()}" in content
    assert "Type=simple" in content
    assert "Restart=on-failure" in content
    # 崩溃循环保护必须写进单元（进程还没起来就崩时只有 init 系统拦得住）
    assert "StartLimitIntervalSec=600" in content
    assert "StartLimitBurst=3" in content
    assert "WantedBy=default.target" in content
    # 手工步骤：放哪、怎么 enable、为什么还要 enable-linger
    joined = "\n".join(unit.install_steps)
    assert "~/.config/systemd/user" in joined
    assert "systemctl --user daemon-reload" in joined
    assert "systemctl --user enable --now orbit-gateway.service" in joined
    assert "loginctl enable-linger" in joined
    assert any("disable" in step for step in unit.uninstall_steps)


def test_systemd_system_unit_targets_multi_user_and_root(tmp_path):
    unit = gw.render_service_unit("systemd-system", data_dir=tmp_path, user="orbit")
    assert unit.path == "/etc/systemd/system/orbit-gateway.service"
    assert "User=orbit" in unit.content
    assert "WantedBy=multi-user.target" in unit.content
    assert any("sudo" in step for step in unit.install_steps)


def test_launchd_plist_is_well_formed(tmp_path):
    unit = gw.render_service_unit("launchd", data_dir=tmp_path)
    assert unit.path == "~/Library/LaunchAgents/com.orbit.gateway.plist"
    content = unit.content
    assert content.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert "<key>Label</key><string>com.orbit.gateway</string>" in content
    assert "<string>gateway</string>" in content and "<string>run</string>" in content
    assert "<key>RunAtLoad</key><true/>" in content
    # KeepAlive.SuccessfulExit=false = "崩了才重启"，对应 systemd 的 on-failure
    assert "<key>SuccessfulExit</key><false/>" in content
    assert "launchctl load -w" in "\n".join(unit.install_steps)


def test_windows_unit_is_a_scheduled_task_not_a_fake_service(tmp_path):
    """Windows 没有"把控制台程序注册成服务"的标准做法——不许假装有。"""
    unit = gw.render_service_unit("windows", data_dir=tmp_path)
    assert "schtasks /Create /TN \"OrbitGateway\" /SC ONLOGON" in unit.content
    assert "nssm" in unit.content  # 真正的服务需要 wrapper，写清名字
    assert "不是 Windows 服务" in unit.content
    assert any("schtasks /Delete" in step for step in unit.uninstall_steps)


def test_unknown_service_kind_is_rejected():
    with pytest.raises(ValueError):
        gw.render_service_unit("upstart", data_dir=Path("."))


def test_cli_service_unit_prints_content_and_refuses_writes_outside_data_dir(tmp_path, capsys):
    """``gateway service-unit`` 真打印内容；``--output`` 只允许写 data_dir 之内。"""
    assert _run(tmp_path, "gateway", "service-unit", "--kind", "systemd-user") == 0
    out = capsys.readouterr().out
    assert "ExecStart=" in out
    assert "systemctl --user enable --now orbit-gateway.service" in out

    # data_dir 之内：允许
    inside = tmp_path / "data" / "orbit-gateway.service"
    assert _run(tmp_path, "gateway", "service-unit", "-o", str(inside)) == 0
    assert "ExecStart=" in inside.read_text(encoding="utf-8")

    # data_dir 之外：拒绝（写入范围铁律，与 install 未实现同一个理由）
    outside = tmp_path / "elsewhere" / "orbit-gateway.service"
    code = _run(tmp_path, "gateway", "service-unit", "-o", str(outside))
    assert code == 1
    assert "只写 data_dir" in capsys.readouterr().err
    assert not outside.exists()


# ══ 3. gateway list / instances：读多实例登记表 ══════════════════════════════


def test_instance_registry_round_trip(tmp_path):
    registry = gw.InstanceRegistry(tmp_path / "gateway" / "instances.json")
    local = registry.ensure_local(tmp_path)
    assert local.id == "local"
    assert Path(local.data_dir) == Path(tmp_path).resolve()
    # ensure_local 幂等：再调一次不追加第二条
    registry.ensure_local(tmp_path)
    assert len(registry.load()) == 1
    assert registry.remove("local") is True
    assert registry.remove("local") is False


def test_instance_rows_says_single_instance_honestly(tmp_path):
    view = gw.instance_rows(tmp_path)
    assert view["count"] == 1 and view["single"] is True
    assert "单数据目录" in view["note"]
    row = view["instances"][0]
    assert row["is_local"] is True
    assert row["dispatcher"] == "未运行"
    assert row["platforms"] == "19/22"


def test_instance_rows_reads_multi_instance_registry(tmp_path):
    """登记表里有两台机器时：本机那条读实时状态，远端那条**如实标未探测**。"""
    registry = gw.InstanceRegistry(gw.GatewayPaths(tmp_path).instances)
    registry.register(registry.local(tmp_path))
    registry.register(
        gw.GatewayInstance(id="spark", data_dir="/srv/orbit", host="spark", url="http://spark:8377")
    )
    view = gw.instance_rows(tmp_path)
    assert view["count"] == 2 and view["single"] is False
    by_id = {row["id"]: row for row in view["instances"]}
    assert by_id["local"]["is_local"] is True
    assert by_id["spark"]["is_local"] is False
    assert "未探测" in by_id["spark"]["dispatcher"]
    assert str(view["registry"]).endswith("instances.json")


def test_cli_gateway_instances_lists_registry(tmp_path, capsys):
    assert _run(tmp_path, "gateway", "instances") == 0
    out = capsys.readouterr().out
    assert "local" in out
    assert "单数据目录" in out
    assert (tmp_path / "data" / "gateway" / "instances.json").exists()


def test_gateway_health_ignores_unconfigured_stdlib_adapters(tmp_path):
    """没配邮箱/IRC 的机器上 ``gateway doctor`` 必须仍然全绿。

    ``email`` / ``irc`` 把"未配置"如实报成 ``ok=False``（它们缺一项就一条也发不出去），
    所以健康汇总必须**先看 ``configured``** 再看 ``ok``——顺序反了会让干净环境永远红灯。
    """
    service = gw.GatewayService(tmp_path)
    assert service.start().ok
    report = service.health()
    assert report.healthy is True, report.issues
    assert not any("email" in issue or "irc" in issue for issue in report.issues)
    service.stop()


# ══ 4. email 适配器：MIME 构造 + 发信流程（离线，不碰真网络）═════════════════


class _FakeSMTP:
    """假 SMTP 服务器：只记调用，不开 socket。"""

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[object] = []
        self.sent: list[object] = []

    def starttls(self) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", user, password))

    def send_message(self, message) -> None:
        self.calls.append("send_message")
        self.sent.append(message)

    def quit(self) -> None:
        self.calls.append("quit")


def _email_adapter(**kwargs) -> tuple[list[_FakeSMTP], platforms.EmailAdapter]:
    servers: list[_FakeSMTP] = []

    def factory(host, port, timeout=None):
        server = _FakeSMTP(host, port, timeout)
        servers.append(server)
        return server

    adapter = platforms.EmailAdapter(
        smtp_host="smtp.example.com",
        smtp_port=587,
        username="bot@example.com",
        password="secret",
        smtp_factory=factory,
        **kwargs,
    )
    return servers, adapter


def test_email_build_message_produces_valid_mime():
    _servers, adapter = _email_adapter()
    message = adapter.build_message("someone@example.com", "第一行\n第二行")
    assert message["To"] == "someone@example.com"
    assert message["From"] == "bot@example.com"
    # 主题从正文首行兜底（空主题会被不少反垃圾网关直接丢信）
    assert message["Subject"] == "第一行"
    assert message["Date"]
    assert str(message["Message-ID"]).endswith("@example.com>")
    assert message.get_content_type() == "text/plain"
    assert message.get_content().strip() == "第一行\n第二行"


def test_email_send_text_connects_tls_logs_in_then_sends():
    servers, adapter = _email_adapter()
    result = adapter.send_text("someone@example.com", "磁盘告警")
    assert result.ok is True
    assert result.platform == "email"
    assert result.channel == "someone@example.com"
    assert result.message_id.endswith(">")
    server = servers[0]
    assert (server.host, server.port) == ("smtp.example.com", 587)
    # 顺序不能反：先 STARTTLS 再 login，否则密码明文上网
    assert server.calls == ["starttls", ("login", "bot@example.com", "secret"),
                            "send_message", "quit"]
    assert server.sent[0]["To"] == "someone@example.com"


def test_email_can_skip_tls_for_local_relay():
    servers, adapter = _email_adapter(use_tls=False)
    assert adapter.send_text("someone@example.com", "hi").ok is True
    assert "starttls" not in servers[0].calls


def test_email_reports_smtp_failure_without_raising():
    class _Broken(_FakeSMTP):
        def send_message(self, message) -> None:
            raise __import__("smtplib").SMTPDataError(554, b"rejected")

    servers: list[_FakeSMTP] = []

    def factory(host, port, timeout=None):
        server = _Broken(host, port, timeout)
        servers.append(server)
        return server

    adapter = platforms.EmailAdapter(
        smtp_host="smtp.example.com", username="bot@example.com", password="x",
        smtp_factory=factory,
    )
    result = adapter.send_text("someone@example.com", "hi")
    assert result.ok is False
    assert "554" in result.error or "SMTPDataError" in result.error


def test_email_unconfigured_is_not_ok_and_names_its_own_env(monkeypatch):
    for name in ("EMAIL_SMTP_HOST", "EMAIL_ADDRESS", "EMAIL_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    adapter = platforms.email_adapter()
    assert adapter.available() is False
    result = adapter.send_text("someone@example.com", "hi")
    assert result.ok is False
    assert "EMAIL_SMTP_HOST" in result.error
    assert "EMAIL_ADDRESS" in result.error
    assert "IRC_HOST" not in result.error  # 绝不点名别人的变量
    health = adapter.health()
    assert health.ok is False
    assert health.extra["configured"] is False
    assert "EMAIL_PASSWORD" in health.detail


def test_email_available_when_env_complete(monkeypatch):
    monkeypatch.setenv("EMAIL_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("EMAIL_ADDRESS", "bot@example.com")
    monkeypatch.setenv("EMAIL_PASSWORD", "pw")
    assert platforms.email_adapter().available() is True


# ══ 5. irc 适配器：连接 + 注册 + PRIVMSG（离线，不碰真网络）══════════════════


class _FakeSocket:
    """假 socket：只记 ``sendall`` 的字节与 ``close``。"""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.chunks.append(data)

    def close(self) -> None:
        self.closed = True


def _irc_adapter(**kwargs) -> tuple[list[tuple[tuple[str, int], float]], _FakeSocket, platforms.IrcAdapter]:
    calls: list[tuple[tuple[str, int], float]] = []
    fake = _FakeSocket()

    def connector(address, timeout):
        calls.append((address, timeout))
        return fake

    adapter = platforms.IrcAdapter(
        host="irc.example.com", port=6667, nickname="orbit", channel="#dev",
        connector=connector, **kwargs,
    )
    return calls, fake, adapter


def test_irc_send_text_connects_registers_then_privmsg():
    calls, fake, adapter = _irc_adapter()
    result = adapter.send_text("", "部署完成")
    assert result.ok is True
    assert result.channel == "#dev"
    assert calls == [(("irc.example.com", 6667), 10.0)]
    wire = b"".join(fake.chunks).decode("utf-8")
    lines = wire.split("\r\n")
    # 行序就是协议：NICK / USER 在 JOIN 之前，JOIN 在 PRIVMSG 之前
    assert lines[0] == "NICK orbit"
    assert lines[1].startswith("USER orbit 0 * :")
    assert lines[2] == "JOIN #dev"
    assert lines[3] == "PRIVMSG #dev :部署完成"
    assert fake.closed is True


def test_irc_splits_long_text_by_byte_length():
    """512 字节行上限按**字节**算，所以长正文必须切成多条 PRIVMSG。"""
    _calls, fake, adapter = _irc_adapter()
    text = "x" * 1000  # 1000 字节
    adapter.send_text("#dev", text)
    privmsgs = [
        chunk for chunk in b"".join(fake.chunks).decode("utf-8").split("\r\n")
        if chunk.startswith("PRIVMSG")
    ]
    assert len(privmsgs) == 3
    assert "".join(line.split(":", 1)[1] for line in privmsgs) == text
    for line in privmsgs:
        assert len(line.encode("utf-8")) <= 512


def test_irc_split_counts_bytes_not_characters():
    """一个汉字 3 字节：400 个汉字 = 1200 字节，按字符切会超限。"""
    _calls, fake, adapter = _irc_adapter()
    text = "汉" * 400
    adapter.send_text("#dev", text)
    privmsgs = [
        chunk for chunk in b"".join(fake.chunks).decode("utf-8").split("\r\n")
        if chunk.startswith("PRIVMSG")
    ]
    assert len(privmsgs) >= 3
    for line in privmsgs:
        payload = line.split(":", 1)[1]
        assert len(payload.encode("utf-8")) <= adapter.MAX_TEXT_BYTES
        assert len(line.encode("utf-8")) <= 512
    # 分片不能丢字也不能重复
    assert "".join(line.split(":", 1)[1] for line in privmsgs) == text


def test_irc_flattens_newlines_into_one_privmsg():
    """正文里的换行会变成协议行分隔符，必须压成空格。"""
    _calls, fake, adapter = _irc_adapter()
    adapter.send_text("#dev", "第一行\n第二行")
    privmsgs = [
        chunk for chunk in b"".join(fake.chunks).decode("utf-8").split("\r\n")
        if chunk.startswith("PRIVMSG")
    ]
    assert len(privmsgs) == 1
    assert privmsgs[0] == "PRIVMSG #dev :第一行 第二行"


def test_irc_unconfigured_is_not_ok_and_names_its_own_env(monkeypatch):
    monkeypatch.delenv("IRC_HOST", raising=False)
    opened: list[object] = []
    adapter = platforms.irc_adapter(connector=lambda *args, **kwargs: opened.append(args))
    assert adapter.available() is False
    result = adapter.send_text("#dev", "hi")
    assert result.ok is False
    assert "IRC_HOST" in result.error
    assert "EMAIL_SMTP_HOST" not in result.error
    assert opened == []  # 未配置时**一个连接都不开**
    health = adapter.health()
    assert health.ok is False
    assert health.extra["configured"] is False


def test_irc_available_only_when_host_is_configured(monkeypatch):
    monkeypatch.delenv("IRC_HOST", raising=False)
    assert platforms.irc_adapter().available() is False
    monkeypatch.setenv("IRC_HOST", "irc.example.com")
    assert platforms.irc_adapter().available() is True


def test_stdlib_platform_specs_are_honest():
    """两个标准库平台：填 adapter、写清 scope、**清空 reason**。

    MATRIX 12.5 之后这两家**不再是"仅出站"**：`irc` 有了 PRIVMSG 读取与 PING/PONG 保活、
    `email` 有了 IMAP 轮询与 `STORE \\Seen`（见 `app/platforms.py` 的
    `IrcInboundAdapter` / `EmailImapInboundAdapter`）。所以这条断言从"scope 自陈仅出站"
    改成"scope 必须说清有没有入站、以及**没覆盖什么**" —— 后半句才是这条用例真正要守的
    东西：别让人把 scope 读成全功能。
    """
    for name in ("email", "irc"):
        spec = platforms.PLATFORMS[platforms.Platform(name)]
        assert spec.implemented is True
        assert spec.adapter == name
        assert spec.scope.strip()
        assert "入站" in spec.scope, f"{name} 的 scope 必须交代入站现状"
        assert "无" in spec.scope, f"{name} 的 scope 必须写清**没覆盖**什么"
        assert spec.reason == ""
        assert spec.credentials
    assert set(platforms.STDLIB_PLATFORMS) == {platforms.Platform.EMAIL, platforms.Platform.IRC}


def test_implemented_platform_count_is_nineteen():
    assert len(platforms.IMPLEMENTED_PLATFORMS) == 19
    pending = [
        platform.value
        for platform in platforms.ALL_PLATFORMS
        if not platforms.PLATFORMS[platform].implemented
    ]
    assert pending == ["buzz", "photon", "simplex"]


def test_default_adapters_include_email_and_irc(tmp_path):
    pool = platforms.default_adapters(tmp_path)
    assert isinstance(pool["email"], platforms.EmailAdapter)
    assert isinstance(pool["irc"], platforms.IrcAdapter)
    assert pool["email"].platform is platforms.Platform.EMAIL
    assert pool["irc"].platform is platforms.Platform.IRC


# ══ 6. clarify 按钮（MATRIX 5.13）════════════════════════════════════════════


def test_render_choices_uses_buttons_up_to_five():
    rendered = platforms.render_choices(
        "用哪种呈现？", ("Markdown", "纯文本", "先看草稿"), clarify_id="telegram:-100"
    )
    assert rendered.mode == "buttons"
    assert rendered.degraded is False
    assert len(rendered.buttons) == 3
    assert [label for label, _ in rendered.buttons] == ["Markdown", "纯文本", "先看草稿"]
    # 回调 id 走 cl:<id>:<idx> 约定，且 ident 里的冒号被替换掉（解析靠 split(":")）
    assert rendered.buttons[0][1] == "cl:telegram--100:1"
    assert platforms.parse_callback_id(rendered.buttons[0][1]) == platforms.CallbackRef(
        kind="cl", ident="telegram--100", choice="1"
    )
    # 文本永远带编号列表：按钮被降级时用户照样能选
    assert "1) Markdown" in rendered.text
    assert "回复序号即可" in rendered.text


def test_render_choices_degrades_to_numbered_text_over_five():
    """6 个选项超过 5 个按钮上限 → **一个按钮都不给**，全部走编号文本。"""
    options = tuple(f"选项{i}" for i in range(1, 7))
    rendered = platforms.render_choices("选哪个？", options)
    assert rendered.buttons == ()
    assert rendered.degraded is True
    assert rendered.mode == "text"
    assert "超过按钮上限 5" in rendered.reason
    for index in range(1, 7):
        assert f"{index}) 选项{index}" in rendered.text


def test_render_choices_without_options_degrades():
    rendered = platforms.render_choices("选哪个？", ())
    assert rendered.degraded is True
    assert rendered.buttons == ()
    assert "没有可选项" in rendered.reason


def test_render_choices_respects_free_text_switch():
    strict = platforms.render_choices("选哪个？", ("a", "b"), allow_free_text=False)
    assert "只能从上面选一个" in strict.text
    free = platforms.render_choices("选哪个？", ("a", "b"))
    assert "直接用自己的话" in free.text


class _ButtonAdapter(platforms.ConsoleAdapter):
    """有按钮的假适配器（ConsoleAdapter 本身没有 send_buttons）。"""

    name = "fake-buttons"

    def __init__(self) -> None:
        super().__init__(name="fake-buttons")
        self.sent: tuple[str, str, tuple[tuple[str, str], ...]] | None = None

    def send_buttons(self, channel: str, text: str, buttons: tuple[tuple[str, str], ...]):
        self.sent = (channel, text, buttons)
        return platforms.SendResult(
            ok=True, platform=self.name, channel=channel, message_id="btn-1"
        )


def test_send_clarify_uses_buttons_when_available():
    adapter = _ButtonAdapter()
    delivery = platforms.send_clarify(adapter, "#dev", "用哪个？", ("A", "B"), clarify_id="c1")
    assert delivery.ok is True
    assert delivery.used == "buttons"
    assert delivery.message_id == "btn-1"
    assert adapter.sent is not None
    channel, text, buttons = adapter.sent
    assert channel == "#dev"
    assert "1) A" in text
    assert buttons == (("A", "cl:c1:1"), ("B", "cl:c1:2"))


def test_send_clarify_falls_back_to_text_without_send_buttons(capsys):
    """``ConsoleAdapter`` 没有按钮 → 退回编号文本，且**正文一定发得出去**。"""
    adapter = platforms.ConsoleAdapter()
    delivery = platforms.send_clarify(adapter, "#dev", "用哪个？", ("A", "B"), clarify_id="c1")
    assert delivery.ok is True
    assert delivery.used == "text"
    assert delivery.rendered is not None
    assert delivery.rendered.degraded is True
    assert "没有按钮" in delivery.rendered.reason
    out = capsys.readouterr().out
    assert "1) A" in out and "2) B" in out


def test_send_clarify_degrades_before_sending_when_too_many_options(capsys):
    adapter = _ButtonAdapter()
    delivery = platforms.send_clarify(
        adapter, "#dev", "选哪个？", tuple(f"o{i}" for i in range(1, 7))
    )
    assert delivery.used == "text"
    assert adapter.sent is None  # 没走按钮那条路
    assert "6) o6" in capsys.readouterr().out


def test_cli_gateway_clarify_goes_through_console(tmp_path, capsys):
    assert _run(tmp_path, "gateway", "clarify", "--to", "console", "--question", "选哪个？",
                "--option", "甲", "--option", "乙") == 0
    out = capsys.readouterr().out
    assert "呈现方式 = 编号文本" in out
    assert "1) 甲" in out


def test_cli_gateway_clarify_rejects_single_option(tmp_path, capsys):
    assert _run(tmp_path, "gateway", "clarify", "--option", "只有一个") == 2
    assert "至少要 2 个选项" in capsys.readouterr().err


# ══ 7. peer 客户端接线（CLI 一侧）════════════════════════════════════════════


def test_cli_peer_health_probes_peer(tmp_path, capsys, monkeypatch):
    assert _run(tmp_path, "peer", "add", "spark", "--url", "http://spark.test:8377") == 0
    capsys.readouterr()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "capabilities": ["dm", "run"]})

    monkeypatch.setattr(peer_extra, "PEER_TRANSPORT", httpx.MockTransport(handler))
    assert _run(tmp_path, "peer", "health", "spark") == 0
    out = capsys.readouterr().out
    assert "在线" in out
    assert "dm、run" in out
    assert str(seen[0].url) == "http://spark.test:8377/peer/health"


def test_cli_peer_health_reports_unreachable_peer_as_failure(tmp_path, capsys, monkeypatch):
    assert _run(tmp_path, "peer", "add", "spark", "--url", "http://spark.test:8377") == 0
    capsys.readouterr()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(peer_extra, "PEER_TRANSPORT", httpx.MockTransport(handler))
    assert _run(tmp_path, "peer", "health", "spark") == 1
    assert "连不上" in capsys.readouterr().out


def _args(**kwargs) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


def test_peer_run_handler_raises_unsupported_with_contract(tmp_path, capsys, monkeypatch):
    """对端 404 → 退出码 3，且消息里点名缺哪两个端点。"""
    assert _run(tmp_path, "peer", "add", "spark", "--url", "http://spark.test:8377") == 0
    monkeypatch.setattr(
        peer_extra, "PEER_TRANSPORT", httpx.MockTransport(lambda request: httpx.Response(404))
    )
    with pytest.raises(CliError) as excinfo:
        peer_extra.run_peer_run(
            _context(tmp_path), _args(target="spark", message="长任务", idempotency_key="")
        )
    assert excinfo.value.code == 3
    assert "<base>/peer/run" in str(excinfo.value)
    assert "<base>/peer/stop" in str(excinfo.value)


def test_peer_run_and_stop_handlers_work_when_peer_supports_them(tmp_path, capsys, monkeypatch):
    assert _run(tmp_path, "peer", "add", "spark", "--url", "http://spark.test:8377") == 0
    capsys.readouterr()
    routes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        routes.append(request.url.path)
        if request.url.path.endswith("/peer/run"):
            return httpx.Response(202, json={"run_id": "run_7"})
        return httpx.Response(200, json={"stopped": True})

    monkeypatch.setattr(peer_extra, "PEER_TRANSPORT", httpx.MockTransport(handler))
    context = _context(tmp_path)
    assert peer_extra.run_peer_run(
        context, _args(target="spark/researcher", message="长任务", idempotency_key="k1")
    ) == 0
    assert "run_7" in capsys.readouterr().out
    assert peer_extra.run_peer_stop(
        context, _args(target="spark", run_id="run_7")
    ) == 0
    assert routes == ["/peer/run", "/peer/stop"]


def test_peer_run_requires_registered_peer(tmp_path):
    with pytest.raises(CliError) as excinfo:
        peer_extra.run_peer_run(
            _context(tmp_path), _args(target="nope", message="x", idempotency_key="")
        )
    assert "peer add" in str(excinfo.value)


# ══ 8. 接线与状态诚实性 ══════════════════════════════════════════════════════


def test_new_paths_are_wired_and_states_are_honest():
    peer = REGISTRY.get("peer")
    gateway = REGISTRY.get("gateway")
    assert peer is not None and gateway is not None
    assert peer.child("health") is not None
    assert peer.child("health").handler is peer_extra.run_peer_health
    for leaf in ("clarify", "service-unit", "enroll-plan", "instances"):
        child = gateway.child(leaf)
        assert child is not None, leaf
        assert child.handler is not None, leaf
        assert child.state == "done", leaf


def test_peer_run_and_stop_wiring_is_explicit():
    """``peer run`` / ``peer stop`` 已被 ``platform_extra`` 占位时不抢，并**记下事实**。"""
    assert peer_extra.WIRING["peer run"] in {"occupied", "claimed"}
    assert peer_extra.WIRING["peer stop"] in {"occupied", "claimed"}
    # 无论哪种，注册表里那条的 handler 都必须存在（不能停在空壳上）
    peer = REGISTRY.get("peer")
    assert peer is not None
    assert peer.child("run").handler is not None
    assert peer.child("stop").handler is not None
    # 客户端真实现必须一直可调用（接线决定不改变它的存在）
    assert callable(peer_extra.run_peer_run)
    assert callable(peer_extra.run_peer_stop)


def test_gateway_enroll_plan_cli_states_it_is_not_implemented(tmp_path, capsys):
    assert _run(tmp_path, "gateway", "enroll-plan", "--gateway-id", "gw-1") == 0
    out = capsys.readouterr().out
    assert "仍未实现" in out
    assert "/v1/gateways/gw-1/register" in out
    assert "X-Orbit-Signature" in out


def test_instances_json_is_plain_readable_json(tmp_path):
    _run(tmp_path, "gateway", "instances")
    path = tmp_path / "data" / "gateway" / "instances.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert [item["id"] for item in payload["instances"]] == ["local"]
