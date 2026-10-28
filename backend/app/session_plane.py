"""统一 session 平面：交互 / cron / gateway 三个来源的会话，一条时间线看全。

对标 ``handoff/hermes-parity/research/03-persistence-layer.md`` §1.1 / §5.1 / §6
（MATRIX L10 的 10.1 / 10.2 / 10.4）。

## 为什么是「JSONL 追加日志 + 现有 app.sqlite 的读视图」，而不是新表

Hermes 的做法是 26 张业务表同库：``sessions``(56 列) + ``messages``(24 列) +
``delivery_obligations`` + ``compression_locks``…（实测证据：``state.db`` 40 张表、
``messages.id`` 用 ``AUTOINCREMENT`` 且 ``sqlite_sequence`` 的 ``max(id)=14953`` 大于行数
14,951，说明删过行但 id 不复用 → 可安全当回放游标）。Orbit 这一层**不能照抄**，理由三条：

1. **禁改区**。``app/storage.py`` 与 ``alembic/`` 迁移是本轮的铁律禁改区，加表就要改
   迁移脚本。而会话平面需要的字段（压缩谱系、事件类型、来源）在现有
   ``conversations`` / ``messages`` 里没有，硬塞会污染存储层。
2. **语义本来就适合追加日志**。平面事件（"谁压缩了谁"、"某会话被恢复"、"某次投递
   失败"）是**只追加、从不更新**的事实。用表来表达它，每次都要一条 UPDATE 语句和一次
   写锁；用 JSONL 表达它，写就是 ``O_APPEND`` 一行 + fsync。
3. **压缩谱系用父子引用比外键表更直接**。Hermes 用 ``sessions.parent_session_id``
   自引用 + ``end_reason='compression'``；在追加日志里一条事件同时带上
   ``session_id``（子）与 ``parent_session_id``（父），上溯就是查表 + 走链，
   不需要 join，也不需要先建好父行（Hermes 那边父行必须先存在，因为外键）。
   代价是**没有数据库级完整性**——所以 :meth:`SessionPlane.lineage` 自己做循环引用
   检测（脏数据不该让命令挂死）。

读侧仍然读 ``app.sqlite``：``messages`` 与 ``task_events`` 是既有真相源，平面不复制它们，
只在 :meth:`SessionPlane.timeline` 里与自己的事件**合并成一条时间线**。这就是"统一平面"
的全部价值：交互（messages）、cron / 任务（task_events）、平面事件（本模块）三个来源
在同一屏里按时间排好，谁也不用去猜另一处存了什么。

## 写侧统一（10.1 的最后一块）：谁在写平面，以及为什么还没写完

平面原本只有 ``delivery_ledger`` 与 ``plane`` CLI 在写，``cron`` / ``gateway`` /
``agent`` / ``graph/nodes`` / ``main`` 对它的引用数是 0——于是"统一平面"只有读侧成立，
时间线上永远只有交互那一侧的内容。本轮把**能改的两处**接上：

- ``app/cron.py``：每次运行写 ``cron.run.started`` / ``cron.run.finished`` /
  ``cron.run.failed``（带 ``job_id`` + ``run_id`` + 当时的权限档）；
- ``app/gateway.py``：每次投递写 ``gateway.delivered`` / ``gateway.delivery_failed``
  （带 ``platform`` / ``channel`` / ``message_key``）。

**agent 侧仍然是禁改区**（``app/agent.py`` 与 ``app/graph/*`` 不在本轮可改范围内），
所以"交互会话的写入也过平面"这一条**还没做**——10.1 因此只能从 🟡 前进到"两源已统一、
交互侧未统一"。这一点必须写在任何声称 10.1 完成的报告里，不能含糊。

### 本轮补上的两件（:class:`PlaneSink` / :func:`backfill_from_store`）

不能改那两个文件，但可以**把两边写成同一件事**，于是交互侧分两步落地：

1. :func:`backfill_from_store`：把历史 ``task_events`` 全部补进平面（幂等、可重复跑）。
   做完这一步，"同库同表"在**数据层面**成立——``plane timeline`` 立刻能看到历史交互
   任务的每一步，而不是只看到接线之后新发生的事。
2. :class:`PlaneSink`：``record_task_event`` 一次写两侧、共用同一个 ``message_key``；
   ``attach()`` 把 ``store.event`` 换成"写完会话库顺手写平面"的包装，于是
   ``graph/nodes.py`` 里每一处 ``await self.store.event(...)`` 都会自动进平面，
   **那两个文件一个字都不用改**。

但 ``attach()`` 的**调用点**（``app/main.py`` / ``app/agent.py``）同样是禁改区。
所以 10.1 的判据「CLI / TUI / gateway / cron 四处的会话与事件能在一处查到」：

- 读侧：✅（``plane timeline`` 合并三来源）；
- 数据层面：✅（backfill 之后历史全在）；
- **新事件自动流入：🟡 还差 ``app/main.py`` 里的一行** ``PlaneSink(plane, store).attach()``。

### 压缩阈值（10.4）为什么放在这里

阈值与目标比是**策略**，不是图结构；而真正的截短动作在 ``app/graph/nodes.py`` 的
``compact_messages``（禁改区）。所以本模块只提供纯函数
（:func:`compaction_needed` / :func:`compaction_target` / :func:`maybe_compact`）与
:func:`describe`（把与 ``nodes.py`` 的口径差异摊开），主循环在每轮开始调一次
``maybe_compact`` 就补上了 0.50 的提前判定。

### 挂到统一 session 名下的约定：:func:`plane_session_key`

各来源的键**由本模块给规则**，而不是让每个写入方自己拼字符串：

- ``cron:<job_id>``：一条定时任务就是一个会话，历次运行都在同一条时间线上；
- ``gateway:<platform>``：一个平台通道是一个会话（``channel`` 记在事件字段里）。

为什么不把 ``channel`` 也编进键：同一平台的不同频道属于同一个"对外出口"，
按平台聚合才能回答"这个平台今天发出去多少条、失败几条"；按频道切碎之后就答不了，
而按频道过滤只是事件字段上的一次筛选。

## id 续号的实现方式（10.2 的 Orbit 版）

``AppendLog`` 不靠内存计数器：进程重启后内存从 0 开始，新事件会与旧事件撞 id，
``since(cursor)`` 当场变成"永远返回全部"或"永远返回空"。做法是**进程内锁 + 文件尾读回**：
每次分配 id 前先读文件尾 64KB、倒着找最后一个可解析的 ``id``，与进程内缓存取较大值再 +1。
跨进程并发靠 ``O_APPEND`` 单行写入的原子性——同一微秒两个进程同时追加**有可能**拿到同一个
id，这一点如实标注，不假装它是分布式序号（Hermes 的 SQLite AUTOINCREMENT 才有那个保证；
要用它就必须新表，而新表是禁改区）。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: 平面日志的落点：``<data_dir>/sessions/plane.jsonl``。
#: 放在 ``sessions/`` 而不是 ``data/`` 根：Hermes 的 ``sessions/`` 目录就是这个角色
#: （调研 03 §4.2），整目录备份/搬走时不会漏。
PLANE_DIRNAME = "sessions"
PLANE_FILENAME = "plane.jsonl"

#: 事件类型（``kind``）常量。留成裸字符串也能写，但这几个是**有语义的**，
#: 尤其是 ``session.compressed``——谱系就是靠它串起来的。
KIND_COMPRESSED = "session.compressed"
KIND_RESUMED = "session.resumed"
KIND_CREATED = "session.created"
KIND_IMPORTED = "session.imported"
KIND_PRUNED = "session.pruned"
#: 归档位（10.3）：Hermes 用 ``sessions.archived`` 标记位，Orbit 加不了列（``storage.py``
#: 是禁改区），所以用两条互为反向的平面事件表达"当前是否归档"——**最后一条说了算**。
#: 事件类型常量放在这里而不是 ``session_archive.py``：``kind`` 是平面日志的公共词表，
#: 散到各模块去定义，迟早出现两处拼写不一致的"归档事件"。
KIND_ARCHIVED = "session.archived"
KIND_UNARCHIVED = "session.unarchived"
#: 系统提示词冻结（10.5）：一条事件记 ``task_id → sha256``，正文落
#: ``<data_dir>/system-prompts/<hash>.txt``（见 ``app/session_archive.py``）。
KIND_PROMPT_FROZEN = "prompt.frozen"
#: cron 写侧（10.1）：一次运行的三条事实。``started`` 与 ``finished`` 成对出现，
#: ``failed`` 是失败时**额外**加的一条——只按 ``finished`` 里的 ``ok`` 过滤也能查，
#: 但审计时"直接按 kind 捞失败"比"解析每条 finished 的布尔字段"可靠得多。
KIND_CRON_RUN_STARTED = "cron.run.started"
KIND_CRON_RUN_FINISHED = "cron.run.finished"
KIND_CRON_RUN_FAILED = "cron.run.failed"
#: gateway 写侧（10.1）：一次投递的结论。**每次尝试不单独写一条**——尝试次数记在
#: ``attempts`` 字段里，平面记的是"这件事最后成没成"，逐次尝试在投递账本里。
KIND_GATEWAY_DELIVERED = "gateway.delivered"
KIND_GATEWAY_DELIVERY_FAILED = "gateway.delivery_failed"

#: :func:`plane_session_key` 的来源前缀。写成常量而不是散落的字面量：
#: 时间线要按前缀把"cron 的会话"与"gateway 的会话"分开，拼错一个前缀就静默分裂成两条线。
SOURCE_KEY_CRON = "cron"
SOURCE_KEY_GATEWAY = "gateway"

#: 幂等索引的落点：``<data_dir>/sessions/plane-keys.json``。
#: 与 ``plane.jsonl`` 同目录：整目录备份/搬走时不会漏（漏了就等于幂等判据丢失，
#: 重复事件会在下一次运行全部重放一遍）。
PLANE_KEYS_FILENAME = "plane-keys.json"

#: 任务事件的平面 ``kind``（10.1 写侧统一）。
#: 为什么单开一个 ``task.event`` 而不是把 ``task_events.event_type`` 直接当 kind：
#: 平面日志的 kind 是**分命名空间**的（``session.*`` / ``cron.*`` / ``gateway.*``），
#: 而 ``task_events.event_type`` 是 ``node_entered`` / ``model_retry`` 这种裸名字。
#: 混进同一层之后，"这个 kind 是平面自造的还是会话库里已有的"就分不清了。
#: 原始类型放进字段 ``event_type``，查询时按字段筛，不靠猜 kind 的形状。
KIND_TASK_EVENT = "task.event"

#: ``task_events`` ↔ 平面事件共用的幂等键前缀（见 :func:`task_event_key`）。
TASK_EVENT_KEY_PREFIX = "task"

#: 统一时间线的三个来源（也是排序并列时的次序）。
SOURCE_PLANE = "plane"
SOURCE_MESSAGE = "message"
SOURCE_TASK_EVENT = "task_event"

#: 谱系上溯的硬上限。循环检测之外的第二道闸：脏数据里若出现极长链（不是环），
#: 也不能让一条命令把 CLI 拖死。
MAX_LINEAGE_DEPTH = 100

#: 单条时间线条目的正文预算（字符）。时间线是"看全"视图，不是"读全文"视图——
#: 读全文有 ``session-search read``。超长内容在这里截断并保留 ``chars`` 原始长度。
TIMELINE_TEXT_CHARS = 500

#: 读文件尾找最后一个 id 的窗口。64KB 足够覆盖几十条事件，又不会在十万行日志上变慢。
_TAIL_BYTES = 65_536

# ── 压缩阈值（10.4）────────────────────────────────────────────────────────
#
# 两个数字都照抄 Hermes 的 ``compression`` 段（research 06 §20）：
#
# - **阈值 0.50**：上下文用到窗口一半就开始压，而不是等溢出。等到溢出再压意味着
#   这一轮已经失败了（供应商返回 400），用户看到的是报错而不是一次平滑的压缩。
# - **目标 0.20**：压到窗口的 20%。只压到"刚好够用"会让下一轮工具结果立刻又撑爆，
#   来回压缩比直接报错更糟——这一点 ``app/graph/nodes.py`` 的注释里也写过。

#: 触发压缩的占用比（Hermes ``compression.threshold: 0.5``）。
COMPACTION_THRESHOLD = 0.50
#: 压缩目标占窗口的比例（Hermes ``compression.target_ratio: 0.2``）。
COMPACTION_TARGET_RATIO = 0.20
#: 压缩产物的前缀，**逐字**照抄 Hermes。``session_search`` 靠它们识别"这是机器写的
#: 交接载荷"并在书挡里剔除；两处字符串一旦不一致，压缩载荷就会借召回回流进新会话。
COMPACTION_PREFIX = "[CONTEXT COMPACTION"
SUMMARY_PREFIX = "[CONTEXT SUMMARY]:"
COMPACTION_PREFIXES = (COMPACTION_PREFIX, SUMMARY_PREFIX)
#: 字符/token 换算比（与 ``app/window.py`` 的 ``CHARS_PER_TOKEN`` 同口径）。
#: 复写一份而不是 import：``app/window.py`` 只按 token 算阈值，本模块只按 token 收
#: 输入，两边都**不做**换算——这里留一个常量只为给 CLI 的估算用。
CHARS_PER_TOKEN = 4
#: ``app/graph/nodes.py`` 里压缩目标的硬编码值（``COMPACT_TARGET_RATIO = 0.2``）。
#: 禁改区不能 import（``app.graph.nodes`` 会把 langgraph 整条链拉起来，而本模块要能被
#: 纯文件命令使用），所以复写一份；``tests/test_plane_write_side.py`` 直接解析那份源码
#: 断言两处相等——复写会漂移，断言不会。
NODES_COMPACT_TARGET_RATIO = 0.2


class SessionPlaneError(RuntimeError):
    """可预期的平面错误（谱系循环、会话不存在等）。

    与"真 bug"区分开：CLI 侧把它翻成 ``CliError``（一行消息 + 返回码），不打栈。
    """


# ── 时间 ────────────────────────────────────────────────────────────────────


def now_iso(moment: float | None = None) -> str:
    """带时区的 ISO 时间串，**与 ``app.storage.now()`` 完全同格式**。

    为什么必须同格式：时间线要跨来源按时间排序，而 ``messages.created_at`` /
    ``task_events.created_at`` 是 ``storage.now()`` 写的 ``...+00:00`` 串。平面事件
    若用 epoch 浮点或本地时间（无时区），同一秒内两类来源的相对顺序就会错乱，
    而且 ``sorted()`` 会退化成"字符串比较"而不是时间比较。
    """
    if moment is None:
        return datetime.now(UTC).isoformat()
    return datetime.fromtimestamp(moment, UTC).isoformat()


def to_epoch(value: Any) -> float:
    """把"可能是 epoch 浮点 / ISO 串 / 空"的时间值折成 epoch 秒。

    坏值返回 ``0.0`` 而不是抛异常：一条脏记录不该让整条时间线消失（它会被排到最前，
    仍然可见——这比静默丢弃更诚实）。
    """
    if isinstance(value, bool):  # bool 是 int 的子类，先挡掉
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if stamp.tzinfo is None:
        # 无时区的历史数据按 UTC 解释：本机所有写入都是 UTC，猜本地时区会整体偏移。
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.timestamp()


# ── 压缩阈值（10.4）────────────────────────────────────────────────────────


def tokens_for_chars(chars: Any) -> int:
    """字符数 → token 估算（坏值 0）。只用于 CLI 的"大概占了多少"提示。"""
    try:
        value = int(chars)
    except (TypeError, ValueError):
        return 0
    return max(0, value // CHARS_PER_TOKEN)


def compaction_used_ratio(used_tokens: Any, window: Any) -> float:
    """已用 token 占窗口的比例。窗口未知（≤0）时返回 ``0.0``——未知不等于满。"""
    try:
        used = max(0, int(used_tokens))
        size = int(window)
    except (TypeError, ValueError):
        return 0.0
    if size <= 0:
        return 0.0
    return used / size


def compaction_needed(used_tokens: Any, window: Any) -> bool:
    """要不要压缩：占用比 **≥ 0.50**。

    边界口径：**正好 50% 就压**（``>=``，不是 ``>``）。理由是"到一半"已经是"再塞
    一个工具结果就可能溢出"的状态，而压缩本身是廉价且可重放的本地操作；把边界放在
    ``>`` 只是把同一件事推迟一轮，代价是那一轮可能直接 400。

    未知窗口（``window <= 0``）**不触发**：算不出比例就不该假装算得出——压缩会丢正文
    细节，凭一个假的 100% 去压，是把"没配置"惩罚成"每次都压"。
    """
    try:
        size = int(window)
        used = int(used_tokens)
    except (TypeError, ValueError):
        return False
    if size <= 0 or used <= 0:
        return False
    return used >= size * COMPACTION_THRESHOLD


def compaction_target(window: Any, *, ratio: float = COMPACTION_TARGET_RATIO) -> int:
    """压缩后应占多少 token：窗口的 **20%**（向下取整）。

    向下取整而不是四舍五入：目标值只用于"够不够短"的判定，多给一个 token 没有收益，
    少给一个 token 只会让压缩稍微激进一点——后者是安全方向。
    """
    try:
        size = max(0, int(window))
    except (TypeError, ValueError):
        return 0
    return int(size * float(ratio))


@dataclass(frozen=True)
class CompactionPlan:
    """一次"该不该压"的判定结果。**纯数据，不含 IO**。

    ``needed=False`` 时其余字段仍然有值（目标、余量），因为调用方常要把"现在多少、
    压了会到多少"一起打出来——判 false 就全变 0 会让报告失去解释力。
    """

    needed: bool
    used_tokens: int
    window: int
    used_ratio: float
    target_tokens: int
    headroom_tokens: int
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "needed": self.needed,
            "used_tokens": self.used_tokens,
            "window": self.window,
            "used_ratio": round(self.used_ratio, 4),
            "target_tokens": self.target_tokens,
            "headroom_tokens": self.headroom_tokens,
            "reason": self.reason,
        }


def maybe_compact(used_tokens: Any, window: Any) -> CompactionPlan:
    """**主循环应在每一轮开始时调它**，再决定这一轮要不要压。

    为什么做成纯函数而不是"顺手压一下"：真正把 ``messages`` 截短的是
    ``app/graph/nodes.py`` 的 ``compact_messages``（禁改区，本轮不动），而"要不要压"
    是纯判定。拆开之后主循环只多两行：

        plan = maybe_compact(used_tokens, window)
        if plan.needed:
            messages = nodes.compact_messages(messages)   # 目标是 plan.target_tokens

    ``nodes.compact_messages`` 目前**只在 ``context_overflow`` 之后被调用**
    （见 :func:`describe`），所以这一行接线是 10.4 唯一还缺的东西——接线点在禁改区里。
    """
    try:
        used = max(0, int(used_tokens))
        size = int(window)
    except (TypeError, ValueError):
        used, size = 0, 0
    ratio = compaction_used_ratio(used, size)
    target = compaction_target(size)
    needed = compaction_needed(used, size)
    if size <= 0:
        reason = "窗口未知（context_length 未配置或为 0），不判定压缩"
    elif needed:
        reason = (
            f"已用 {used} token = 窗口的 {ratio:.1%}，达到阈值 {COMPACTION_THRESHOLD:.0%}；"
            f"应压到 {target} token（{COMPACTION_TARGET_RATIO:.0%}）"
        )
    else:
        reason = (
            f"已用 {used} token = 窗口的 {ratio:.1%}，未到阈值 "
            f"{COMPACTION_THRESHOLD:.0%}（还差 {max(0, int(size * COMPACTION_THRESHOLD) - used)} token）"
        )
    return CompactionPlan(
        needed=needed,
        used_tokens=used,
        window=max(0, size),
        used_ratio=ratio,
        target_tokens=target,
        headroom_tokens=max(0, size - used),
        reason=reason,
    )


def describe(window: Any = None) -> dict[str, Any]:
    """把本模块的压缩口径摊开，并**如实报出与 ``nodes.py`` 的差异**。

    两个用途：

    1. ``orbit plane compact-check`` 用它给出一屏可核对的数字；
    2. 出问题时不用读代码——"阈值是多少、目标是多少、主循环到底有没有提前判"都在
       这一份数据里。

    差异（本轮实测，``app/graph/nodes.py``）：

    - **目标比一致**：那边 ``COMPACT_TARGET_RATIO = 0.2``，与这里
      :data:`COMPACTION_TARGET_RATIO` 相同（有测试钉住）；
    - **触发时机不一致**：那边**没有 0.50 的提前判定**，只有模型返回
      ``context_overflow`` 之后才压（``errors_retry.decide`` 的 ``COMPACT`` 分支）。
      也就是说"到一半就压"这件事，Orbit 目前**没有生效**——本函数把它写出来，
      而不是假装已经接上。
    """
    plan = maybe_compact(0, window) if window is not None else None
    return {
        "threshold": COMPACTION_THRESHOLD,
        "target_ratio": COMPACTION_TARGET_RATIO,
        "prefixes": list(COMPACTION_PREFIXES),
        "window": int(window) if window is not None else None,
        "target_tokens": compaction_target(window) if window is not None else None,
        "sample": plan.as_dict() if plan is not None else None,
        "nodes_py": {
            "target_ratio": NODES_COMPACT_TARGET_RATIO,
            "target_matches": NODES_COMPACT_TARGET_RATIO == COMPACTION_TARGET_RATIO,
            "trigger": "reactive（只在 context_overflow 之后压）",
            "trigger_matches": False,
            "note": (
                "app/graph/nodes.py 的 compact_messages 用同一组目标比（0.2），但触发是"
                "被动的：先等模型返回 context_overflow，再压缩重试。主循环在每轮开始时"
                "调 maybe_compact(...) 才补上 0.50 的提前判定——那两行在禁改区里。"
            ),
        },
    }


# ── 统一 session 键 ─────────────────────────────────────────────────────────


def plane_session_key(source: str, ident: str = "") -> str:
    """把"某个来源的某个对象"折成统一平面上的会话名：``<source>[:<ident>]``。

    约定（写入方一律调本函数，**不要自己拼字符串**）：

    - ``plane_session_key(SOURCE_KEY_CRON, job.id)`` → ``cron:cron-1a2b3c4d``
    - ``plane_session_key(SOURCE_KEY_GATEWAY, "telegram")`` → ``gateway:telegram``
    - ``plane_session_key(SOURCE_KEY_GATEWAY)`` → ``gateway``（该来源的整体）

    为什么集中在一个函数里：时间线按 ``session_id`` 精确匹配，而"谁把前缀写成
    ``cron/``、谁写成 ``cron:``"这种差异不会报错，只会让同一条任务的事件
    静默分裂到两条时间线上——那正是统一平面要消除的现象。

    ``ident`` 里的空白被折成 ``-``、``:`` 被折成 ``_``：这两个字符是分隔符，
    出现在标识里会让"``cron:a:b`` 到底是任务 ``a:b`` 还是任务 ``a`` 的 ``b`` 阶段"
    变得不可判（``run_id`` 之类的标识由调用方保证不含分隔符）。
    """
    head = str(source or "").strip().lower()
    if not head:
        raise SessionPlaneError("plane_session_key 需要来源（例如 cron / gateway）")
    tail = str(ident or "").strip()
    if not tail:
        return head
    cleaned = "_".join(part for part in tail.replace(":", "_").split())
    return f"{head}:{cleaned}"


def _atomic_write_text(path: Path, text: str) -> None:
    """临时文件 + ``os.replace`` 原子写。

    幂等索引是"哪些事件已经写过"的唯一判据，写到一半崩溃就留下半个 JSON，
    下一次读不出来等于幂等判据全丢——所以它必须原子落盘（与 ``app/cron.py``
    的 ``_atomic_write`` 同一套做法）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


