"""MATRIX 12.5 平台入站：`irc` 与 `email` 两条**真能离线验证**的入站。

## 为什么是这两条

出站只需要"能发一个请求"；入站要的是 webhook 服务端（要公网）/ 长连接守护（要常驻
进程）/ 平台私有 daemon。本机没有公网、也不许装依赖，所以只有两条能自证：

* ``irc`` —— 裸 TCP 文本协议，**socket 可注入**：对着假 socket 断言握手、
  ``PRIVMSG`` 解析、``PING``/``PONG`` 保活、粘包重组；
* ``email`` —— IMAP 是标准库，**客户端可注入**：对着假 IMAP 断言
  ``search UNSEEN`` / ``fetch`` / MIME 解析 / ``STORE \\Seen``（ack）。

**一个真实连接都不开**，所以这些用例在无网络机器上也是确定性的。

## 这份文件同时钉住"其余 20 个只有出站"这件事

:func:`app.platforms.inbound_report` 里 ``only_outbound`` 有 20 条（17 个已实现的
出站平台 + 3 个连出站都没实现的），每一条都必须在 ``INBOUND_MISSING`` 里写明缺哪
一层 —— 没有原因的"没做"与"做不到"分不开，而这两者的后续动作完全不同。
"""

from __future__ import annotations

import email.message

import pytest

from app import platforms as p

# ── 假 socket / 假 IMAP ─────────────────────────────────────────────────────


