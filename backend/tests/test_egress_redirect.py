"""egress 的两块收尾：**重定向链 + DNS rebinding**，以及密钥不进日志（MATRIX 4.8）。

`tests/test_egress.py` 已经钉住了三态判定、重定向逐跳再审、头注入与如实说明。这里补的是
它没有的那两块：

1. **域名和它解析到的地址是两件事**。允许 `api.example.com` 出站，不等于允许它此刻指向
   `127.0.0.1` 或云元数据端点 `169.254.169.254`。所以命中 allow 规则之后，解析出的每个
   IP 还要再过一遍策略。解析器（`socket.getaddrinfo`）是**注入点**：本机无网络，真去查
   DNS 的用例永远只能被跳过，等于没有。
2. **注入的头只记名字、不记值**。这一层存在的意义就是让密钥不出现在会被看到的地方，
   一次 `log.debug(headers)` 就能把整份努力抹掉，所以要用 caplog 正面钉住。

四条刻意的边界也各有用例：显式 IP 字面量不二次判定、`localhost` 不算 rebinding、
`default=allow` 的主机不解析、`allow_private_ips` 可以关掉这一层。
"""

from __future__ import annotations

import logging

import pytest

from app import egress

SECRET_VALUE = "sk-orbit-super-secret-value"


class _FakeResolver:
    """假密钥解析器（注入头用）。"""

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self.mapping = mapping or {}

    def resolve(self, key: str) -> str | None:
        return self.mapping.get(key)


