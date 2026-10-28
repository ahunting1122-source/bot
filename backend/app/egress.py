"""出口凭证注入：Orbit **现在真能做到的那一部分**（MATRIX 4.8 / 14.x）。

## 先说清楚：Orbit 没有代理进程

Hermes 的 `egress` 是一个真实的 TLS 中间人代理（iron-proxy）：独立守护进程 + 自签 CA +
按供应商铸造的代理令牌 + `proxy.yaml` + `mappings.json`。沙箱里只放**代理令牌**，
真密钥留在代理侧，请求出去时由代理按令牌换成真凭据——所以**调用方拿不到真密钥**。

Orbit 没有这一层，本模块**不假装有**。真正的"令牌代换"至少要四样东西：独立进程、
能签发叶证书的 CA、令牌铸造与轮换、以及把出站流量**强制**导向代理的那一层
（防火墙规则 / 网络命名空间）。本机既没有容器也没有网络，做出来只能是一段没人
验证过的壳——而一个"看起来配好了、其实没拦"的出口层，比明说"没有"危险得多。

## 那本模块做什么

两件真能做、且现在就有用的事：

1. **出站前的静态检查**（:meth:`EgressPolicy.check`）：按 host 声明
   ``allow`` / ``deny`` / ``ask``，落 ``<data_dir>/egress.json``。
   这是"把请求发出去之前先问一句"的闸，不是代理。
2. **请求头注入**（:meth:`EgressPolicy.inject_headers`）：按 host 规则从系统 keyring
   取凭证，**只在内存里**塞进 headers dict——不落盘、不回显、不写日志。

## 它挡不住什么（写在这里，因为它决定了这层能不能当安全边界）

- **挡不住绕过**：`run_command` 里的目标 host 是**按命令行文本**认出来的。
  写成 `curl $(cat u)`、`python -c "import requests; ..."`、或让脚本自己去连，
  就认不出、也就不会过这道闸。
- **不做令牌代换**：注入的仍是**真密钥**。能读进程内存、能看命令行、能读 keyring 的
  东西，照样能拿到它。这层只是"别把它写进命令行参数"。
- **不是防火墙**：它只作用于**经过 Orbit 代码**的调用点，管不了机器上别的进程。

## 默认值为什么是 allow

``default`` 默认 ``allow``，因为"引入 egress 层"不能改变既有行为——`curl https://…`
在配置为空时必须照旧跑得通。要更严就显式写 ``"default": "deny"`` 再按需 allow；
把默认值改成 deny 会让所有没写配置的人当场失去出站能力，那不是安全，那是故障。
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

#: 策略文件名（相对 `data_dir`）。与 `settings.json` / `approvals.json` / `tools.json`
#: 同规矩：放在数据目录，"换一个数据目录"能连出口策略一起换走。
CONFIG_FILENAME = "egress.json"

#: 三态。`ask` 与 `deny` 在 Orbit 里的处置相同（都拒绝），差别只在**措辞**：
#: `ask` 的意思是"这条规则本来要问人，而 Orbit 没有交互式审批通道"，
#: 把两者混成一句话会让用户以为 `ask` 规则写坏了。
ALLOW, DENY, ASK = "allow", "deny", "ask"
ACTIONS: tuple[str, ...] = (ALLOW, DENY, ASK)

#: 默认动作。见模块 docstring 的"默认值为什么是 allow"。
DEFAULT_ACTION = ALLOW

#: 从命令文本里认 URL 用的正则。只认**显式写出来**的 http(s) URL——
#: 变量展开、命令替换、`--data @file` 里的地址都认不出，这是如实的边界。
_URL = re.compile(r"https?://[^\s'\"`<>|;]+", re.IGNORECASE)

#: 会出站取网络内容的命令名。只有它们的目标 host 需要过 egress 闸。
FETCHERS: tuple[str, ...] = (
    "curl",
    "wget",
    "curl.exe",
    "wget.exe",
    "iwr",
    "invoke-webrequest",
    "invoke-restmethod",
    "irm",
)


@dataclass(frozen=True)
class CredentialRule:
    """一条"给某个 host 注入哪个请求头"的规则。

    ``key`` 是 **keyring 里的键名**，不是密钥值。配置文件里只有键名——这是这份文件
    能安全进版本库/进备份的前提。
    """

    host: str
    header: str = "Authorization"
    scheme: str = "Bearer"
    key: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"host": self.host, "header": self.header, "scheme": self.scheme, "key": self.key}


@dataclass(frozen=True)
class EgressDecision:
    """一次出站检查的结论。`rule` 是命中的规则原文（没命中为空串）。"""

    url: str
    host: str
    action: str
    rule: str = ""
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.action == ALLOW

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "host": self.host,
            "action": self.action,
            "rule": self.rule,
            "reason": self.reason,
        }


def normalize_host(url: str) -> str:
    """从 URL 或裸 host 里取出小写 host。

    两种形状都吃：`https://api.example.com/v1` 与 `api.example.com`。为什么要兼容裸
    host：CLI 上 `orbit egress allow api.example.com` 是更自然的写法，逼用户写完整
    URL 只会让人抄错。
    """
    text = (url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        # 裸 host 可能带端口（`localhost:8080`），也可能带路径——统一按"给它一个 scheme"
        # 处理，让 urlsplit 自己拆。
        text = "//" + text
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
    except ValueError:
        return ""
    return host.strip().lower()


def match_host(rule: str, host: str) -> bool:
    """一条 host 规则是否命中。

    支持的写法（刻意只有这三种，多一种就多一种"为什么没生效"的排查成本）：

    - ``api.example.com``：**精确**匹配（不连带子域）；
    - ``*.example.com``：子域通配（`fnmatch`），**不含** `example.com` 本身；
    - ``*``：全部。
    """
    pattern = (rule or "").strip().lower()
    target = (host or "").strip().lower()
    if not pattern or not target:
        return False
    if pattern == "*":
        return True
    if "*" in pattern or "?" in pattern:
        return fnmatch.fnmatchcase(target, pattern)
    return pattern == target


def urls_in_command(command: str) -> list[str]:
    """从命令文本里抽出**显式写出**的 http(s) URL（去重保序）。"""
    seen: dict[str, None] = {}
    for match in _URL.finditer(command or ""):
        url = match.group(0).rstrip(".,);")
        seen.setdefault(url, None)
    return list(seen)


def command_hosts(command: str) -> list[str]:
    """命令里**需要过 egress 闸**的目标 host。

    只看命令名是不是取网器（`curl` / `wget` / `iwr` …）：一条 `python build.py` 里
    恰好有 `https://…` 字样，不该被当成出站意图——误拦正常构建比漏拦更常见，
    而漏拦这一半由"这本来就不是防火墙"的定位承认了（见模块 docstring）。
    """
    text = (command or "").strip()
    if not text:
        return []
    head = text.split()[0].strip("\"'").lower()
    head = os.path.basename(head) if os.sep in head or "/" in head else head
    if head not in FETCHERS:
        return []
    hosts: dict[str, None] = {}
    for url in urls_in_command(text):
        host = normalize_host(url)
        if host:
            hosts.setdefault(host, None)
    return list(hosts)


def _resolve_secret(key: str, resolver: Any = None) -> str:
    """从一个密钥解析器取一个值。取不到就返回空串（绝不抛、绝不回显）。

    **绝不把值写进日志、返回值或异常信息**：这层存在的意义就是让密钥不出现在
    会被人看到的地方，一次 `log.debug` 就能把这份努力抹掉。
    """
    name = (key or "").strip()
    if not name or resolver is None:
        return ""
    try:
        value = resolver.resolve(name) if hasattr(resolver, "resolve") else None
    except Exception:  # noqa: BLE001 - 取不到密钥不该让整条请求炸掉
        return ""
    return str(value or "")


@dataclass
class EgressPolicy:
    """出站策略：allow / deny 规则 + 默认动作 + 注入规则。

    全部字段都是**声明**，没有副作用；唯一的写操作是 :meth:`save`。
    """

    path: Path | None = None
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    default: str = DEFAULT_ACTION
    credentials: list[CredentialRule] = field(default_factory=list)

    # ── 读写 ────────────────────────────────────────────────────────────────
    @classmethod
    def path_for(cls, data_dir: str | Path) -> Path:
        return Path(data_dir) / CONFIG_FILENAME

    @classmethod
    def load(cls, data_dir: str | Path) -> EgressPolicy:
        """读策略。**任何异常都退回"空策略"**（= 全部放行，与没有这层时一致）。

        理由与 `app/toolsets.load_config` 同一条：配置读不出来时把出站全禁掉，
        会让整个 agent 当场失去联网能力，而这个故障比"多放行了几个 host"更严重。
        坏文件不是静默的——`describe()` 会如实说"策略文件读不出来"。
        """
        path = cls.path_for(data_dir)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls(path=path)
        if not isinstance(raw, dict):
            return cls(path=path)
        default = str(raw.get("default") or DEFAULT_ACTION).strip().lower()
        if default not in ACTIONS:
            default = DEFAULT_ACTION
        credentials: list[CredentialRule] = []
        for item in raw.get("credentials") or []:
            if not isinstance(item, dict):
                continue
            host = str(item.get("host") or "").strip()
            if not host:
                continue
            credentials.append(
                CredentialRule(
                    host=host,
                    header=str(item.get("header") or "Authorization").strip() or "Authorization",
                    scheme=str(item.get("scheme") if item.get("scheme") is not None else "Bearer"),
                    key=str(item.get("key") or "").strip(),
                )
            )
        return cls(
            path=path,
            allow=_str_list(raw.get("allow")),
            deny=_str_list(raw.get("deny")),
            default=default,
            credentials=credentials,
        )

    def save(self) -> Path:
        """原子写（临时文件 + `os.replace`）：半截 JSON 会被 `load` 读成空策略，
        也就是**静默变成全部放行**——那是最不该出现的一种失败。"""
        if self.path is None:
            raise ValueError("策略没有落点路径（请用 EgressPolicy.load(data_dir) 构造）")
        payload = {
            "default": self.default,
            "allow": list(self.allow),
            "deny": list(self.deny),
            "credentials": [rule.as_dict() for rule in self.credentials],
            "_note": "本文件只有 host 规则与 keyring 键名，**没有密钥值**。",
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, self.path)
        return self.path

    # ── 判定 ────────────────────────────────────────────────────────────────
    def check(self, url: str) -> EgressDecision:
        """判定一个目标能不能出站。

        判定顺序：**deny → allow → default**。deny 先判是刻意的：`allow` 是便利决策，
        `deny` 是安全决策，冲突时该选保守的那个（与 `app/toolsets.resolve` 里
        "禁用优先于启用"同一条理由）。
        """
        host = normalize_host(url)
        if not host:
            return EgressDecision(
                url=str(url or ""),
                host="",
                action=ASK,
                reason=(
                    "解析不出主机名，无法判定出站目标。请给出完整 URL（如 https://host/path），"
                    "或用裸主机名（api.example.com）。"
                ),
            )
        for rule in self.deny:
            if match_host(rule, host):
                return EgressDecision(
                    url=str(url or ""),
                    host=host,
                    action=DENY,
                    rule=rule,
                    reason=f"命中 deny 规则「{rule}」：这个主机被显式禁止出站",
                )
        for rule in self.allow:
            if match_host(rule, host):
                return EgressDecision(
                    url=str(url or ""),
                    host=host,
                    action=ALLOW,
                    rule=rule,
                    reason=f"命中 allow 规则「{rule}」",
                )
        if self.default == DENY:
            return EgressDecision(
                url=str(url or ""),
                host=host,
                action=DENY,
                reason="没有匹配的规则，且 default=deny（未加白的主机一律不出站）",
            )
        if self.default == ASK:
            return EgressDecision(
                url=str(url or ""),
                host=host,
                action=ASK,
                reason="没有匹配的规则，且 default=ask（需要人工确认这个主机）",
            )
        return EgressDecision(
            url=str(url or ""),
            host=host,
            action=ALLOW,
            reason="没有匹配的规则，按 default=allow 放行",
        )

    # ── 注入 ────────────────────────────────────────────────────────────────
    def resolver(self, override: Any = None) -> Any:
        """注入用的密钥解析器：调用方给了就用它的，否则按策略文件所在目录现建一个。

        为什么由**策略**自己决定 data_dir：keyring 的 service 名是
        `orbit:<data_dir>`（见 `app/secrets.py`），换了数据目录就是另一套密钥。
        拿错目录会得到"密钥不存在"，而那看起来像"用户没配"。
        """
        if override is not None:
            return override
        data_dir = self.path.parent if self.path is not None else None
        if data_dir is None:
            return None
        try:
            from app.secrets import SecretResolver

            return SecretResolver.default(Path(data_dir))
        except Exception:  # noqa: BLE001 - keyring 不可用时按"取不到"处理
            return None

    def inject_headers(
        self, url: str, headers: dict[str, str] | None = None, *, resolver: Any = None
    ) -> dict[str, str]:
        """按 host 规则注入请求头，返回**新的** dict（不改入参）。

        三条硬约束，都是"为什么这么写"：

        1. **只在内存里**：不写盘、不进日志、不进返回值（返回值只有 header 名）。
        2. **取不到就不注入**：绝不塞一个 `Bearer <placeholder>` 之类的假值——
           那会让服务端返回 401，而排查方向会跑到"令牌是不是过期了"。
        3. **不覆盖调用方已有的同名头**：调用方显式给的头优先。这条规则让
           "手工调试时临时换一个令牌"成为可能，而不是被策略悄悄改回去。
        """
        merged = dict(headers or {})
        host = normalize_host(url)
        if not host:
            return merged
        source = self.resolver(resolver)
        for rule in self.credentials:
            if not match_host(rule.host, host):
                continue
            if rule.header in merged:
                continue
            value = _resolve_secret(rule.key, source)
            if not value:
                continue
            merged[rule.header] = f"{rule.scheme} {value}".strip() if rule.scheme else value
        return merged

    def injection_plan(self, url: str, *, resolver: Any = None) -> list[dict[str, Any]]:
        """诊断用：这条 URL 会注入哪些头、值从哪个键取、此刻取不取得到。

        **只回键名与布尔，不回值**——这个方法的调用点是 CLI 与日志。
        """
        host = normalize_host(url)
        source = self.resolver(resolver)
        rows: list[dict[str, Any]] = []
        for rule in self.credentials:
            if not match_host(rule.host, host):
                continue
            rows.append(
                {
                    "host": rule.host,
                    "header": rule.header,
                    "key": rule.key,
                    "resolved": bool(_resolve_secret(rule.key, source)),
                }
            )
        return rows

    # ── 视图 ────────────────────────────────────────────────────────────────
    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path) if self.path else "",
            "default": self.default,
            "allow": list(self.allow),
            "deny": list(self.deny),
            "credentials": [rule.as_dict() for rule in self.credentials],
        }

    def describe(self) -> str:
        """如实说明这层是什么、不是什么。CLI 与文档共用同一份文字。"""
        where = str(self.path) if self.path else "<未配置>"
        return (
            "Orbit 的 egress 只有两层，**没有代理进程**：\n"
            f"  1) 出站前的静态检查（allow {len(self.allow)} 条 / deny {len(self.deny)} 条 / "
            f"default={self.default}），策略文件 {where}；\n"
            f"  2) 请求头注入（{len(self.credentials)} 条规则，值从系统 keyring 取，只在内存里）。\n"
            "没有的：iron-proxy 那类独立守护进程、自签 CA、按供应商铸造的代理令牌、"
            "proxy.yaml / mappings.json，以及把出站流量强制导向代理的那一层。\n"
            "所以**调用方仍然拿得到真密钥**（注入的就是真值），也挡不住绕过 "
            "（`curl $(cat u)`、脚本自己发请求都认不出）。\n"
            "真正要做令牌代换，需要：独立进程 + 能签发叶证书的 CA + 令牌铸造与轮换 + "
            "强制出站改道（防火墙/命名空间）。本机没有容器也没有网络，做不了。"
        )


def _str_list(value: Any) -> list[str]:
    """把配置里的列表字段读成去重保序的字符串列表；坏值当空。"""
    if not isinstance(value, (list, tuple)):
        return []
    seen: dict[str, None] = {}
    for item in value:
        text = str(item).strip()
        if text:
            seen.setdefault(text, None)
    return list(seen)


def describe() -> str:
    """没有 settings 时的边界说明（CLI 的 `egress status` 之外的入口用它）。"""
    return EgressPolicy().describe()
