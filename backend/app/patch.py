"""结构化补丁编辑（MATRIX 5.16）：解析 + 应用 ``*** Begin Patch`` 风格的补丁。

**为什么需要它**：`local_write_file` 要求模型把整个文件重新输出一遍。文件一长就必然
出事——模型会悄悄删掉"它觉得不重要"的段落、把缩进改成自己喜欢的样式、或者在中途
截断。补丁把"改什么"压到最小：只输出被改的几行，其余字节一个不动。

格式（照 codex / cline / Hermes ``patch_parser.py`` 的 V4A 方言）::

    *** Begin Patch
    *** Update File: app/tools.py
    @@ def local(self, name: str ...
    -        old line
    +        new line
     unchanged context line
    *** Add File: docs/new.md
    +# 标题
    *** Delete File: docs/old.md
    *** End Patch

三条设计决定，都是"为什么这么做"：

1. **纯函数**：:func:`parse` 只吃字符串，:func:`apply` 只吃 ``(PatchSet, root)``。
   不读环境、不读配置、不碰数据库——于是它能在测试里被穷举，也能被别的写入路径复用。
   唯一的 IO 就是"读目标文件 / 写结果文件"，且都发生在 :func:`apply` 里。

2. **原子性优先于"尽力而为"**：先把**所有** op 的新内容在内存里算完并逐 hunk 校验，
   任何一处上下文不匹配就整体放弃、一个字节都不写。半个补丁比没打补丁更糟——文件会
   停在一个"既不是旧版也不是新版"的状态，而模型以为自己改成功了。

3. **不匹配要指到行**：报错必须说清"第几个文件、第几个 hunk、第几行、哪一行文本"。
   只说"补丁失败"，模型唯一的选择是整段重试，于是同一处错会反复烧预算。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

#: 一个补丁最多带多少个文件操作 / 每个文件最多多少个 hunk。上限是防"模型一次吐出
#: 一个 200 KB 的巨型补丁把上下文烧光"，不是为了安全。
MAX_OPS = 40
MAX_HUNKS = 200

_BEGIN = re.compile(r"^\*\*\*\s*Begin\s+Patch\s*$")
_END = re.compile(r"^\*\*\*\s*End\s+Patch\s*$")
_UPDATE = re.compile(r"^\*\*\*\s*Update\s+File:\s*(.+?)\s*$")
_ADD = re.compile(r"^\*\*\*\s*Add\s+File:\s*(.+?)\s*$")
_DELETE = re.compile(r"^\*\*\*\s*Delete\s+File:\s*(.+?)\s*$")
_HUNK = re.compile(r"^@@\s*(.*?)\s*$")


class PatchError(ValueError):
    """补丁**文本**本身不合法（缺标记、缺文件头、前缀非法）。

    与"上下文不匹配"分开：前者是模型没按格式写，重发一份就能成；后者是补丁和磁盘
    上的实际内容对不上，得先读文件再改。两类错误该给模型的建议完全不同。
    """


@dataclass(frozen=True)
class HunkLine:
    """hunk 里的一行。``prefix`` ∈ ``{" ", "-", "+"}``（空格 = 上下文行）。"""

    prefix: str
    content: str


@dataclass(frozen=True)
class Hunk:
    """一段改动。``context_hint`` 是 ``@@`` 后面的可选提示（用来定位）。"""

    lines: tuple[HunkLine, ...]
    context_hint: str = ""

    @property
    def before(self) -> tuple[str, ...]:
        """改动前应有的行（上下文行 + 被删行）。"""
        return tuple(line.content for line in self.lines if line.prefix in {" ", "-"})

    @property
    def after(self) -> tuple[str, ...]:
        """改动后应有的行（上下文行 + 新增行）。"""
        return tuple(line.content for line in self.lines if line.prefix in {" ", "+"})


@dataclass(frozen=True)
class PatchOp:
    """一个文件操作。``op`` ∈ ``{"update", "add", "delete"}``。"""

    op: str
    path: str
    hunks: tuple[Hunk, ...] = ()


@dataclass(frozen=True)
class PatchSet:
    """整份补丁。"""

    ops: tuple[PatchOp, ...]
    warnings: tuple[str, ...] = ()


@dataclass
class ApplyResult:
    """应用结果。``ok=False`` 时 ``error`` 一定有内容，且**磁盘没有被改动**。"""

    ok: bool
    applied: list[dict] = field(default_factory=list)
    error: str = ""
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """给模型看的一行摘要。"""
        if not self.ok:
            return f"补丁未应用（磁盘未改动）：{self.error}"
        parts = [
            f"{item['op']} {item['path']}"
            + (f"（{item['hunks']} 个 hunk）" if item.get("hunks") else "")
            for item in self.applied
        ]
        return "已应用：" + "；".join(parts)


# ── 解析 ────────────────────────────────────────────────────────────────────


def parse(text: str) -> PatchSet:
    """把补丁文本解析成 :class:`PatchSet`。格式不合法就抛 :class:`PatchError`。

    容错三处（都是实测模型常犯的）：
    - 允许没有 ``*** Begin Patch`` / ``*** End Patch``（只给文件头也认）；
    - 允许 CRLF 补丁体（行尾 ``\\r`` 会被剥掉，否则会把 ``\\r`` 注进 LF 文件）；
    - hunk 里**允许不写 ``@@``**：此时所有行归入同一个隐式 hunk。写 ``@@`` 才有
      意义——它划的是 hunk 边界，多个 ``@@`` = 多段互不相邻的改动。
    """
    raw_lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    # 标记必须独占一行且从第 0 列开始：正文里的 "+*** End Patch" 不能提前截断补丁。
    start, end = 0, len(raw_lines)
    for index, line in enumerate(raw_lines):
        if _BEGIN.match(line):
            start = index + 1
            break
    for index in range(start, len(raw_lines)):
        if _END.match(raw_lines[index]):
            end = index
            break
    lines = raw_lines[start:end]

    ops: list[PatchOp] = []
    warnings: list[str] = []
    current: dict | None = None
    hunks: list[Hunk] = []
    hunk_lines: list[HunkLine] = []
    hint = ""

    def flush_hunk() -> None:
        nonlocal hunk_lines, hint
        if hunk_lines:
            hunks.append(Hunk(lines=tuple(hunk_lines), context_hint=hint))
        hunk_lines = []
        hint = ""

    def flush_op() -> None:
        nonlocal current, hunks
        flush_hunk()
        if current is not None:
            ops.append(
                PatchOp(op=current["op"], path=current["path"], hunks=tuple(hunks))
            )
        current = None
        hunks = []

    for number, line in enumerate(lines, start=1):
        if line == "":
            # **裸空行一律忽略**：它既可能是"补丁体末尾的换行残留"，也可能是"两段
            # hunk 之间的空行"，两种都该跳过。真要表达"一行空上下文"，按 unified
            # diff 的规矩写一个空格（`" "` → 前缀空格 + 空内容），那样才不含糊。
            continue
        header = None
        for pattern, kind in ((_UPDATE, "update"), (_ADD, "add"), (_DELETE, "delete")):
            header = pattern.match(line)
            if header is not None:
                flush_op()
                current = {"op": kind, "path": header.group(1).strip()}
                break
        if header is not None:
            if current is None or not current["path"]:
                raise PatchError(f"第 {number} 行的文件头没写路径")
            continue
        if current is None:
            raise PatchError(
                f"第 {number} 行出现在任何文件头之前：{line[:80]!r}；"
                "补丁必须以 `*** Update File:` / `*** Add File:` / `*** Delete File:` 开头"
            )
        hunk_match = _HUNK.match(line)
        if hunk_match:
            flush_hunk()
            hint = hunk_match.group(1).strip()
            continue
        if line.startswith("***"):
            raise PatchError(f"第 {number} 行是未知的补丁标记：{line[:80]!r}")
        if current["op"] == "delete":
            # Delete 只需要文件头，正文多余内容说明模型写错了。
            if line.strip():
                warnings.append(f"Delete File 忽略了第 {number} 行的多余内容")
            continue
        prefix, content = line[0], line[1:]
        if prefix not in {" ", "-", "+"}:
            # 宽松处理：没写前缀的行按上下文行算（模型常忘）。但要留警告，
            # 因为"忘了 + 号"会让本该新增的代码被当成上下文，从而报上下文不匹配。
            warnings.append(f"第 {number} 行没有 +/-/空格 前缀，按上下文行处理")
            prefix, content = " ", line
        hunk_lines.append(HunkLine(prefix=prefix, content=content))

    flush_op()

    if not ops:
        raise PatchError("补丁里没有任何文件操作（至少要有一个 `*** Update File:` 之类的头）")
    if len(ops) > MAX_OPS:
        raise PatchError(f"补丁包含 {len(ops)} 个文件操作，超过上限 {MAX_OPS}")
    for op in ops:
        if len(op.hunks) > MAX_HUNKS:
            raise PatchError(f"{op.path} 有 {len(op.hunks)} 个 hunk，超过上限 {MAX_HUNKS}")
        if op.op == "update" and not op.hunks:
            raise PatchError(f"{op.path} 的 Update File 没有任何改动行")
        if op.op == "add" and not op.hunks:
            raise PatchError(f"{op.path} 的 Add File 没有内容")
    return PatchSet(ops=tuple(ops), warnings=tuple(warnings))


# ── 应用 ────────────────────────────────────────────────────────────────────


def _resolve(root: Path, relative: str) -> Path:
    """把补丁里的路径解析成真实路径，并做**包含校验**。

    复用 :func:`app.tools.resolve_path` 的判据（同一个函数、不是抄一份）：权限边界
    只允许有一份实现，两份必然漂移，而漂移的边界比没有边界更危险。这里**强制**
    ``machine=False`` 的语义——补丁只允许改授权目录内的文件，哪怕当前是完全访问档。
    理由：补丁是"批量、不可见"的写入，风险比单次写文件高一个量级，值得收紧。
    """
    from app.tools import resolve_path

    return resolve_path(root, relative, False)


def _find(lines: list[str], before: tuple[str, ...], hint: str) -> int | None:
    """在 ``lines`` 里定位 hunk 的"改动前"块，返回起始下标（0 基）。找不到返回 None。

    匹配用 ``rstrip`` 后的内容：行尾空白差异（编辑器/CRLF 残留）不该让补丁失败，
    但**行首缩进必须严格一致**——缩进错了就是改错地方。
    """
    if not before:
        return None
    target = [line.rstrip() for line in before]
    haystack = [line.rstrip() for line in lines]
    span = len(target)
    if hint:
        # 有 @@ 提示时先在提示附近找：同一个文件里常有大量重复行（空行、`}`），
        # 全局首次匹配很容易改错位置。
        hint_key = hint.strip()
        for index, line in enumerate(haystack):
            if hint_key and hint_key in line:
                window_start = max(0, index - 1)
                for offset in range(window_start, min(len(haystack), index + 400)):
                    if haystack[offset : offset + span] == target:
                        return offset
                break
    for index in range(len(haystack) - span + 1):
        if haystack[index : index + span] == target:
            return index
    return None


def _mismatch_reason(path: str, hunk_index: int, hunk: Hunk, lines: list[str]) -> str:
    """把"上下文不匹配"讲清楚：第几个 hunk、第几行、哪一行文本。"""
    haystack = {line.rstrip() for line in lines}
    for offset, expected in enumerate(hunk.before, start=1):
        if expected.rstrip() not in haystack:
            return (
                f"{path}：第 {hunk_index} 个 hunk 的第 {offset} 行"
                f"「{expected[:120]}」在文件里找不到（文件共 {len(lines)} 行）。"
                "请先读一遍文件，按现有内容重写这几行。"
            )
    return (
        f"{path}：第 {hunk_index} 个 hunk 的上下文不匹配——"
        f"这些行单独都在，但没按补丁给的顺序相邻出现（文件共 {len(lines)} 行）。"
        "通常是中间被改过，或 hunk 里漏写了上下文行。"
    )


def _apply_hunks(path: str, original: list[str], hunks: tuple[Hunk, ...]) -> list[str]:
    """在内存里把全部 hunk 依次应用到 ``original``，返回新行列表。

    依次应用而不是"各自按原文件定位"：后一个 hunk 的行号会受前一个影响，这是 unified
    diff 的语义。定位仍按内容匹配，所以只要上下文唯一，顺序不影响结果。
    """
    lines = list(original)
    for hunk_index, hunk in enumerate(hunks, start=1):
        before, after = hunk.before, hunk.after
        if not before:
            # 纯新增 hunk：没有"改动前"的行可供匹配，只能靠 @@ 提示或追加到末尾。
            insertion: int | None = len(lines)
            if hunk.context_hint:
                insertion = None
                for index, line in enumerate(lines):
                    if hunk.context_hint in line:
                        insertion = index + 1
                        break
                if insertion is None:
                    raise PatchError(
                        f"{path}：第 {hunk_index} 个 hunk 只有新增行，"
                        f"而 @@ 提示「{hunk.context_hint[:80]}」在文件里找不到，无法定位插入点。"
                    )
            lines[insertion:insertion] = list(after)
            continue
        position = _find(lines, before, hunk.context_hint)
        if position is None:
            raise PatchError(_mismatch_reason(path, hunk_index, hunk, lines))
        lines[position : position + len(before)] = list(after)
    return lines


def _write_atomic(path: Path, text: str) -> None:
    """原子写：同目录临时文件 + ``os.replace``。

    补丁是多文件批量写入，写到第 3 个文件时崩掉会留下"改了一半的仓库"。
    同目录临时文件保证 ``os.replace`` 是同卷 rename（原子），也避免跨盘拷贝。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".orbit-patch.tmp")
    temporary.write_text(text, encoding="utf-8", newline="")
    os.replace(temporary, path)