# ── 追加日志 ────────────────────────────────────────────────────────────────


class AppendLog:
    """JSONL 追加日志：单行一条 JSON，``id`` 单调递增且**跨进程/重启续号**。

    三条不变量：

    - ``id`` 严格递增，且**不复用**（与 Hermes ``messages.id AUTOINCREMENT`` 同语义）；
    - 只追加，从不重写（流水账要留下已发生的部分）；
    - 坏行跳过，不抛异常（一个手抖写坏的半行不该让整个平面读不出来）。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        #: 进程内串行化"读尾 + 追加"。没有它，两个线程会读到同一个尾部 id。
        self._lock = threading.Lock()
        #: 进程内已知的最大 id。**只是加速缓存**：真正的权威永远是文件尾，
        #: 所以每次分配都重新读一次（见 :meth:`_next_locked`）。
        self._cached_last = 0

    # ── 读 ──
    def read_all(self) -> list[dict[str, Any]]:
        """按文件顺序读出全部事件（坏行跳过）。"""
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                item = json.loads(stripped)
            except ValueError:
                continue
            if isinstance(item, dict):
                events.append(item)
        return events

    def since(self, cursor: int = 0, limit: int = 0) -> list[dict[str, Any]]:
        """**按 id 游标**增量读：返回 ``id > cursor`` 的事件。

        为什么按 id 而不按时间戳：同一毫秒内的事件时间戳相同，``ts > cursor_ts``
        会把它们一起漏掉或一起重复。Hermes 的 ``idx_messages_session_id``
        就是为这件事建的（调研 03 §1.1）。

        ``limit <= 0`` 表示不限量。
        """
        try:
            floor = int(cursor)
        except (TypeError, ValueError):
            floor = 0
        fresh = [item for item in self.read_all() if _as_int(item.get("id")) > floor]
        if limit and limit > 0:
            return fresh[:limit]
        return fresh

    def tail_events(self, count: int = 20) -> list[dict[str, Any]]:
        """最近 ``count`` 条事件（按文件顺序，返回正序）。"""
        events = self.read_all()
        if count <= 0:
            return events
        return events[-count:]

    def last_id(self) -> int:
        """当前最大 id（文件尾读回，不是内存计数）。"""
        with self._lock:
            return self._last_id_locked()

    def _last_id_locked(self) -> int:
        tail = self._last_id_from_tail()
        if tail is None:
            # 尾部窗口里没有一条完整可解析的行（文件刚被截断 / 单行超长 / 全坏）：
            # 退回全量扫描。慢，但只在异常路径上发生。
            tail = self._last_id_from_scan()
        self._cached_last = max(self._cached_last, tail)
        return self._cached_last

    def _last_id_from_tail(self) -> int | None:
        """读文件尾 ``_TAIL_BYTES`` 字节，倒着找最后一个带 ``id`` 的行。"""
        if not self.path.exists():
            return 0
        try:
            with self.path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - _TAIL_BYTES))
                chunk = handle.read()
        except OSError:
            return None
        for raw in reversed(chunk.split(b"\n")):
            if not raw.strip():
                continue
            try:
                item = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(item, dict) and "id" in item:
                value = _as_int(item.get("id"))
                if value > 0:
                    return value
        return None

    def _last_id_from_scan(self) -> int:
        best = 0
        for item in self.read_all():
            best = max(best, _as_int(item.get("id")))
        return best

    # ── 写 ──
    def append(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """追加一条事件，返回**真正落盘**的那一行（含分配的 ``id`` 与 ``ts``）。"""
        with self._lock:
            ident = self._last_id_locked() + 1
            self._cached_last = ident
            payload: dict[str, Any] = {"id": ident, "ts": now_iso()}
            # 调用方给的 id 被覆盖（id 由本类分配）；ts 允许调用方指定，且**保留其类型**
            # ——投递账本要写 epoch 浮点（与 gateway 对齐），时间线事件写 ISO 串。
            for key, value in record.items():
                if key in {"id", "ts"}:
                    continue
                payload[key] = value
            if record.get("ts"):
                payload["ts"] = record["ts"]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
                handle.flush()
                # fsync：追加日志的"至少一次"前提是它真的在盘上。少了这一步，
                # 断电后账本会缺行，而缺行意味着"已发生的事没留下痕迹"。
                os.fsync(handle.fileno())
            return payload


def _as_int(value: Any) -> int:
    """宽松整数转换（坏值 0）。``id`` 在坏数据里出现过 ``"12"`` 这种字符串。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ── 统一时间线条目 ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TimelineEntry:
    """统一时间线上的一条。``source`` 说明它来自哪个存储，``ref`` 是回跳用的 id。"""

    source: str
    ts: str
    kind: str
    ref: str
    session_id: str
    text: str
    chars: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "ts": self.ts,
            "kind": self.kind,
            "ref": self.ref,
            "session_id": self.session_id,
            "text": self.text,
            "chars": self.chars,
            "detail": self.detail,
        }

    @property
    def sort_key(self) -> tuple[float, int, str]:
        """时间 → 来源次序 → ref。三级并列键保证**同一时间戳下顺序稳定**。"""
        return (to_epoch(self.ts), _SOURCE_RANK.get(self.source, 9), self.ref)


