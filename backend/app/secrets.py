"""外部密钥源：把"密钥从哪来"从"密钥怎么用"里拆出来。

## 为什么需要一层抽象

Orbit 现在有四处各自读密钥的地方：`Context.hydrate()` 读 keyring、
`providers.py` 读 `Settings`/环境变量、`mcp.py` 读 `keyring:mcp:<server>:<name>`、
`connectors.py` 读 keyring。四处都是"能读到就用，读不到就当没配置"，于是：

- 没人能回答"这个键**从哪来的**"（排查时最常问的一句）；
- 同名密钥在 keyring 与 `.env` 里各有一份时，**低优先级的那份会静默失效**，
  用户改了 `.env` 却不生效，且没有任何提示。

本模块把"源"做成对象（`name` / `available()` / `get()` / `list_keys()` /
`describe()`），由一个 :class:`SecretResolver` 按**固定优先级**取值，并把被遮蔽的
源记进 `shadowed`——"你改的那份没生效，因为 keyring 里有一把更高优先级的"这句话
必须能被说出来。

## 四个源与优先级

优先级 = `sources` 列表顺序，默认：

1. **keyring**（`service = f"orbit:{data_dir}"`，与 `Context.hydrate()` /
   `mcp.py` / `connectors.py` 既有约定一致）。系统级加密存储，是唯一"默认就该用"的源。
2. **env**（`ORBIT_*` 环境变量）。只读——进程环境不该被 Orbit 改写。
3. **file**（`<data_dir>/secrets.json`）。**明文**，`describe()` 里明说，文件里也写
   `_warning`。不假装它是加密的：真加密需要 `cryptography`，本机没装，
   假装加密比明文更危险（人会因此把更敏感的东西放进去）。
4. **外部源**（Bitwarden / 1Password CLI）。只做 `status`；取值路径
   `NotImplementedError`——解锁需要联网与交互式会话，本轮不做。

## 为什么 keyring 的 `list_keys()` 要靠一个索引文件

keyring 没有"列出某个 service 下所有 account"的可移植接口（`fallback.py` 里
`KNOWN_PROVIDERS` 的注释已经记过这件事）。所以 `set`/`remove` 顺手把**键名**
（不含值）写进 `<data_dir>/secrets.index.json`，`list_keys()` 读它。索引丢了不影响
取值，只影响"列清单"——如实写在 `describe()` 里，而不是假装 keyring 能枚举。
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.redact import is_secret_name

#: keyring service 的命名约定。**与 `Context.hydrate()` / `mcp.py` / `connectors.py`
#: 保持一致**：`f"orbit:{data_dir.resolve()}"`。换写法等于让已有密钥全部失联。
SERVICE_PREFIX = "orbit:"

#: 环境变量源的前缀。
ENV_PREFIX = "ORBIT_"

#: 明文密钥文件的文件名（相对 `data_dir`）。
FILE_NAME = "secrets.json"

#: keyring 键名索引（只有键名，没有值）。
INDEX_NAME = "secrets.index.json"

#: 明文文件里固定写入的警示。放在文件里而不是只放文档里：拿到这个文件的人
#: 未必读过 Orbit 的文档，但一定会打开这个文件。
FILE_WARNING = (
    "本文件以明文保存密钥，未加密。任何能读这个文件的进程、备份、同步盘都能拿到明文。"
    "优先用系统 keyring（`orbit secrets set <键>`）；"
    "真加密需要 `cryptography` 包，本机未安装，Orbit 不假装这里是加密的。"
)


def service_name(data_dir: Path) -> str:
    """keyring 的 service 名。集中一处，避免四处各写一遍写歪。"""
    return f"{SERVICE_PREFIX}{Path(data_dir).resolve()}"


@runtime_checkable
class SecretSource(Protocol):
    """一个密钥源。只读能力，够表达"取值 / 列键 / 能不能用"。"""

    #: 稳定标识，写进 `shadowed` 与 CLI 输出。
    name: str

    def available(self) -> bool:
        """这个源当前能不能提供值（keyring 后端可用 / CLI 已安装 / 文件可读）。"""
        ...

    def get(self, key: str) -> str | None:
        """取值。取不到返回 None——**不要**用空串表示"没有"，两者语义不同。"""
        ...

    def list_keys(self) -> list[str]:
        """能列出的键名（**绝不含值**）。列不出来就返回空列表并说明原因。"""
        ...

    def describe(self) -> str:
        """一句话说明：这是什么、有什么坑、缺什么。给 `orbit secrets status` 用。"""
        ...


@runtime_checkable
class WritableSecretSource(SecretSource, Protocol):
    """可写入的源。`EnvSource` 刻意不实现它（环境变量只读）。"""

    def set(self, key: str, value: str) -> None: ...

    def remove(self, key: str) -> bool: ...


class KeyringSource:
    """系统 keyring。默认源，也是唯一"默认就该用"的源。

    `backend` 可注入是为了测试：真跑测试会往 Windows 凭据管理器里写东西，
    那是**用户机器的真实副作用**，测试不该碰。
    """

    name = "keyring"

    def __init__(self, data_dir: Path, *, backend: Any | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.service = service_name(self.data_dir)
        self._backend = backend

    @property
    def backend(self) -> Any:
        if self._backend is None:
            import keyring  # 延迟导入：无桌面会话的环境里导入本身就可能很慢/失败

            self._backend = keyring
        return self._backend

    @property
    def index_path(self) -> Path:
        return self.data_dir / INDEX_NAME

    def available(self) -> bool:
        # 探一次读：能读不存在的键（返回 None）说明后端活着；抛异常说明后端不可用
        # （无桌面会话 / 没有可用的 keyring 实现）。写探测会留下垃圾条目，所以用读。
        try:
            self.backend.get_password(self.service, "__orbit_probe__")
        except Exception:
            return False
        return True

    def get(self, key: str) -> str | None:
        try:
            value = self.backend.get_password(self.service, key)
        except Exception:
            # keyring 的错误类型各平台不同（KeyringError / WinError / DBusError），
            # 对调用方来说都是"取不到"，不必分类。
            return None
        return str(value) if value else None

    def list_keys(self) -> list[str]:
        return sorted(self._read_index())

    def set(self, key: str, value: str) -> None:
        self.backend.set_password(self.service, key, value)
        self._remember(key)

    def remove(self, key: str) -> bool:
        try:
            self.backend.delete_password(self.service, key)
        except Exception:
            # 条目本来就不存在时各后端行为不一（有的抛 PasswordDeleteError）。
            existed = False
        else:
            existed = True
        self._forget(key)
        return existed

    def describe(self) -> str:
        state = "可用" if self.available() else "不可用（没有可用的密钥后端，如无桌面会话）"
        return (
            f"系统 keyring（service = {self.service}）：{state}；"
            f"密钥不落任何配置文件。键名清单来自 {INDEX_NAME}——keyring 本身没有"
            f"可移植的枚举接口，索引缺失只影响「列清单」，不影响取值。"
        )

    # ── 键名索引 ─────────────────────────────────────────────────────────────
    def _read_index(self) -> list[str]:
        if not self.index_path.exists():
            return []
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        keys = data.get("keys") if isinstance(data, dict) else None
        return [str(item) for item in keys or [] if str(item).strip()]

    def _write_index(self, keys: Sequence[str]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = {"_note": "只有键名，没有值。用于 keyring 的键名枚举。", "keys": sorted(set(keys))}
        self.index_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _remember(self, key: str) -> None:
        keys = self._read_index()
        if key not in keys:
            keys.append(key)
            self._write_index(keys)

    def _forget(self, key: str) -> None:
        keys = self._read_index()
        if key in keys:
            self._write_index([item for item in keys if item != key])


class EnvSource:
    """`ORBIT_*` 环境变量。**只读**——进程环境不该被一个 CLI 命令改写。"""

    name = "env"

    def __init__(
        self,
        prefix: str = ENV_PREFIX,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.prefix = prefix
        self._environ = environ

    @property
    def environ(self) -> Mapping[str, str]:
        return os.environ if self._environ is None else self._environ

    def available(self) -> bool:
        # 环境变量永远"可用"，只是可能一个都没设。可用性描述的是"这个源能不能被问"。
        return True

    def get(self, key: str) -> str | None:
        # 先按 Orbit 的约定找 `ORBIT_<KEY>`，再退回裸大写名（`OPENAI_API_KEY` 这种
        # 是各家 SDK 自己的约定，用户往往两边都设了，读不到会误报"未配置"）。
        for candidate in (f"{self.prefix}{key.upper()}", key.upper()):
            value = self.environ.get(candidate)
            if value:
                return str(value)
        return None

    def list_keys(self) -> list[str]:
        keys = []
        for name in self.environ:
            if not name.startswith(self.prefix):
                continue
            bare = name[len(self.prefix) :]
            # 只列"看起来是密钥"的键：`ORBIT_DATA_DIR` 不是密钥，列出来只是噪声。
            if is_secret_name(bare):
                keys.append(bare.lower())
        return sorted(set(keys))

    def describe(self) -> str:
        return (
            f"环境变量（{self.prefix}* 与裸大写名）：只读；"
            f"改了要重启进程才生效（环境在进程启动时读取）。"
        )


class FileSource:
    """`<data_dir>/secrets.json`。**明文**存储，`describe()` 与文件内都明说。"""

    name = "file"

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)

    @property
    def path(self) -> Path:
        return self.data_dir / FILE_NAME

    def available(self) -> bool:
        # 文件不存在也算可用：`set()` 会创建它。可用性描述的是"这个源能不能被问"。
        return True

    def get(self, key: str) -> str | None:
        value = self._read().get(key)
        return str(value) if value else None

    def list_keys(self) -> list[str]:
        return sorted(self._read())

    def set(self, key: str, value: str) -> None:
        data = self._read()
        data[key] = value
        self._write(data)

    def remove(self, key: str) -> bool:
        data = self._read()
        if key not in data:
            return False
        data.pop(key, None)
        self._write(data)
        return True

    def describe(self) -> str:
        state = f"已存在（{len(self._read())} 个键）" if self.path.exists() else "尚未创建"
        return (
            f"明文 JSON 文件（{self.path}）：{state}；"
            f"**未加密**——能读这个文件的进程 / 备份 / 同步盘都能拿到明文。"
            f"真加密需要 `cryptography` 包（本机未安装），Orbit 不假装它是加密的。"
            f"优先用系统 keyring。"
        )

    # ── 读写 ────────────────────────────────────────────────────────────────
    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        # 下划线开头的是元数据（`_warning` / `_updated_at`），不是密钥。
        return {
            str(key): str(value)
            for key, value in data.items()
            if not str(key).startswith("_")
        }

    def _write(self, data: dict[str, str]) -> None:
        from datetime import UTC, datetime

        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "_warning": FILE_WARNING,
            "_updated_at": datetime.now(UTC).isoformat(),
        }
        payload.update({key: data[key] for key in sorted(data)})
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # 尽量收紧权限。Windows 上 chmod 只影响只读位，**不等于**加密或 ACL 隔离，
        # 这里做的是"能做的部分"，不做任何额外承诺。
        try:
            self.path.chmod(0o600)
        except OSError:
            pass


class _CliSource:
    """外部 CLI 密钥源的共同骨架：只检测二进制、只报状态，不取值。

    **为什么不实现取值**：`bws` / `op` 都要先联网解锁（交互式会话或服务账号令牌），
    而本轮环境没有网络、也不该把用户的解锁会话拉进一个 CLI 命令里。假装实现
    （返回 None 或空串）比抛错更糟：用户会以为"密钥没配"而不是"这条路还没通"。
    """

    name = ""
    #: 可能出现的二进制名，按优先级排（Bitwarden 的 Secrets Manager CLI 叫 `bws`，
    #: 老的通用 CLI 叫 `bw`，两个都认）。
    binaries: tuple[str, ...] = ()
    #: 安装提示。`available() = False` 时由 `describe()` 原样给出。
    install_hint = ""

    def __init__(self, *, which: Any | None = None) -> None:
        self._which = which or shutil.which

    def binary(self) -> str | None:
        for name in self.binaries:
            path = self._which(name)
            if path:
                return str(path)
        return None

    def available(self) -> bool:
        return self.binary() is not None

    def get(self, key: str) -> str | None:
        raise NotImplementedError(
            f"{self.name} 取值路径未实现：需要联网解锁（交互式会话或服务账号令牌），"
            f"本轮不做。用 `orbit secrets status` 看二进制是否就位。"
        )

    def list_keys(self) -> list[str]:
        raise NotImplementedError(
            f"{self.name} 的键名枚举未实现：需要联网解锁，本轮不做。"
        )

    def status(self) -> dict[str, Any]:
        """只报"装没装 / 配没配"。这是本轮**唯一**实现的路径。"""
        return {
            "source": self.name,
            "available": self.available(),
            "binary": self.binary() or "",
            "read_path": "未实现（需要联网解锁，本轮不做）",
            "install_hint": self.install_hint,
        }

    def describe(self) -> str:
        binary = self.binary()
        if binary is None:
            return (
                f"{self.name}：未安装（找不到 {' / '.join(self.binaries)}）。"
                f"安装：{self.install_hint} 装好后 `orbit secrets status` 会看到它。"
            )
        return (
            f"{self.name}：已安装（{binary}），但**取值路径未实现**——"
            f"解锁需要联网与交互式会话，本轮不做。"
        )


class BitwardenSource(_CliSource):
    """Bitwarden Secrets Manager。Hermes 用 `bws`（v2.0.0）拉 `BWS_ACCESS_TOKEN`。"""

    name = "bitwarden"
    binaries = ("bws", "bw")
    install_hint = "npm i -g @bitwarden/cli（`bw`）或从 Bitwarden 官方发布页取 `bws` 二进制；"


class OnePasswordSource(_CliSource):
    """1Password CLI（`op`）。Hermes 用 `op://` 引用 + 服务账号令牌。"""

    name = "onepassword"
    binaries = ("op",)
    install_hint = "https://developer.1password.com/docs/cli/get-started/ （装好后 `op signin`）；"


class SecretResolver:
    """按优先级取值，并把"被遮蔽的源"记下来。

    `shadowed[key] = ["env"]` 的含义是：**env 里也有这把键，但 keyring 优先级更高，
    所以 env 那份没生效**。没有这条信息，"我改了 `.env` 为什么不生效"永远查不出来。
    """

    def __init__(self, sources: Sequence[SecretSource]) -> None:
        self.sources: list[SecretSource] = list(sources)
        #: 键 → 被遮蔽的源名（按优先级顺序）。
        self.shadowed: dict[str, list[str]] = {}
        #: 键 → 实际提供值的源名。`None` 表示"问过，但没有源有它"。
        self.origin: dict[str, str] = {}

    @classmethod
    def default(cls, data_dir: Path, *, keyring_backend: Any | None = None) -> SecretResolver:
        """生产装配：keyring → env → file → bitwarden → onepassword。"""
        return cls(
            [
                KeyringSource(data_dir, backend=keyring_backend),
                EnvSource(),
                FileSource(data_dir),
                BitwardenSource(),
                OnePasswordSource(),
            ]
        )

    # ── 取值 ────────────────────────────────────────────────────────────────
    def resolve(self, key: str) -> str | None:
        """按优先级取值。同名冲突时高优先级胜，并记进 :attr:`shadowed`。"""
        self.shadowed.pop(key, None)
        self.origin.pop(key, None)
        winner: str | None = None
        value: str | None = None
        losers: list[str] = []
        for source in self.sources:
            found = self._ask(source, key)
            if not found:
                continue
            if winner is None:
                winner, value = source.name, found
            else:
                losers.append(source.name)
        if winner is not None:
            self.origin[key] = winner
        if losers:
            self.shadowed[key] = losers
        return value

    def _ask(self, source: SecretSource, key: str) -> str | None:
        """问一个源。**不可用 / 未实现 / 出错都算"没有"**，但要能区分原因。"""
        try:
            if not source.available():
                return None
            return source.get(key)
        except NotImplementedError:
            # 外部源的取值路径本来就没实现（见 `_CliSource`），不是故障。
            return None
        except Exception:
            return None

    # ── 写入 ────────────────────────────────────────────────────────────────
    def writable(self) -> WritableSecretSource | None:
        for source in self.sources:
            if isinstance(source, WritableSecretSource) and source.available():
                return source
        return None

    def set(self, key: str, value: str) -> str:
        """写进**最高优先级的可用可写源**，返回那个源的名字。"""
        target = self.writable()
        if target is None:
            raise RuntimeError("没有可写入的密钥源（keyring 不可用，也没有回退到明文文件）")
        target.set(key, value)
        return target.name

    def remove(self, key: str) -> list[str]:
        """从**所有**可写源里删。返回真的删掉了的源名。"""
        removed: list[str] = []
        for source in self.sources:
            if not isinstance(source, WritableSecretSource):
                continue
            try:
                if source.remove(key):
                    removed.append(source.name)
            except Exception:
                continue
        self.shadowed.pop(key, None)
        self.origin.pop(key, None)
        return removed

    # ── 清单 ────────────────────────────────────────────────────────────────
    def keys(self) -> dict[str, list[str]]:
        """每个源能列出的键名（**只有名字，没有值**）。"""
        out: dict[str, list[str]] = {}
        for source in self.sources:
            try:
                if not source.available():
                    out[source.name] = []
                    continue
                out[source.name] = list(source.list_keys())
            except NotImplementedError:
                out[source.name] = []
            except Exception:
                out[source.name] = []
        return out

    def all_keys(self) -> list[str]:
        """所有源里出现过的键名（去重，供"有没有配"的判断用）。"""
        seen: set[str] = set()
        for names in self.keys().values():
            seen.update(names)
        return sorted(seen)

    def status(self) -> list[dict[str, Any]]:
        """`orbit secrets status` 的数据源。**绝不含任何值。**"""
        keys = self.keys()
        return [
            {
                "source": source.name,
                "available": bool(source.available()),
                "keys": keys.get(source.name, []),
                "describe": source.describe(),
            }
            for source in self.sources
        ]
