"""桌面 UI 状态（`5.14` 的 `desktop` 域 4 条：`focus_pane` / `apply_layout` / `tip` / `tour`）。

## 为什么这 4 条现在能做了（第 121 轮）

第 107 轮我把它们判成「纯 UI 状态，没有 UI 渲染就是空转，做了等于假覆盖」。
**那个判断的前提是「Orbit 没有渲染它们的界面」。用户明确说「UI 状态也做进去，
可以在 UI 上也模仿 Hermes」—— 前提变了，所以这 4 条现在做，而且两头都做：**

- **后端**（本模块）：状态落盘 + 4 条工具；
- **前端**（`frontend/src/UiState.tsx`）：真的渲染聚焦环 / 布局 / 气泡 / 导览。

**判据：一条 UI 工具的价值 = 状态能存 + 用户能看到。缺一半就是假覆盖。
用户把「看到」那一半交给我之后，这 4 条才成立。**

## 状态形状（落 `<data>/ui/state.json`）

```json
{
  "focused_pane": "preview",
  "layout": {"preset": "split", "panes": [{"id": "chat", "size": 60}, ...]},
  "tips": [{"id": "...", "target": "composer", "text": "...", "placement": "top", "expires_at": 0}],
  "tour": {"steps": [{"target": "...", "title": "...", "body": "..."}], "index": 0, "active": true}
}
```

**`tips` 带 `expires_at`（epoch 秒）** —— 气泡是「此刻指一下」，
不会永远挂在那儿；前端读到过期的不渲染。

## 为什么和预览状态用同一套约定

都是「一次工具调用写的 UI 状态，前端轮询读」。**共用 `_load`/`_save` 的形状，
读代码的人不用学两套。**
"""

from __future__ import annotations

import json
import pathlib
import time
import uuid
from typing import Any

from pydantic import Field

from app.tools import Args, Tool

#: 已知面板。`focus_pane` 只接受这些 —— **拼错的面板名会让前端静默什么都不高亮**。
PANES = ("chat", "preview", "files", "skills", "memories", "settings", "kanban")

#: 内置布局预设。Hermes 的 `apply_layout` 是「应用保存的布局预设」，这里给 4 个常用的。
LAYOUT_PRESETS: dict[str, list[tuple[str, int]]] = {
    "single": [("chat", 100)],
    "split": [("chat", 60), ("preview", 40)],
    "triple": [("chat", 50), ("preview", 30), ("files", 20)],
    "review": [("preview", 65), ("chat", 35)],
}

#: 气泡默认存活秒数。
DEFAULT_TIP_TTL = 60


class FocusPaneArgs(Args):
    pane: str = Field(min_length=1, description="要聚焦的面板：" + " / ".join(PANES))


class ApplyLayoutArgs(Args):
    preset: str = Field(
        default="",
        description="布局预设：" + " / ".join(LAYOUT_PRESETS) + "（留空且给 panes 时用自定义）",
    )
    panes: list[dict[str, Any]] = Field(
        default_factory=list,
        description='自定义面板与占比，如 [{"id":"chat","size":60},{"id":"preview","size":40}]',
    )


class TipArgs(Args):
    target: str = Field(min_length=1, description="指向的界面元素 id（前端 data-ui 属性值）")
    text: str = Field(min_length=1, description="气泡里显示的话")
    placement: str = Field(default="top", description="气泡方位：top / bottom / left / right")
    ttl_seconds: int = Field(default=DEFAULT_TIP_TTL, ge=1, le=3600, description="存活秒数")


class TourArgs(Args):
    action: str = Field(default="start", description="start / next / prev / stop")
    steps: list[dict[str, Any]] = Field(
        default_factory=list,
        description='导览步骤，如 [{"target":"composer","title":"从这里开始","body":"输入一句话"}]',
    )


UI_TOOLS = [
    Tool("focus_pane", "聚焦某个桌面面板", FocusPaneArgs),
    Tool("apply_layout", "应用布局预设（或自定义面板占比）", ApplyLayoutArgs),
    Tool("tip", "用一个小气泡指向某个 UI 元素", TipArgs),
    Tool("tour", "引导式导览：变暗 + 高亮 + 分步 popover", TourArgs),
]


def _state_path(data_dir: str | pathlib.Path) -> pathlib.Path:
    return pathlib.Path(data_dir) / "ui" / "state.json"


def load_ui_state(data_dir: str | pathlib.Path) -> dict[str, Any]:
    """读状态，**顺手滤掉过期的气泡**。

    过期判断放在读侧而不是写侧：写侧清理需要定时器，
    而读侧清理对「前端轮询」这个用法是免费的。
    """
    path = _state_path(data_dir)
    state: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state = loaded
        except (OSError, ValueError):
            state = {}
    now = time.time()
    tips = [t for t in (state.get("tips") or []) if float(t.get("expires_at") or 0) > now]
    state["tips"] = tips
    return state


def _save(data_dir: str | pathlib.Path, state: dict[str, Any]) -> None:
    path = _state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