_SOURCE_RANK = {SOURCE_PLANE: 0, SOURCE_MESSAGE: 1, SOURCE_TASK_EVENT: 2}


def _clip(text: str, limit: int = TIMELINE_TEXT_CHARS) -> str:
    """截到 ``limit`` 字符，并如实标注被截断。"""
    raw = text or ""
    return raw if len(raw) <= limit else raw[:limit] + "…"


# ── 平面本体 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AppendOutcome:
    """一次幂等追加的结果。

    ``written=False`` 表示**同一条事件已经写过**（命中了幂等索引），此时 ``event``
    是当初真正落盘的那一条（带原始 ``id``），不是本次构造的新对象——调用方拿到的
    ``id`` 因此始终指向文件里真实存在的行。
    """

    event: dict[str, Any]
    written: bool
    message_key: str = ""

    @property
    def ident(self) -> int:
        return _as_int(self.event.get("id"))

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.ident, "written": self.written, "message_key": self.message_key}


class SessionPlane:
    """统一 session 平面：追加日志 + 压缩谱系 + 三来源合并时间线。

    ``store`` 参数一律**按需传入**（:meth:`timeline`），本类自己不建 ``Store``：
    这样纯文件操作（``append`` / ``since`` / ``lineage``）在没有数据库的机器上也能用，
    测试也不必为了测一行日志去迁移整个库。
    """

    def __init__(self, data_dir: Path, *, log: AppendLog | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.log = log or AppendLog(self.data_dir / PLANE_DIRNAME / PLANE_FILENAME)
        #: 幂等索引的读改写必须串行：两个线程同时"查不到 → 各写一行"就会写出两条。
        self._keys_lock = threading.Lock()

    # ── 路径 ──
    @property
    def path(self) -> Path:
        return self.log.path

    @property
    def keys_path(self) -> Path:
        """幂等索引文件（``message_key → 事件 id``）。"""
        return self.data_dir / PLANE_DIRNAME / PLANE_KEYS_FILENAME

    # ── 写 ──
    def append(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """追加一条平面事件。``kind`` 必填——没有类型的流水账没法查询。"""
        payload = dict(event)
        kind = str(payload.get("kind") or "").strip()
        if not kind:
            raise SessionPlaneError("平面事件必须有 kind（例如 session.compressed）")
        payload["kind"] = kind
        payload.setdefault("session_id", "")
        return self.log.append(payload)

    def append_once(self, event: Mapping[str, Any], *, message_key: str) -> AppendOutcome:
        """**幂等追加**：同一 ``message_key`` 只落一行，重复调用返回当初那一行。

        为什么 plane 的 id 游标做不到幂等：``id`` 是"这一行在文件里的位置"，每次
        append 都会分配一个新 id。要判重只能靠事件自己带一个**由业务决定的键**
        （Hermes 的 ``tasks.idempotency_key`` / ``delivery_obligations.obligation_id``
        就是这个角色）。所以这里要求调用方给 ``message_key``，并把
        ``message_key → 事件 id`` 落在 :attr:`keys_path` 这个小索引文件里。

        ``message_key`` 的粒度由调用方定，约定是"**一次可重放的事实**"：

        - cron 一次运行的一个阶段：``cron:<job_id>:<run_id>:started``；
        - gateway 一次投递的一个结论：``<message_key>|<delivered|failed>``
          ——**结论要编进键**，否则"先失败后成功"的重试会被前一次失败去重掉，
          账面上永远只有失败那一条。

        跨进程并发如实标注：索引是"读文件 → 查 → 写文件"，没有文件锁，
        两个进程同时写同一个新键**有可能**各写一行（与 :class:`AppendLog` 的 id
        续号同一个局限）。进程内由 ``self._keys_lock`` 串行化。
        """
        key = str(message_key or "").strip()
        if not key:
            raise SessionPlaneError("append_once 需要 message_key（没有它就无法判重）")
        with self._keys_lock:
            index = self._load_keys()
            hit = index.get(key)
            if isinstance(hit, Mapping):
                previous = self._event_by_id(_as_int(hit.get("id")))
                if previous is not None:
                    return AppendOutcome(event=previous, written=False, message_key=key)
                # 索引指向的行已经不在了（被 compact / 手工截断）：当作没写过，
                # 重新写一条并覆盖索引——**不能**因为索引里有键就永远跳过写入，
                # 那会让事件彻底消失且没有任何痕迹。
            payload = dict(event)
            payload["message_key"] = key
            written = self.append(payload)
            index[key] = {
                "id": _as_int(written.get("id")),
                "kind": str(written.get("kind") or ""),
                "session_id": str(written.get("session_id") or ""),
                "ts": written.get("ts"),
            }
            self._save_keys(index)
            return AppendOutcome(event=written, written=True, message_key=key)

    # ── 幂等索引 ──
    def known(self, message_key: str) -> dict[str, Any] | None:
        """该 ``message_key`` 是否已经写过；写过则返回**当初那一行**，否则 ``None``。

        与 :meth:`append_once` 的区别只在"不写"：``PlaneSink`` 要先问一句
        "这条要不要写"，因为答案决定**两个存储**都要不要动（见其注释）。
        """
        key = str(message_key or "").strip()
        if not key:
            return None
        with self._keys_lock:
            hit = self._load_keys().get(key)
        if not isinstance(hit, Mapping):
            return None
        return self._event_by_id(_as_int(hit.get("id")))

    def _load_keys(self) -> dict[str, Any]:
        """读索引。坏文件当空表（下一轮会重建；索引丢了最坏结果是重复一条事件）。"""
        if not self.keys_path.exists():
            return {}
        try:
            raw = json.loads(self.keys_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        items = raw.get("keys")
        return items if isinstance(items, dict) else {}

    def _save_keys(self, index: Mapping[str, Any]) -> None:
        _atomic_write_text(
            self.keys_path,
            json.dumps({"version": 1, "keys": dict(index)}, ensure_ascii=False, indent=2),
        )

    def _event_by_id(self, ident: int) -> dict[str, Any] | None:
        """按 id 取回已落盘的事件（只在幂等命中时走一次，扫全文件可接受）。"""
        if ident <= 0:
            return None
        for item in self.log.read_all():
            if _as_int(item.get("id")) == ident:
                return item
        return None

    def compress(
        self,
        child_session_id: str,
        parent_session_id: str,
        *,
        reason: str = "compression",
        summary_chars: int = 0,
        summary: str = "",
        ts: str = "",
    ) -> dict[str, Any]:
        """记一次压缩：子会话 ← 父会话。

        一条事件同时携带 ``session_id``（子）与 ``parent_session_id``（父），
        谱系就是靠这一对串起来的（对应 Hermes 的 ``parent_session_id`` +
        ``end_reason='compression'``，调研 03 §5.4）。
        """
        if not child_session_id or not parent_session_id:
            raise SessionPlaneError("压缩事件需要子会话与父会话 id")
        event: dict[str, Any] = {
            "kind": KIND_COMPRESSED,
            "session_id": child_session_id,
            "parent_session_id": parent_session_id,
            "compressed_at": ts or now_iso(),
            "reason": reason,
            "summary_chars": int(summary_chars),
        }
        if summary:
            event["summary"] = summary
        if ts:
            event["ts"] = ts
        return self.append(event)

    # ── 读 ──
    def since(self, cursor: int = 0, limit: int = 0) -> list[dict[str, Any]]:
        """按 id 游标增量读（见 :meth:`AppendLog.since`）。"""
        return self.log.since(cursor, limit)

    def cursor(self) -> int:
        """当前最大 id：调用方保存它，下次用它做增量起点。"""
        return self.log.last_id()

    def events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """全部事件，可按会话过滤（``None`` = 不过滤）。"""
        items = self.log.read_all()
        if session_id is None:
            return items
        return [item for item in items if str(item.get("session_id") or "") == session_id]

    def tail(self, session_id: str, n: int = 20) -> list[dict[str, Any]]:
        """某会话最近 ``n`` 条**平面事件**（正序返回）。"""
        items = self.events(session_id)
        if n and n > 0:
            return items[-n:]
        return items

    def parent_of(self, session_id: str) -> str | None:
        """该会话的父会话（没有则 ``None``）。同一会话被压缩多次时取**最后一条**。"""
        parent = None
        for item in self.log.read_all():
            if str(item.get("session_id") or "") != session_id:
                continue
            candidate = str(item.get("parent_session_id") or "").strip()
            if candidate:
                parent = candidate
        return parent

    # ── 压缩谱系 ──
    def lineage(self, session_id: str) -> list[dict[str, Any]]:
        """沿父链上溯到根：``[{session_id, parent, depth, summary_chars}]``。

        **循环引用必须检测并报错**：脏数据（手工编辑、两次压缩互相指向）不该让
        命令挂死在一个死循环里。判据是"本次上溯已经见过的 id 再次出现"，
        并且额外给 ``MAX_LINEAGE_DEPTH`` 兜底（不是环但极长的链也停下来）。
        """
        target = str(session_id or "").strip()
        if not target:
            raise SessionPlaneError("lineage 需要会话 id")

        parents: dict[str, str] = {}
        chars: dict[str, int] = {}
        for item in self.log.read_all():
            parent = str(item.get("parent_session_id") or "").strip()
            if not parent:
                continue
            child = str(item.get("session_id") or "").strip()
            if not child:
                continue
            # 后写的事件覆盖先写的：同一子会话被压缩两次时，最后一次才是当前谱系。
            parents[child] = parent
            chars[child] = _as_int(item.get("summary_chars"))

        chain: list[dict[str, Any]] = []
        seen: list[str] = []
        current = target
        while True:
            if current in seen:
                trail = " → ".join([*seen, current])
                raise SessionPlaneError(f"压缩谱系存在循环引用，拒绝上溯：{trail}")
            seen.append(current)
            up: str | None = parents.get(current)
            chain.append(
                {
                    "session_id": current,
                    "parent": up,
                    "depth": len(chain),
                    "summary_chars": chars.get(current, 0),
                }
            )
            if not up:
                return chain
            if len(chain) > MAX_LINEAGE_DEPTH:
                raise SessionPlaneError(
                    f"压缩谱系超过 {MAX_LINEAGE_DEPTH} 层，疑似脏数据，拒绝继续上溯"
                )
            # 必须走 `up`。这里早先写的是 `current = parent`——`parent` 是上面建
            # `parents` 映射时留下的循环变量，循环结束后它恒等于**最后一条事件的父会话**。
            # 结果每条谱系都在第二步就自我循环，被误判成"脏数据"直接报错。
            # 变量名只差一个字母，而症状是"所有谱系都坏了"，很难一眼看出来。
            current = up

    # ── 统一时间线 ──
    async def timeline(
        self,
        session_id: str,
        *,
        store: Any = None,
        limit: int = 0,
        sources: tuple[str, ...] = (SOURCE_PLANE, SOURCE_MESSAGE, SOURCE_TASK_EVENT),
    ) -> list[TimelineEntry]:
        """把三个来源合并成一条按时间排序的时间线。

        三个来源：

        - ``plane``：本模块的追加日志（压缩、恢复、导入、清理等平面事件）；
        - ``message``：``app.sqlite`` 的 ``messages``（交互与工具轨迹的真相源）；
        - ``task_event``：``app.sqlite`` 的 ``task_events``（cron / 任务编排的每一步）。

        ``store`` 为 ``None`` 时只返回平面事件——没有数据库的机器上这条命令仍然可用，
        而不是报错（"平面"不该比它合并的东西更脆弱）。

        ## 回填之后不能把同一条事实显示两遍

        ``backfill_from_store`` 会把 ``task_events`` 复制进平面（一条 ``task.event``
        镜像，带 ``task_event_id`` 回指原行）。于是同一个事实会同时以
        **平面镜像**与 **task_event 原行**两种身份出现——如果原样合并，``plane
        timeline`` 里每一步都会重复一行，而那正是"统一平面"最不该有的样子。

        所以这里按 ``task_event_id`` 去重，并且**保留原行、丢掉镜像**：原行的 ``ref``
        是 ``task_events.id``（能回跳到真实存储），镜像的 ``ref`` 只是平面自己的行号。
        """
        target = str(session_id or "").strip()
        if not target:
            raise SessionPlaneError("timeline 需要会话 id")

        #: ``(时间线条目, 它镜像的 task_events.id)``。镜像 id 为 0 表示"这不是镜像"。
        plane_entries: list[tuple[TimelineEntry, int]] = []
        if SOURCE_PLANE in sources:
            for item in self.events(target):
                mirror_of = 0
                if str(item.get("kind") or "") == KIND_TASK_EVENT:
                    mirror_of = _as_int(item.get("task_event_id"))
                plane_entries.append(
                    (
                        TimelineEntry(
                            source=SOURCE_PLANE,
                            ts=str(item.get("ts") or ""),
                            kind=str(item.get("kind") or ""),
                            ref=str(item.get("id") or ""),
                            session_id=target,
                            text=_clip(_plane_text(item)),
                            chars=len(_plane_text(item)),
                            detail=item,
                        ),
                        mirror_of,
                    )
                )

        task_entries: list[TimelineEntry] = []
        if store is not None and SOURCE_TASK_EVENT in sources:
            task_entries = await self._task_event_entries(store, target)
        #: 库里真实存在的 ``task_events.id``。只有它们才有资格"顶掉"平面镜像——
        #: 原行被删掉之后，镜像是这条事实唯一的痕迹，那时必须留着它。
        live_ids = {_as_int(entry.ref) for entry in task_entries}

        entries: list[TimelineEntry] = [
            entry
            for entry, mirror_of in plane_entries
            if mirror_of <= 0 or mirror_of not in live_ids
        ]
        if store is not None and SOURCE_MESSAGE in sources:
            entries.extend(await self._message_entries(store, target))
        entries.extend(task_entries)

        entries.sort(key=lambda item: item.sort_key)
        if limit and limit > 0:
            return entries[-limit:]
        return entries

    async def _message_entries(self, store: Any, session_id: str) -> list[TimelineEntry]:
        """``messages`` 那一来源。只取该会话，按 ``created_at`` + ``id`` 排序（与
        ``session_search`` 的窗口切分同一条并列规则）。"""
        from sqlalchemy import select

        from app.storage import Message

        async with store.sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(Message)
                        .where(Message.conversation_id == session_id)
                        .order_by(Message.created_at.asc(), Message.id.asc())
                    )
                ).all()
            )
        return [
            TimelineEntry(
                source=SOURCE_MESSAGE,
                ts=str(row.created_at or ""),
                kind=str(row.role or ""),
                ref=str(row.id or ""),
                session_id=session_id,
                text=_clip(row.content or ""),
                chars=len(row.content or ""),
                detail={"role": row.role, "created_at": row.created_at},
            )
            for row in rows
        ]

    async def _task_event_entries(self, store: Any, session_id: str) -> list[TimelineEntry]:
        """``task_events`` 那一来源。

        ``task_events`` 只挂 ``task_id``，会话归属在 ``tasks.conversation_id`` 上，
        所以要 join 一次——否则 cron 跑的每一步都进不了会话时间线，
        而"cron 的会话看不全"正是本层要解决的问题。
        """
        from sqlalchemy import select

        from app.storage import Task, TaskEvent

        async with store.sessions() as session:
            rows = (
                await session.execute(
                    select(TaskEvent)
                    .join(Task, Task.id == TaskEvent.task_id)
                    .where(Task.conversation_id == session_id)
                    .order_by(TaskEvent.created_at.asc(), TaskEvent.id.asc())
                )
            ).all()
        entries: list[TimelineEntry] = []
        for (event,) in rows:
            payload = event.payload if isinstance(event.payload, dict) else {"value": event.payload}
            text = json.dumps(payload, ensure_ascii=False)
            entries.append(
                TimelineEntry(
                    source=SOURCE_TASK_EVENT,
                    ts=str(event.created_at or ""),
                    kind=str(event.event_type or ""),
                    ref=str(event.id),
                    session_id=session_id,
                    text=_clip(text),
                    chars=len(text),
                    detail={"task_id": event.task_id, "payload": payload},
                )
            )
        return entries


