"""桌面域剩下的 4 条：`close_terminal` / `drive_preview` / `browser_vision` / `vision_analyze`。

## 这 4 条为什么算「纯代码活」（第 122 轮）

MATRIX 里它们一直挂着「要外部条件」，**逐条复核后不是**：

| 工具 | 缺的其实是什么 |
|---|---|
| `close_terminal` | **一个「只读终端视图」的概念** —— 和预览面板同一套状态模式，纯代码 |
| `drive_preview` | **把动作转给浏览器层** —— Orbit 的等价能力在 `browser_*` 里，只是没挂到预览面板上 |
| `browser_vision` | **「工具→模型」通道** —— 截图那一半早就做完了（`last_screenshot_data_url`） |
| `vision_analyze` | **同一通道** —— `GrowthContext` 里已经有 `settings`，工具能自己建模型客户端 |

**判据：把「缺的东西」写成一句具体的话，往往就会发现它不是「缺外部服务」。**

## 模型通道怎么建

`providers.resolve_provider(ModelSettings) -> ProviderProfile` → `build_chat_model(profile, settings)`。
**没配模型时返回可照做的错误，不去猜。**

## 终端视图的诚实边界

`close_terminal` 关的是**视图**，不是进程。**这两件事在 Hermes 里也是分开的** ——
把它做成 `process kill` 会把「看一眼就关掉」变成「把跑着的东西杀掉」。
"""

from __future__ import annotations

import base64
import json
import pathlib
from typing import Any

from pydantic import Field

from app import providers as pv
from app.tools import Args, Tool

#: 视觉请求的默认提示词 —— 用户没给问题时问「你看到了什么」。
DEFAULT_VISION_PROMPT = "描述这张截图里能看到的内容；如果有报错或关键 UI 元素，指出来。"


class CloseTerminalArgs(Args):
    view_id: str = Field(default="", description="要关掉的终端视图 id（留空且 all=true 时全关）")
    all: bool = Field(default=False, description="关掉全部终端视图")


class DrivePreviewArgs(Args):
    action: str = Field(min_length=1, description="要转给浏览器层的动作名，如 open / click / fill")
    url: str = Field(default="", description="action=open 时的地址；留空则用当前预览目标")
    target: str = Field(default="", description="动作目标（选择器或 ref）")
    value: str = Field(default="", description="动作的值（如填表内容）")


class BrowserVisionArgs(Args):
    prompt: str = Field(default=DEFAULT_VISION_PROMPT, description="问模型的问题")


class VisionAnalyzeArgs(Args):
    image: str = Field(min_length=1, description="图片路径（工作目录内）或 http(s) URL 或 data: URL")
    prompt: str = Field(default=DEFAULT_VISION_PROMPT, description="问模型的问题")


DESKTOP_EXTRA_TOOLS = [
    Tool("close_terminal", "关闭某个后台进程的只读终端视图（不杀进程）", CloseTerminalArgs),
    Tool("drive_preview", "驱动预览面板里的网页（动作转给浏览器层）", DrivePreviewArgs),
    Tool("browser_vision", "看一眼当前浏览器页面并让模型描述它", BrowserVisionArgs),
    Tool("vision_analyze", "让模型分析一张图片", VisionAnalyzeArgs),
]


def _views_path(data_dir: str | pathlib.Path) -> pathlib.Path:
    return pathlib.Path(data_dir) / "terminal" / "views.json"


def load_terminal_views(data_dir: str | pathlib.Path) -> list[dict[str, Any]]:
    path = _views_path(data_dir)
    if not path.is_file():
        return []
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return loaded if isinstance(loaded, list) else []


def register_terminal_view(data_dir: str | pathlib.Path, view: dict[str, Any]) -> None:
    """登记一个终端视图。**给进程层调用** —— 开了后台进程就该有个视图。"""
    views = [v for v in load_terminal_views(data_dir) if v.get("id") != view.get("id")]
    views.append(view)
    path = _views_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(views, ensure_ascii=False, indent=2), encoding="utf-8")


def _model_settings(settings):
    """`ModelSettings` 是 **Protocol**，`Settings` 天然满足它 —— **不用构造，直接传**。"""
    return settings


