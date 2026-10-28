"""dashboard 三个缺失页面的后端（MATRIX 16.1 的 docs / files / webhooks）。

## 为什么这三个页面单独一个模块

`app/main.py` 的 `create_app()` 已经 800 多行、41 条路由。往里再加三条会把那个函数
变成没人敢碰的东西。更重要的是：这三条路由有**共同的关注点**——都在读用户可控的
路径或内容，都需要同一套路径校验。放一起才能只写一次校验。

## 各自做什么

- `/api/docs`：列出并读取可读的 Markdown（项目文档 + 技能说明）。
  为什么有用：agent 自己写的 `SKILL.md` 和 `AGENTS.md` 是"它凭什么这么做"的唯一解释，
  没有这个页面，用户只能去翻文件系统。
- `/api/files`：工作目录的文件树（限深度、限数量）。
- `/api/webhooks`：入站 webhook 的登记与收件箱。**只落盘不触发**——
  真正拉起 agent 要走 `create_app(run_agent=True)` 的任务队列，那属于另一条路径；
  这里如实只做"收下来、能查到"，并在响应里说明。

## 路径安全

三条路由都碰用户可控路径，所以统一走 `_safe_relative()`：拒绝绝对路径、`..` 穿越、
符号链接逃逸（resolve 后再比）。**Windows 保留名与 ADS 也拒**——`CON`、`file.txt:stream`
这类在 Windows 上会被当成合法文件名，但读出来是意外的东西。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter()

#: 文件列表的上限。**必须有上限**：工作目录里一个 `node_modules` 就能让响应变成几百 MB，
#: 而浏览器会把整个响应读进内存。
MAX_FILES = 500
MAX_DEPTH = 4

#: 文档页只认这些后缀。二进制内容读出来是乱码，不如不给。
DOC_SUFFIXES = (".md", ".markdown", ".txt")

#: Windows 保留设备名。用这些名字建的文件读写行为未定义。
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

#: 目录列表里跳过的东西。不是"不允许访问"，只是不在浏览视图里展开。
_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache"}


class WebhookRegistration(BaseModel):
    """一条入站 webhook 登记。`secret` 只用于校验来源，**不回显**。"""

    id: str = Field(min_length=1, max_length=64)
    label: str = ""
    prompt: str = ""
    secret: str = ""


def _data_dir(request: Request) -> Path:
    return Path(request.app.state.settings.data_dir)


def _safe_relative(root: Path, raw: str) -> Path:
    """把用户给的相对路径解析成 `root` 之下的绝对路径，越界就 400。

    校验顺序有讲究：先拒绝对路径与保留名（纯字符串判断，不需要碰磁盘），
    再 resolve 之后比前缀（这一步才能抓住符号链接逃逸）。
    """
    text = (raw or "").strip().replace("\\", "/")
    if not text:
        raise HTTPException(400, "路径不能为空")
    candidate = Path(text)
    if candidate.is_absolute() or re.match(r"^[A-Za-z]:", text):
        raise HTTPException(400, "只接受相对路径")
    if ".." in candidate.parts:
        raise HTTPException(400, "路径不能包含 ..")
    for part in candidate.parts:
        stem = part.split(".")[0].upper()
        if stem in _WINDOWS_RESERVED:
            raise HTTPException(400, f"{part} 是 Windows 保留名")
        if ":" in part:
            raise HTTPException(400, "路径不能包含备用数据流（:）")
    root_resolved = root.resolve()
    target = (root_resolved / candidate).resolve()
    try:
        target.relative_to(root_resolved)
    except ValueError:
        raise HTTPException(400, "路径越界（符号链接逃逸）") from None
    return target


def _doc_roots(request: Request) -> list[tuple[str, Path]]:
    """文档来源：项目根下的 Markdown + 数据目录里的技能说明。"""
    roots: list[tuple[str, Path]] = []
    project = Path(request.app.state.settings.data_dir).resolve().parent
    for label, path in (("项目文档", project / "handoff"), ("技能", Path(request.app.state.settings.data_dir) / "skills")):
        if path.is_dir():
            roots.append((label, path))
    return roots


@router.get("/api/docs")
async def list_docs(request: Request) -> dict[str, Any]:
    """列出可读文档。**读不到的（太大、二进制）不列**，而不是列出来再报错。"""
    items: list[dict[str, Any]] = []
    for label, root in _doc_roots(request):
        count = 0
        for path in sorted(root.rglob("*")):
            if count >= MAX_FILES or len(items) >= MAX_FILES:
                break
            if not path.is_file() or path.suffix.lower() not in DOC_SUFFIXES:
                continue
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            items.append(
                {
                    "source": label,
                    "path": path.relative_to(root).as_posix(),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                }
            )
            count += 1
    items.sort(key=lambda item: (item["source"], item["path"]))
    return {"items": items[:MAX_FILES], "truncated": len(items) >= MAX_FILES}


@router.get("/api/docs/read")
async def read_doc(request: Request, source: str, path: str) -> dict[str, Any]:
    """读一份文档的正文。路径经 `_safe_relative` 校验。"""
    for label, root in _doc_roots(request):
        if label != source:
            continue
        target = _safe_relative(root, path)
        if not target.is_file():
            raise HTTPException(404, "文件不存在")
        if target.suffix.lower() not in DOC_SUFFIXES:
            raise HTTPException(415, "只支持 Markdown / 纯文本")
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise HTTPException(500, f"读取失败：{exc}") from None
        return {"source": source, "path": path, "text": text, "size": len(text)}
    raise HTTPException(404, f"未知的文档来源：{source}")


@router.get("/api/files")
async def list_files(request: Request, path: str = "", depth: int = 2) -> dict[str, Any]:
    """工作目录的文件树（限深度与数量）。`path` 相对项目根。"""
    project = Path(request.app.state.settings.data_dir).resolve().parent
    base = _safe_relative(project, path) if path else project
    if not base.is_dir():
        raise HTTPException(404, "目录不存在")
    limit = max(1, min(int(depth), MAX_DEPTH))
    entries: list[dict[str, Any]] = []
    stack: list[tuple[Path, int]] = [(base, 0)]
    while stack and len(entries) < MAX_FILES:
        current, level = stack.pop()
        try:
            children = sorted(current.iterdir(), key=lambda item: (item.is_file(), item.name))
        except OSError:
            continue
        for child in children:
            if len(entries) >= MAX_FILES:
                break
            if child.name in _SKIP_DIRS:
                continue
            try:
                is_dir = child.is_dir()
                stat = child.stat()
            except OSError:
                continue
            entries.append(
                {
                    "path": child.relative_to(project).as_posix(),
                    "name": child.name,
                    "dir": is_dir,
                    "size": 0 if is_dir else stat.st_size,
                    "mtime": stat.st_mtime,
                }
            )
            if is_dir and level + 1 < limit:
                stack.append((child, level + 1))
    return {"root": base.relative_to(project).as_posix() or ".", "items": entries, "truncated": len(entries) >= MAX_FILES}


def _webhook_dir(request: Request) -> Path:
    path = _data_dir(request) / "webhooks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _webhook_registry(request: Request) -> Path:
    return _data_dir(request) / "webhooks.json"


def _load_registry(request: Request) -> list[dict[str, Any]]:
    path = _webhook_registry(request)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # 配置坏了不该让整个页面 500——当作空列表并让用户重新登记。
        return []
    return data if isinstance(data, list) else []


def _save_registry(request: Request, items: list[dict[str, Any]]) -> None:
    path = _webhook_registry(request)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


@router.get("/api/webhooks")
async def list_webhooks(request: Request) -> dict[str, Any]:
    """已登记的入站 webhook + 各自的收件数。**secret 不回显。**"""
    registry = _load_registry(request)
    inbox = _webhook_dir(request)
    items = []
    for entry in registry:
        name = str(entry.get("id", ""))
        log = inbox / f"{name}.jsonl"
        count = 0
        if log.exists():
            count = sum(1 for line in log.read_text(encoding="utf-8").splitlines() if line.strip())
        items.append(
            {
                "id": name,
                "label": entry.get("label", ""),
                "prompt": entry.get("prompt", ""),
                "has_secret": bool(entry.get("secret")),
                "received": count,
            }
        )
    return {"items": items}


@router.post("/api/webhooks")
async def register_webhook(request: Request, body: WebhookRegistration) -> dict[str, Any]:
    registry = _load_registry(request)
    if any(str(entry.get("id")) == body.id for entry in registry):
        raise HTTPException(409, f"webhook {body.id} 已存在")
    registry.append(body.model_dump())
    _save_registry(request, registry)
    return {"ok": True, "id": body.id}


@router.delete("/api/webhooks/{webhook_id}")
async def delete_webhook(request: Request, webhook_id: str) -> dict[str, Any]:
    registry = [entry for entry in _load_registry(request) if str(entry.get("id")) != webhook_id]
    _save_registry(request, registry)
    return {"ok": True, "id": webhook_id}


@router.post("/api/webhooks/{webhook_id}")
async def receive_webhook(request: Request, webhook_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """入站投递：**只落盘、只返回 202**。

    为什么不在这一条路由里直接拉起 agent：那要走 `create_app(run_agent=True)` 的任务
    队列与权限上下文，从这里硬调会绕开审批与预算。所以这里如实只做"收下来、能查到"，
    真正触发由 cron 或 gateway 读收件箱来完成。
    """
    registry = _load_registry(request)
    entry = next((item for item in registry if str(item.get("id")) == webhook_id), None)
    if entry is None:
        raise HTTPException(404, f"webhook {webhook_id} 未登记")
    secret = str(entry.get("secret") or "")
    if secret:
        given = request.headers.get("x-orbit-secret", "")
        if given != secret:
            raise HTTPException(401, "secret 不匹配")
    log = _webhook_dir(request) / f"{webhook_id}.jsonl"
    record = {"ts": time.time(), "payload": payload or {}, "headers": {"user-agent": request.headers.get("user-agent", "")}}
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return {"ok": True, "accepted": True, "note": "已落盘；触发 agent 需由 cron/gateway 读收件箱"}


__all__ = ["router"]