# ── 写侧统一：task_events ↔ 平面（10.1）────────────────────────────────────


def task_event_key(task_id: str, event_type: str, payload: Mapping[str, Any]) -> str:
    """``task_events`` 一行与它对应的平面事件**共用的幂等键**。

    键由**内容**决定（``task:<id>:<类型>:<payload 摘要>``），不由"写了几次"决定。
    这是 :meth:`PlaneSink.record_task_event` 与 :func:`backfill_from_store` 能对齐的
    唯一前提：两条路径面对的是同一行事实，必须算出同一个键，否则 backfill 会把
    实时写入过的事件再补一遍，平面日志当场翻倍。

    代价如实标注：**两次内容完全相同的事件会被去重成一条**（例如同一步重试两次、
    payload 逐字节相同）。要区分它们，调用方显式传 ``message_key``
    （与 ``AppendLog`` 的 id 续号、``EventLog`` 的内容指纹是同一类取舍）。
    """
    body = json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    digest = hashlib.sha256(f"{task_id}|{event_type}|{body}".encode()).hexdigest()[:16]
    return f"{TASK_EVENT_KEY_PREFIX}:{task_id}:{event_type}:{digest}"


@dataclass(frozen=True)
class SinkOutcome:
    """一次"两边都写"的结果。

    ``written_*`` 分开报：命中幂等时两个都是 ``False``（一条都不新增），
    只写一侧的情况（``attach`` 挂在 ``store.event`` 上时 store 行由原方法写）
    也能如实表达，不会被压成一句"成功"。
    """

    task_id: str
    event_type: str
    message_key: str
    session_id: str
    plane_id: int = 0
    written_plane: bool = False
    store_id: int = 0
    written_store: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "event_type": self.event_type,
            "message_key": self.message_key,
            "session_id": self.session_id,
            "plane_id": self.plane_id,
            "written_plane": self.written_plane,
            "store_id": self.store_id,
            "written_store": self.written_store,
        }


