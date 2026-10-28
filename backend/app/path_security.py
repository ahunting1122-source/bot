"""路径与 URL 安全（MATRIX 14.8）：`PathPolicy` / `UrlPolicy` / `website_policy` / `threat_*`。

## 这个模块为什么独立存在

MATRIX 14.8 的四样东西原本散落在三处，而且都不完整：

- `path_security` 由 `app/tools.py::resolve_path` 顺手承担——它只做"解析后是否还在
  授权目录内"，**不认 Windows 保留名、不认 ADS、不认符号链接逃逸**。
- `url_safety` 在 `app/web.py::check_url` 里（本机/内网/元数据/重定向再审），做得
  很好，但它是**私有函数 + 只有一条路径用**，没法被 CLI 或别的模块按策略复用。
- `website_policy` 与 `threat_patterns` **完全没有独立实现**。

本模块把前两样抽成**可配置的策略对象**，把后两样补上。刻意**不重写**已有判据：
URL 的"哪些 IP 段不能碰"直接调 `app/web.py::blocked_ip_reason`（那里已经把
`::ffff:169.254.169.254` 这类映射形式处理过了），威胁规则直接**读** `app/tirith.py`
的 16 条 `RULES`。抄一份规则表出来，两边迟早会漂移，而漂移的安全边界比没有更危险。

## 路径逃逸：必须防住的三件事（外加一件 Windows 特有的）

1. **符号链接逃逸**：`root/link` 指向 `C:\\Windows\\System32`。光比字符串前缀看不出
   问题，所以先 `resolve()` 再比；`follow_symlinks=False`（默认）时还会逐段检查
   路径上有没有链接，**发现就拒**。这里的"链接"包含 **Windows 目录联接**
   （`mklink /J`）——它不需要管理员权限就能建，而 `is_symlink()` 对它返回 `False`，
   只判符号链接等于把 Windows 上最容易做的那条逃逸路径漏掉。
2. **`..` 穿越**：`root/../../etc/passwd`。默认拒（`allow_dotdot=False`），因为
   "`a/../b` 其实无害"这个例外无法在解析前判断——等解析完再判，等于把判断建立在
   已经被穿越的路径上。
3. **Windows 保留名与 ADS**：`CON` / `NUL` / `AUX` / `COM1`… 这类名字在 Windows 上
   指向设备而不是文件；`file.txt:stream` 是 NTFS 的**替代数据流**——往那里写内容
   不改变文件可见内容，`dir` 也看不出来，是隐藏载荷的常用手法。
4. **设备命名空间**：`\\\\?\\C:\\...` 与 `\\\\.\\PhysicalDrive0` 会**绕过 Win32 路径
   规范化**，`..` 与保留名检查在它面前全部失效。所以单独拒。

## 大小写：为什么统一 `casefold`

Windows 的文件系统大小写不敏感（`C:\\Windows` 与 `c:\\windows` 是同一个目录）。
如果比较时区分大小写，攻击者只要换一种大小写写法就能通过根目录包含检查。
所以**比较前统一 `casefold`**。代价是在大小写敏感的系统上会多拦几个"只差大小写"
的路径——宁可多拦，不要漏拦。

## 默认值为什么是"不限制根目录"

`allowed_roots` 为空时**不限制根**（其余四类检查照做）。这不是疏忽：`PathPolicy`
是一个策略对象，`security path` 这类诊断命令需要能在"没有声明根"的情况下回答
"这个名字本身有没有问题"。真正要用它当边界的地方（工具层）必须显式传根——
`resolve_path` 的既有行为就是这样。要更严就显式传根，那时根检查生效。
"""

from __future__ import annotations

import fnmatch
import ipaddress
import os
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app import tirith
from app.web import BLOCKED_HOSTNAMES, BLOCKED_IPS, BLOCKED_NETWORKS, blocked_ip_reason

# ── 常量 ────────────────────────────────────────────────────────────────────

#: Windows 保留设备名。**不论当前平台**都拦：这些名字在 Windows 上指向设备，
#: 而"在 Linux 上写好的脚本搬到 Windows 就变成漏洞"是最典型的可移植性陷阱。
RESERVED_NAMES: frozenset[str] = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)