class _FakeDNS:
    """假解析器：`host -> [ip, ...]`，并记下每个 host 被问了几次。"""

    def __init__(self, mapping: dict[str, list[str]]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def __call__(self, host: str) -> list[str]:
        self.calls.append(host)
        return self.mapping.get(host, [])


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


# ── 1. DNS rebinding：域名 allow，但解析到本机/内网 ────────────────────────


def test_allowed_domain_resolving_to_loopback_is_denied(data_dir):
    """这一条就是本次要补的那件事：按名字放行，按地址拒绝。"""
    policy = _policy(data_dir, allow=["api.example.com"], default=egress.DENY)
    dns = _FakeDNS({"api.example.com": ["127.0.0.1"]})
    decision = policy.check("https://api.example.com/v1", resolve=dns)
    assert decision.action == egress.DENY
    assert decision.host == "api.example.com"
    assert "DNS rebinding" in decision.reason
    assert "127.0.0.1" in decision.reason
    # 报错要给出出路，否则用户只知道"被拒了"却不知道能不能放行。
    assert "allow_private_ips" in decision.reason


def test_allowed_domain_resolving_to_cloud_metadata_is_denied(data_dir):
    policy = _policy(data_dir, allow=["cdn.example.com"], default=egress.DENY)
    dns = _FakeDNS({"cdn.example.com": ["169.254.169.254"]})
    decision = policy.check("https://cdn.example.com/latest/meta-data/", resolve=dns)
    assert decision.action == egress.DENY
    assert "169.254.169.254" in decision.reason


def test_private_range_resolution_is_denied_too(data_dir):
    policy = _policy(data_dir, allow=["intra.example.com"], default=egress.DENY)
    dns = _FakeDNS({"intra.example.com": ["10.0.0.5"]})
    assert policy.check("https://intra.example.com/x", resolve=dns).action == egress.DENY


def test_public_resolution_is_allowed_and_says_it_was_checked(data_dir):
    policy = _policy(data_dir, allow=["api.example.com"], default=egress.DENY)
    dns = _FakeDNS({"api.example.com": ["93.184.216.34"]})
    decision = policy.check("https://api.example.com/v1", resolve=dns)
    assert decision.action == egress.ALLOW
    # 说清"这一次真的看了 IP"，否则无法区分"检查通过"和"根本没检查"。
    assert "93.184.216.34" in decision.reason
    assert "通过 IP 策略" in decision.reason


def test_ip_literal_hosts_are_taken_at_face_value(data_dir):
    """用户自己写下 `127.0.0.1` 是显式意图，不是 rebinding——不该被二次判死。"""
    policy = _policy(data_dir, allow=["127.0.0.1"], default=egress.DENY)
    dns = _FakeDNS({"127.0.0.1": ["127.0.0.1"]})
    assert policy.check("http://127.0.0.1:11434/v1", resolve=dns).action == egress.ALLOW
    assert dns.calls == [], "IP 字面量不该再去解析"


def test_localhost_is_not_treated_as_rebinding(data_dir):
    policy = _policy(data_dir, allow=["localhost"], default=egress.DENY)
    dns = _FakeDNS({"localhost": ["127.0.0.1", "::1"]})
    assert policy.check("http://localhost:8645/v1", resolve=dns).action == egress.ALLOW


def test_default_allow_hosts_are_not_resolved(data_dir):
    """走 `default=allow` 的主机不解析。

    那一档本来就是"全部放行"，再对它加一道只会误伤内网用法（`curl http://nas.lan`），
    而收益为零：策略作者根本没对那个名字表过态。
    """
    policy = _policy(data_dir)  # default=allow、没有任何规则
    dns = _FakeDNS({"nas.lan": ["192.168.1.5"]})
    decision = policy.check("https://nas.lan/share", resolve=dns)
    assert decision.action == egress.ALLOW
    assert dns.calls == []


def test_allow_private_ips_turns_the_layer_off(data_dir):
    policy = _policy(data_dir, allow=["dev.example.com"], default=egress.DENY)
    policy.allow_private_ips = True
    dns = _FakeDNS({"dev.example.com": ["127.0.0.1"]})
    assert policy.check("https://dev.example.com/x", resolve=dns).action == egress.ALLOW


def test_resolution_failure_neither_passes_silently_nor_blocks(data_dir):
    """解析不出来时：放行（离线环境里必然如此），但**必须在 reason 里写明没做 IP 校验**。"""
    policy = _policy(data_dir, allow=["api.example.com"], default=egress.DENY)
    decision = policy.check("https://api.example.com/v1", resolve=_FakeDNS({}))
    assert decision.action == egress.ALLOW
    assert "未做 IP 校验" in decision.reason


def test_broken_resolver_does_not_crash_the_gate(data_dir):
    def resolver(host: str) -> list[str]:
        raise RuntimeError("解析器坏了")

    policy = _policy(data_dir, allow=["api.example.com"], default=egress.DENY)
    decision = policy.check("https://api.example.com/v1", resolve=resolver)
    assert decision.action == egress.ALLOW
    assert "未做 IP 校验" in decision.reason


def test_resolution_is_cached_per_host(data_dir):
    """一次 `curl -L` 会对同一批主机反复判定；每次都查 DNS 又慢又不稳定。"""
    policy = _policy(data_dir, allow=["api.example.com"], default=egress.DENY)
    dns = _FakeDNS({"api.example.com": ["93.184.216.34"]})
    for _ in range(3):
        assert policy.check("https://api.example.com/v1", resolve=dns).action == egress.ALLOW
    assert dns.calls == ["api.example.com"]


def test_redirect_hop_that_rebinds_is_refused(data_dir):
    """第二跳是"名字 allow、地址是内网"——重定向链上同样要拦。"""
    policy = _policy(
        data_dir,
        allow=["ok.example", "hop.example"],
        default=egress.DENY,
    )
    dns = _FakeDNS({"ok.example": ["93.184.216.34"], "hop.example": ["10.1.2.3"]})
    decision = policy.check(
        "https://ok.example/x", redirects=["https://hop.example/collect"], resolve=dns
    )
    assert decision.action == egress.DENY
    assert decision.host == "hop.example"
    assert "第 1 跳" in decision.reason
    assert "DNS rebinding" in decision.reason


def test_rebinding_does_not_change_deny_rule_behaviour(data_dir):
    """deny 规则命中时不再解析（已经拒了，查 DNS 只会白白慢一拍）。"""
    policy = _policy(data_dir, deny=["evil.example"], default=egress.ALLOW)
    dns = _FakeDNS({"evil.example": ["93.184.216.34"]})
    decision = policy.check("https://evil.example/x", resolve=dns)
    assert decision.action == egress.DENY
    assert dns.calls == []


def test_special_ip_classification_covers_the_dangerous_ranges():
    for address in (
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "0.0.0.0",
        "::1",
        "fe80::1",
        "::ffff:127.0.0.1",
    ):
        assert egress.is_special_ip(address) is True, address
    for address in ("93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"):
        assert egress.is_special_ip(address) is False, address
    assert egress.is_special_ip("not-an-ip") is False


def test_allow_private_ips_round_trips_through_the_policy_file(data_dir):
    policy = _policy(data_dir, allow=["dev.example.com"], allow_private_ips=True)
    path = policy.save()
    reloaded = egress.EgressPolicy.load(data_dir)
    assert reloaded.allow_private_ips is True
    assert "allow_private_ips" in path.read_text(encoding="utf-8")
    # 默认必须是关着（= 防护生效），否则"引入这一层"就变成了"默认不防护"。
    assert egress.EgressPolicy().allow_private_ips is False


# ── 2. 注入的头：只记名字，不记值 ──────────────────────────────────────────


def test_inject_headers_never_writes_the_secret_to_the_log(data_dir, caplog):
    policy = _policy(data_dir)
    policy.credentials = [
        egress.CredentialRule(host="api.anthropic.com", vendor="anthropic", key="ANTHROPIC_KEY")
    ]
    with caplog.at_level(logging.DEBUG, logger="orbit.egress"):
        merged = policy.inject_headers(
            "https://api.anthropic.com/v1/messages",
            {},
            resolver=_FakeResolver({"ANTHROPIC_KEY": SECRET_VALUE}),
        )
    assert merged["x-api-key"] == SECRET_VALUE, "值必须真的注入（否则这条用例是空转）"
    assert SECRET_VALUE not in caplog.text, "密钥绝不能进日志"
    # 但"注入了哪些头"要留痕，否则排障时看不出是规则没命中还是密钥取不到。
    assert "x-api-key" in caplog.text


def test_rebinding_denial_is_logged_without_any_credential(data_dir, caplog):
    policy = _policy(data_dir, allow=["api.example.com"], default=egress.DENY)
    policy.credentials = [
        egress.CredentialRule(host="api.example.com", key="API_KEY", vendor="openai")
    ]
    dns = _FakeDNS({"api.example.com": ["127.0.0.1"]})
    with caplog.at_level(logging.DEBUG, logger="orbit.egress"):
        decision = policy.check("https://api.example.com/v1", resolve=dns)
        policy.inject_headers(
            "https://api.example.com/v1", {}, resolver=_FakeResolver({"API_KEY": SECRET_VALUE})
        )
    assert decision.action == egress.DENY
    assert SECRET_VALUE not in caplog.text


# ── 3. 如实说明：最小可行形态写清了路怎么走 ────────────────────────────────


def test_describe_lists_the_minimal_viable_shape(data_dir):
    text = _policy(data_dir, allow=["api.example.com"]).describe()
    assert "没有代理进程" in text and "iron-proxy" in text
    # 四块都要有：独立进程 / 自签 CA / 令牌铸造 / 强制出站改道。
    for word in ("独立进程", "自签 CA", "令牌铸造", "强制出站改道"):
        assert word in text, word
    # 每一块都要给出工作量级与验收标准，而不是只有名词。
    assert "人日" in text
    assert "验收标准" in text


def test_describe_reports_the_rebinding_layer_state(data_dir):
    assert "DNS rebinding" in _policy(data_dir).describe()
    permissive = _policy(data_dir, allow_private_ips=True)
    assert "已关闭" in permissive.describe()
