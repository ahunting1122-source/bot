"""日志配置：结构化行格式 + 按大小轮转 + 任务上下文。

为什么必须有这个模块：Orbit 各模块都写了 ``log = logging.getLogger("orbit.xxx")``，
但**从来没有配置过任何 handler**——消息全靠 Python 的 lastResort 打到 stderr，
落到文件里是空的（`data/orbit-backend.log` 一直是 0 字节）。
结果是"出问题想查日志时，日志不存在"。这不是小瑕疵：一个没有日志的常驻服务，
排障只能靠复现。

三条设计取舍：

1. **行格式带任务上下文**。Hermes 的日志行是
   ``时间 级别 [session_id] 模块名: k=v…``。Orbit 照这个形状，把 ``task=`` 放进去——
   同一个任务的多条日志能被一条 grep 串起来，这是排障时最常用的动作。
   上下文用 ``contextvars`` 而不是全局变量：任务在 asyncio 里并发跑，
   全局变量会把两个任务的日志串味。
2. **轮转按大小而不是按天**。单机长时间运行，按天轮转会让"一天里出 2GB 异常日志"
   把盘写满；按大小（5 MB × 5 份）有硬上限。
3. **错误单独一份**。``errors.log`` 只收 WARNING 以上：出事后第一眼看它，
   不用在几万行 INFO 里翻。

``configure()`` 是幂等的：重复调用会先摘掉旧 handler 再装新的。
不这么做的话，测试里每个用例配一次日志，root logger 会挂上几百个 handler，
既拖慢又会往已删除的临时目录写文件。
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import logging.handlers
import sys
from collections.abc import Iterator
from pathlib import Path

#: 单文件上限 5 MB × 5 份 = 25 MB 硬上限（主日志）。
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5

#: 错误日志留少一点：它应该很少被写，写多了本身就是信号。
ERROR_MAX_BYTES = 2 * 1024 * 1024
ERROR_BACKUP_COUNT = 3

#: 行格式。``task`` 由 :class:`TaskContextFilter` 注入。
FORMAT = "%(asctime)s %(levelname)s [task=%(task_id)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

#: 当前任务 id。用 contextvar 而不是全局变量：asyncio 下多个任务并发，
#: 全局变量会把 A 任务的日志标成 B 任务的。
_task_id: contextvars.ContextVar[str] = contextvars.ContextVar("orbit_task_id", default="-")

#: 标记我们自己装的 handler，便于幂等重配时精确摘除（不去动别人的 handler）。
_TAG = "_orbit_logging"


def current_task_id() -> str:
    return _task_id.get()


@contextlib.contextmanager
def task_context(task_id: str) -> Iterator[None]:
    """在 ``with`` 块内给所有日志行打上 ``task=<id>``。退出时恢复上一个值。"""
    token = _task_id.set(task_id or "-")
    try:
        yield
    finally:
        _task_id.reset(token)


class TaskContextFilter(logging.Filter):
    """把 contextvar 里的任务 id 塞进 record。缺了它格式化会 KeyError。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.task_id = current_task_id()
        return True


def log_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "logs"


def _detach() -> None:
    """摘掉上一次装的 handler（只摘带我们标记的那些）。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _TAG, False):
            root.removeHandler(handler)
            with contextlib.suppress(Exception):
                handler.close()


def configure(
    data_dir: Path,
    *,
    level: str = "INFO",
    console: bool = False,
) -> Path:
    """装好文件日志（主 + 错误）并返回日志目录。

    ``console=True`` 时额外挂一个 stderr handler——CLI/前台运行时用；
    常驻服务不要开，否则同一行会既进文件又进终端。
    """
    directory = log_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    _detach()

    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    formatter = logging.Formatter(FORMAT, datefmt=DATE_FORMAT)
    context_filter = TaskContextFilter()

    main_handler = logging.handlers.RotatingFileHandler(
        directory / "orbit.log",
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    main_handler.setFormatter(formatter)
    main_handler.addFilter(context_filter)
    setattr(main_handler, _TAG, True)
    root.addHandler(main_handler)

    error_handler = logging.handlers.RotatingFileHandler(
        directory / "errors.log",
        maxBytes=ERROR_MAX_BYTES,
        backupCount=ERROR_BACKUP_COUNT,
        encoding="utf-8",
    )
    error_handler.setFormatter(formatter)
    error_handler.addFilter(context_filter)
    error_handler.setLevel(logging.WARNING)
    setattr(error_handler, _TAG, True)
    root.addHandler(error_handler)

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        stream.addFilter(context_filter)
        setattr(stream, _TAG, True)
        root.addHandler(stream)

    return directory


def reset() -> None:
    """摘掉我们装的 handler。测试用，避免用例之间互相写对方的临时目录。"""
    _detach()
    _task_id.set("-")