#: 设备命名空间前缀。`\\?\` 让 Win32 跳过路径规范化，`\\.\` 直接打开设备。
DEVICE_PREFIXES: tuple[str, ...] = ("\\\\?\\", "\\\\.\\", "//?/", "//./")

#: 路径段分隔符（两种都认：Windows 的 `\` 与 POSIX 的 `/`，以及混合写法）。
_SEGMENT_SPLIT = re.compile(r"[\\/]+")

#: 盘符前缀，例如 `C:`。ADS 检查要先把这一段摘掉，否则每个绝对路径都会被判成 ADS。
_DRIVE = re.compile(r"^[A-Za-z]:")

#: `UrlPolicy` 默认允许的协议。**只有这两个**——`file:` 能读本地任意文件，
#: `javascript:` 与 `data:` 能在渲染端执行脚本，三者都不是"网页地址"。
DEFAULT_SCHEMES: tuple[str, ...] = ("http", "https")

#: 网站策略三态。与 `app/egress.py` 同名同义，但作用对象是"模型可以打开哪些网站"。
ALLOW, DENY, ASK = "allow", "deny", "ask"
WEBSITE_ACTIONS: tuple[str, ...] = (ALLOW, DENY, ASK)


# ── 路径 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PathDecision:
    """一条路径的检查结论。

    `kind` 是**机器可读的判据名**（`traversal` / `reserved_name` / `ads` /
    `symlink` / `device_path` / `outside_root` / `deny_pattern` / `ok`），
    写进日志后能回答"这条为什么被拒"，而 `reason` 是给人看的中文说明。
    """

    path: str
    resolved: str = ""
    allowed: bool = False
    kind: str = "ok"
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "resolved": self.resolved,
            "allowed": self.allowed,
            "kind": self.kind,
            "reason": self.reason,
        }


def _segments(text: str) -> list[str]:
    """把路径拆成段（`..` / `.` / 空段都保留，判断要用）。"""
    return [part for part in _SEGMENT_SPLIT.split(text) if part]


def _strip_drive(text: str) -> str:
    """去掉开头的盘符（`C:`），剩下的部分才适合做 ADS 检查。"""
    return _DRIVE.sub("", text, count=1)


def reserved_name_hit(segment: str) -> str:
    """一个路径段是不是 Windows 保留名。返回命中的名字（空串 = 没命中）。

    判据用**第一个点之前的部分**：`NUL.txt` 在 Windows 上同样指向空设备。
    """
    stem = segment.split(".", 1)[0].strip().casefold()
    return stem if stem in RESERVED_NAMES else ""


def ads_hit(segment: str) -> str:
    """一个路径段是不是 NTFS 替代数据流（`file.txt:stream`）。返回流名（空串 = 没有）。"""
    if ":" not in segment:
        return ""
    return segment.split(":", 1)[1]


def _compare_form(text: str) -> str:
    """把路径归一成"比较形态"：反斜杠 + 大小写折叠 + 去掉尾部分隔符。

    为什么连反斜杠一起归一：Windows 同时接受 `/` 与 `\\`，只归一大小写的话
    `C:/Windows` 与 `C:\\Windows` 会被判成两个不同目录。
    """
    normalized = str(text).replace("/", "\\").casefold()
    if len(normalized) > 3:  # 保留 `C:\` 这类根形态
        normalized = normalized.rstrip("\\")
    return normalized


def _within(child: str, root: str) -> bool:
    """`child` 是否在 `root` 之内（含等于）。两边都要是 `_compare_form` 的产物。"""
    if not root:
        return True
    return child == root or child.startswith(root.rstrip("\\") + "\\")


