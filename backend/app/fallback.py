"""容错链（fallback chain）+ 凭证池（credential pool）。

这两件事在 Hermes 里是分开的两块（调研 §4.6 的 `fallback_model` 与 §3.4 的
`credential_pool`），但它们是**同一个问题的两个尺度**：

- 单个 provider 撞 429 → 换一把 key（凭证池）；
- 所有 key 都耗尽 / 整家挂了 → 换一家 provider（容错链）。

所以放在同一个模块里，共享同一套"什么错误值得重试"的判据（`is_failover_error`）。
判据只有一份，是因为两处一旦各写一份，就一定会出现"链会切、池不切"的错位。

## 设计红线

1. **白名单，不是黑名单**：只有 429 / 529 / 503 / 连接失败触发切换。401、400 之类
   一律原样抛出——换 provider 治不好写错的 key 或写错的参数，只会把真 bug 掩盖成
   "换个 provider 试试"。
2. **密钥只进系统 keyring**，元数据也进 keyring（理由见 `CredentialPool`）。
   任何路径都不打印明文，`PooledCredential` 里也没有明文字段。
3. **可离线单测**：模型工厂（`ModelFactory`）与密钥后端（`SecretStore`）都是注入的，
   测试用 fake 就够，不需要网络也不需要真的钥匙串。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

# ── 什么错误值得切换 ────────────────────────────────────────────────────────

#: 触发切换的 HTTP 状态码。
#:
#: 429 = rate limit，529 = overloaded（Anthropic 的过载码），503 = service
#: unavailable。取值直接照 `hermes fallback --help` 的原文
#: "rate-limit, overload, or connection errors"（调研 §5.4）。
FAILOVER_STATUS: frozenset[int] = frozenset({429, 529, 503})

#: 视为"连接失败"的异常类名。同时覆盖 httpx 与 openai SDK 两套命名：
#: 模型层既可能直接抛 httpx 异常（本仓库的轻量客户端），也可能抛 openai 的
#: `APIConnectionError`（`ChatOpenAI` 内部）。**按类名而不是按 isinstance 判断**，
#: 是为了不硬依赖 openai 包的类型（它只是 langchain-openai 的传递依赖）。
_CONNECTION_ERROR_NAMES = frozenset(
    {
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "ReadError",
        "WriteError",
        "RemoteProtocolError",
        "NetworkError",
        "APIConnectionError",
        "APITimeoutError",
    }
)


def status_code_of(exc: BaseException) -> int | None:
    """从各种异常形状里挖出 HTTP 状态码。

    三种形状都要认，因为异常来自三处不同代码：
    `httpx.HTTPStatusError.response.status_code`、`openai.APIStatusError.status_code`、
    以及测试里自造的假异常（直接挂 `status_code`）。
    """
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    return None


def is_failover_error(exc: BaseException) -> bool:
    """429 / 529 / 503 / 连接失败 → True，**其它一律 False**。

    注意"有状态码但不是白名单"必须先判掉：一个 `status_code=400` 的异常如果继续往
    连接错误那条分支走，会被某些自定义异常类名误伤（例如包一层就叫 `NetworkError`
    的 SDK 异常），结果是 400 也触发切换。
    """
    code = status_code_of(exc)
    if code is not None:
        return code in FAILOVER_STATUS
    if isinstance(exc, httpx.TransportError):
        return True
    return any(klass.__name__ in _CONNECTION_ERROR_NAMES for klass in type(exc).__mro__)


# ── 容错链 ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FallbackTarget:
    """链上的一站。`base_url` / `key_env` 对应 Hermes `fallback_model` 的两个可选字段
    （调研 §4.6）：自定义 OpenAI 兼容端点靠它们落地，标准 provider 留空即可。"""

    provider: str
    model: str = ""
    base_url: str = ""
    key_env: str = ""

    def label(self) -> str:
        return f"{self.provider}/{self.model}" if self.model else self.provider

    def to_config(self) -> dict[str, str]:
        data = {"provider": self.provider, "model": self.model}
        if self.base_url:
            data["base_url"] = self.base_url
        if self.key_env:
            data["key_env"] = self.key_env
        return data


def target_from_mapping(raw: object) -> FallbackTarget | None:
    """宽容解析一条链目。

    **坏条目静默丢弃而不是抛错**：链存在 `data/settings.json`，用户可能手改坏；
    一条坏目不该让整条命令（甚至整轮对话）失败。丢掉的条目会体现在 `list` 输出里
    的数量差上，用户看得见。
    """
    if not isinstance(raw, dict):
        return None
    provider = str(raw.get("provider", "")).strip()
    if not provider:
        return None
    return FallbackTarget(
        provider=provider,
        model=str(raw.get("model", "")).strip(),
        base_url=str(raw.get("base_url", "")).strip(),
        key_env=str(raw.get("key_env", "")).strip(),
    )


def targets_from_config(raw: object) -> list[FallbackTarget]:
    """把 settings.json 里的链（list[dict]）解析成目标列表。"""
    if not isinstance(raw, list):
        return []
    return [target for target in (target_from_mapping(item) for item in raw) if target]


def targets_to_config(targets: Sequence[FallbackTarget]) -> list[dict[str, str]]:
    return [target.to_config() for target in targets]


@dataclass(frozen=True)
class Attempt:
    """一次尝试的记录。`status` 三态：ok / failover（可切换）/ fatal（直接抛）。"""

    target: FallbackTarget
    status: str
    error: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "target": self.target.label(),
            "status": self.status,
            "error": self.error,
        }


class FallbackExhausted(RuntimeError):
    """整条链都失败。

    消息里必须带**每一次**尝试的原因：只说"全部失败"等于没报错，用户还是不知道是
    配额问题、网络问题还是配置问题。
    """

    def __init__(self, attempts: Sequence[Attempt]) -> None:
        self.attempts = list(attempts)
        detail = "；".join(
            f"{item.target.label()} → {item.status}"
            + (f"（{item.error}）" if item.error else "")
            for item in self.attempts
        )
        super().__init__(f"回退链全部失败（{len(self.attempts)} 次尝试）：{detail or '链为空'}")


#: 造一个可 `await model.ainvoke(messages)` 的客户端。注入点：测试给 fake，
#: 生产给 `lambda target: build_chat_model(...)`。
ModelFactory = Callable[[FallbackTarget], Any]


class FallbackChain:
    """按顺序尝试 provider/model，只在 429/529/503/连接失败时切换。

    `targets[0]` 是主模型，其余是回退项。链的构造与存储（settings.json）在 CLI 层，
    这里只管执行顺序与判据，因此可以完全离线单测。
    """

    def __init__(
        self,
        targets: Sequence[FallbackTarget],
        factory: ModelFactory,
        *,
        on_event: Callable[[Attempt], None] | None = None,
    ) -> None:
        self.targets = list(targets)
        self.factory = factory
        self.attempts: list[Attempt] = []
        #: 观测钩子。CLI 用它把切换写进日志；测试用它断言"到底试了几站"。
        self._on_event = on_event

    def _record(self, attempt: Attempt) -> None:
        self.attempts.append(attempt)
        if self._on_event is not None:
            self._on_event(attempt)

    async def ainvoke(self, messages: Sequence[Any], **kwargs: Any) -> Any:
        """按序尝试，返回第一个成功的结果。

        **只实现 `ainvoke`，不实现 `astream`**：流式一旦吐出首块，就没法"退回上一站"
        重新生成——用户会看到两段拼接起来的答案。Hermes 的 fallback 同样只管非流式
        路径。要做流式容错，得先有"首块之前的失败才可回退"的约定，那是另一个设计。
        """
        if not self.targets:
            raise ValueError("回退链为空：至少需要一个主模型目标")
        for target in self.targets:
            try:
                model = self.factory(target)
                result = await model.ainvoke(messages, **kwargs)
            except Exception as exc:  # noqa: BLE001 —— 判据决定要不要切换，不能只捕特定类型
                # 状态码要显式写进记录：`str(exc)` 常常只有 "rate limited"，
                # 没有码就没法区分"429 限流"和"503 挂了"，排障时会白跑一趟。
                code = status_code_of(exc)
                detail = f"{type(exc).__name__}" + (f"[{code}]" if code is not None else "")
                detail = f"{detail}: {exc}"
                if is_failover_error(exc):
                    self._record(Attempt(target, "failover", detail))
                    continue
                # 非白名单错误：立刻抛，保留原始异常类型与栈。
                self._record(Attempt(target, "fatal", detail))
                raise
            self._record(Attempt(target, "ok"))
            return result
        raise FallbackExhausted(self.attempts)


# ── 凭证池 ──────────────────────────────────────────────────────────────────


class SecretStore(Protocol):
    """密钥后端。只用三个动作，够表达 keyring 与内存实现。"""

    def get(self, account: str) -> str | None: ...

    def set(self, account: str, value: str) -> None: ...

    def delete(self, account: str) -> None: ...


class KeyringStore:
    """系统 keyring 后端。

    `service` 沿用 `ConnectorConfig` 的约定 `f"orbit:{data_dir}"`（见
    `app/connectors.py`），account 形如 `pool:<provider>:<index>`。

    **所有异常都收敛成"取不到"**：headless / 无桌面会话 / 后端拒绝（
    `KeyringError`）都发生过，而这些情况下正确的行为是"当作没有这把 key"并让上层
    报一条可读提示，而不是让 `orbit auth list` 直接栈崩。
    """

    def __init__(self, service: str) -> None:
        self.service = service

    def _backend(self) -> Any:
        import keyring

        return keyring

    def get(self, account: str) -> str | None:
        try:
            return self._backend().get_password(self.service, account)
        except Exception:  # noqa: BLE001 —— 见类 docstring
            return None

    def set(self, account: str, value: str) -> None:
        self._backend().set_password(self.service, account, value)

    def delete(self, account: str) -> None:
        try:
            self._backend().delete_password(self.service, account)
        except Exception:  # noqa: BLE001 —— 删不存在的条目各后端行为不一，都当成功
            return


class MemoryStore:
    """内存后端。

    **故意放在生产模块而不是 tests/**：`orbit doctor` 之类的自检需要一条"不碰系统
    钥匙串"的路径，测试也需要一个可断言的确定性后端。放 tests/ 里生产代码就用不上。
    """

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.data: dict[str, str] = dict(initial or {})

    def get(self, account: str) -> str | None:
        return self.data.get(account)

    def set(self, account: str, value: str) -> None:
        self.data[account] = value

    def delete(self, account: str) -> None:
        self.data.pop(account, None)


#: keyring account 前缀。**密钥与元数据分开前缀**，便于"只删密钥"或"只清状态"。
KEY_PREFIX = "pool:"
META_PREFIX = "pool-meta:"
#: 已知 provider 名册。keyring 没有"列出 service 下所有 account"的可移植接口，
#: 所以自己维护一份索引——没有它 `orbit auth list`（不带 provider）就没法枚举。
ROSTER_ACCOUNT = "pool-providers"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def fingerprint(secret: str) -> str:
    """密钥指纹：只留末 4 位。

    用途是"确认我配的是哪把钥匙"（对齐 Hermes `dump --show-keys` 的口径），
    **不足以复原**。空串与短 key 一律打成 `…***`，免得 3 位 key 被整条显示。
    """
    if len(secret) < 8:
        return "…***"
    return f"…{secret[-4:]}"


class CredentialPoolExhausted(RuntimeError):
    """该 provider 下所有 key 都不可用（耗尽或取不到值）。"""


@dataclass
class PooledCredential:
    """池里的一把 key 的**元数据**。刻意没有明文字段——它会被打印、被 JSON 化。"""

    provider: str
    index: int
    label: str = ""
    priority: int = 0
    last_status: str = ""
    last_error: str = ""
    last_error_at: str = ""
    request_count: int = 0
    fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "label": self.label,
            "priority": self.priority,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "request_count": self.request_count,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, provider: str, raw: dict[str, Any]) -> PooledCredential:
        return cls(
            provider=provider,
            index=int(raw.get("index", 0)),
            label=str(raw.get("label", "")),
            priority=int(raw.get("priority", 0)),
            last_status=str(raw.get("last_status", "")),
            last_error=str(raw.get("last_error", "")),
            last_error_at=str(raw.get("last_error_at", "")),
            request_count=int(raw.get("request_count", 0)),
            fingerprint=str(raw.get("fingerprint", "")),
        )


@dataclass
class CredentialLease:
    """一次租用：元数据 + 明文密钥。

    密钥放在 `repr=False` 的字段上，且类自己实现 `__repr__`——这样它出现在日志、
    pytest 断言消息、异常栈里都不会泄露明文。
    """

    credential: PooledCredential
    secret: str = field(repr=False)

    def __repr__(self) -> str:
        return f"CredentialLease({self.credential.provider}#{self.credential.index})"


class CredentialPool:
    """同一 provider 挂多把 key，按 priority 轮转，失败的标记耗尽并跳过。

    ## 为什么元数据也进 keyring

    密钥本体放 `pool:<provider>:<index>`，元数据（priority / label / last_status /
    request_count）放 `pool-meta:<provider>` 的一段 JSON，**两边都在 keyring**，
    不写任何配置文件。

    理由不只是"密钥别落盘"：如果把元数据写进 `data/settings.json`，就会有两个真相源。
    密钥被外部工具清掉而元数据还在时，列表里会出现"存在但取不到值"的幽灵条目，而那
    恰好最容易被误读成"429 还没恢复"。同库同事务语义（哪怕只是近似），才不会自相矛盾。

    ## 为什么是"轮转"而不是"永远用第一把"

    Hermes 的语义是"按 priority 排序后取第一个可用"。顺序取会让第一把 key 先把配额
    烧光，池就退化成了单 key。这里在**未耗尽的集合内做均匀轮转**，priority 只作为
    排序基准——多 key 的意义本来就是分摊配额。
    """

    def __init__(self, store: SecretStore, *, rotate: bool = True) -> None:
        self.store = store
        self.rotate = rotate
        self._cursor: dict[str, int] = {}

    # -- 元数据读写 -------------------------------------------------------
    def _load_meta(self, provider: str) -> list[PooledCredential]:
        raw = self.store.get(f"{META_PREFIX}{provider}")
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except ValueError:
            return []
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return []
        return [
            PooledCredential.from_dict(provider, item)
            for item in entries
            if isinstance(item, dict)
        ]

    def _save_meta(self, provider: str, entries: Sequence[PooledCredential]) -> None:
        payload = {"entries": [item.to_dict() for item in entries], "updated_at": _now()}
        self.store.set(f"{META_PREFIX}{provider}", json.dumps(payload, ensure_ascii=False))
        roster = self.known_providers()
        if entries and provider not in roster:
            self._save_roster([*roster, provider])
        elif not entries and provider in roster:
            self._save_roster([name for name in roster if name != provider])

    def _save_roster(self, providers: Sequence[str]) -> None:
        self.store.set(ROSTER_ACCOUNT, json.dumps(sorted(set(providers)), ensure_ascii=False))

    def known_providers(self) -> list[str]:
        """有池化凭证的 provider 名册（按字母序）。坏数据当空。"""
        raw = self.store.get(ROSTER_ACCOUNT)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(data, list):
            return []
        return [str(name) for name in data]

    # -- 增删查 -----------------------------------------------------------
    def entries(self, provider: str) -> list[PooledCredential]:
        """按 (priority, index) 排序的全部条目——耗尽的不隐藏，`last_status` 会显示。"""
        return sorted(self._load_meta(provider), key=lambda item: (item.priority, item.index))

    def add(
        self,
        provider: str,
        secret: str,
        *,
        label: str = "",
        priority: int | None = None,
    ) -> PooledCredential:
        """追加一把 key。index 取"现有最大 + 1"，删掉中间条目也不会撞号。"""
        secret = secret.strip()
        if not secret:
            raise ValueError("密钥不能为空")
        entries = self._load_meta(provider)
        index = max((item.index for item in entries), default=-1) + 1
        credential = PooledCredential(
            provider=provider,
            index=index,
            label=label.strip() or f"{provider}#{index}",
            priority=len(entries) if priority is None else priority,
            fingerprint=fingerprint(secret),
        )
        self.store.set(f"{KEY_PREFIX}{provider}:{index}", secret)
        self._save_meta(provider, [*entries, credential])
        return credential

    def acquire(self, provider: str) -> CredentialLease:
        """租一把可用的 key。

        跳过 `last_status` 非空的条目；取不到明文（keyring 里被删了）也当作不可用，
        并且**顺手标记耗尽**——否则每次调用都会重试同一个空槽，等于空转。
        """
        candidates = [item for item in self.entries(provider) if not item.last_status]
        if not candidates:
            raise CredentialPoolExhausted(
                f"{provider}：没有可用的池化凭证"
                f"（共 {len(self.entries(provider))} 条，全部耗尽或为空）。"
                f"用 `orbit auth reset {provider}` 清除耗尽状态，或 `orbit auth add {provider}` 追加。"
            )
        start = self._cursor.get(provider, 0) % len(candidates) if self.rotate else 0
        for offset in range(len(candidates)):
            credential = candidates[(start + offset) % len(candidates)]
            secret = self.store.get(f"{KEY_PREFIX}{provider}:{credential.index}")
            if secret:
                if self.rotate:
                    self._cursor[provider] = (start + offset + 1) % len(candidates)
                return CredentialLease(credential=credential, secret=secret)
            self.report_failure(
                provider, credential.index, status="missing", message="keyring 里取不到密钥"
            )
        raise CredentialPoolExhausted(f"{provider}：所有池化凭证都取不到密钥")

    # -- 状态 -------------------------------------------------------------
    def report_failure(
        self,
        provider: str,
        index: int,
        *,
        status: str = "exhausted",
        message: str = "",
    ) -> PooledCredential | None:
        """标记一把 key 不可用。`status` 用短词（exhausted / missing），便于筛选。"""
        entries = self._load_meta(provider)
        for item in entries:
            if item.index == index:
                item.last_status = status
                item.last_error = message
                item.last_error_at = _now()
                self._save_meta(provider, entries)
                return item
        return None

    def report_success(self, provider: str, index: int) -> PooledCredential | None:
        """成功一次：累计 `request_count`，并清掉耗尽标记。

        清标记是刻意的（Hermes 要 `auth reset` 才清）：一把 key 刚刚成功过，说明配额
        已经恢复，此时还留着 exhausted 只会让它被永久跳过。
        """
        entries = self._load_meta(provider)
        for item in entries:
            if item.index == index:
                item.request_count += 1
                item.last_status = ""
                item.last_error = ""
                item.last_error_at = ""
                self._save_meta(provider, entries)
                return item
        return None

    def reset(self, provider: str) -> int:
        """清除该 provider 全部条目的耗尽状态，返回被清掉的条目数。"""
        entries = self._load_meta(provider)
        cleared = 0
        for item in entries:
            if item.last_status:
                cleared += 1
            item.last_status = ""
            item.last_error = ""
            item.last_error_at = ""
        self._save_meta(provider, entries)
        self._cursor.pop(provider, None)
        return cleared

    def remove(self, provider: str, target: int | str) -> PooledCredential | None:
        """按 index（int 或数字串）或 label 删除一条。两样都对不上就返回 None。"""
        entries = self._load_meta(provider)
        victim: PooledCredential | None = None
        text = str(target).strip()
        for item in entries:
            if text == str(item.index) or (text and text == item.label):
                victim = item
                break
        if victim is None:
            return None
        self.store.delete(f"{KEY_PREFIX}{provider}:{victim.index}")
        self._save_meta(provider, [item for item in entries if item.index != victim.index])
        self._cursor.pop(provider, None)
        return victim

    def clear(self, provider: str) -> int:
        """删掉该 provider 的全部凭证（密钥 + 元数据），返回删除条数。"""
        entries = self._load_meta(provider)
        for item in entries:
            self.store.delete(f"{KEY_PREFIX}{provider}:{item.index}")
        self._save_meta(provider, [])
        self._cursor.pop(provider, None)
        return len(entries)


__all__ = [
    "FAILOVER_STATUS",
    "KEY_PREFIX",
    "META_PREFIX",
    "Attempt",
    "CredentialLease",
    "CredentialPool",
    "CredentialPoolExhausted",
    "FallbackChain",
    "FallbackExhausted",
    "FallbackTarget",
    "KeyringStore",
    "MemoryStore",
    "ModelFactory",
    "PooledCredential",
    "SecretStore",
    "fingerprint",
    "is_failover_error",
    "status_code_of",
    "target_from_mapping",
    "targets_from_config",
    "targets_to_config",
]
