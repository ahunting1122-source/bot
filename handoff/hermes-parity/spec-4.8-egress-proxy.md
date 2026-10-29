# `4.8` 的实现规格：`app/egress_proxy.py`

**这份文档是可执行的规格，不是设计散文。** 它把「下一轮该写什么」写到不用再探路。

## 为什么是这份文档而不是代码

第 81 轮查明：**策略层已全部建好且有测试**（`EgressPolicy` / `CredentialRule` /
`inject_headers` / `injection_plan` / `refuse_cloud_metadata`），
**缺的是一个调用它们的守护进程**（`inject_headers` 在全仓零消费者）。

**本轮剩余上下文不足以把安全敏感功能做完整并验证** ——
**半成品的凭证代理比没有更糟**：它会把「真凭证留在代理里」这件事做成假的。

## 已核实的 API（照抄即可，不用再查）

```python
EgressPolicy.check(urls: Sequence[str], *, resolve=None) -> EgressDecision
    # EgressDecision 有 .allowed / .reason / .rule / .as_dict()

EgressPolicy.inject_headers(
    url: str, headers: dict[str, str] | None = None, *, resolver=None
) -> dict[str, str]
    # 「按 host 规则注入请求头，返回**新的** dict（不改入参）。」

EgressPolicy.injection_plan(url: str, *, resolver=None, detailed: bool = False) -> list[dict]
    # 诊断用：这条 URL 会注入哪些头、值从哪个键取、此刻取不取得到。

egress.refuse_cloud_metadata(url: str, *, where: str = "出站请求") -> None
    # 命中云元数据端点时抛 ValueError。
```

## 要写的东西（顺序即依赖顺序）

### ① `app/egress_proxy.py` —— 一个最小的正向代理

```python
class EgressProxy:
    """把 `EgressPolicy` 应用到真实出站流量的最小守护进程。

    **它只做三件事，每件都调用已有函数，不重新实现任何判断逻辑**：
    1. `refuse_cloud_metadata(url)` —— 云元数据硬拦（与其余出站点同一条规则）；
    2. `policy.check([url])` —— 不许就 403，`reason` 原样回给调用方；
    3. `policy.inject_headers(url, headers)` —— 把占位符换成真凭证。
    """
```

**接线方式（关键：不要自己造判断）**：

| 步骤 | 调用 | 失败时 |
|---|---|---|
| 解析目标 | `urllib.parse.urlsplit(target)` | 400 |
| 云元数据 | `refuse_cloud_metadata(target)` | 403 + 原文 |
| 策略裁决 | `policy.check([target]).allowed` | 403 + `decision.reason` |
| 凭证注入 | `policy.inject_headers(target, incoming)` | 500（**不许降级为「不注入就发」**） |
| 转发 | `httpx.Client(...).stream(...)` | 502 |

**必须用 `inject_headers` 的返回值**，且**不许**把入参的占位符原样透传 ——
**「注入失败就原样发」等于把占位符当凭证发出去，那是最坏的一种失败。**

### ② 占位符约定

进程只拿到 `ORBIT_PROXY_TOKEN=<随机串>`；代理把 `Authorization: Bearer <该串>` 换成真 key。
**`EgressPolicy.inject_headers` 已经实现了「密钥缺失时绝不注入占位符」**（有测试），
所以这里不需要再判一次 —— **但要把「代理启动时没有可用凭证」变成启动期报错，而不是请求期静默。**

### ③ 测试（全部用本地 HTTP server，不依赖外网）

| 测试 | 断言 |
|---|---|
| 真 key 到达上游 | 上游 echo server 收到的 `Authorization` **等于真 key**、**不等于占位符** |
| 被禁 host | 返回 403，且**上游一次都没被连上**（用计数器断言） |
| 云元数据 | `http://169.254.169.254/...` → 403，**上游零连接** |
| 注入失败不降级 | 密钥缺失时**不许**发出去（断言上游零连接，而不是「发了但没头」） |
| 无匹配规则 | 头原样转发（这是允许的：没有规则就没有要注入的） |

**「上游零连接」这个断言是本组测试的核心** ——
只断言「返回了 403」不够：**403 可能是先转发出去、拿到响应再改成 403。**

### ④ 接线到 CLI（`egress start/stop/status`）

现在这三条是 `run_proxy_unavailable` 的桩。接线后：
`start` 起线程、写 pid 文件到 `<data>/egress/proxy.pid`；`stop` 读 pid 发信号；
`status` 报「在跑 / 不在跑 + 端口 + 当前策略条数」。

## 完成标准（可复核）

1. `python -m pytest tests/test_egress_proxy.py -q` 全绿，**且含「上游零连接」断言**；
2. `orbit egress start` 起得来、`orbit egress status` 报在跑、`orbit egress stop` 停得掉；
3. **手动一次端到端**：设一个假 provider base_url 指向本地 echo server，
   通过代理发一次请求，**打印上游收到的头**，确认是真 key；
4. MATRIX `4.8` 从 🟡 改 ✅，证据栏写上面 1–3 的实跑输出。

## 判据

**「建好了但没接线」是一类独立的欠账 —— 它的成本不是「重写」，是「接线 + 证明接对了」。
而证明接对了的标准不是「返回 200」，是「上游收到的凭证是哪一个」。**