@dataclass
class PathPolicy:
    """路径策略：根目录限制 + 拒绝模式 + 逃逸检查。

    全部字段都是**声明**，`check` 不产生任何副作用（不建目录、不改文件）。
    """

    #: 允许的根目录。空 = 不限制根（见模块 docstring 的"默认值"一节）。
    allowed_roots: tuple[Path, ...] = ()
    #: 拒绝模式（`fnmatch`），对**解析后的完整路径**与**每一段**都试一次。
    deny_patterns: tuple[str, ...] = ()
    #: 是否跟随符号链接。默认 `False`：跟随意味着"根里的一个链接能指向根外"。
    follow_symlinks: bool = False
    #: 是否允许路径里出现 `..`。默认 `False`（见模块 docstring）。
    allow_dotdot: bool = False
    #: 相对路径的基准目录。`None` = 当前工作目录。
    base: Path | None = None

    def __post_init__(self) -> None:
        # 允许用字符串构造（CLI / 配置文件都这么传），统一成 Path。
        self.allowed_roots = tuple(Path(root) for root in self.allowed_roots)
        if self.base is not None:
            self.base = Path(self.base)

    # ── 主入口 ──────────────────────────────────────────────────────────────
    def check(self, path: str | Path) -> PathDecision:
        """检查一条路径。返回 :class:`PathDecision`，**不抛异常**。

        为什么用返回值而不是抛异常：`security path` 要把"为什么被拒"打印出来，
        异常只带一句话；而 `kind` 需要能被脚本读取。
        """
        raw = str(path if path is not None else "").strip()
        if not raw:
            return PathDecision(path=raw, allowed=False, kind="empty", reason="路径为空")

        # ① 设备命名空间：必须在一切规范化之前判——它本身就是"绕过规范化"的开关。
        lowered = raw.casefold()
        for prefix in DEVICE_PREFIXES:
            if lowered.startswith(prefix.casefold()):
                return PathDecision(
                    path=raw,
                    allowed=False,
                    kind="device_path",
                    reason=(
                        f"路径以设备命名空间开头（{prefix}）：它会绕过 Win32 的路径规范化，"
                        "`..` 与保留名检查在它面前全部失效。"
                    ),
                )

        segments = _segments(raw)

        # ② Windows 保留名。
        for segment in segments:
            hit = reserved_name_hit(segment)
            if hit:
                return PathDecision(
                    path=raw,
                    allowed=False,
                    kind="reserved_name",
                    reason=(
                        f"路径段「{segment}」是 Windows 保留设备名（{hit.upper()}）："
                        "它指向设备而不是文件，读写它的后果与预期完全不同。"
                    ),
                )

        # ③ NTFS 替代数据流。先摘掉盘符，否则 `C:` 会被当成流。
        for segment in _segments(_strip_drive(raw)):
            stream = ads_hit(segment)
            if stream:
                return PathDecision(
                    path=raw,
                    allowed=False,
                    kind="ads",
                    reason=(
                        f"路径段「{segment}」带 NTFS 替代数据流（:{stream}）："
                        "写进去的内容不改变文件可见内容，`dir` 也看不出来，是隐藏载荷的常用手法。"
                    ),
                )

        # ④ `..` 穿越。
        if not self.allow_dotdot and ".." in segments:
            return PathDecision(
                path=raw,
                allowed=False,
                kind="traversal",
                reason=(
                    "路径含上跳段（..）：上跳之后的位置在解析前无法确认，"
                    "所以默认拒绝。确实需要时把路径写成解析后的绝对路径。"
                ),
            )

        # ⑤ 解析。
        try:
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                candidate = (self.base or Path.cwd()) / candidate
            absolute = Path(os.path.abspath(candidate))
            resolved = absolute.resolve()
        except (OSError, ValueError, RuntimeError) as exc:
            return PathDecision(
                path=raw,
                allowed=False,
                kind="unresolvable",
                reason=f"路径无法解析（{type(exc).__name__}: {exc}）",
            )

        # ⑥ 链接（符号链接 / Windows 目录联接）：逐段看。`resolve()` 会把链接
        #    **跟掉**，跟完之后已经看不出"原本经过了一个链接"，所以这一步必须在
        #    解析结果之外单独做。
        if not self.follow_symlinks:
            link = _first_link(absolute)
            if link is not None:
                return PathDecision(
                    path=raw,
                    resolved=str(resolved),
                    allowed=False,
                    kind="symlink",
                    reason=(
                        f"路径经过链接「{link}」（符号链接或目录联接）：链接可以指向根目录"
                        "之外，而前缀比较看不出这一点。要允许链接请显式设置 follow_symlinks=True。"
                    ),
                )

        # ⑦ 根目录包含检查（大小写折叠后比较）。
        resolved_form = _compare_form(str(resolved))
        if self.allowed_roots:
            roots = [
                _compare_form(str(Path(root).expanduser().resolve()))
                for root in self.allowed_roots
            ]
            if not any(_within(resolved_form, root) for root in roots):
                return PathDecision(
                    path=raw,
                    resolved=str(resolved),
                    allowed=False,
                    kind="outside_root",
                    reason=(
                        f"解析后的路径不在允许的根目录内（根："
                        f"{'、'.join(str(root) for root in self.allowed_roots)}）"
                    ),
                )

        # ⑧ 拒绝模式。
        hit = self._deny_hit(resolved_form, segments)
        if hit:
            return PathDecision(
                path=raw,
                resolved=str(resolved),
                allowed=False,
                kind="deny_pattern",
                reason=f"命中拒绝模式「{hit}」",
            )

        return PathDecision(
            path=raw,
            resolved=str(resolved),
            allowed=True,
            kind="ok",
            reason="通过：无保留名 / 无数据流 / 无上跳 / 无符号链接 / 在允许的根内",
        )

    def _deny_hit(self, resolved_form: str, segments: list[str]) -> str:
        """拒绝模式匹配。完整路径与每一段都试——`*.pem` 与 `**/secrets/*` 都要能写。"""
        for pattern in self.deny_patterns:
            text = str(pattern).strip().replace("/", "\\").casefold()
            if not text:
                continue
            if fnmatch.fnmatchcase(resolved_form, text):
                return pattern
            if any(fnmatch.fnmatchcase(part.casefold(), text) for part in segments):
                return pattern
        return ""

    def describe(self) -> str:
        """如实说明这层做什么、默认值是什么。"""
        roots = "、".join(str(root) for root in self.allowed_roots) or "（未限制根目录）"
        return (
            "路径安全（MATRIX 14.8）判据：设备命名空间 → Windows 保留名 → NTFS 数据流 → "
            "`..` 上跳 → 符号链接 → 根目录包含（大小写折叠）→ 拒绝模式。\n"
            f"允许的根：{roots}\n"
            f"跟随符号链接：{'是' if self.follow_symlinks else '否'}；"
            f"允许 `..`：{'是' if self.allow_dotdot else '否'}；"
            f"拒绝模式：{len(self.deny_patterns)} 条\n"
            "**没做的**：不做 ACL / 属主判断，不做磁盘配额，不判断目标是否存在。"
            "它回答的是「这个路径名能不能被安全地使用」，不是「这个文件你有权限读」。"
        )