@dataclass(frozen=True)
class BackfillReport:
    """一次历史回填的账。``skipped`` = 命中了幂等索引（已经写过）。"""

    scanned: int = 0
    written: int = 0
    skipped: int = 0
    cursor: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "written": self.written,
            "skipped": self.skipped,
            "cursor": self.cursor,
        }


class PlaneSink:
    """把 ``app.storage.task_events`` 与平面 JSONL **双向对齐**的唯一入口。

    ## 为什么需要它（10.1 的最后一块）

    ``cron`` / ``gateway`` 已经直接写平面了，但交互会话的写入路径在
    ``app/agent.py`` 与 ``app/graph/*`` 里——那是本轮禁改区。于是"统一平面"只有
    读侧成立：时间线上永远看不到交互侧新发生的事。

    不能改那两个文件，能做的是**把两边写成同一件事**：

    - :meth:`record_task_event`：写一次，两边各落一行，共用同一个 ``message_key``；
    - :func:`backfill_from_store`：把历史 ``task_events`` 一次性补进平面（幂等）；
    - :meth:`attach`：把 ``store.event`` 换成"写完会话库顺手写平面"的包装。**这一步
      只需要一行调用**（``PlaneSink(plane, store).attach()``），而那一行必须写在
      ``app/main.py`` / ``app/agent.py`` 里——禁改区。所以 10.1 的判据
      「CLI / TUI / gateway / cron 四处的会话与事件能在一处查到」在数据层面已经成立，
      但"新事件自动流入"仍差那一行，**只能标 🟡**。

    ## 为什么不新增一张"对齐表"

    ``app/storage.py`` 与 alembic 迁移是禁改区（见模块 docstring 前三条理由），
    所以对齐状态就是平面自己的 ``plane-keys.json``：``message_key → 事件 id``。
    它已经存在、已经原子落盘、已经被 ``append_once`` 用着——再发明一套只会多一处
    可能与它不一致的真相源。
    """

    def __init__(self, plane: SessionPlane, store: Any = None) -> None:
        self.plane = plane
        self.store = store
        #: ``task_id → conversation_id`` 缓存。任务不会换会话，而 ``attach`` 挂在
        #: 主循环的热路径上——每条事件多查一次 tasks 表是不可接受的。
        self._conversations: dict[str, str] = {}
        #: ``attach`` 期间被吞掉的平面写入错误（见 :meth:`attach`）。
        self.errors: list[str] = []

    # ── 写（两侧）──
    async def record_task_event(
        self,
        task_id: str,
        event: str,
        *,
        session_id: str = "",
        message_key: str = "",
        **fields: Any,
    ) -> SinkOutcome:
        """记一条任务事件：``task_events`` 一行 + 平面一行，``message_key`` 幂等。

        幂等粒度是**内容**（见 :func:`task_event_key`）：同一个任务、同一类型、同一
        payload 重复调用只落一次，两个存储都不会多出一行。命中时返回**当初那一行**
        的 id（``written_*=False``），所以调用方拿到的 id 始终指向真实存在的行。
        """
        target = str(task_id or "").strip()
        kind = str(event or "").strip()
        if not target:
            raise SessionPlaneError("record_task_event 需要 task_id")
        if not kind:
            raise SessionPlaneError("record_task_event 需要事件类型（例如 node_entered）")
        payload = dict(fields)
        key = str(message_key or "").strip() or task_event_key(target, kind, payload)
        resolved = str(session_id or "").strip() or await self._conversation_of(target)

        existing = self.plane.known(key)
        if existing is not None:
            # 两侧都跳过：只补平面而不补 store，会让"平面里两次、会话库里一次"，
            # 时间线上同一步出现两遍；反过来更糟（事件凭空多一行）。
            return SinkOutcome(
                task_id=target,
                event_type=kind,
                message_key=key,
                session_id=resolved or str(existing.get("session_id") or ""),
                plane_id=_as_int(existing.get("id")),
                written_plane=False,
                store_id=_as_int(existing.get("task_event_id")),
                written_store=False,
            )

        store_id = await self._write_store(target, kind, payload)
        written = self._record_plane(
            target,
            kind,
            payload,
            session_id=resolved,
            message_key=key,
            store_id=store_id,
        )
        return SinkOutcome(
            task_id=target,
            event_type=kind,
            message_key=key,
            session_id=resolved,
            plane_id=_as_int(written.get("id")),
            written_plane=True,
            store_id=store_id,
            written_store=store_id > 0,
        )

    async def backfill(self, *, limit: int = 0) -> BackfillReport:
        """把历史 ``task_events`` 补进平面（幂等，可重复跑）。"""
        if self.store is None:
            raise SessionPlaneError("backfill 需要 store（本实例没有绑定数据库）")
        return await backfill_from_store(self.store, plane=self.plane, limit=limit)

    # ── 挂到 store.event 上（需要禁改区里的那一行调用）──
    def attach(self) -> Callable[[], None]:
        """把 ``store.event`` 换成"写会话库 + 顺手写平面"的包装，返回卸载函数。

        **这是 10.1 唯一还差的接线**：``app/agent.py`` / ``app/graph/nodes.py`` 里
        每一处 ``await self.store.event(...)`` 都会因此自动进平面，而那两个文件一个字
        都不用改。但**调用这一行的地方**（``app/main.py`` 或 ``app/agent.py``）是禁改区，
        所以本轮只能把它做成"一行就能接上"的钩子，不能声称已经接上。

        包装里的平面写入失败**被吞掉并计数**（:attr:`errors`）：主循环不能因为
        "记账失败"而让一条正在跑的对话崩掉，但也不能假装没发生——所以计数可查。
        """
        if self.store is None:
            raise SessionPlaneError("attach 需要 store（没有它就没有 store.event 可挂）")
        store = self.store
        original = store.event
        if getattr(original, "_orbit_plane_sink", False):
            raise SessionPlaneError("这个 store 已经挂过 PlaneSink（重复挂会写两份平面事件）")

        async def event(task_id: str, kind: str, **payload: Any) -> Any:
            row = await original(task_id, kind, **payload)
            try:
                session_id = await self._conversation_of(str(task_id))
                self._record_plane(
                    str(task_id),
                    str(kind),
                    dict(payload),
                    session_id=session_id,
                    message_key=task_event_key(str(task_id), str(kind), payload),
                    store_id=_as_int(getattr(row, "id", 0)),
                )
            except Exception as exc:  # 记账失败不该打断正在跑的对话
                self.errors.append(f"{kind}: {exc}")
            return row

        event._orbit_plane_sink = True  # type: ignore[attr-defined]
        store.event = event

        def detach() -> None:
            store.event = original

        return detach

    # ── 内部 ──
    async def _write_store(self, task_id: str, kind: str, payload: Mapping[str, Any]) -> int:
        """写一行 ``task_events``，返回它的整数 id（失败/无 store 时 0）。"""
        if self.store is None:
            return 0
        from app.storage import TaskEvent

        saved = await self.store.save(TaskEvent(task_id=task_id, event_type=kind, payload=dict(payload)))
        return _as_int(getattr(saved, "id", 0))

    def _record_plane(
        self,
        task_id: str,
        kind: str,
        payload: Mapping[str, Any],
        *,
        session_id: str,
        message_key: str,
        store_id: int = 0,
        ts: str = "",
    ) -> dict[str, Any]:
        """只写平面那一半（幂等）。``summary`` 给时间线一个可读的一行。"""
        document: dict[str, Any] = {
            "kind": KIND_TASK_EVENT,
            "session_id": str(session_id or ""),
            "task_id": task_id,
            "event_type": kind,
            "payload": dict(payload),
            "task_event_id": int(store_id),
            "summary": f"{kind}: {json.dumps(dict(payload), ensure_ascii=False)[:160]}",
        }
        if ts:
            document["ts"] = ts
        return self.plane.append_once(document, message_key=message_key).event

    async def _conversation_of(self, task_id: str) -> str:
        """``task_id → conversation_id``（带缓存；无 store 时返回空串）。"""
        cached = self._conversations.get(task_id)
        if cached is not None:
            return cached
        if self.store is None:
            return ""
        from app.storage import Task

        task = await self.store.get(Task, task_id)
        conversation = str(getattr(task, "conversation_id", "") or "") if task is not None else ""
        self._conversations[task_id] = conversation
        return conversation