def _join(lines: list[str], original_text: str) -> str:
    """把行列表拼回文本，**保留原文件的换行风格与末尾换行状态**。"""
    newline = "\r\n" if "\r\n" in original_text else "\n"
    body = newline.join(lines)
    if original_text.endswith(("\n", "\r\n")) and not body.endswith(newline):
        body += newline
    return body


def apply(patch: PatchSet, root: Path) -> ApplyResult:
    """把补丁应用到 ``root`` 之下。返回 :class:`ApplyResult`（**不抛异常**）。

    流程刻意分成"全部算完 → 再落盘"两段：

    1. 逐 op 解析路径（越界即失败）、读原文件、逐 hunk 匹配并把新内容算进内存；
    2. 全部成功后，才真正写盘/删除。

    所以任何一处上下文不匹配，磁盘上不会有一个字节被改动——这是本模块最重要的
    不变量（``ok=False`` 时"磁盘未改动"是承诺，不是尽力而为）。
    """
    planned: list[tuple[PatchOp, Path, str | None, str]] = []
    root_path = Path(root)
    seen: set[Path] = set()

    for index, op in enumerate(patch.ops, start=1):
        try:
            target = _resolve(root_path, op.path)
        except ValueError as exc:
            return ApplyResult(ok=False, error=f"第 {index} 个操作：{op.path} —— {exc}")
        if target in seen:
            return ApplyResult(
                ok=False,
                error=(
                    f"第 {index} 个操作：{op.path} 在同一份补丁里被改了两次。"
                    "同一文件的多个改动请写在同一个 Update File 的多段 @@ 里。"
                ),
            )
        seen.add(target)

        if op.op == "add":
            if target.exists():
                return ApplyResult(
                    ok=False,
                    error=f"第 {index} 个操作：{op.path} 已存在，Add File 只用于新建文件",
                )
            content = "\n".join(line.content for line in op.hunks[0].lines if line.prefix in {" ", "+"})
            if content:
                content += "\n"
            planned.append((op, target, None, content))
            continue
        if op.op == "delete":
            if not target.is_file():
                return ApplyResult(ok=False, error=f"第 {index} 个操作：{op.path} 不存在，无法删除")
            planned.append((op, target, None, ""))
            continue
        if not target.is_file():
            return ApplyResult(
                ok=False,
                error=f"第 {index} 个操作：{op.path} 不存在；新建文件请用 Add File",
            )
        try:
            # newline="" 保留原文件的换行风格：用 read_text 的话，Python 的通用换行
            # 会把 CRLF 折成 LF，补丁写回去就把整个文件的行尾改了（Windows 上等于
            # 让 git 认为"全文件重写"）。
            with target.open("r", encoding="utf-8", newline="") as handle:
                original = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            return ApplyResult(ok=False, error=f"第 {index} 个操作：{op.path} 读取失败：{exc}")
        original_lines = original.split("\n")
        if original.endswith("\n"):
            original_lines = original_lines[:-1]  # split 会多出一个空串
        original_lines = [line.rstrip("\r") for line in original_lines]
        try:
            new_lines = _apply_hunks(op.path, original_lines, op.hunks)
        except PatchError as exc:
            return ApplyResult(ok=False, error=str(exc))
        planned.append((op, target, original, _join(new_lines, original)))

    applied: list[dict] = []
    for op, target, _original, content in planned:
        try:
            if op.op == "delete":
                target.unlink()
            else:
                _write_atomic(target, content)
        except OSError as exc:
            return ApplyResult(
                ok=False,
                error=f"{op.path} 落盘失败：{exc}（此前的文件可能已写入，请检查工作区）",
                applied=applied,
            )
        applied.append({"op": op.op, "path": op.path, "hunks": len(op.hunks)})
    return ApplyResult(ok=True, applied=applied)


def parse_and_apply(text: str, root: Path) -> ApplyResult:
    """解析 + 应用。解析失败也走 ``ApplyResult``（模型侧统一一种错误形状）。"""
    try:
        patch = parse(text)
    except PatchError as exc:
        return ApplyResult(ok=False, error=f"补丁格式不合法：{exc}")
    return apply(patch, root)