def _is_link(path: Path) -> bool:
    """是不是一个会"把路径带到别处"的链接。

    两种都算：

    - **符号链接**（`mklink` / `ln -s`）：`Path.is_symlink()`；
    - **Windows 目录联接**（`mklink /J`）：`Path.is_junction()`。

    第二种是这个函数存在的理由：目录联接**不需要管理员权限**就能创建，而
    `is_symlink()` 对它返回 `False`（reparse tag 是 `IO_REPARSE_TAG_MOUNT_POINT`
    而不是 `IO_REPARSE_TAG_SYMLINK`）。只判符号链接，等于在 Windows 上把最容易
    做出来的那条逃逸路径漏掉。`is_junction` 是 3.12 才有的，所以用 `getattr` 兜底。
    """
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction()) if callable(is_junction) else False
    except OSError:
        return False


def _first_link(absolute: Path) -> Path | None:
    """从根往下逐段找第一个链接（符号链接或目录联接）。找不到返回 `None`。

    为什么不用 `resolved.parts` 反推：解析之后链接已经被跟掉了，原始链路上
    那个链接名根本不出现。必须从**未解析**的绝对路径逐段走。
    """
    parts = absolute.parts
    if not parts:
        return None
    current = Path(parts[0])
    for part in parts[1:]:
        current = current / part
        if _is_link(current):
            return current
    return None


