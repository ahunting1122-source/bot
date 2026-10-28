"""`code_execution`：把一段代码放到**子进程**里跑（MATRIX 5.17）。

## 为什么必须是子进程，不是 `exec` / `eval`

`run_code` 的入参是模型自己写的代码。如果它跑在 agent 进程里：

    while True: pass          # 整个后端服务卡死，WebSocket 全部掉线，只能强杀
    os._exit(0)               # 进程直接消失，连一条"任务失败"都写不进库
    sys.exit(1)               # SystemExit 不属于 Exception，漏网后行为取决于外层
    ctypes.string_at(0)       # 段错误：把宿主的解释器一起带走

这些都不是假想的边界情况——"写个死循环看看"是模型最常犯的错之一。子进程把这类
错误关在它**自己**的进程里：超时杀掉整棵树、非零退出码原样返回、崩溃不传染宿主。
代价是一次进程启动的开销（几十毫秒），相对"服务被一段代码带走"完全可以接受。

## 这不是沙箱（别把这段读成"安全"）

本机没有容器、没有命名空间、没有系统调用过滤，所以这一层**只**做到三件事：

1. 独立进程（崩溃/退出不传染）；
2. 超时后杀掉**整棵进程树**（复用 `app.tools.terminate_tree`：只杀直接子进程会留下
   孤儿，管道不关闭，`communicate()` 会一直阻塞，超时形同虚设）；
3. 工作目录由调用方限定（`cwd`）。

**做不到的**（如实列出，因为"以为有隔离"比"知道没有"危险得多）：

- 文件系统不受限：代码能读写当前用户能读写的任何路径；
- 网络不受限：代码里没有任何出站拦截（本机没网，但这是环境事实，不是这里的防护）；
- 资源不受限：没有内存 / CPU / 进程数上限，`[0] * 10**10` 会真的把内存吃掉；
- **环境变量继承**：子进程拿到当前进程的完整环境（含 API key），代码里一句
  `os.environ` 就能读到。刻意**不**做环境擦除：擦掉哪些键是猜的，猜错了会让正常
  代码莫名失败，而"擦干净"的假象更危险。真要隔离得靠容器
  （`docker run --network=none --read-only -e …`），本机没有。

## 为什么只支持 Python

`sys.executable` 就是**当前进程自己的**解释器，它在不在是确定的；`node` / `ruby` /
`perl` 在不在本机完全没保证，而本机无网络、装不了任何东西。与其写一个"看起来支持
多语言、真跑就 FileNotFoundError"的壳，不如明确只支持 Python 并在错误信息里说清楚。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

#: 默认超时。与 `run_command` 的 120 秒不同：一段代码的正常预期是"几秒内跑完"，
#: 30 秒还没结束基本就是死循环或阻塞等待，早点杀掉比让模型干等更省预算。
DEFAULT_TIMEOUT = 30
#: 硬上限。超过这个数的"代码执行"应该改成后台任务，而不是占着一次工具调用。
MAX_TIMEOUT = 600
#: 代码正文上限。与 `run_command` 的 8000 字符对齐一个量级：代码不是文档，
#: 写得更长说明模型在把整份文件贴进来，那件事有 `local_write_file` / `apply_patch`。
MAX_CODE_CHARS = 200_000
#: 单侧输出上限。超了按"头 + 尾"保留（报错总结在尾部，正常输出在头部）。
MAX_OUTPUT_CHARS = 8000

#: 本机保证能跑的语言。见模块 docstring 的"为什么只支持 Python"。
SUPPORTED_LANGUAGES: tuple[str, ...] = ("python",)

#: 沙箱边界的如实说明。给 CLI / 工具描述复用同一份文字，避免两处说法漂移。
SANDBOX_NOTE = (
    "这不是沙箱：独立子进程 + 超时杀整棵树 + 限定工作目录，仅此三件。"
    "文件系统、网络、内存/CPU 均不受限，且子进程继承当前进程的环境变量（含 API key）。"
    "真正的隔离需要容器（--network=none --read-only），本机没有。"
)


@dataclass
class CodeResult:
    """一次代码执行的结果。

    `exit_code` 在超时被杀时是 `None` 而不是 0：0 会被读成"成功"，而"被杀"和
    "成功"是两件完全相反的事，不能让它们在数据里长得一样。
    """

    language: str
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    seconds: float
    cwd: str
    truncated: bool = False
    notice: str = ""
    sandbox: str = SANDBOX_NOTE

    @property
    def ok(self) -> bool:
        """只有"正常结束且退出码为 0"才算成功。超时一律不算。"""
        return not self.timed_out and self.exit_code == 0

    def as_dict(self) -> dict:
        """工具返回值。键名与 `run_command` 的返回对齐（模型不必学两套形状）。"""
        payload = {
            "language": self.language,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "ok": self.ok,
            "seconds": self.seconds,
            "cwd": self.cwd,
            "truncated": self.truncated,
            "sandbox": self.sandbox,
        }
        if self.notice:
            payload["notice"] = self.notice
        return payload


def describe_sandbox() -> str:
    """边界说明（CLI 的 `terminal list` / `tools backends` 会打印它）。"""
    return SANDBOX_NOTE


def _argv(language: str, code: str) -> list[str]:
    """把代码变成 argv。**不用 `shell=True`**：代码正文里出现 `&` `|` `>` 时，
    经过 shell 会被解释成管道/重定向，跑起来的东西和模型写的根本不是一回事。
    `-c` 直接交给解释器，参数边界清晰。"""
    return [sys.executable, "-c", code]


def _child_env() -> dict[str, str]:
    """子进程环境：**继承当前进程**，只额外钉住编码。

    钉 `PYTHONIOENCODING` 是因为 Windows 下子进程默认按 cp936 输出，`print("中文")`
    会变成一堆问号，而 `decode_output` 只能按"先 utf-8 再 gbk"猜——猜错就把乱码
    交给模型。让子进程直接说 utf-8，比事后猜更可靠。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_code(
    code: str,
    *,
    language: str = "python",
    timeout: int = DEFAULT_TIMEOUT,
    cwd: str | Path | None = None,
) -> CodeResult:
    """在子进程里跑一段代码并回收结果。**同步函数**，调用方用 `asyncio.to_thread`。

    为什么是同步的：它本质是一次阻塞的 `communicate()`。放在事件循环里直接调会把
    整个进程卡住（这正是本模块要避免的那类事故），所以接口保持同步，由调用方决定
    放到哪个线程。
    """
    text = code or ""
    if not text.strip():
        raise ValueError("code 不能为空")
    if len(text) > MAX_CODE_CHARS:
        raise ValueError(f"代码超过 {MAX_CODE_CHARS} 字符上限（这次 {len(text)}）")
    lang = (language or "python").strip().lower()
    if lang not in SUPPORTED_LANGUAGES:
        raise ValueError(
            f"不支持的语言 {language!r}：本机只保证 Python"
            f"（{sys.executable} 就是当前解释器）。node / ruby / perl 在不在没有保证，"
            "而且本机无网络装不了——要跑别的语言请先用 run_command 确认解释器存在。"
        )
    # 刻意不用 `timeout or DEFAULT_TIMEOUT`：那会把 `timeout=0` 静默变成 30 秒，
    # 而"我要它立刻停"和"随便跑"是相反的意图。0 应该被拒绝（下面的范围检查会做）。
    seconds = DEFAULT_TIMEOUT if timeout is None else int(timeout)
    if not 1 <= seconds <= MAX_TIMEOUT:
        raise ValueError(f"timeout 必须在 1-{MAX_TIMEOUT} 秒之间，收到 {timeout}")
    workdir = Path(cwd).expanduser() if cwd else Path.cwd()
    if not workdir.is_dir():
        raise ValueError(f"工作目录不存在：{workdir}")

    # 延迟导入：`app/tools.py` 要在分发链里引用本模块，模块级互 import 会成环。
    from app.tools import clip_text, decode_output, terminate_tree

    started = time.time()
    process = subprocess.Popen(  # noqa: S603 - argv 是显式列表，不经 shell
        _argv(lang, text),
        cwd=str(workdir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_child_env(),
        start_new_session=(os.name != "nt"),
    )
    timed_out = False
    try:
        raw_out, raw_err = process.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_tree(process)
        try:
            raw_out, raw_err = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            raw_out, raw_err = b"", b""
    stdout, cut_out = clip_text(decode_output(raw_out), MAX_OUTPUT_CHARS)
    stderr, cut_err = clip_text(decode_output(raw_err), MAX_OUTPUT_CHARS)
    result = CodeResult(
        language=lang,
        stdout=stdout,
        stderr=stderr,
        exit_code=None if timed_out else process.returncode,
        timed_out=timed_out,
        seconds=round(time.time() - started, 2),
        cwd=str(workdir),
        truncated=cut_out or cut_err,
    )
    if timed_out:
        result.notice = f"代码超过 {seconds} 秒仍未结束，已终止整棵进程树。"
    return result
