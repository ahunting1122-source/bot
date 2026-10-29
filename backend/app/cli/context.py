"""命令运行时上下文：配置、存储、异步桥、输出。

一条硬规则：**命令 handler 不碰全局状态，也不自己建 Store**。所有副作用入口都在
``Context`` 上，于是测试可以塞一个假的 Context，``--json`` 也能在一处统一生效。

关于异步桥：Orbit 的存储层是 SQLAlchemy async（``aiosqlite``），而 CLI 的 handler
是同步的。这里用一个**懒创建、命令结束即关闭**的事件循环把它们接起来——
不用 ``asyncio.run`` 是因为一条命令里可能多次查询，``asyncio.run`` 每次都新建/关闭
循环，而 aiosqlite 的连接池绑在循环上，第二次查询会炸。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import unicodedata
from collections.abc import Coroutine, Iterable, Sequence
from pathlib import Path
from typing import Any

from app.config import Settings


class CliError(RuntimeError):
    """可预期的命令失败：打印消息 + 返回码，不打栈。

    与"真 bug"区分开：真 bug 让异常冒出去，栈信息才有价值。
    """

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


async def _drain(loop: asyncio.AbstractEventLoop) -> None:
    """在循环自己的线程上等异步生成器收尾。``close`` 用。"""
    await loop.shutdown_asyncgens()


def display_width(text: str) -> int:
    """终端显示宽度。CJK 全角字符占两列，不算就会让表格错位。"""
    width = 0
    for char in text:
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def pad(text: str, width: int) -> str:
    """按显示宽度右侧补空格。"""
    return text + " " * max(0, width - display_width(text))


def truncate(text: str, width: int) -> str:
    """按显示宽度截断并加省略号。表格里宁可截断也不要换行。"""
    if display_width(text) <= width:
        return text
    out, used = [], 0
    for char in text:
        step = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if used + step > width - 1:
            break
        out.append(char)
        used += step
    return "".join(out) + "…"


class Context:
    """一次命令调用的全部环境。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        json_output: bool = False,
        quiet: bool = False,
        data_dir: Path | None = None,
    ) -> None:
        self.settings = settings or Settings()
        #: `--source` 的落地处（MATRIX 1.7 / 6.4）。
        #:
        #: 为什么挂在 `Context` 上而不是 `state`：`_apply_flags()` 与 `_submit()`
        #: 在**不同的层**，`state` 传不过去。而 `--source` 的语义是「这个任务从哪来」，
        #: 它要被 `_submit()` 拿去当 `enqueue(..., platform=...)`。
        self.session_source = ""
        #: --usage-file 用的 task_id（_one_turn 写，_write_usage_report 读）。
        self.last_task_id: str | None = None
        if data_dir is not None:
            self.settings = self.settings.model_copy(update={"data_dir": data_dir})
        self.json = json_output
        self.quiet = quiet
        self._loop: asyncio.AbstractEventLoop | None = None
        #: 跑 ``_loop`` 的那条专用线程。``close`` 要用它 join，所以必须一开始就有值。
        self._thread: threading.Thread | None = None
        self._store: Any = None
        self._payload: dict[str, Any] | None = None

    # ── 配置水合 ────────────────────────────────────────────────────────────
    def hydrate(self) -> None:
        """把"运行时设置 + 系统 keyring"读进 ``settings``。

        必须做这一步，否则 CLI 看到的模型密钥永远是空的（密钥存在系统 keyring，
        不落任何配置文件），``orbit status`` 就会谎报"未配置"。

        刻意**不**复用 ``ConnectorConfig.load()``：那个方法在检测到旧版 ``notion_url``
        时会顺手改盘（迁移副作用）。查询类命令不该改数据，所以这里只读。
        """
        path = self.settings_path
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            if isinstance(data, dict):
                for key, value in data.items():
                    # **只写「声明字段」，不写「任何有同名属性的东西」。**
                    #
                    # 原来是 `hasattr(self.settings, key)` —— 那个守卫**挡得住裸属性，
                    # 挡不住 property**：`Settings` 上有 `machine_wide` /
                    # `file_scope_label` 两个只读 property，**pydantic 还自带
                    # `model_extra` / `model_fields_set`**。这四个 `hasattr` 全返回 True，
                    # 而 `setattr` 会抛
                    # `AttributeError: property '...' has no setter` ——
                    # **未捕获 → CLI 启动直接崩**。
                    #
                    # 换句话说：用户只要在 `settings.json` 里写一个合法但只读的属性名，
                    # 整条 CLI 就起不来，而错误信息（property 没有 setter）与
                    # 「你的配置写错了」毫无关系。
                    #
                    # `key in type(...).model_fields` 的语义正好是「只有声明字段可写」：
                    # 挡裸属性 + 挡 property + 挡 pydantic 内置属性，一次解决。
                    #
                    # **未来风险（留给以后的人一句话）**：这里只写 `model_fields` 里的字段。
                    # **将来若加入 `allow_*` / `skip_approvals` 这类敏感字段，
                    # 必须显式决定它是否允许被 `settings.json` 覆盖**——否则任何能写
                    # 这个文件的东西都能关掉审批，配置面就变成提权面。
                    if key in type(self.settings).model_fields:
                        setattr(self.settings, key, value)
        try:
            import keyring
        except Exception:  # keyring 不可用：密钥留空，由 doctor 报出来
            return
        service = f"orbit:{Path(self.settings.data_dir).resolve()}"
        for key in ("model_api_key", "github_token"):
            try:
                secret = keyring.get_password(service, key)
            except Exception:
                secret = None
            if secret:
                from pydantic import SecretStr

                setattr(self.settings, key, SecretStr(secret))

    # ── 路径 ────────────────────────────────────────────────────────────────
    @property
    def data_dir(self) -> Path:
        return Path(self.settings.data_dir)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.sqlite"

    @property
    def checkpoint_path(self) -> Path:
        return self.data_dir / "checkpoints.sqlite"

    @property
    def settings_path(self) -> Path:
        """运行时可改设置的落点（前端"连接与设置"写的就是这个文件）。"""
        return self.data_dir / "settings.json"

    def require_db(self) -> Path:
        """要求数据库存在，否则给出可操作的报错而不是 SQLAlchemy 的栈。"""
        path = self.db_path
        if not path.exists():
            raise CliError(f"数据库不存在：{path}（先运行 `orbit setup` 或启动一次后端）")
        return path

    # ── 异步桥 ──────────────────────────────────────────────────────────────
    #
    # 为什么**总是**用一条独立线程上的独立事件循环，而不是 `asyncio.run`：
    #
    # 1. `asyncio.run` 每次新建并关闭循环，而 aiosqlite 的连接池绑在创建它的循环上——
    #    一条命令里第二次查询就会炸（"Event loop is closed"）。
    # 2. 更隐蔽的一种：`main()` 被**异步宿主**调用时（测试、将来可能的 API 端点），
    #    当前线程里已经有运行中的循环，`run_until_complete` 会直接抛
    #    "Cannot run the event loop while another loop is running"，而且协程对象
    #    已经创建却永远没被 await，只留一条 RuntimeWarning，排查成本极高。
    #
    # 固定用"一条专用线程 + 一个常驻循环"就把这两种情况一起解决：调用方在同步还是
    # 异步上下文里都一样，Store 的连接也始终活在同一个循环上。
    def _ensure_loop_thread(self) -> None:
        if self._loop is not None:
            return
        # 先落到局部变量再用：闭包里 `self._loop` 的类型是 Optional，
        # mypy 不会因为上面判过空就认；而且局部变量能让"这条线程跑的就是这个循环"显式可见。
        loop = asyncio.new_event_loop()
        self._loop = loop
        ready = threading.Event()

        def runner() -> None:
            asyncio.set_event_loop(loop)
            loop.call_soon(ready.set)
            loop.run_forever()

        self._thread = threading.Thread(target=runner, daemon=True, name="orbit-cli-loop")
        self._thread.start()
        if not ready.wait(5):
            raise CliError("无法启动 CLI 事件循环线程")

    def loop(self) -> asyncio.AbstractEventLoop:
        self._ensure_loop_thread()
        assert self._loop is not None  # _ensure_loop_thread 保证
        return self._loop

    def run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """在专用循环线程上跑一个协程并等结果。异常原样抛回调用方。"""
        loop = self.loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result()

    @property
    def store(self) -> Any:
        """懒加载 Store。命令声明 ``needs_store=True`` 时由 main 保证数据库存在。"""
        if self._store is None:
            from app.storage import Store

            self._store = Store(self.require_db())
        return self._store

    def close(self) -> None:
        """释放数据库连接、停掉循环线程。``main`` 在 finally 里调用。

        顺序不能反：先在有循环的情况下 dispose 引擎（否则连接池没人关），
        再停循环、关线程。**不能在别的线程上直接 ``run_until_complete``**——
        那个循环正跑在 CLI 线程里，直调是数据竞争。
        """
        if self._store is not None:
            try:
                self.run(self._store.engine.dispose())
            except Exception:  # 关闭失败不该覆盖命令本身的退出码
                pass
            self._store = None
        loop, thread = self._loop, self._thread
        self._loop, self._thread = None, None
        if loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(_drain(loop), loop).result(timeout=5)
        except Exception:
            pass
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass
        if thread is not None:
            thread.join(timeout=5)
        try:
            loop.close()
        except Exception:
            pass

    # ── 输出 ────────────────────────────────────────────────────────────────
    def out(self, *parts: str) -> None:
        """普通输出。``--json`` 模式下**静默**——结构化输出由 :meth:`emit` 独占。"""
        if self.json:
            return
        print(*parts)

    def note(self, *parts: str) -> None:
        """``--quiet`` 下静默的附加信息。"""
        if self.quiet:
            return
        self.out(*parts)

    def warn(self, message: str) -> None:
        print(f"警告：{message}", file=sys.stderr)

    def emit(self, payload: dict[str, Any]) -> None:
        """``--json`` 的结构化载荷。非 json 模式下什么都不做。"""
        if self.json:
            self._payload = payload

    def table(self, headers: Sequence[str], rows: Iterable[Sequence[str]]) -> None:
        """等宽对齐的文本表。空行集只打印表头，不打印"（空）"这种废话。"""
        if self.json:
            return
        body = [[str(cell) for cell in row] for row in rows]
        widths = [display_width(header) for header in headers]
        for row in body:
            for index, cell in enumerate(row):
                if index < len(widths):
                    widths[index] = max(widths[index], display_width(cell))
        print("  ".join(pad(header, widths[i]) for i, header in enumerate(headers)).rstrip())
        print("  ".join("-" * widths[i] for i in range(len(headers))))
        for row in body:
            print("  ".join(pad(cell, widths[i]) for i, cell in enumerate(row)).rstrip())

    def finish(self) -> None:
        """把 ``--json`` 载荷刷出去。由 ``main`` 在 handler 成功后调用一次。"""
        if self.json and self._payload is not None:
            print(json.dumps(self._payload, ensure_ascii=False, indent=2, default=str))


def env_flag(name: str, default: bool = False) -> bool:
    """把 ``ORBIT_*`` 风格的环境变量读成布尔。坏值退回默认，不抛错。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
