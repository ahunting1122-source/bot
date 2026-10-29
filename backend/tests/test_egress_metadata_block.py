"""egress 的两个安全洞（第 51 轮修，MATRIX 4.8 邻域）。

## 实测发现的洞

```
allow  | http://169.254.169.254/latest/meta-data  ← 按 default=allow 放行
allow  | http://127.0.0.1:8080/admin
allow  | http://10.0.0.5/internal
allow  | http://metadata.google.internal/x
```

**两个独立缺陷**：
1. **云元数据端点没有硬拦**——而 `app/path_security.py` 里早就有 `BLOCKED_IPS`
   与 `BLOCKED_HOSTNAMES`。**又一份平行实现，安全的那份没接上**
   （与 14.8 同族：URL 侧的那一半一直没接）。
2. **`default=allow` 绕过了 IP 闸**——`_guard_resolved_ip` 只挂在「命中 allow 规则」
   那条路上。**一个解析到内网的域名，只要没写规则就能出站。**
"""

from __future__ import annotations

import pytest

from app.egress import ALLOW, DENY, EgressPolicy


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",  # AWS / Azure / GCP 通用
        "http://169.254.170.2/creds",  # ECS 任务凭证
        "http://169.254.169.253/x",  # Azure IMDS
        "http://100.100.100.200/latest/meta-data",  # 阿里云
    ],
)
def test_cloud_metadata_ips_are_denied_without_any_rule(url):
    """**云元数据地址一律拒绝，不需要用户先写 deny 规则。**

    SSRF 取凭证的经典目标。`path_security.BLOCKED_IPS` 早就有这份名单，
    只是 egress 这侧从没查过。
    """
    decision = EgressPolicy().check(url)
    assert decision.action == DENY, f"{url} 被判成 {decision.action}"
    assert decision.rule == "cloud-metadata", decision.rule


@pytest.mark.parametrize(
    "url",
    [
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://instance-data/latest",
    ],
)
def test_cloud_metadata_hostnames_are_denied(url):
    decision = EgressPolicy().check(url)
    assert decision.action == DENY, f"{url} 被判成 {decision.action}"
    assert decision.rule == "cloud-metadata"


def test_an_allow_rule_cannot_whitelist_cloud_metadata():
    """**显式 allow 也不行**——没有正当用途，也就没有例外。

    这条防的是「有人为了排查问题先加条 allow，然后忘了删」。
    """
    policy = EgressPolicy(allow=("169.254.169.254",))
    decision = policy.check("http://169.254.169.254/latest")
    assert decision.action == DENY, "allow 规则把云元数据放行了"


def test_loopback_is_still_allowed_by_default():
    """**私网/回环不该被硬拦**：本地模型 provider（Ollama 默认 `127.0.0.1:11434`）
    和本地 gateway 都有正当用途，一刀切会打死「配一个 provider 就能用」。"""
    decision = EgressPolicy().check("http://127.0.0.1:11434/api/tags")
    assert decision.action == ALLOW, decision.reason


def test_the_default_allow_path_deliberately_skips_the_ip_guard():
    """**`default=allow` 不做 IP 校验是刻意设计**，不是洞。

    `_guard_resolved_ip` 的 docstring 列了四条跳过条件，其中一条就是
    「走 `default=allow`（没有命中任何规则）不查」——理由是**没配策略时
    不该每次出站都真查 DNS**。

    **第 51 轮我曾把它当「第二个洞」去打补丁，那是误判**：它是有名字、有位置、
    有理由的设计决定。这条测试把**现状**钉住，免得下一个人（包括我）
    再把它当 bug 改一遍。

    **判据：把「我没读到的设计」当成「洞」，与把「我没读到的实现」当成「不存在」
    是同一类错误。**
    """
    policy = EgressPolicy()  # 没有任何规则 → 走 default=allow 那条路
    decision = policy.check(
        "https://no-rule-matches.example/x", resolve=lambda _host: ["10.0.0.5"]
    )
    assert decision.action == ALLOW, (
        "行为变了——如果这是有意改的，请同时更新 `_guard_resolved_ip` 的 docstring"
        "里那四条跳过条件"
    )


def test_an_explicit_allow_rule_does_run_the_ip_guard():
    """**有规则时 IP 闸是生效的**——这才是 DNS rebinding 防护的作用面。

    与上一条对照：区别不在「主机名解析到内网」，而在**有没有命中规则**。
    """
    policy = EgressPolicy(allow=("evil.example",))
    decision = policy.check("https://evil.example/x", resolve=lambda _host: ["10.0.0.5"])
    assert decision.action == DENY, f"命中 allow 却解析到内网，仍被判 {decision.action}"