async def backfill_from_store(
    store: Any, *, plane: SessionPlane, limit: int = 0
) -> BackfillReport:
    """把历史 ``task_events`` **一次性补进平面**（幂等，可重复跑）。

    为什么需要它：写侧接线（:meth:`PlaneSink.attach`）要等禁改区里的那一行，但
    "同库同表"这件事在**数据层面**现在就能成立——把已有的 ``task_events`` 全部按
    :func:`task_event_key` 补进平面之后，``plane timeline`` 立刻能看到历史交互任务
    的每一步，而不是只有接线之后新发生的事。

    三个刻意的选择：

    1. **``ts`` 用原来的 ``created_at``**，不是"现在"。补进去的事件必须落在它真正
       发生的位置，否则时间线会把三个月的历史全挤在最后一行。
    2. **``session_id`` 取 ``tasks.conversation_id``**（join 一次）。``task_events``
       自己只挂 ``task_id``，不 join 就补不进任何会话的时间线——那等于白补。
    3. **``limit`` 只限制本批扫描行数**，不是"只补最新的 N 条"：补历史要从最早的
       开始，跳着补会让幂等索引出现空洞。
    """
    from sqlalchemy import select

    from app.storage import Task, TaskEvent

    statement = (
        select(TaskEvent, Task.conversation_id)
        .join(Task, Task.id == TaskEvent.task_id)
        .order_by(TaskEvent.id.asc())
    )
    if limit and limit > 0:
        statement = statement.limit(int(limit))
    async with store.sessions() as session:
        rows = (await session.execute(statement)).all()

    scanned = written = skipped = 0
    for event, conversation_id in rows:
        scanned += 1
        payload = event.payload if isinstance(event.payload, dict) else {"value": event.payload}
        key = task_event_key(str(event.task_id), str(event.event_type), payload)
        document = {
            "kind": KIND_TASK_EVENT,
            "session_id": str(conversation_id or ""),
            "task_id": str(event.task_id),
            "event_type": str(event.event_type),
            "payload": payload,
            "task_event_id": _as_int(event.id),
            "summary": f"{event.event_type}: {json.dumps(payload, ensure_ascii=False)[:160]}",
            "ts": str(event.created_at or ""),
        }
        outcome = plane.append_once(document, message_key=key)
        if outcome.written:
            written += 1
        else:
            skipped += 1
    return BackfillReport(scanned=scanned, written=written, skipped=skipped, cursor=plane.cursor())