# ── URL ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class UrlDecision:
    """一条 URL 的检查结论。`kind` 见 :meth:`UrlPolicy.check` 的各分支。"""

    url: str
    host: str = ""
    scheme: str = ""
    allowed: bool = False
    kind: str = "ok"
    reason: str = ""
    #: 真的做了 DNS 解析没有。False 时 `reason` 里会写明"域名指向内网"查不出来。
    resolved: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "host": self.host,
            "scheme": self.scheme,
            "allowed": self.allowed,
            "kind": self.kind,
            "reason": self.reason,
            "resolved": self.resolved,
        }


@dataclass
class UrlPolicy:
    """URL 策略：协议白名单 + 主机拒绝 + 私有地址闸。

    `resolve` 默认 `None`（**不做 DNS 解析**）。理由：本机离线，解析必然失败，
    而"解析失败就拒"会把所有域名一律判死——那道闸就永远在说"不"，等于没有闸。
    需要更强保证的地方显式传 `resolve=app.web.resolve_host`，那时解析失败一律拒绝。
    """

    allow_schemes: tuple[str, ...] = DEFAULT_SCHEMES
    deny_hosts: tuple[str, ...] = ()
    #: 是否允许本机 / 私有网段。**云元数据地址不受此开关影响**，永远拒。
    allow_private: bool = False
    #: 是否允许 URL 里带 `user:password@`（会被日志与索引记录下来）。
    allow_credentials: bool = False
    resolve: Callable[[str], list[str]] | None = None

    def check(self, url: str) -> UrlDecision:
        """检查一条 URL。返回 :class:`UrlDecision`，**不抛异常**。"""
        text = str(url or "").strip()
        if not text:
            return UrlDecision(url=text, allowed=False, kind="empty", reason="网址为空")

        try:
            parsed = urlsplit(text)
        except ValueError as exc:
            return UrlDecision(url=text, allowed=False, kind="unparsable", reason=f"网址无法解析（{exc}）")

        scheme = (parsed.scheme or "").casefold()
        if scheme not in {item.casefold() for item in self.allow_schemes}:
            return UrlDecision(
                url=text,
                scheme=scheme,
                allowed=False,
                kind="scheme",
                reason=(
                    f"协议「{scheme or '空'}」不在允许列表（{'/'.join(self.allow_schemes)}）里。"
                    "`file:` 能读本地任意文件，`javascript:` 与 `data:` 能在渲染端执行脚本，"
                    "三者都不是网页地址。"
                ),
            )

        host = (parsed.hostname or "").strip().casefold().rstrip(".")
        if not host:
            return UrlDecision(
                url=text, scheme=scheme, allowed=False, kind="no_host", reason="网址缺少主机名"
            )

        if (parsed.username or parsed.password) and not self.allow_credentials:
            return UrlDecision(
                url=text,
                host=host,
                scheme=scheme,
                allowed=False,
                kind="credentials",
                reason="网址里带用户名或密码：会被日志与索引记录下来，请改用 keyring 引用",
            )

        for pattern in self.deny_hosts:
            if match_pattern(pattern, host):
                return UrlDecision(
                    url=text,
                    host=host,
                    scheme=scheme,
                    allowed=False,
                    kind="deny_host",
                    reason=f"主机命中拒绝规则「{pattern}」",
                )

        if host in BLOCKED_HOSTNAMES or host == "localhost":
            return UrlDecision(
                url=text,
                host=host,
                scheme=scheme,
                allowed=False,
                kind="metadata",
                reason=f"「{host}」是云元数据 / 本机主机名：访问它等于读本机或实例凭证",
            )

        literal = _literal_ip(host)
        if literal is not None:
            reason = self._ip_reason(literal)
            if reason:
                return UrlDecision(
                    url=text,
                    host=host,
                    scheme=scheme,
                    allowed=False,
                    kind="private_ip",
                    reason=reason,
                    resolved=True,
                )
            return UrlDecision(
                url=text,
                host=host,
                scheme=scheme,
                allowed=True,
                reason="通过：公网 IP，协议与主机均合规",
                resolved=True,
            )

        if self.resolve is None:
            return UrlDecision(
                url=text,
                host=host,
                scheme=scheme,
                allowed=True,
                reason=(
                    "通过：协议与主机名合规。**没有做 DNS 解析**——"
                    "所以「这个域名指向 127.0.0.1」这类 SSRF 变体查不出来；"
                    "需要这层保证请传 resolve=app.web.resolve_host。"
                ),
            )

        try:
            addresses = self.resolve(host)
        except (OSError, ValueError) as exc:
            return UrlDecision(
                url=text,
                host=host,
                scheme=scheme,
                allowed=False,
                kind="resolve_failed",
                reason=f"域名解析失败，按安全策略拒绝：{host}（{exc}）",
            )
        if not addresses:
            return UrlDecision(
                url=text,
                host=host,
                scheme=scheme,
                allowed=False,
                kind="resolve_failed",
                reason=f"域名没有解析出任何地址，按安全策略拒绝：{host}",
            )
        for address in addresses:
            parsed_ip = _literal_ip(str(address).split("%", 1)[0])
            reason = self._ip_reason(parsed_ip) if parsed_ip is not None else ""
            if reason:
                return UrlDecision(
                    url=text,
                    host=host,
                    scheme=scheme,
                    allowed=False,
                    kind="private_ip",
                    reason=f"拒绝访问 {host}（解析到 {address}）：{reason}",
                    resolved=True,
                )
        return UrlDecision(
            url=text,
            host=host,
            scheme=scheme,
            allowed=True,
            reason=f"通过：{host} 解析到 {len(addresses)} 个公网地址",
            resolved=True,
        )

    def _ip_reason(self, address: Any) -> str:
        """一个 IP 该不该拒。**云元数据永远拒**，`allow_private` 只放开本机/私有网段。"""
        if address in BLOCKED_IPS or any(address in network for network in BLOCKED_NETWORKS):
            return "这是云厂商元数据 / 链路本地地址（能读到实例凭证），没有任何开关能放开"
        if self.allow_private:
            return ""
        return blocked_ip_reason(address)

    def describe(self) -> str:
        return (
            "URL 安全（MATRIX 14.8）判据：协议白名单 → 主机拒绝规则 → 凭据内嵌 → "
            "元数据主机名 → IP 段 → （可选）DNS 解析。\n"
            f"允许的协议：{'/'.join(self.allow_schemes)}（`file:` / `javascript:` / `data:` 默认拒）\n"
            f"允许本机与私有网段：{'是' if self.allow_private else '否'}；"
            f"拒绝规则：{len(self.deny_hosts)} 条；"
            f"DNS 解析：{'做了' if self.resolve is not None else '没做（离线）'}\n"
            "**没做的**：不做重定向后的再审（那是 `app/egress.py::check_chain` 与 "
            "`app/web.py` 的职责），不判断目标服务是否可用。"
        )


