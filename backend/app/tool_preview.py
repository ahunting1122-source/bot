"""预览面板（`5.14` 的 `desktop` 域前 4 条）。

## 这 4 条为什么能做

Hermes 的 `desktop_preview` 是「在聊天旁开一个预览面板」。Orbit 没有桌面壳，
**但「预览」本身是一个状态**：当前预览哪个文件/URL、用户在上面留了什么标记。

**所以这一层做成「预览状态存储」**：
- `desktop_preview`：登记一个要预览的目标（本地文件路径或 URL）；
- `read_preview`：读当前预览目标的正文（本地文件直接读，URL 返回地址与说明）；
- `annotate_preview`：在预览上留一条持久标记；
- `close_preview`：清掉当前预览。

**UI 渲染那一半属于 Web UI**（`frontend/`），本模块只做后端状态 ——
**这比假装「面板已经开了」诚实。**

## 为什么状态落盘而不是放内存

「预览」跨工具调用存在（先 open 再 read），而工具调用之间进程可能重启。
落 `<data>/preview/state.json` 与 `kanban/board.json` 同一个约定。

## 安全

`desktop_preview` 只接受**工作目录内**的本地文件 —— 走 `PathPolicy.check()`，
与其余读文件路径同一条规则。**预览面板不能变成绕过路径策略的旁路。**
"""

from __future__ import annotations

import json
import pathlib
import time
from typing import Any

from pydantic import Field

from app.tools import Args, Tool, resolve_path

#: 读预览时返回的最大字符数（面板是给人看的，不需要整本）。
PREVIEW_CHARS = 20_000


class PreviewOpenArgs(Args):
    target: str = Field(min_length=1, description="要预览的本地文件路径（工作目录内）或 http(s) URL")
    title: str = Field(default="", description="面板标题（空 = 用文件名）")


class PreviewReadArgs(Args):
    limit: int = Field(default=PREVIEW_CHARS, ge=100, le=200_000, description="最多返回多少字符")


class PreviewAnnotateArgs(Args):
    note: str = Field(min_length=1, description="要留在预览上的标记")


class PreviewCloseArgs(Args):
    reason: str = Field(default="", description="关闭原因（只记日志用）")


PREVIEW_TOOLS = [
    Tool("desktop_preview", "在预览面板里打开一个本地文件或 URL", PreviewOpenArgs),
    # **同一个处理器的第二个名字** —— Hermes 里两者是同一能力的不同命名。
    Tool("open_preview", "打开预览面板（`desktop_preview` 的同义工具）", PreviewOpenArgs),
    Tool("read_preview", "读当前预览面板的内容", PreviewReadArgs),
    Tool("annotate_preview", "在预览上留一条持久标记", PreviewAnnotateArgs),
    Tool("close_preview", "关闭预览面板", PreviewCloseArgs),
]


def _state_path(data_dir: str | pathlib.Path) -> pathlib.Path:
    return pathlib.Path(data_dir) / "preview" / "state.json"


def _load(data_dir: str | pathlib.Path) -> dict[str, Any]:
    path = _state_path(data_dir)
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _save(data_dir: str | pathlib.Path, state: dict[str, Any]) -> None:
    path = _state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


class PreviewTool:
    """4 个预览工具的执行器。**状态落盘，跨调用存活。**"""

    def __init__(self, data_dir: str | pathlib.Path, workspace: str | pathlib.Path) -> None:
        self.data_dir = pathlib.Path(data_dir)
        #: **路径策略的 root 是工作目录**，不是数据目录 —— 预览只能看工作目录里的文件。
        self.workspace = pathlib.Path(workspace)

    def execute(self, name, args, task_id, operation_id) -> dict[str, Any]:
        if name in ("desktop_preview", "open_preview"):
            return self._open(args)
        if name == "read_preview":
            return self._read(args)
        if name == "annotate_preview":
            return self._annotate(args)
        if name == "close_preview":
            return self._close(args)
        raise ValueError(f"未知的预览工具：{name}")

    def _open(self, args) -> dict[str, Any]:
        target = str(args["target"])
        is_url = target.startswith(("http://", "https://"))
        state: dict[str, Any] = {
            "target": target,
            "is_url": is_url,
            "title": str(args.get("title") or "") or pathlib.PurePath(target).name,
            "opened_at": time.time(),
            "annotations": [],
        }
        if not is_url:
            # **预览不能变成绕过路径策略的旁路** —— 与读文件同一条规则。
            resolved = resolve_path(self.workspace, target)
            if not resolved.is_file():
                return {"status": "error", "error": f"文件不存在：{resolved}"}
            state["target"] = str(resolved)
            state["title"] = state["title"] or resolved.name
        _save(self.data_dir, state)
        return {"status": "ok", "preview": {k: v for k, v in state.items() if k != "annotations"}}

    def _read(self, args) -> dict[str, Any]:
        state = _load(self.data_dir)
        if not state:
            return {"status": "error", "error": "没有打开的预览（先调 desktop_preview）"}
        if state.get("is_url"):
            return {
                "status": "ok",
                "kind": "url",
                "target": state.get("target"),
                "title": state.get("title"),
                "annotations": state.get("annotations") or [],
                "note": "URL 预览由 UI 渲染；后端不抓取远端内容。",
            }
        path = pathlib.Path(str(state.get("target") or ""))
        if not path.is_file():
            return {"status": "error", "error": f"预览目标已不存在：{path}"}
        text = path.read_text(encoding="utf-8", errors="replace")
        limit = int(args.get("limit") or PREVIEW_CHARS)
        return {
            "status": "ok",
            "kind": "file",
            "target": str(path),
            "title": state.get("title"),
            "chars": len(text),
            "truncated": len(text) > limit,
            "content": text[:limit],
            "annotations": state.get("annotations") or [],
        }

    def _annotate(self, args) -> dict[str, Any]:
        state = _load(self.data_dir)
        if not state:
            return {"status": "error", "error": "没有打开的预览（先调 desktop_preview）"}
        annotations = list(state.get("annotations") or [])
        annotations.append({"note": str(args["note"]), "at": time.time()})
        state["annotations"] = annotations
        _save(self.data_dir, state)
        return {"status": "ok", "annotations": annotations}

    def _close(self, args) -> dict[str, Any]:
        _state_path(self.data_dir).unlink(missing_ok=True)
        return {"status": "ok", "closed": True}