def _plane_text(event: Mapping[str, Any]) -> str:
    """平面事件的显示文本：摘要优先，否则退化成"类型 + 其余字段"。"""
    summary = str(event.get("summary") or "").strip()
    if summary:
        return summary
    detail = {
        key: value
        for key, value in event.items()
        if key not in {"id", "ts", "kind", "session_id"}
    }
    return f"{event.get('kind')}" + (f" {json.dumps(detail, ensure_ascii=False)}" if detail else "")


__all__ = [
    "CHARS_PER_TOKEN",
    "COMPACTION_PREFIX",
    "COMPACTION_PREFIXES",
    "COMPACTION_TARGET_RATIO",
    "COMPACTION_THRESHOLD",
    "KIND_ARCHIVED",
    "KIND_COMPRESSED",
    "KIND_CREATED",
    "KIND_CRON_RUN_FAILED",
    "KIND_CRON_RUN_FINISHED",
    "KIND_CRON_RUN_STARTED",
    "KIND_GATEWAY_DELIVERED",
    "KIND_GATEWAY_DELIVERY_FAILED",
    "KIND_IMPORTED",
    "KIND_PRUNED",
    "KIND_PROMPT_FROZEN",
    "KIND_RESUMED",
    "KIND_TASK_EVENT",
    "KIND_UNARCHIVED",
    "MAX_LINEAGE_DEPTH",
    "NODES_COMPACT_TARGET_RATIO",
    "PLANE_DIRNAME",
    "PLANE_FILENAME",
    "PLANE_KEYS_FILENAME",
    "SOURCE_KEY_CRON",
    "SOURCE_KEY_GATEWAY",
    "SOURCE_MESSAGE",
    "SOURCE_PLANE",
    "SOURCE_TASK_EVENT",
    "SUMMARY_PREFIX",
    "TASK_EVENT_KEY_PREFIX",
    "TIMELINE_TEXT_CHARS",
    "AppendLog",
    "AppendOutcome",
    "BackfillReport",
    "CompactionPlan",
    "PlaneSink",
    "SessionPlane",
    "SessionPlaneError",
    "SinkOutcome",
    "TimelineEntry",
    "backfill_from_store",
    "compaction_needed",
    "compaction_target",
    "compaction_used_ratio",
    "describe",
    "maybe_compact",
    "now_iso",
    "plane_session_key",
    "task_event_key",
    "to_epoch",
    "tokens_for_chars",
]