def _literal_ip(host: str) -> Any:
    """主机名是 IP 字面量就返回 `ipaddress` 对象，否则 `None`。"""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


# ── 网站策略 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WebsiteDecision:
    """一条网站访问的结论。三态：`allow` / `deny` / `ask`。"""

    target: str
    host: str
    action: str
    rule: str = ""
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.action == ALLOW

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "host": self.host,
            "action": self.action,
            "rule": self.rule,
            "reason": self.reason,
        }


def match_pattern(pattern: str, host: str) -> bool:
    """一条主机/网站规则是否命中。

    三种写法，**刻意只有这三种**（多一种就多一种"为什么没生效"的排查成本）：

    - `example.com`：精确（不连带子域）
    - `*.example.com`：子域通配，**不含** `example.com` 本身
    - `*`：全部

    与 `app/egress.py::match_host` 同一套语义——两处规则写法不一致，人就会写错。
    """
    text = str(pattern or "").strip().casefold()
    target = str(host or "").strip().casefold()
    if not text or not target:
        return False
    if text == "*":
        return True
    if "*" in text or "?" in text:
        return fnmatch.fnmatchcase(target, text)
    return text == target


@dataclass
class WebsitePolicy:
    """`website_policy`：模型可以打开哪些网站。

    与 `UrlPolicy` 的分工：`UrlPolicy` 判"这个 URL **技术上**能不能碰"（SSRF / 协议），
    `WebsitePolicy` 判"这个网站**业务上**该不该碰"（白名单 / 黑名单 / 问一句）。
    两者都要过：先过 URL 闸，再过网站闸。
    """

    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    default: str = ASK

    def __post_init__(self) -> None:
        action = str(self.default or ASK).casefold()
        self.default = action if action in WEBSITE_ACTIONS else ASK

    def decide(self, target: str) -> WebsiteDecision:
        """判定一条网站（URL 或裸主机名）。判定顺序 **deny → allow → default**。

        deny 先判是刻意的：`allow` 是便利决策、`deny` 是安全决策，冲突时选保守的
        那个（与 `app/egress.py::check` 同一条理由）。
        """
        text = str(target or "").strip()
        host = _host_of(text)
        if not host:
            return WebsiteDecision(
                target=text,
                host="",
                action=ASK,
                reason="解析不出主机名，无法判定；按 ask 处理（不是放行）",
            )
        for pattern in self.deny:
            if match_pattern(pattern, host):
                return WebsiteDecision(
                    target=text,
                    host=host,
                    action=DENY,
                    rule=pattern,
                    reason=f"命中网站黑名单「{pattern}」",
                )
        for pattern in self.allow:
            if match_pattern(pattern, host):
                return WebsiteDecision(
                    target=text,
                    host=host,
                    action=ALLOW,
                    rule=pattern,
                    reason=f"命中网站白名单「{pattern}」",
                )
        return WebsiteDecision(
            target=text,
            host=host,
            action=self.default,
            reason=f"没有命中任何规则，按 default={self.default} 处理",
        )

    def describe(self) -> str:
        return (
            f"网站策略：白名单 {len(self.allow)} 条 / 黑名单 {len(self.deny)} 条 / "
            f"default={self.default}（判定顺序 deny → allow → default）。\n"
            "通配写法：`example.com`（精确）、`*.example.com`（子域，不含裸域）、`*`（全部）。\n"
            "`ask` 与 `deny` 在 Orbit 里都要经过审批通道："
            "`app/approval_channel.py` 决定「问谁」，问不到就是超时拒绝。"
        )