class DesktopExtraTool:
    """4 条工具的执行器。**`async` 是硬要求**（注册表 await 它）。"""

    def __init__(self, data_dir, *, settings=None, browser=None, workspace=None) -> None:
        self.data_dir = pathlib.Path(data_dir)
        self.settings = settings
        self.browser = browser
        self.workspace = pathlib.Path(workspace) if workspace else None

    async def execute(self, name, args, task_id, operation_id) -> dict[str, Any]:
        if name == "close_terminal":
            return self._close_terminal(args)
        if name == "drive_preview":
            return await self._drive_preview(args, task_id, operation_id)
        if name == "browser_vision":
            return await self._browser_vision(args, task_id)
        if name == "vision_analyze":
            return await self._vision_analyze(args)
        raise ValueError(f"未知的桌面工具：{name}")

    # ---- close_terminal ----

    def _close_terminal(self, args) -> dict[str, Any]:
        views = load_terminal_views(self.data_dir)
        if not views:
            return {"status": "ok", "closed": [], "note": "当前没有终端视图"}

        if bool(args.get("all")):
            _views_path(self.data_dir).unlink(missing_ok=True)
            return {"status": "ok", "closed": [v.get("id") for v in views]}

        view_id = str(args.get("view_id") or "").strip()
        if not view_id:
            return {
                "status": "error",
                "error": "要给 view_id，或 all=true。当前视图："
                + ", ".join(str(v.get("id")) for v in views),
            }
        remaining = [v for v in views if str(v.get("id")) != view_id]
        if len(remaining) == len(views):
            return {
                "status": "error",
                "error": f"没有 id 为 `{view_id}` 的终端视图。当前："
                + ", ".join(str(v.get("id")) for v in views),
            }
        path = _views_path(self.data_dir)
        if remaining:
            path.write_text(json.dumps(remaining, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
        # **只说「关了视图」** —— 不暗示进程被停掉。
        return {"status": "ok", "closed": [view_id], "process_still_running": True}

    # ---- drive_preview ----

    async def _drive_preview(self, args, task_id, operation_id) -> dict[str, Any]:
        if self.browser is None:
            return {"status": "error", "error": "浏览器层没有接上（drive_preview 需要它）"}
        action = str(args["action"]).strip()
        payload: dict[str, Any] = {}
        for key in ("url", "target", "value"):
            if args.get(key):
                payload[key] = args[key]
        try:
            result = await self.browser.execute(action, payload, task_id, operation_id)
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": f"浏览器层拒绝了这个动作：{type(exc).__name__}: {exc}"}
        return {"status": "ok", "action": action, "result": result}

    # ---- 模型通道 ----

    async def _ask_model(self, prompt: str, image_url: str) -> dict[str, Any]:
        """把一张图 + 一句问题发给配置的模型。**没配就返回可照做的错误。**"""
        if self.settings is None:
            return {"status": "error", "error": "没有 settings，建不了模型客户端"}
        if not (getattr(self.settings, "model", "") or "").strip():
            return {"status": "error", "error": "还没配模型（`settings.model`），先配一个再调这个工具"}
        try:
            model_settings = _model_settings(self.settings)
            profile = pv.resolve_provider(model_settings)
            client = pv.build_chat_model(profile, model_settings)
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": f"建模型客户端失败：{type(exc).__name__}: {exc}"}

        try:
            reply = await client.ainvoke(
                [
                    (
                        "human",
                        [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ],
                    )
                ]
            )
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": f"模型调用失败：{type(exc).__name__}: {exc}"}

        text = getattr(reply, "content", reply)
        if isinstance(text, list):
            # 有些 provider 回的是内容块列表 —— 拼成一段文本，别把结构直接丢给模型。
            text = "".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in text
            )
        return {"status": "ok", "text": str(text)}

    async def _browser_vision(self, args, task_id) -> dict[str, Any]:
        if self.browser is None:
            return {"status": "error", "error": "浏览器层没有接上（browser_vision 需要它）"}
        data_url = self.browser.last_screenshot_data_url()
        if not data_url:
            # **截图不存在时先让它拍一张** —— 让调用方少一步。
            try:
                await self.browser.observe(task_id)
            except Exception as exc:  # noqa: BLE001
                return {"status": "error", "error": f"截图失败：{type(exc).__name__}: {exc}"}
            data_url = self.browser.last_screenshot_data_url()
        if not data_url:
            return {"status": "error", "error": "浏览器没有可用的截图（先 browser_observe 打开一个页面）"}
        return await self._ask_model(str(args.get("prompt") or DEFAULT_VISION_PROMPT), data_url)

    async def _vision_analyze(self, args) -> dict[str, Any]:
        image = str(args["image"]).strip()
        if image.startswith(("http://", "https://", "data:")):
            return await self._ask_model(str(args.get("prompt") or DEFAULT_VISION_PROMPT), image)

        if self.workspace is None:
            return {"status": "error", "error": "没有工作目录，读不了本地图片"}
        from app.tools import resolve_path

        path = resolve_path(self.workspace, image)
        if not path.is_file():
            return {"status": "error", "error": f"图片不存在：{path}"}
        suffix = path.suffix.lower().lstrip(".") or "png"
        if suffix == "jpg":
            suffix = "jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return await self._ask_model(
            str(args.get("prompt") or DEFAULT_VISION_PROMPT),
            f"data:image/{suffix};base64,{encoded}",
        )