class FakeSocket:
    """只记 ``sendall`` 的字节、按剧本返回 ``recv`` 的假 socket。

    ``recv`` 返回空串 = "这一轮没有更多数据"（真实 socket 的 EOF 与超时在
    :meth:`IrcInboundAdapter._recv` 里被同样处理成空串）。
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.sent: list[bytes] = []
        self.closed = False
        self.timeout: float | None = None

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, size: int) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def close(self) -> None:
        self.closed = True

    # 方便断言：把所有写入拼成文本行
    def lines(self) -> list[str]:
        return [line for line in b"".join(self.sent).decode("utf-8").split("\r\n") if line]


def irc_adapter(chunks: list[bytes], **kwargs) -> tuple[p.IrcInboundAdapter, FakeSocket]:
    """造一个注入了假 socket 的 IRC 入站适配器。"""
    sock = FakeSocket(chunks)

    def connector(address, timeout):
        sock.address = address
        sock.timeout_arg = timeout
        return sock

    adapter = p.IrcInboundAdapter(
        host="irc.example", nickname="orbit", channel="#ops", connector=connector, **kwargs
    )
    return adapter, sock


class FakeImap:
    """假 IMAP 客户端：记录调用顺序，按剧本返回 ``fetch`` 的原始邮件。"""

    def __init__(
        self,
        *,
        uids: list[str] | None = None,
        messages: dict[str, bytes] | None = None,
        store_status: str = "OK",
        fetch_status: str = "OK",
    ) -> None:
        self.uids = uids if uids is not None else ["11", "12"]
        self.messages = messages or {}
        self.store_status = store_status
        self.fetch_status = fetch_status
        self.calls: list[tuple] = []
        self.stored: list[tuple[str, str, str]] = []
        self.logged: tuple[str, str] | None = None
        self.selected: str | None = None
        self.closed = False

    def login(self, username: str, password: str):
        self.logged = (username, password)
        self.calls.append(("login", username))
        return "OK", [b"logged in"]

    def select(self, folder: str):
        self.selected = folder
        self.calls.append(("select", folder))
        return "OK", [b"1"]

    def search(self, charset, criteria):  # noqa: ANN001 - 与 imaplib 的签名对齐
        self.calls.append(("search", charset, criteria))
        return "OK", [" ".join(self.uids).encode()]

    def fetch(self, uid: str, spec: str):
        self.calls.append(("fetch", uid, spec))
        raw = self.messages.get(uid, b"")
        return self.fetch_status, [(b"1 (RFC822 {100}", raw), b")"]

    def store(self, uid: str, flag_op: str, flags: str):
        self.calls.append(("store", uid, flag_op, flags))
        self.stored.append((uid, flag_op, flags))
        return self.store_status, [uid.encode()]

    def close(self) -> None:
        self.closed = True

    def logout(self) -> None:
        self.closed = True


def raw_mail(
    *,
    subject: str = "测试主题",
    sender: str = "alice@example.com",
    body: str = "你好，这是一封测试邮件。",
    html: str = "",
) -> bytes:
    """造一封真 MIME 邮件的原始字节（走标准库构造，避免手写头出错）。"""
    if html:
        message = email.message.EmailMessage()
        message["From"] = sender
        message["Subject"] = subject
        message.set_content(body, subtype="plain", charset="utf-8")
        message.add_alternative(html, subtype="html", charset="utf-8")
    else:
        message = email.message.EmailMessage()
        message["From"] = sender
        message["Subject"] = subject
        message.set_content(body, subtype="plain", charset="utf-8")
    return bytes(message)


def email_adapter(client: FakeImap, **kwargs) -> p.EmailImapInboundAdapter:
    """造一个注入了假 IMAP 的邮件入站适配器。"""

    def factory(host, port, timeout=None):  # noqa: ANN001 - 与 imaplib 的签名对齐
        client.host_arg = (host, port, timeout)
        return client

    return p.EmailImapInboundAdapter(
        host="imap.example",
        username="bot@example.com",
        password="secret",
        imap_factory=factory,
        **kwargs,
    )


# ── 1. 契约：两个适配器都满足 InboundAdapter ────────────────────────────────


def test_both_adapters_satisfy_the_inbound_protocol():
    """协议是 `runtime_checkable` 的：三个方法缺一个就不是入站适配器。"""
    irc, _sock = irc_adapter([])
    mail = email_adapter(FakeImap())
    assert isinstance(irc, p.InboundAdapter)
    assert isinstance(mail, p.InboundAdapter)
    for adapter in (irc, mail):
        assert callable(adapter.poll) and callable(adapter.ack) and callable(adapter.describe)
        text = adapter.describe()
        assert "requires:" in text and "install:" in text and "reason:" in text


# ── 2. IRC 入站：握手 / PRIVMSG / PING-PONG / 粘包 ──────────────────────────


def test_irc_handshake_sends_nick_user_join_before_anything_else():
    """注册行序是协议的一部分：NICK / USER 必须在 JOIN 之前，否则服务器拒掉。"""
    adapter, sock = irc_adapter([])
    assert adapter.poll() == []
    assert sock.lines()[:3] == ["NICK orbit", "USER orbit 0 * :Orbit Gateway", "JOIN #ops"]
    assert sock.address == ("irc.example", 6667)
    assert sock.closed is True, "poll 结束必须关连接（否则轮询几十轮就把句柄耗光）"


def test_irc_privmsg_is_parsed_into_an_inbound_message():
    adapter, _sock = irc_adapter(
        [":alice!user@host PRIVMSG #ops :hello 世界\r\n".encode()]
    )
    messages = adapter.poll()
    assert len(messages) == 1
    message = messages[0]
    assert message.platform == "irc"
    assert message.channel == "#ops"
    assert message.sender == "alice"
    assert message.text == "hello 世界"
    assert message.message_id, "没有 id 就没法 ack"
    assert message.received_at


def test_irc_answers_ping_with_pong_and_keeps_polling():
    """保活是入站的一部分：不回 PONG 会被服务器在几十秒内踢掉。"""
    adapter, sock = irc_adapter(
        [b"PING :keepalive-token\r\n:alice!u@h PRIVMSG #ops :after ping\r\n"]
    )
    messages = adapter.poll()
    assert adapter.pongs == ["keepalive-token"]
    assert "PONG :keepalive-token" in sock.lines()
    assert [m.text for m in messages] == ["after ping"], "PONG 之后的行必须继续被解析"


def test_irc_reassembles_messages_split_across_tcp_chunks():
    """TCP 不保证按行到达：半行 + 半行拼起来才是一条完整消息。"""
    adapter, _sock = irc_adapter(
        [b":bob!u@h PRIVMSG #ops :first ha", b"lf of the line\r\n:carol!u@h PRIVMSG #ops :second\r\n"]
    )
    messages = adapter.poll()
    assert [m.text for m in messages] == ["first half of the line", "second"]
    assert [m.sender for m in messages] == ["bob", "carol"]


def test_irc_ignores_noise_and_keeps_identical_texts_apart():
    """服务器欢迎语不是消息；同一句话连说两遍必须是**两条**（不能被内容去重吞掉）。"""
    adapter, _sock = irc_adapter(
        [
            b":server 001 orbit :Welcome\r\n"
            b":alice!u@h PRIVMSG #ops :same\r\n"
            b":alice!u@h PRIVMSG #ops :same\r\n"
        ]
    )
    messages = adapter.poll()
    assert [m.text for m in messages] == ["same", "same"]
    assert messages[0].message_id != messages[1].message_id


def test_irc_private_message_to_the_bot_is_treated_as_a_channel():
    """``PRIVMSG orbit :hi``（私聊机器人）也必须能收到，而不是被当成未知频道丢掉。"""
    adapter, _sock = irc_adapter([":dave!u@h PRIVMSG orbit :私聊一句\r\n".encode()])
    messages = adapter.poll()
    assert [m.channel for m in messages] == ["orbit"]
    assert [m.text for m in messages] == ["私聊一句"]


def test_irc_poll_without_config_raises_and_poll_all_turns_it_into_an_error():
    """未配置要**抛**（由 `poll_all` 折成 error），而不是静默返回空列表。"""
    bare = p.IrcInboundAdapter()
    assert bare.available() is False
    assert "IRC_HOST" in bare.describe()
    with pytest.raises(p.InboundUnavailable) as excinfo:
        bare.poll()
    assert "IRC_HOST" in str(excinfo.value)

    reports = p.poll_all([bare])
    assert reports[0].ok is False
    assert "IRC_HOST" in reports[0].error


def test_irc_ack_records_ids_locally_and_says_so():
    """IRC 没有投递确认：`ack` 只能本地记账，返回值是**真记下的条数**。"""
    adapter, _sock = irc_adapter([])
    assert adapter.ack(["#ops#1", "#ops#2", "  "]) == 2
    assert adapter.acked() == ("#ops#1", "#ops#2")


# ── 3. Email 入站：search UNSEEN / fetch / 解析 / STORE \Seen ────────────────


def test_email_poll_fetches_unseen_and_parses_headers_and_body():
    client = FakeImap(uids=["11"], messages={"11": raw_mail()})
    adapter = email_adapter(client)
    messages = adapter.poll()
    assert len(messages) == 1
    message = messages[0]
    assert message.platform == "email"
    assert message.channel == "INBOX"
    assert message.sender == "alice@example.com"
    assert message.subject == "测试主题"
    assert "这是一封测试邮件" in message.text
    assert message.message_id == "11", "id 必须是 IMAP 的 UID —— ack 要拿它去 STORE"
    # 搜索条件必须是 UNSEEN：把已读邮件重新投给 agent 会重复干活。
    assert ("search", None, "UNSEEN") in client.calls
    assert client.selected == "INBOX"
    assert client.closed is True


def test_email_prefers_plain_text_over_html():
    client = FakeImap(
        uids=["7"],
        messages={"7": raw_mail(body="纯文本正文", html="<p>HTML 正文</p>")},
    )
    messages = email_adapter(client).poll()
    assert "纯文本正文" in messages[0].text
    assert "HTML 正文" not in messages[0].text


def test_email_ack_marks_seen_and_counts_only_ok():
    client = FakeImap(store_status="OK")
    adapter = email_adapter(client)
    assert adapter.ack(["11", "12"]) == 2
    assert client.stored == [("11", "+FLAGS", "\\Seen"), ("12", "+FLAGS", "\\Seen")]

    failing = FakeImap(store_status="NO")
    assert email_adapter(failing).ack(["11"]) == 0, "NO 不能算确认成功"


def test_email_ack_with_no_ids_does_not_open_a_connection():
    """空 ack 不该连服务器：轮询循环里绝大多数轮次都没有新消息。"""
    client = FakeImap()
    adapter = email_adapter(client)
    assert adapter.ack([]) == 0
    assert client.calls == []


def test_email_bad_fetch_is_skipped_not_fatal():
    """一封读不出来不该让整批失败（真实邮箱里畸形邮件很常见）。"""
    client = FakeImap(uids=["1", "2"], messages={"2": raw_mail()}, fetch_status="NO")
    assert email_adapter(client).poll() == []


def test_email_poll_without_config_raises_with_the_missing_variables():
    bare = p.EmailImapInboundAdapter()
    assert bare.available() is False
    assert "EMAIL_IMAP_HOST" in bare.describe()
    with pytest.raises(p.InboundUnavailable) as excinfo:
        bare.poll()
    assert "EMAIL_IMAP_HOST" in str(excinfo.value)


# ── 4. poll_all：一个平台挂了不许带崩其它平台 ───────────────────────────────


def test_poll_all_isolates_failures_and_reports_every_platform():
    irc, _sock = irc_adapter([b":alice!u@h PRIVMSG #ops :hi\r\n"])
    mail = email_adapter(FakeImap(uids=["9"], messages={"9": raw_mail(body="邮件正文")}))

    class Broken:
        name = "broken"

        def poll(self, *, limit: int = 50):
            raise OSError("连接被重置")

        def ack(self, ids):  # pragma: no cover - 只用于满足协议
            return 0

        def describe(self) -> str:
            return "broken：不可用"

    reports = p.poll_all([irc, Broken(), mail])
    by_name = {report.platform: report for report in reports}
    assert by_name["broken"].ok is False
    assert "连接被重置" in by_name["broken"].error
    # 另外两个平台的消息**一条都不能丢**。
    assert [m.text for m in by_name["irc"].messages] == ["hi"]
    # 邮件正文保留原始换行（标准库 set_content 会补一个结尾换行），所以按 strip 比。
    assert [m.text.strip() for m in by_name["email"].messages] == ["邮件正文"]
    assert by_name["email"].as_dict()["count"] == 1


def test_inbound_adapters_only_builds_configured_ones():
    """按环境变量造适配器：没配的不造（否则每轮轮询都要为空配置付出一次失败）。"""
    assert p.inbound_adapters(env={}) == []

    built = p.inbound_adapters(
        env={
            "IRC_HOST": "irc.example",
            "IRC_CHANNEL": "#ops",
            "EMAIL_IMAP_HOST": "imap.example",
            "EMAIL_ADDRESS": "bot@example.com",
            "EMAIL_PASSWORD": "secret",
        }
    )
    assert [adapter.name for adapter in built] == ["irc", "email"]
    assert built[1].folder == "INBOX"


# ── 5. 诚实账：其余 20 个只有出站，且每条都写了缺哪一层 ─────────────────────


def test_inbound_report_lists_two_implemented_and_twenty_without_inbound():
    report = p.inbound_report()
    assert report["implemented"] == ["irc", "email"]
    assert len(report["only_outbound"]) == 20
    assert "poll_all" in str(report["entry"])
    missing = set(report["only_outbound"]) - set(report["reasons"])
    assert missing == set(), f"这些平台没有入站也没写原因：{sorted(missing)}"
    for name, reason in report["reasons"].items():
        assert len(str(reason).strip()) >= 10, f"{name} 的原因太短（等于没写）"
    # 三个连出站都没实现的平台也必须在账上（不能凭空消失）。
    for name in ("buzz", "photon", "simplex"):
        assert name in report["only_outbound"]


def test_platform_specs_no_longer_claim_irc_and_email_are_outbound_only():
    """声明表里的 `scope` 不能与实现相反：这两家现在**有**入站了。"""
    for name in ("irc", "email"):
        scope = p.PLATFORMS[p.Platform(name)].scope
        assert "入站" in scope, f"{name} 的 scope 还写着'仅出站'，与实现不符"
        assert not scope.startswith("仅出站")
    # 其它平台的 scope 仍然必须诚实地说"没有入站"。
    for platform, reason in p.INBOUND_MISSING.items():
        if platform in p.INBOUND_IMPLEMENTED:
            continue
        scope = p.PLATFORMS[platform].scope
        if not scope:
            continue
        assert "入站" in scope or reason, f"{platform} 既没写入站也没有原因"