def _host_of(target: str) -> str:
    """从 URL 或裸主机名里取主机（小写）。取不到返回空串。"""
    text = str(target or "").strip()
    if not text:
        return ""
    if "://" in text:
        try:
            return (urlsplit(text).hostname or "").strip().casefold().rstrip(".")
        except ValueError:
            return ""
    return text.split("/", 1)[0].strip().casefold().rstrip(".")


# ── 威胁规则汇总（读 tirith，不复制规则）──────────────────────────────────────


def threat_rules() -> tuple[dict[str, str], ...]:
    """把 `app/tirith.py` 的规则表读成字典列表。**不复制正则**。

    为什么要读而不是抄：`tirith` 是执行前的权威判据，抄一份出来两边迟早漂移。
    这里只做"换个形状展示"和"按严重度汇总"。
    """
    return tuple(
        {"kind": kind, "severity": severity, "detail": detail}
        for kind, severity, _pattern, detail in tirith.RULES
    )


def threat_summary() -> dict[str, Any]:
    """按 severity 汇总 tirith 的规则，供 `orbit security threat` 展示。

    返回值里带 `total` 与 `by_severity`：**数目本身就是信息**——如果 `tirith`
    改了规则表，这里的数字会跟着变，而不是继续报一个过期的常量。
    """
    rules = threat_rules()
    by_severity = Counter(rule["severity"] for rule in rules)
    return {
        "source": "app/tirith.py::RULES",
        "total": len(rules),
        "severities": list(tirith.SEVERITIES),
        "by_severity": {name: by_severity.get(name, 0) for name in tirith.SEVERITIES},
        "block_at": tirith.DEFAULT_BLOCK_AT,
        "rules": list(rules),
    }


def threat_scan(command: str, *, settings: Any = None, block_at: str | None = None) -> dict[str, Any]:
    """对一条命令跑一遍 tirith，返回可 JSON 化的结果（含拒绝说明）。"""
    result = tirith.scan(command, settings=settings, block_at=block_at)
    payload = result.as_dict()
    payload["refusal"] = tirith.refusal(result) if result.blocked else ""
    return payload
