"""终端后端抽象：把"命令在哪台机器/哪个环境里跑"变成一份可查的声明（MATRIX 5.18）。

## 为什么要有这一层

`run_command` 原来把"执行"写死成"本机 `subprocess` + `shell=True`"。这在一台单机应用
里够用，但它把三件事混在一起了：**在哪跑**（本机 / WSL / 容器）、**怎么跑**
（shell 形状）、**跑完怎么读**（stdout/stderr/退出码）。混在一起的后果是"想加一个
后端"必须改工具层，而工具层是安全边界所在——改它就得重新论证一遍安全性。

这一层只抽出**在哪跑**这一维：`TerminalBackend` 协议 + 三个实现（本机 / WSL / 容器）。
`run_command` 改成"先 `resolve_backend()` 再 `backend.run()`"，默认仍是 `local`，
**行为与改动前逐字一致**（有回归用例锁着）。

## 不可用的后端必须说实话

本机可能没装 WSL、可能没有 docker。这时 `available()` 返回 `False`，`describe()`
给出**具体原因**（"找不到 wsl.exe" / "WSL 已安装但没有发行版" / "容器不存在或未运行"），
而不是返回一个"看起来能用、一跑就炸"的对象。假装支持比不支持更坏：用户会花半小时
排查自己根本没装的东西。

## 三个后端各自的能力差异

| 后端 | 工作目录 | 环境变量 | 备注 |
|---|---|---|---|
| `local` | 宿主绝对路径 | 支持（整份继承） | 与改动前完全一致 |
| `wsl` | `--cd` 传宿主路径给 wsl.exe | **不支持**（WSL 只按 `WSLENV` 白名单翻译） | 需要 wsl.exe 与至少一个发行版 |
| `container` | **容器内**路径（不是宿主路径） | 支持（`docker exec -e`） | 需要 docker CLI + 运行中的容器；`docker` 组等于 root |

`env` 在 `wsl` 后端不生效这件事会以 `notice` 的形式回给模型——静默忽略会让模型以为
变量传进去了，然后在 `$FOO` 为空时得到一段莫名其妙的输出。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

#: 默认后端。留 `local` 是为了"引入这一层不改变任何既有行为"。
DEFAULT_BACKEND = "local"

#: 全部后端名。`resolve_backend` 只认这些；未知名字**明确报错**而不是退回默认——
#: 静默退回会让 `terminal_backend: wsl` 拼错的人以为自己在 WSL 里跑。
BACKEND_NAMES: tuple[str, ...] = ("local", "wsl", "container")

#: 探测子进程的超时。`wsl -l -q` / `docker inspect` 冷启动可能要一两秒，
#: 但它们**不该**让一条诊断命令卡住，所以给一个很短的上限。
PROBE_TIMEOUT = 5

#: 运行时可改设置里读后端名的键（与 `approval_mode` 同一套读法：
#: `settings.json` 优先，其次 `Settings` 字段，都没有就 `local`）。
SETTING_BACKEND = "terminal_backend"
SETTING_CONTAINER = "terminal_container"


@dataclass
class TerminalResult:
    """一次命令执行的结果。

    `as_dict()` 刻意与 `app.tools.run_shell` 的返回**逐键一致**（键名、顺序、取值），
    唯一的额外信息 `backend` 留在属性上而不进 payload：`run_command` 的返回形状是
    模型已经学会的东西，为了"多一个字段"去改它，会让既有提示词与解析逻辑一起漂。
    """

    backend: str
    command: str
    cwd: str
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    truncated: bool
    seconds: float
    notice: str = ""

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.exit_code == 0

    def as_dict(self) -> dict:
        """`run_command` 的返回值。与改动前的 `run_shell` 返回逐键一致。"""
        payload = {
            "command": self.command,
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "truncated": self.truncated,
            "seconds": self.seconds,
        }
        if self.notice:
            payload["notice"] = self.notice
        return payload


@runtime_checkable
class TerminalBackend(Protocol):
    """执行后端。四个成员是最小集：叫什么、能不能用、怎么跑、为什么不能用。"""

    name: str

    def available(self) -> bool:
        """此刻能不能真的执行命令。**会做真实探测**（不缓存，见各实现说明）。"""
        ...

    def run(
        self,
        command: str,
        *,
        cwd: Path | str,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> TerminalResult:
        """执行一条命令。同步——调用方负责放到线程里。"""
        ...

    def describe(self) -> str:
        """一行"这是什么 + 此刻能不能用 + 不能用为什么"。给人看，也给模型看。"""
        ...


# ── 设置读取 ────────────────────────────────────────────────────────────────


def _runtime_settings(settings: Any) -> dict:
    """读 `<data_dir>/settings.json`（前端设置页与 `orbit config set` 写的就是它）。

    为什么要这条兜底：后端名是要能**当场改**的东西，而 `app/config.py` 的 `Settings`
    是启动期模型——只放在那里等于"改完必须重启"。读法与 `app/approvals.py` 的
    `_runtime_settings` 一致（这里不 import 那个私有函数：跨模块借用私有名比重复
    五行代码更容易出事）。
    """
    data_dir = getattr(settings, "data_dir", None)
    if data_dir is None:
        return {}
    path = Path(data_dir) / "settings.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def resolve_backend_name(settings: Any = None) -> str:
    """当前配置的后端名。非法值**原样返回**，交给 :func:`resolve_backend` 报错。

    刻意不在这里"修正"成 `local`：拼错的 `wsl2` 被静默改成 `local` 之后，用户会以为
    自己在 WSL 里跑，实际每条命令都落在宿主上——这种错必须响亮。
    """
    value = _runtime_settings(settings).get(SETTING_BACKEND)
    if value is None and settings is not None:
        value = getattr(settings, SETTING_BACKEND, None)
    text = str(value or DEFAULT_BACKEND).strip().lower()
    return text or DEFAULT_BACKEND


def container_name(settings: Any = None) -> str:
    """容器名。`settings.json` 优先，其次环境变量 `ORBIT_DOCKER_CONTAINER`。"""
    value = _runtime_settings(settings).get(SETTING_CONTAINER)
    if not value:
        value = os.environ.get("ORBIT_DOCKER_CONTAINER", "")
    return str(value or "").strip()


# ── 共用：跑一条"后端命令" ──────────────────────────────────────────────────


def _spawn(
    argv: list[str],
    backend: str,
    command: str,
    cwd: Path | str,
    timeout: int,
    env: dict[str, str] | None = None,
) -> TerminalResult:
    """跑 argv 并回收结果。超时杀整棵树，裁剪与解码复用 `app.tools`。

    延迟 import：`app/tools.py` 要 import 本模块来做分发，模块级互 import 会成环。
    """
    from app.tools import clip_text, decode_output, terminate_tree

    started = time.time()
    process = subprocess.Popen(  # noqa: S603 - argv 是显式列表，不经 shell
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=(os.name != "nt"),
    )
    timed_out = False
    try:
        raw_out, raw_err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_tree(process)
        try:
            raw_out, raw_err = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            raw_out, raw_err = b"", b""
    stdout, cut_out = clip_text(decode_output(raw_out))
    stderr, cut_err = clip_text(decode_output(raw_err))
    result = TerminalResult(
        backend=backend,
        command=command,
        cwd=str(cwd),
        exit_code=None if timed_out else process.returncode,
        timed_out=timed_out,
        stdout=stdout,
        stderr=stderr,
        truncated=cut_out or cut_err,
        seconds=round(time.time() - started, 2),
    )
    if timed_out:
        result.notice = f"命令超过 {timeout} 秒仍未结束，已终止整棵进程树。"
    return result


# ── 本机 ────────────────────────────────────────────────────────────────────


class LocalBackend:
    """本机 `subprocess`（Windows 走 cmd.exe，其余走 sh）。改动前的唯一行为。"""

    name = "local"

    #: 能力说明（与状态分开：`describe()` 拼状态，`label()` 只讲"这是什么"）。
    LABEL = "local：本机 subprocess（shell=True，Windows 走 cmd.exe）"

    def probe(self) -> tuple[bool, str]:
        """不 spawn 任何进程：Windows 的 cmd.exe 是系统组件，POSIX 只看 /bin/sh。

        与 `app/toolsets._check_shell` 同一判据——"能不能执行命令"这个问题只能有一个
        答案，两处各写一套必然漂。
        """
        if os.name == "nt":
            return True, ""
        if Path("/bin/sh").exists():
            return True, ""
        return False, "找不到 /bin/sh"

    def available(self) -> bool:
        return self.probe()[0]

    def run(
        self,
        command: str,
        *,
        cwd: Path | str,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> TerminalResult:
        """委托 `app.tools.run_shell`：超时杀整棵树、头尾裁剪、GBK 兜底解码都在那里。

        `env` 直接透传（`run_shell` 的 `env=None` 与改动前的行为逐字一致）。
        """
        from app.tools import run_shell

        payload = run_shell(command, Path(cwd), timeout, env=env)
        return TerminalResult(
            backend=self.name,
            command=payload["command"],
            cwd=payload["cwd"],
            exit_code=payload["exit_code"],
            timed_out=payload["timed_out"],
            stdout=payload["stdout"],
            stderr=payload["stderr"],
            truncated=payload["truncated"],
            seconds=payload["seconds"],
            notice=payload.get("notice", ""),
        )

    def label(self) -> str:
        return self.LABEL

    def describe(self) -> str:
        ok, reason = self.probe()
        return f"{self.label()}；{'可用' if ok else '不可用：' + reason}"


# ── WSL ─────────────────────────────────────────────────────────────────────


def _decode_wsl(raw: bytes | None) -> str:
    """解 `wsl.exe` 的输出。`wsl -l -q` 输出的是 **UTF-16LE**，不是 UTF-8。

    直接按 UTF-8 解会得到一串带 `\\x00` 的乱码，于是"发行版列表"看起来永远是空的——
    这个坑会把"已装 WSL 但没有发行版"和"装了发行版"混成同一个结论。
    """
    if not raw:
        return ""
    for encoding in ("utf-16-le", "utf-8", "gbk"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        # UTF-16LE 解出来的正常文本不该带 NUL；带 NUL 说明猜错了编码。
        if "\x00" not in text:
            return text.replace("\r", "")
    return raw.decode("utf-8", errors="replace")


class WslBackend:
    """`wsl.exe -e`：把命令交给 WSL 里的 shell。只在 Windows 上可能有。"""

    name = "wsl"

    LABEL = (
        "wsl：wsl.exe -e sh -lc（工作目录用 --cd 传宿主路径；"
        "环境变量不支持注入，需要时请在命令里 export）"
    )

    def _binary(self) -> str:
        return shutil.which("wsl.exe") or shutil.which("wsl") or ""

    def probe(self) -> tuple[bool, str]:
        """真实探测：wsl.exe 在 + 至少一个发行版。返回 `(可用?, 原因)`。"""
        if os.name != "nt":
            return False, f"只有 Windows 才有 WSL（当前平台是 {os.name}）"
        binary = self._binary()
        if not binary:
            return False, "找不到 wsl.exe：本机没装 WSL（可运行 wsl --install）"
        try:
            completed = subprocess.run(
                [binary, "-l", "-q"],
                capture_output=True,
                timeout=PROBE_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, f"wsl -l -q 超过 {PROBE_TIMEOUT} 秒没返回（WSL 子系统可能没启动）"
        except OSError as exc:
            return False, f"调用 wsl.exe 失败：{exc}"
        if completed.returncode != 0:
            detail = _decode_wsl(completed.stderr).strip().splitlines()
            hint = detail[0][:200] if detail else ""
            return False, f"wsl -l -q 返回 {completed.returncode}：{hint or '没有输出'}"
        distros = [
            line.strip() for line in _decode_wsl(completed.stdout).splitlines() if line.strip()
        ]
        if not distros:
            return False, "WSL 已安装但没有可用发行版（先 wsl --install -d Ubuntu）"
        return True, ""

    def available(self) -> bool:
        return self.probe()[0]

    def run(
        self,
        command: str,
        *,
        cwd: Path | str,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> TerminalResult:
        ok, reason = self.probe()
        if not ok:
            raise ValueError(f"wsl 后端不可用：{reason}")
        argv = [self._binary()]
        if cwd:
            # `--cd` 传的是**宿主路径**，wsl.exe 自己翻译。老版本 wsl.exe 不支持它，
            # 会直接报错——那样 stderr 里能看到原因，比静默退回宿主目录好得多。
            argv += ["--cd", str(cwd)]
        argv += ["-e", "sh", "-lc", command]
        payload = _spawn(argv, self.name, command, cwd, timeout)
        if env:
            payload.notice = (
                (payload.notice + " " if payload.notice else "")
                + "wsl 后端不支持注入环境变量（WSL 只按 WSLENV 白名单翻译）；"
                "需要变量请在命令里显式 export。"
            )
        return payload

    def label(self) -> str:
        return self.LABEL

    def describe(self) -> str:
        ok, reason = self.probe()
        return f"{self.label()}；{'可用' if ok else '不可用：' + reason}"


# ── 容器 ────────────────────────────────────────────────────────────────────


class ContainerBackend:
    """`docker exec`：在**已经跑起来**的容器里执行命令。

    刻意不负责"起容器"：起容器要决定镜像、网络、挂载、用户，那是另一个决定
    （而且本机无网络，拉不了镜像）。这里只做"进已有的容器里跑一条命令"。
    """

    name = "container"

    def __init__(self, container: str = "") -> None:
        self.container = (container or "").strip()

    def _binary(self) -> str:
        return shutil.which("docker") or shutil.which("docker.exe") or ""

    def label(self) -> str:
        target = self.container or "<未指定容器>"
        return f"container：docker exec -w（容器 {target}；工作目录是**容器内**路径）"

    def probe(self) -> tuple[bool, str]:
        binary = self._binary()
        if not binary:
            return False, "找不到 docker CLI：本机没装 Docker"
        if not self.container:
            return False, (
                "未指定容器名（在 settings.json 里设 terminal_container，"
                "或设环境变量 ORBIT_DOCKER_CONTAINER）"
            )
        try:
            completed = subprocess.run(
                [binary, "inspect", "-f", "{{.State.Running}}", self.container],
                capture_output=True,
                timeout=PROBE_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, (
                f"docker inspect 超过 {PROBE_TIMEOUT} 秒没返回（docker 守护进程可能没起来）"
            )
        except OSError as exc:
            return False, f"调用 docker 失败：{exc}"
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip().splitlines()
            hint = detail[0][:200] if detail else ""
            return False, f"容器 {self.container} 不存在或不可查：{hint or '没有输出'}"
        if completed.stdout.decode("utf-8", errors="replace").strip().lower() != "true":
            return False, f"容器 {self.container} 存在但没在运行（先 docker start {self.container}）"
        return True, ""

    def available(self) -> bool:
        return self.probe()[0]

    def run(
        self,
        command: str,
        *,
        cwd: Path | str,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> TerminalResult:
        ok, reason = self.probe()
        if not ok:
            raise ValueError(f"container 后端不可用：{reason}")
        argv = [self._binary(), "exec"]
        if cwd:
            # 注意：这是**容器内**的路径，不是宿主路径。传宿主路径会得到
            # "no such file or directory"，那是如实报错，不是这里的 bug。
            argv += ["-w", str(cwd)]
        for key, value in (env or {}).items():
            argv += ["-e", f"{key}={value}"]
        argv += [self.container, "sh", "-lc", command]
        return _spawn(argv, self.name, command, cwd, timeout)

    def describe(self) -> str:
        ok, reason = self.probe()
        return f"{self.label()}；{'可用' if ok else '不可用：' + reason}"


# ── 解析 / 视图 ─────────────────────────────────────────────────────────────

#: 三个实现的联合。`resolve_backend` 返回它而不是 `TerminalBackend`：诊断命令要调
#: `probe()` 拿"不可用的具体原因"，而协议里刻意只放执行所需的四个成员。
AnyBackend = LocalBackend | WslBackend | ContainerBackend


def resolve_backend(name: str | None = None, settings: Any = None) -> AnyBackend:
    """按名字取后端。未知名字**明确报错**（并列出可用的三个）。

    `name` 留空 = 用配置里的（:func:`resolve_backend_name`）。

    这里**不**检查 `available()`：解析是解析，能不能跑是另一件事。诊断命令
    （`orbit terminal list`）需要拿到"不可用但存在"的后端来打印原因；
    真正执行时由各后端的 `run()` 自己在前置检查失败时抛错。

    返回类型是**具体实现的联合**而不是 `TerminalBackend`：诊断命令要调 `probe()`
    拿"不可用的具体原因"，而协议里刻意只放执行所需的四个成员（`name` /
    `available` / `run` / `describe`）——协议是给调用方用的最小契约，不是实现清单。
    """
    key = (name or resolve_backend_name(settings) or DEFAULT_BACKEND).strip().lower()
    if key == "local":
        return LocalBackend()
    if key == "wsl":
        return WslBackend()
    if key in {"container", "docker"}:
        return ContainerBackend(container_name(settings))
    raise ValueError(
        f"未知终端后端 {name!r}。可用："
        + "、".join(BACKEND_NAMES)
        + "（local=本机 subprocess；wsl=Windows Subsystem for Linux；container=docker exec）"
    )


def list_backends(settings: Any = None) -> list[dict[str, Any]]:
    """三个后端的现状。**会做真实探测**（诊断命令专用，不要挂在每轮请求上）。

    每个后端只探测**一次**：`probe()` 会 spawn 子进程（`wsl -l -q`、`docker inspect`），
    在循环里重复调用会把一条诊断命令拖成好几秒。
    """
    rows: list[dict[str, Any]] = []
    for name in BACKEND_NAMES:
        backend = resolve_backend(name, settings)
        available, reason = backend.probe()
        rows.append(
            {
                "name": name,
                "available": available,
                "reason": reason,
                "describe": f"{backend.label()}；{'可用' if available else '不可用：' + reason}",
            }
        )
    return rows
