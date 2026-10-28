"""工具结果的三层治理：输出层上限 / 落盘层 / 整轮层。

对标 Hermes 的三件套（`tool_output_limits.py` + `budget_config.py` +
`tool_result_storage.py`），把它们收成一个模块，因为三层是**同一条管线的三个
阶段**，拆成三个文件后"谁先谁后、谁记什么账"会散落在三处 import 里：

```
第一层 cap_output   单个工具结果先按 行数 / 单行宽 / 字节 裁到能进上下文
第二层 spill        裁完仍超阈值 → 正文落盘，上下文只留头尾预览 + 文件路径
第三层 TurnBudget   一次 assistant 轮次内所有结果合计超预算 → 按大小降序强制落盘
```

**另有一层不属于上面这条管线**：`spill_output` / `spill_safe`（MATRIX 5.7，对齐 Hermes 的
`hook_output_spill.py`）。上面三层按**结果大小分级**（100K / 200K / 1.5K 预览），这一层按
**上下文预算**裁剪（10K 字符上限，头尾各 500）。两者叠加时的顺序是
**先分层治理、再按上下文预算裁剪**，理由写在 :func:`spill_safe` 的 docstring 里：
分层治理已经在 `app/policies.py` 里跑过一遍，`as_tool_text` 是模型侧最后一道出口，
它看到的是"治理后的正文"，此时再按上下文预算裁剪才不会把同一段内容截两次——
两次截断会让"被截过"这件事从模型视野里消失（第二次截断的是第一次的预览，
文件路径和原始大小都可能被剪掉）。

为什么三层都要有，而不是一个 `TOOL_RESULT_LIMIT`：

* 单阈值只能**砍掉尾巴**——构建日志的结论、报错的堆栈都在尾部，砍掉尾巴等于
  把最有信息量的部分丢掉。所以第一层按头尾比例裁，第二层干脆不丢（落盘）。
* 「不丢」需要一个落点，而落点必须**可回收**（24h 清理），否则磁盘无限涨。
* 单条都不超限、但十条加起来超限的情况，前两层都看不见——这就是第三层的存在理由。

阈值全部来自 `BudgetConfig`（frozen dataclass），默认值逐字节等于 Hermes 的常量，
所以"默认即对齐"；将来接配置项只需构造时换值，不必改调用方。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("orbit.budget")

# ── 落盘层（第二层）的默认值：单一真相源 ────────────────────────────────────
#: 单条工具结果超过这个字符数就落盘（MCP 工具另有更紧的阈值，见 resolve_threshold）
DEFAULT_RESULT_SIZE = 100_000
#: 一次 assistant 轮次内所有工具结果的合计上限（第三层）
DEFAULT_TURN_BUDGET = 200_000
#: 落盘后留在上下文里的预览长度（头 2/3 + 尾 1/3）
DEFAULT_PREVIEW_SIZE = 1_500
#: MCP 工具的默认阈值：MCP server 常返回未分页的 20~50K 大包，
#: 通用 100K 阈值拦不住，会把上下文悄悄撑大（Hermes 对齐竞品的 30~50K 硬帽）
DEFAULT_MCP_RESULT_SIZE = 50_000
#: 识别 MCP 工具的名字前缀（与 Hermes `MCP_TOOL_PREFIX` 一致）
MCP_TOOL_PREFIX = "mcp_"

# ── 输出层（第一层）的默认值 ────────────────────────────────────────────────
#: 单个工具结果进上下文前的字节上限
DEFAULT_MAX_BYTES = 50_000
#: 行数上限（超过就头尾保留、中间省略）
DEFAULT_MAX_LINES = 2_000
#: 单行宽度上限（超宽的行按字符截断并标记）
DEFAULT_MAX_LINE_LENGTH = 2_000

#: 钉死的阈值：`read_file` 必须永远不落盘，否则「落盘 → 模型读文件 → 又落盘」
#: 会变成死循环（Hermes `budget_config.py:10` 的理由，照抄）。这是唯一一条
#: 不允许被 overrides 覆盖的规则，所以它单独放在解析函数最前面。
PINNED_THRESHOLDS: dict[str, float] = {"read_file": math.inf}

#: 终端类工具：**构建日志的结论在尾部**（报错、测试结果、退出码都在最后几行），
#: 所以尾部占 60%；其它工具（网页正文、文档）开头更重要，头占 70%。
TERMINAL_TOOLS = frozenset({"run_command", "terminal"})
TERMINAL_HEAD_RATIO = 0.4
DEFAULT_HEAD_RATIO = 0.7

#: 落盘子目录（相对 data_dir）。与 Hermes `$HERMES_HOME/cache/spillover` 同构。
SPILLOVER_SUBDIR = Path("cache") / "spillover"
#: 落盘文件的最长存活时间：过期即删（由 cleanup_spillover 执行）
SPILLOVER_MAX_AGE_HOURS = 24

#: 文件名里不允许出现的字符（工具名可能带斜杠、空格、冒号）
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9_.-]+")
#: 文件名里工具名的最大长度，超出部分靠哈希区分
_MAX_TOOL_STEM = 40

#: 单行截断标记：留个显式记号，模型才不会把"被截断的行"当成完整内容
LINE_MARKER = "…[本行已截断]"

# ── hook 上下文落盘（MATRIX 5.7）：按上下文预算裁剪的默认值 ──────────────────
#
# 数字逐个照抄 Hermes `hook_output_spill.py` 的 `DEFAULT_MAX_CHARS` /
# `DEFAULT_PREVIEW_HEAD` / `DEFAULT_PREVIEW_TAIL`（也等于 `config.yaml` 的
# `hooks.output_spill` 默认段）。为什么是 10,000 而不是上面那套 100,000：
# 两层的**目的不同**。100K 那套问的是"这条结果大到该不该落盘"，10K 这套问的是
# "这条结果占掉多少上下文预算"——一次对话里几十条工具结果各占 10K，上下文就满了。
#: 超过这个字符数就在进上下文前落盘（只留头尾预览）
DEFAULT_SPILL_LIMIT = 10_000
#: 预览头部字符数
DEFAULT_SPILL_HEAD = 500
#: 预览尾部字符数
DEFAULT_SPILL_TAIL = 500
#: 落盘子目录（相对 data_dir）：`<data>/spill/<task>/<tool>-<n>.txt`。
#: 与上面第二层的 `cache/spillover` **分开放**：那层的文件是"按大小分级"的产物，
#: 这一层是"按上下文预算"的产物，混在一个目录里之后清理策略就没法分别调。
HOOK_SPILL_SUBDIR = Path("spill")
#: 单目录里同名工具最多允许的编号（`<tool>-<n>.txt`）。到顶就复用最后一个文件：
#: 编号无限涨会让目录里堆出几万个文件，而模型只关心"最新那份在哪"。
MAX_SPILL_INDEX = 9_999

#: 已按目录做过一次过期清理的目录集合（进程级）。对齐 Hermes 的
#: `_prune_spillover_once`：每次落盘都扫一遍目录在大目录上不划算，
#: 但完全不扫又会让纯本地进程（没有后台 housekeeping）的落盘文件永久堆积。
_PRUNED_DIRS: set[str] = set()


@dataclass(frozen=True)
class Cut:
    """一次裁剪的账：裁掉了多少、留下了多少。

    `head_kept` / `tail_kept` 记的是**最终文本里保留的原文头部/尾部字符数**
    （无裁剪时 head_kept == 原文长度、tail_kept == 0）。分开记是因为
    "留下多少"比"裁掉多少"更能说明模型实际看到了什么。
    """

    lines_cut: int = 0
    bytes_cut: int = 0
    head_kept: int = 0
    tail_kept: int = 0

    def touched(self) -> bool:
        """是否真的裁过（`truncated` 标志与降级判定都靠它）。"""
        return bool(self.lines_cut or self.bytes_cut)

    def to_dict(self) -> dict[str, int]:
        """落库/事件里用的 JSON 安全形态（事件负载要能 json.dumps）。"""
        return {
            "lines_cut": self.lines_cut,
            "bytes_cut": self.bytes_cut,
            "head_kept": self.head_kept,
            "tail_kept": self.tail_kept,
        }


@dataclass(frozen=True)
class SpillResult:
    """一次落盘的结果：文件在哪、上下文里留了什么、原文多大。

    同一形状服务两件事，靠 :attr:`spilled` 区分：

    * 第二层 `spill()`（按结果大小分级）——一定落过盘，`spilled=True`；
    * MATRIX 5.7 的 `spill_output()`（按上下文预算裁剪）——没超阈值时**原样返回**，
      `spilled=False`、`path=None`、`preview` 就是原文。调用方据此判断"要不要把
      预览换成正文"，不必自己比长度。
    """

    path: Path | None
    preview: str
    bytes: int
    cut: Cut
    #: 是否真的写了文件。`False` 只出现在"没超阈值、什么都没做"那一条分支上。
    spilled: bool = True
    #: 原文的**字符**数。提示行要报它（`bytes` 是 UTF-8 字节数，中文一字 3 字节，
    #: 报字节数会让模型对"到底截掉了多少"判断偏三倍）。
    chars: int = 0


@dataclass(frozen=True)
class Governed:
    """三层串起来之后的产出：模型该看到的正文 + 三层各自的账。"""

    text: str
    cut: Cut
    spill: SpillResult | None = None
    degraded: bool = False


@dataclass(frozen=True)
class BudgetConfig:
    """三层治理的全部数值。frozen：一次构造定终身，避免中途被谁改一半。

    所有字段都能通过构造参数覆盖——这是给测试和将来的配置项留的口子：
    `BudgetConfig(default_result_size=5_000)` 就能在一条测试里把第二层逼出来，
    不必去改模块常量（模块常量一旦可变，"默认值"就不再是单一真相源）。
    """

    default_result_size: int = DEFAULT_RESULT_SIZE
    turn_budget: int = DEFAULT_TURN_BUDGET
    preview_size: int = DEFAULT_PREVIEW_SIZE
    mcp_result_size: int = DEFAULT_MCP_RESULT_SIZE
    max_bytes: int = DEFAULT_MAX_BYTES
    max_lines: int = DEFAULT_MAX_LINES
    max_line_length: int = DEFAULT_MAX_LINE_LENGTH
    tool_overrides: dict[str, int] = field(default_factory=dict)


def resolve_threshold(tool_name: str, *, config: BudgetConfig | None = None) -> float:
    """解析单个工具的落盘阈值。优先级：pinned > overrides > mcp_ 前缀 > default。

    第四档（Hermes 里是 `registry.get_max_result_size`）在 Orbit 里没有对应的
    per-tool 注册表值，退化为 `default_result_size`——这一档仍然保留，因为将来
    若 `tools.py` 要给某个工具登记自己的 `max_result_size`，只需在这里插一层，
    所有调用点都不用动。

    MCP 前缀那一档还要再取 `min(mcp_result_size, default_result_size)`：
    当预算被按上下文窗口缩小（小模型）时，`default_result_size` 会变小，
    如果直接用 `mcp_result_size` 这个固定值，per-tool 的大值就会把缩小后的
    上限重新顶回去——缩放等于白做（Hermes issue #23767 的教训）。
    """
    cfg = config or BudgetConfig()
    if tool_name in PINNED_THRESHOLDS:
        return PINNED_THRESHOLDS[tool_name]
    if tool_name in cfg.tool_overrides:
        return float(cfg.tool_overrides[tool_name])
    if tool_name.startswith(MCP_TOOL_PREFIX):
        return float(min(cfg.mcp_result_size, cfg.default_result_size))
    return float(cfg.default_result_size)


def _head_ratio(tool_name: str) -> float:
    """终端类按 40/60（结论在尾部），其余按 70/30（正文开头更重要）。"""
    return TERMINAL_HEAD_RATIO if tool_name in TERMINAL_TOOLS else DEFAULT_HEAD_RATIO


def _preview_split(preview_size: int) -> tuple[int, int]:
    """预览的头尾切分：头占 2/3、尾占 1/3（1500 → 1000 + 500）。"""
    size = max(0, int(preview_size))
    head = size * 2 // 3
    return head, size - head


def _kept_spans(original: str, final: str) -> tuple[int, int]:
    """最终文本保留了原文的多少头部字符、多少尾部字符。"""
    head = 0
    limit = min(len(original), len(final))
    while head < limit and original[head] == final[head]:
        head += 1
    tail = 0
    limit -= head
    while tail < limit and original[-1 - tail] == final[-1 - tail]:
        tail += 1
    return head, tail


def _split_lines(text: str) -> list[str]:
    """按行切且保留行尾（`"".join(result) == text` 恒成立，方便逐字还原）。"""
    return text.splitlines(keepends=True)


def cap_output(
    text: str, *, tool_name: str, config: BudgetConfig | None = None
) -> tuple[str, Cut]:
    """第一层：把单个工具结果裁到"能进上下文"的尺寸，返回 (正文, 裁剪账)。

    **顺序是行数 → 单行宽 → 字节**，不能反：先按字节砍会把行结构砍断，
    后面再按行裁就无从判断"哪一行是完整的"；而先按行裁之后总量必然下降，
    字节这一刀往往就不需要了（构建日志从 3 万行降到 2 千行，字节自然合规）。

    没超任何上限时**逐字返回原文**——这是向后兼容的底线：绝大多数工具结果
    都是小的，它们必须一个字节都不变。
    """
    cfg = config or BudgetConfig()
    body = text or ""
    if not body:
        return body, Cut()

    ratio = _head_ratio(tool_name)
    lines_cut = 0
    original_bytes = len(body.encode("utf-8"))

    # ① 行数上限：头尾各留一段，中间用一行标记代替。
    lines = _split_lines(body)
    if cfg.max_lines > 0 and len(lines) > cfg.max_lines:
        head_n = max(0, int(cfg.max_lines * ratio))
        tail_n = max(0, cfg.max_lines - head_n)
        dropped = len(lines) - head_n - tail_n
        marker = f"…[已按行数上限 {cfg.max_lines} 截断：省略 {dropped} 行]…\n"
        kept = lines[:head_n] + [marker]
        if tail_n:
            kept += lines[-tail_n:]
        body = "".join(kept)
        lines_cut = dropped

    # ② 单行宽度上限：只动超宽的行，其它行逐字保留。
    if cfg.max_line_length > 0:
        parts = _split_lines(body)
        for index, line in enumerate(parts):
            ending = "\n" if line.endswith("\n") else ""
            content = line[: -len(ending)] if ending else line
            if len(content) > cfg.max_line_length:
                parts[index] = content[: cfg.max_line_length] + LINE_MARKER + ending
        body = "".join(parts)

    # ③ 字节上限：按 UTF-8 字节算（落库、传输、模型请求都是字节）。
    # 中文一字 3 字节，按字符算会低估真实体积约 3 倍，所以这一层必须用字节。
    raw = body.encode("utf-8")
    if cfg.max_bytes > 0 and len(raw) > cfg.max_bytes:
        head_budget = int(cfg.max_bytes * ratio)
        tail_budget = cfg.max_bytes - head_budget
        # errors="ignore" 保证不把多字节字符劈成半个（劈开就是非法 UTF-8）。
        head = raw[:head_budget].decode("utf-8", "ignore")
        tail = raw[len(raw) - tail_budget :].decode("utf-8", "ignore") if tail_budget > 0 else ""
        marker = f"\n…[已按字节上限 {cfg.max_bytes} 截断]…\n"
        body = head + marker + tail

    head_kept, tail_kept = _kept_spans(text or "", body)
    # `bytes_cut` 取**净减少**的字节数（原文 - 最终），三层哪一层动过都算得进来：
    # 行宽截断不单列字段，但必须体现在账上，否则 `touched()` 会把它当成"没裁"。
    bytes_cut = max(0, original_bytes - len(body.encode("utf-8")))
    return body, Cut(
        lines_cut=lines_cut, bytes_cut=bytes_cut, head_kept=head_kept, tail_kept=tail_kept
    )


def spillover_dir(data_dir) -> Path:
    """落盘目录：`<data_dir>/cache/spillover`（未创建）。"""
    return Path(data_dir) / SPILLOVER_SUBDIR


def _inside(data_dir, *parts: str) -> Path:
    """把目标路径解析到 data_dir 之内，越界即拒。

    威胁模型照 `spill_safety.py`：落盘目录是**可预测、世界可发现**的
    （`<data_dir>/cache/spillover/<工具名>-<哈希>.txt`）。如果目录树里某一级
    被换成指向 `~/.ssh` 的符号链接，一次 `write_text` 就会把用户的关键文件
    覆盖掉。`resolve()` 会把所有符号链接展开，所以"解析后的路径必须仍在
    data_dir 内"这一条同时挡住了**目录级**和**文件级**的链接逃逸。
    """
    root = Path(data_dir).resolve()
    target = root.joinpath(*parts).resolve()
    if target != root and root not in target.parents:
        raise PermissionError(f"落盘路径逃出了 data_dir：{target}（疑似符号链接）")
    return target


def _safe_stem(tool_name: str) -> str:
    """工具名 → 安全文件名片段（保留可读性，非法字符换成下划线）。"""
    stem = _UNSAFE_FILENAME.sub("_", str(tool_name or "")).strip("._-")
    if not stem:
        return "tool"
    return stem[:_MAX_TOOL_STEM].rstrip("._-") or "tool"


def _write_atomic(path: Path, body: str) -> None:
    """原子写：同目录临时文件 + `os.replace`。

    为什么不直接 `path.write_text()`：模型随时可能在读这个文件（`read_file`），
    半截文件会被读成"内容就是这么多"，而且进程被杀会留下残文件。
    `os.replace` 在同一卷上是原子的，读的人只会看到"旧的完整版"或"新的完整版"。

    为什么临时文件用 `O_CREAT | O_EXCL`（对齐 `spill_safety.open_exclusive`）：
    独占创建对**任何**已存在路径（含悬空符号链接）都会失败，绝不跟随链接。
    目标文件本身的符号链接也不需要单独防——`os.replace` 替换的是链接本身，
    不是它指向的文件，所以写入无法被重定向。
    """
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as handle:
            handle.write(body)
        os.replace(tmp, path)
    except BaseException:
        # 失败不能留垃圾：临时文件清掉再把异常交出去（调用方决定降级策略）。
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _prune_once(directory: Path) -> None:
    """首次落到某个目录时顺手清一次过期文件（进程级只做一次，尽力而为）。"""
    key = str(directory)
    if key in _PRUNED_DIRS:
        return
    _PRUNED_DIRS.add(key)
    try:
        removed = cleanup_spillover(directory.parent.parent)
        if removed:
            log.debug("清理过期落盘文件 %d 个", removed)
    except OSError as exc:  # 清理失败绝不能影响落盘本身
        log.debug("清理过期落盘文件失败（忽略）：%s", exc)


def spill(
    text: str,
    *,
    tool_name: str,
    data_dir,
    config: BudgetConfig | None = None,
) -> SpillResult:
    """第二层：把超阈值正文落盘，返回「预览 + 文件路径」。

    预览是**头 1,000 + 尾 500** 字符（合计 = `preview_size`），中间插一句
    `…（完整内容已落盘：<path>，共 N 字符）…`。为什么头尾都要：头的 1,000 字
    足够模型判断"这是什么内容"，尾的 500 字常常是结论/错误/退出码——只给头
    等于让模型看不到结果。

    落盘不可用（`data_dir` 为 None）或路径逃逸时**抛异常**，由 `govern`
    决定退化为内联截断：这里不静默降级，因为"以为存下来了其实没存"是最坏的
    状态——模型会去读一个不存在的文件。
    """
    if data_dir is None:
        raise ValueError("没有 data_dir，无法落盘")
    cfg = config or BudgetConfig()
    body = text or ""

    directory = _inside(data_dir, SPILLOVER_SUBDIR.parts[0], SPILLOVER_SUBDIR.parts[1])
    directory.mkdir(parents=True, exist_ok=True)
    # mkdir 之后再核一次：`mkdir(exist_ok=True)` 对"已存在的符号链接目录"也会
    # 静默成功，所以创建完必须重新解析确认它真的还在 data_dir 里。
    if directory.resolve() != directory:
        raise PermissionError(f"落盘目录是符号链接：{directory}")

    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    path = directory / f"{_safe_stem(tool_name)}-{digest}.txt"
    _write_atomic(path, body)
    _prune_once(directory)

    total = len(body)
    head_n, tail_n = _preview_split(cfg.preview_size)
    head = body[:head_n]
    tail = body[total - tail_n :] if tail_n and total > head_n else ""
    notice = f"…（完整内容已落盘：{path}，共 {total} 字符）…"
    preview = f"{head}\n{notice}\n{tail}" if tail else f"{head}\n{notice}"

    raw_bytes = len(body.encode("utf-8"))
    cut = Cut(
        head_kept=len(head),
        tail_kept=len(tail),
        bytes_cut=max(0, raw_bytes - len(preview.encode("utf-8"))),
    )
    return SpillResult(path=path, preview=preview, bytes=raw_bytes, cut=cut, chars=total)


def spill_notice(total: int, head_kept: int, tail_kept: int, path: Path | None, *, error: str = "") -> str:
    """超限时插在上下文里的那一行提示。**措辞是这一层最重要的东西**。

    为什么这么啰嗦（"原文共 N 字符"+"中间 M 字符不在上下文里"+文件路径+"用什么读"）：
    模型看到的是一段头 + 一段尾，中间的缺失**在文本上不可见**——它只会读到
    "前半段…后半段"，然后当成完整内容来推理。所以提示行必须同时回答三个问题：

    1. **被截了吗**：明写"已截断"和"中间 N 字符不在上下文里"，而不是"已省略"这种
       可以被理解成"省略了几个空行"的说法；
    2. **原文多大**：报字符数（不是字节数），模型据此决定"要不要去读文件"；
    3. **去哪看**：给出**绝对文件路径**，并点名可以用哪个工具读（`local_read_file`），
       否则模型只会回一句"内容被截断了"然后放弃。

    落盘失败时（`path is None`）第三点换成"找不回来"——这比给一个不存在的路径好：
    模型会去读一个不存在的文件，然后得出"工具坏了"的错误结论。
    """
    middle = max(0, total - head_kept - tail_kept)
    where = (
        f"完整内容已写入 {path}，需要时用 local_read_file 读它"
        if path is not None
        else f"中间 {middle} 字符**没有落盘、找不回来**（{error or '落盘不可用'}）"
    )
    return (
        f"…[输出过长已截断：原文共 {total} 字符，这里只保留开头 {head_kept} + 结尾 "
        f"{tail_kept} 字符，中间 {middle} 字符不在上下文里；{where}]…"
    )


def _spill_target(data_dir, task_id: str) -> Path:
    r"""`<data>/spill/<task>` 目录，并确保它真的落在 data_dir 里。

    威胁模型与第二层 `spill()` 相同（见 `_inside`）：这个路径是可预测、可发现的，
    目录树里任何一级被换成符号链接都会把写入重定向到用户的关键文件。`task_id`
    先过 `_safe_stem` 收敛（去掉斜杠、反斜杠、`..`），再用 `resolve()` 复核一次。
    """
    if data_dir is None:
        raise ValueError("没有 data_dir，无法落盘")
    directory = _inside(data_dir, HOOK_SPILL_SUBDIR.name, _safe_stem(task_id or "no-task"))
    directory.mkdir(parents=True, exist_ok=True)
    # `mkdir(exist_ok=True)` 对"已存在的符号链接目录"也会静默成功，所以创建完必须
    # 重新解析确认它真的还在 data_dir 里（与 `spill()` 同一处理）。
    if directory.resolve() != directory:
        raise PermissionError(f"落盘目录是符号链接：{directory}")
    return directory


def _next_spill_path(directory: Path, tool: str) -> Path:
    """`<tool>-<n>.txt` 里第一个还没被占用的编号。

    为什么按编号而不是按内容哈希（第二层 `spill()` 用的是哈希）：这一层是按
    **上下文预算**裁剪，同一段内容被重复裁剪时，后一次应当**覆盖前一次**（模型要的是
    "最近这次输出在哪"），哈希命名会让同名内容永远指向第一次的旧文件。编号到顶
    （:data:`MAX_SPILL_INDEX`）就复用最后一个位置，避免目录无限膨胀。
    """
    stem = _safe_stem(tool)
    for index in range(1, MAX_SPILL_INDEX + 1):
        candidate = directory / f"{stem}-{index}.txt"
        if not candidate.exists():
            return candidate
    return directory / f"{stem}-{MAX_SPILL_INDEX}.txt"


def spill_output(
    text: str,
    *,
    task_id: str,
    tool: str,
    data_dir,
    limit: int = DEFAULT_SPILL_LIMIT,
    head: int = DEFAULT_SPILL_HEAD,
    tail: int = DEFAULT_SPILL_TAIL,
) -> SpillResult:
    """MATRIX 5.7：超过 `limit` 就把正文落盘，上下文里只留 `head` + 提示行 + `tail`。

    对齐 Hermes `hook_output_spill.py::spill_if_oversized`，但落点换成
    `<data>/spill/<task>/<tool>-<n>.txt`（Hermes 是 `<HERMES_HOME>/hook_outputs/<session>`）。
    按任务分目录的理由与 Hermes 按会话分目录一致：一个任务里几十条大输出落在一起，
    出问题时要能"按任务打包看"，而不是在一万个文件里翻。

    三条行为：

    1. **没超阈值 → 原样返回**（`spilled=False`，`preview` 就是原文）。绝大多数结果
       走这条，必须一个字节都不动。
    2. **超阈值 → 原子写 + 返回头尾预览**（提示行见 :func:`spill_notice`）。
    3. **写不了（没有 data_dir / 路径逃逸 / 磁盘错误）→ 抛异常**，由 :func:`spill_safe`
       决定退化为内联截断。这里不静默降级，理由与第二层 `spill()` 相同：悄悄不落盘
       而提示行还写着路径，等于把模型指向一个不存在的文件。
    """
    body = text or ""
    total = len(body)
    head_n = max(0, int(head))
    tail_n = max(0, int(tail))
    if limit <= 0 or total <= limit:
        return SpillResult(
            path=None,
            preview=body,
            bytes=len(body.encode("utf-8")),
            cut=Cut(head_kept=total, tail_kept=0),
            spilled=False,
            chars=total,
        )

    directory = _spill_target(data_dir, task_id)
    path = _next_spill_path(directory, tool)
    # 落盘正文补一个换行：`tail -f` / 编辑器读到最后一行没换行会显示"缺行尾"，
    # 而模型读到半截行也会困惑（Hermes 同样补 `\n`）。
    _write_atomic(path, body if body.endswith("\n") else body + "\n")
    _prune_hook_once(data_dir)

    head_chunk = body[:head_n]
    tail_chunk = body[total - tail_n :] if tail_n and total > head_n else ""
    notice = spill_notice(total, len(head_chunk), len(tail_chunk), path)
    preview = f"{head_chunk}\n{notice}" + (f"\n{tail_chunk}" if tail_chunk else "")
    return SpillResult(
        path=path,
        preview=preview,
        bytes=len(body.encode("utf-8")),
        cut=Cut(
            head_kept=len(head_chunk),
            tail_kept=len(tail_chunk),
            bytes_cut=max(0, len(body.encode("utf-8")) - len(preview.encode("utf-8"))),
        ),
        spilled=True,
        chars=total,
    )


def spill_safe(
    text: str,
    *,
    task_id: str = "",
    tool: str = "",
    data_dir=None,
    limit: int = DEFAULT_SPILL_LIMIT,
    head: int = DEFAULT_SPILL_HEAD,
    tail: int = DEFAULT_SPILL_TAIL,
) -> str:
    """`:func:`spill_output` 的**格式安全外壳**：不抛异常、也不把结果形状改坏。

    这是 `app/tools.py::as_tool_text` 唯一接进来的入口（MATRIX 5.7）。三条行为：

    1. **没超阈值 → 逐字返回**。绝大多数工具结果走这条，零成本、零风险。
    2. **超阈值且原文是 JSON**（`as_tool_text` 序列化出来的就是 JSON）→ 返回**仍然合法
       的 JSON 信封**：`_spilled` 说"被截了"、`_spill_path` 指路、`_chars` 报原始大小、
       `_notice` 是给人看的那句话、`head`/`tail` 留头尾。为什么不直接拼一段文本：
       `as_tool_text` 的产出在下游被当 JSON 读（`app/errors_retry.py`、观测面、
       `untrusted.wrap` 的包裹体），把一个 JSON 对象从中间劈开会让"被截断"表现成
       "工具返回了坏数据"，比截断本身更难排查——这就是"不破坏已有格式"的具体含义。

       代价要说清：信封**替换**掉原来的对象，所以 `status` / `data` 这些键在超限时
       不再出现在上下文里（完整原文在 `_spill_path` 指向的文件里，一个字都没丢）。
       这是刻意的取舍——留着 `status: ok` 而正文是截断的，比"明说被截了"更容易误导。
    3. **落盘失败（没有 data_dir / 写盘异常）→ 内联截断 + 明说"找不回来"**。
       永不抛异常：这一层在模型侧最后一道出口上，抛出去等于整条工具结果丢失。

    ## 与三层治理的叠加顺序（重要）

    `app/policies.py` 的三层治理（50,000 / 100,000 / 1,500 预览）**先**跑，本函数**后**跑，
    因为两层回答的不是同一个问题：三层治理按**结果大小分级**（"这条大到该落盘了吗"），
    本层按**上下文预算裁剪**（"这条占掉多少预算"）。顺序反过来（先 spill）会出两个错：

    * 三层治理会把本层已经裁成"头尾 + 文件路径"的预览当成新正文再判一次大小，
      于是**同一段内容被截两次**，第二次截掉的是第一次的提示行——模型再也看不到
      "原文多大、去哪看"，只看到一段没头没尾的文本；
    * 本层落盘的文件路径会随第二次落盘失效，模型按提示行去读文件会扑空。

    所以：治理在前（它会算出 `spilled` / `truncated` 记账），本层在后（它只负责
    "这一条别撑爆上下文"）。两层都落盘时，模型看到的是**后者**的路径（最近一次输出）。
    """
    body = text or ""
    if limit <= 0 or len(body) <= limit:
        return body

    head_n = max(0, int(head))
    tail_n = max(0, int(tail))
    total = len(body)
    head_chunk = body[:head_n]
    tail_chunk = body[total - tail_n :] if tail_n and total > head_n else ""

    path: Path | None = None
    notice = ""
    try:
        result = spill_output(
            body,
            task_id=task_id,
            tool=tool,
            data_dir=data_dir,
            limit=limit,
            head=head_n,
            tail=tail_n,
        )
        path = result.path
        notice = spill_notice(total, len(head_chunk), len(tail_chunk), path)
    except (OSError, ValueError) as exc:
        log.warning("hook 上下文落盘失败（退化为内联截断）：%s", exc)
        notice = spill_notice(
            total, len(head_chunk), len(tail_chunk), None, error=f"{type(exc).__name__}: {exc}"
        )

    plain = f"{head_chunk}\n{notice}" + (f"\n{tail_chunk}" if tail_chunk else "")
    # 只有**对象**才包信封：JSON 数组/标量被包成对象会改变语义（模型可能真的需要
    # 一个数组），这类原文退回纯文本预览。
    try:
        parsed = json.loads(body)
    except ValueError:
        return plain
    if not isinstance(parsed, dict):
        return plain
    envelope = {
        "_spilled": True,
        "_spill_path": str(path) if path is not None else None,
        "_chars": total,
        "_notice": notice,
        "head": head_chunk,
        "tail": tail_chunk,
    }
    return json.dumps(envelope, ensure_ascii=False)


def cleanup_spillover(data_dir, *, max_age_hours: int = SPILLOVER_MAX_AGE_HOURS) -> int:
    """删掉超过 `max_age_hours` 的落盘文件，返回删除数。

    没有这一步，落盘就是把"上下文溢出"换成"磁盘溢出"——落盘文件是可回收的
    中间产物，24 小时足够覆盖任何一次任务/调试窗口。
    """
    directory = spillover_dir(data_dir)
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    for entry in entries:
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:  # 单个文件删不掉（被占用/权限）不该中断整轮清理
            continue
    return removed


def cleanup_hook_spills(data_dir, *, max_age_hours: int = SPILLOVER_MAX_AGE_HOURS) -> int:
    """删掉 `<data>/spill/**` 下超过 `max_age_hours` 的文件，返回删除数。

    为什么单独一个清理函数而不是复用 `cleanup_spillover`：两层的目录不同
    （`cache/spillover` 按内容哈希命名、`spill/<task>` 按编号命名），混在一个函数里
    会让"只清其中一层"变成不可能——而调阈值时恰恰需要分开试。
    清完文件顺手删空的任务目录：任务目录本身也是可回收的（一个任务跑完就没有新写入）。
    """
    root = Path(data_dir) / HOOK_SPILL_SUBDIR
    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    try:
        task_dirs = [item for item in root.iterdir() if item.is_dir()]
    except OSError:
        return 0
    for task_dir in task_dirs:
        try:
            entries = list(task_dir.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
            except OSError:
                continue
        try:
            if not any(task_dir.iterdir()):  # 空目录一并回收
                task_dir.rmdir()
        except OSError:
            continue
    return removed


#: 已做过一次 hook spill 过期清理的 data_dir 集合（进程级）。
#: 与 `_PRUNED_DIRS` 同一个理由：每次落盘都扫一遍目录在大目录上不划算，但完全不扫
#: 会让纯本地进程（没有后台 housekeeping）的 spill 文件永久堆积。
_PRUNED_HOOK_DIRS: set[str] = set()


def _prune_hook_once(data_dir) -> None:
    """首次往某个 data_dir 落 hook spill 时顺手清一次过期文件（尽力而为）。"""
    key = str(data_dir)
    if key in _PRUNED_HOOK_DIRS:
        return
    _PRUNED_HOOK_DIRS.add(key)
    try:
        removed = cleanup_hook_spills(data_dir)
        if removed:
            log.debug("清理过期 hook spill 文件 %d 个", removed)
    except OSError as exc:  # 清理失败绝不能影响落盘本身
        log.debug("清理过期 hook spill 文件失败（忽略）：%s", exc)


def _inline_truncate(body: str, cfg: BudgetConfig) -> str:
    """落盘不可用时的兜底：头尾预览 + 明确说明"没能存下来"。

    绝不能只说"已截断"就完事——模型必须知道**完整内容没地方找了**，
    否则它会去尝试读一个不存在的文件（Hermes 的内联兜底文案同理）。
    """
    head_n, tail_n = _preview_split(cfg.preview_size)
    total = len(body)
    head = body[:head_n]
    tail = body[total - tail_n :] if tail_n and total > head_n else ""
    notice = f"…（正文过长且无法落盘：已内联截断，原 {total} 字符，完整内容不可找回）…"
    return f"{head}\n{notice}\n{tail}" if tail else f"{head}\n{notice}"


def _hard_truncate(body: str, budget: int) -> str:
    """整轮预算超限且落盘不可用时的硬截断（比内联预览更保守：只留头部）。"""
    marker = f"\n…[整轮预算不足且无法落盘：已硬截断，原 {len(body)} 字符]…"
    keep = max(0, int(budget) - len(marker))
    return body[:keep] + marker


class TurnBudget:
    """第三层：一次 assistant 轮次的工具结果累加器。

    为什么内部要存 `(tool_name, text, size)` 原文而不是只存一个总数：
    超预算时要**按大小降序把最大的先落盘**，没有原文就没法回填预览。
    这也是它和"一个整数计数器"的区别——它得能重写已经收下的条目。

    `add()` 返回**当前这条**治理后的正文（可能就是刚被换成的预览），
    调用方拿它去替换上下文里的内容即可；已经返回给调用方的旧条目无法撤回，
    这一点与 Hermes 的 `enforce_turn_budget`（直接改消息列表）不同，
    所以这里的策略是"宁可多落一条大的，也不要让总量继续涨"。
    """

    def __init__(self, *, data_dir=None, config: BudgetConfig | None = None):
        self.data_dir = data_dir
        self.config = config or BudgetConfig()
        self._entries: list[tuple[str, str, int]] = []
        #: 已经治理过的下标（落盘或硬截断都算）：保证每条只处理一次，
        #: 否则"降级硬截断后仍然超预算"会让循环反复处理同一条。
        self._governed: set[int] = set()
        #: 真正落盘成功的下标（`spilled` 计数只认它，降级不算落盘）
        self._spilled: set[int] = set()
        self.degraded = False

    @property
    def total(self) -> int:
        """当前仍留在上下文里的合计字符数（落盘后按预览长度算）。"""
        return sum(size for _, _, size in self._entries)

    @property
    def spilled(self) -> int:
        """已落盘的条目数。"""
        return len(self._spilled)

    def reset(self) -> None:
        """轮次结束：清空累加（下一轮从零开始计数）。"""
        self._entries.clear()
        self._governed.clear()
        self._spilled.clear()
        self.degraded = False

    def add(self, tool_name: str, text: str) -> str:
        """收下一条工具结果并返回治理后的正文（未超预算时原样返回）。"""
        body = text or ""
        self._entries.append((tool_name, body, len(body)))
        self._enforce()
        return self._entries[-1][1]

    def _enforce(self) -> None:
        """超预算就按大小降序把最大的先落盘，直到总量回到预算内。"""
        budget = int(self.config.turn_budget)
        if budget <= 0 or self.total <= budget:
            return
        order = sorted(
            range(len(self._entries)), key=lambda i: self._entries[i][2], reverse=True
        )
        for index in order:
            if self.total <= budget:
                break
            if index in self._governed:
                continue
            name, body, _size = self._entries[index]
            replacement = self._spill_entry(name, body)
            if replacement is None:
                # 落盘不可用：如实记降级，并硬截断保证总量能真的降下来
                self.degraded = True
                replacement = _hard_truncate(body, budget)
            else:
                self._spilled.add(index)
            self._entries[index] = (name, replacement, len(replacement))
            self._governed.add(index)

    def _spill_entry(self, tool_name: str, body: str) -> str | None:
        """落盘一条；不可用（没 data_dir / 写失败）时返回 None 交给调用方降级。"""
        if self.data_dir is None:
            return None
        try:
            result = spill(body, tool_name=tool_name, data_dir=self.data_dir, config=self.config)
        except (OSError, ValueError) as exc:
            log.warning("整轮预算落盘失败（退化为硬截断）：%s", exc)
            return None
        return result.preview


def govern(
    name: str,
    text: str,
    *,
    data_dir=None,
    turn: TurnBudget | None = None,
    config: BudgetConfig | None = None,
) -> Governed:
    """三层串联的门面：输出层 → 落盘层 → 整轮层，`DefaultToolResultPolicy` 只调它。

    顺序不能变：第一层先把单条裁到"能进上下文"，第二层才有机会判断"裁完还超不超
    单条阈值"；第三层必须在最后，因为它算的是**已经治理过的**正文的合计——
    若放在前面，落盘后缩小了的正文会被重复计入，预算永远判超。
    """
    cfg = config or (turn.config if turn is not None else None) or BudgetConfig()
    body = text or ""

    # 第一层：输出层上限
    capped, cut = cap_output(body, tool_name=name, config=cfg)

    # 第二层：落盘层（`read_file` 阈值是 inf，永不落盘——见 PINNED_THRESHOLDS）
    result: SpillResult | None = None
    degraded = False
    threshold = resolve_threshold(name, config=cfg)
    if not math.isinf(threshold) and len(capped) > threshold:
        target = data_dir if data_dir is not None else (turn.data_dir if turn else None)
        if target is None:
            degraded = True
            capped = _inline_truncate(capped, cfg)
        else:
            try:
                result = spill(capped, tool_name=name, data_dir=target, config=cfg)
                capped = result.preview
            except (OSError, ValueError) as exc:
                log.warning("工具结果落盘失败（退化为内联截断）：%s", exc)
                degraded = True
                capped = _inline_truncate(capped, cfg)

    # 第三层：整轮预算
    if turn is not None:
        capped = turn.add(name, capped)
        degraded = degraded or turn.degraded

    return Governed(text=capped, cut=cut, spill=result, degraded=degraded)


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_LINE_LENGTH",
    "DEFAULT_MAX_LINES",
    "DEFAULT_MCP_RESULT_SIZE",
    "DEFAULT_PREVIEW_SIZE",
    "DEFAULT_RESULT_SIZE",
    "DEFAULT_SPILL_HEAD",
    "DEFAULT_SPILL_LIMIT",
    "DEFAULT_SPILL_TAIL",
    "DEFAULT_TURN_BUDGET",
    "HOOK_SPILL_SUBDIR",
    "MCP_TOOL_PREFIX",
    "PINNED_THRESHOLDS",
    "SPILLOVER_MAX_AGE_HOURS",
    "BudgetConfig",
    "Cut",
    "Governed",
    "SpillResult",
    "TurnBudget",
    "cap_output",
    "cleanup_hook_spills",
    "cleanup_spillover",
    "govern",
    "resolve_threshold",
    "spill",
    "spill_notice",
    "spill_output",
    "spill_safe",
    "spillover_dir",
]