class UiStateTool:
    """4 条 UI 工具的执行器。**`async` 是硬要求** —— 注册表 `await external[name](...)`。"""

    def __init__(self, data_dir: str | pathlib.Path) -> None:
        self.data_dir = pathlib.Path(data_dir)

    async def execute(self, name, args, task_id, operation_id) -> dict[str, Any]:
        if name == "focus_pane":
            return self._focus(args)
        if name == "apply_layout":
            return self._layout(args)
        if name == "tip":
            return self._tip(args)
        if name == "tour":
            return self._tour(args)
        raise ValueError(f"未知的 UI 工具：{name}")

    # ---- 内部 ----

    def _write(self, mutate) -> dict[str, Any]:
        state = load_ui_state(self.data_dir)
        error = mutate(state)
        if error is not None:
            return {"status": "error", "error": error}
        _save(self.data_dir, state)
        return {"status": "ok", "ui": load_ui_state(self.data_dir)}

    def _focus(self, args) -> dict[str, Any]:
        pane = str(args["pane"]).strip()
        if pane not in PANES:
            return {
                "status": "error",
                "error": f"未知面板 `{pane}`。可选：{' / '.join(PANES)}",
            }

        def mutate(state):
            state["focused_pane"] = pane
            return None

        return self._write(mutate)

    def _layout(self, args) -> dict[str, Any]:
        preset = str(args.get("preset") or "").strip()
        raw_panes = list(args.get("panes") or [])

        if preset:
            if preset not in LAYOUT_PRESETS:
                return {
                    "status": "error",
                    "error": f"未知布局 `{preset}`。可选：{' / '.join(LAYOUT_PRESETS)}",
                }
            panes = [{"id": pid, "size": size} for pid, size in LAYOUT_PRESETS[preset]]
        elif raw_panes:
            panes = []
            for entry in raw_panes:
                pid = str(entry.get("id") or "").strip()
                if pid not in PANES:
                    return {"status": "error", "error": f"未知面板 `{pid}`。可选：{' / '.join(PANES)}"}
                try:
                    size = int(entry.get("size"))
                except (TypeError, ValueError):
                    return {"status": "error", "error": f"面板 `{pid}` 的 size 不是整数"}
                if size <= 0:
                    return {"status": "error", "error": f"面板 `{pid}` 的 size 必须为正"}
                panes.append({"id": pid, "size": size})
            preset = "custom"
        else:
            return {"status": "error", "error": "要么给 preset，要么给 panes"}

        def mutate(state):
            state["layout"] = {"preset": preset, "panes": panes}
            return None

        return self._write(mutate)

    def _tip(self, args) -> dict[str, Any]:
        placement = str(args.get("placement") or "top").strip()
        if placement not in ("top", "bottom", "left", "right"):
            return {"status": "error", "error": "placement 只能是 top / bottom / left / right"}
        ttl = int(args.get("ttl_seconds") or DEFAULT_TIP_TTL)

        def mutate(state):
            tips = list(state.get("tips") or [])
            # **同一个 target 上的旧气泡先撤掉** —— 否则同一点会叠出好几个气泡。
            tips = [t for t in tips if t.get("target") != str(args["target"])]
            tips.append(
                {
                    "id": uuid.uuid4().hex[:8],
                    "target": str(args["target"]),
                    "text": str(args["text"]),
                    "placement": placement,
                    "expires_at": time.time() + ttl,
                }
            )
            state["tips"] = tips
            return None

        return self._write(mutate)

    def _tour(self, args) -> dict[str, Any]:
        action = str(args.get("action") or "start").strip()
        steps_in = list(args.get("steps") or [])

        if action == "stop":
            def mutate(state):
                state.pop("tour", None)
                return None

            return self._write(mutate)

        if action == "start":
            if not steps_in:
                return {"status": "error", "error": "start 需要 steps（至少一步）"}
            steps = []
            for entry in steps_in:
                target = str(entry.get("target") or "").strip()
                if not target:
                    return {"status": "error", "error": "每一步都要有 target"}
                steps.append(
                    {
                        "target": target,
                        "title": str(entry.get("title") or ""),
                        "body": str(entry.get("body") or ""),
                    }
                )

            def mutate(state):
                state["tour"] = {"steps": steps, "index": 0, "active": True}
                return None

            return self._write(mutate)

        if action in ("next", "prev"):
            def mutate(state):
                tour = state.get("tour") or {}
                steps = tour.get("steps") or []
                if not steps:
                    return "没有进行中的导览（先 action=start）"
                index = int(tour.get("index") or 0) + (1 if action == "next" else -1)
                if index >= len(steps):
                    # **走到最后一步之后自动结束** —— 停在最后一步会让人以为还欠一步。
                    state.pop("tour", None)
                    return None
                if index < 0:
                    index = 0
                tour["index"] = index
                tour["active"] = True
                state["tour"] = tour
                return None

            return self._write(mutate)

        return {"status": "error", "error": f"未知 action `{action}`：只能是 start / next / prev / stop"}
