"""cron 定时子系统：任务定义、调度表达式、到点执行与失败治理。

设计取舍（动这里之前先读）：

- **落盘用 JSON，不建数据库表**。任务定义进 ``<data_dir>/cron/jobs.json``，每次执行
  一行进 ``<data_dir>/cron/runs.jsonl``。迁移文件是禁改区，而定时任务恰恰是用户最想
  手改、最想备份的东西——可读、可 diff、可手工修，比多一张表值钱。代价是没有事务，
  所以写盘一律"临时文件 + ``os.replace``"（原子替换），并且**坏文件当空表**处理：
  一个写坏的 JSON 不该让整个子系统消失，只该让这一轮少几个任务。
- **调度表达式自己实现，不引 croniter**。本机无网络、装不了新依赖，而这里需要的只是
  "间隔 + 5 字段 cron 的最小子集"；自己写一百行换来的是纯函数、可测、无版本漂移。
- **执行器注入**。:class:`CronRunner` 只认 ``Callable[[Job], Awaitable[dict]]``，不
  import agent：测试不必真跑模型，CLI 也能按需换成脚本执行器。
- **不常驻**。:meth:`CronRunner.tick` 是一次性的，由外部调度器（Windows 计划任务 /
  systemd timer）按分钟调用。常驻线程会引入"进程死了任务就停"的隐形依赖，而这个子系统
  的用户本来就希望它跟着系统调度器活。

## 本轮新增的三条（MATRIX 10.1 / 11.3 / 11.5 / 11.6）

- **写侧统一（10.1）**：每次运行往 ``app/session_plane.py`` 写
  ``cron.run.started`` / ``cron.run.finished`` / ``cron.run.failed``，
  会话键是 ``cron:<job_id>``。这样"这条任务昨天到底跑没跑"在统一时间线里就能看到，
  而不必去翻 ``runs.jsonl`` 和别的存储各查一遍。**agent 侧仍是禁改区**，
  交互会话的写入还没统一——那是 10.1 变 ✅ 的最后一块。
- **平台投递（11.3）**：``--deliver`` 除 ``none`` / ``file:<路径>`` 之外，
  现在支持 ``platform:<平台>[:<频道>]``，走 ``app/gateway.py`` 的 ``deliver()``。
  为什么加 ``platform:`` 前缀而不是直接写 ``telegram:chat``：``file:`` 已经占了
  "前缀 + 参数"的形状，两种写法混在一个命名空间里迟早互相吃掉（``file`` 既是
  投递模式又是开发旁路平台名）。**未实现的平台在 ``create`` 时就拒绝**——等到跑的
  时候才失败，用户已经以为它配好了。
- **权限共享（11.5）**：任务运行时读**与交互同一份** ``<data_dir>/settings.json``
  的 ``file_scope`` / ``approval_mode``，并把这两档写进运行记录与平面事件。
  要能回答"这个定时任务当时有没有权限写工作目录外"，否则事后追责只能靠猜。
- **紧急停止级联（11.6）**：:func:`pause_all` / :func:`resume_all` 同时管
  cron 哨兵、gateway 哨兵与"正在跑的任务"（后者**只能标记**——执行体在别的进程里，
  跨进程强杀是另一个量级的风险）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.session_plane import (
    KIND_CRON_RUN_FAILED,
    KIND_CRON_RUN_FINISHED,
    KIND_CRON_RUN_STARTED,
    SOURCE_KEY_CRON,
    SessionPlane,
    plane_session_key,
)

#: 连续失败到这个次数就自动停用任务（照 Hermes "别让坏任务无限重试"的思路）。
MAX_CONSECUTIVE_FAILURES = 3

#: ``runs.jsonl`` 里单条输出保留的字符数。任务输出可能很长，记录只留能定位问题的量；
#: 要完整输出请配 ``deliver=file:<路径>``，那是给人看的、不截断。
OUTPUT_KEEP = 4000

#: 投递目标三种形状：``none`` / ``file:<路径>`` / ``platform:<平台>[:<频道>]``。
#: ``platform:`` 前缀是刻意的（见模块 docstring）：不给前缀的话 ``telegram:1`` 这类
#: 历史无效值会突然变得合法，而 ``file`` 既是模式名又是开发旁路平台名。
DELIVER_NONE = "none"
DELIVER_FILE_PREFIX = "file:"
DELIVER_PLATFORM_PREFIX = "platform:"

#: 不需要凭证的开发旁路平台（``app/platforms.py`` 的 ``ConsoleAdapter`` / ``FileAdapter``）。
#: 它们不在 22 个平台里，但``implemented`` 判据对它们没有意义——stdout 与 outbox
#: 永远可用，所以单独放行而不是塞进 ``PLATFORMS``。
DELIVER_DEV_PLATFORMS: tuple[str, ...] = ("console", "file")

#: 正在跑的任务登记簿。执行体可能在**另一个进程**里（外部调度器起的 tick），
#: 所以紧急停止只能在这里打标记，不能杀。
RUNNING_FILENAME = "running.json"

#: 事故三态。``detected`` = 刚发现；``alerted`` = 人已看到；``closed`` = 已恢复/已处理。
INCIDENT_STATES: tuple[str, ...] = ("detected", "alerted", "closed")


# ── 时间与原子写 ────────────────────────────────────────────────────────────


def _aware(moment: datetime) -> datetime:
    """统一成带时区的 UTC 时刻。

    手写的 ISO 串经常缺时区（用户拿记事本改 jobs.json 时不会写 ``+00:00``），
    按 UTC 解释而不是本地时间：本地时区会把"下一次触发"整体偏移几个小时。
    """
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def to_iso(moment: datetime) -> str:
    """落盘用的时间串（秒精度、带时区偏移）。"""
    return _aware(moment).isoformat(timespec="seconds")


def from_iso(value: str | None) -> datetime | None:
    """读回时间串。坏值返回 ``None``，让调用方决定怎么兜底——不抛。"""
    if not value:
        return None
    try:
        return _aware(datetime.fromisoformat(str(value).strip()))
    except ValueError:
        return None


def now_utc() -> datetime:
    return datetime.now(UTC)


def _atomic_write(path: Path, text: str) -> None:
    """临时文件 + ``os.replace``：中途崩溃只会留下旧文件，不会留下半个 JSON。

    不能直接 ``path.write_text``：那条路径在"写完一半"时崩溃会留下截断的文件，而
    jobs.json 截断就等于把用户全部定时任务清空（本文件把坏文件当空表，于是"数据静默
    消失"）。临时文件与目标同目录，保证 ``os.replace`` 是同卷 rename（原子）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)  # 失败不留垃圾临时文件
        raise


# ── 投递目标 ────────────────────────────────────────────────────────────────


def _platform_rejection(name: str) -> str:
    """平台能不能投？能投返回空串，不能投返回**一句能行动的话**。

    判据来自 ``app.platforms.PLATFORMS[...].implemented``（单一事实源）：
    22 个平台里 11 个有出站适配器，其余 11 个只有声明。这里读声明而不是读
    ``default_adapters()`` 的键，因为后者只反映"本进程装了哪些适配器"，
    而"这个平台到底有没有实现"是产品事实，不该随进程配置变化。

    ``app.platforms`` 是**惰性导入**：``app/cron.py`` 的核心（调度表达式 + 落盘）
    在纯文件场景下也用得上，不该为了校验一个投递目标就把 httpx 适配器栈拉起来。
    """
    from app.platforms import PLATFORMS, Platform

    if name in DELIVER_DEV_PLATFORMS:
        return ""
    try:
        platform = Platform(name)
    except ValueError:
        known = "、".join(sorted({item.value for item in PLATFORMS} | set(DELIVER_DEV_PLATFORMS)))
        return f"未知投递平台：{name!r}。可用：{known}"
    spec = PLATFORMS[platform]
    if not spec.implemented:
        needs = f"；需要凭证：{'、'.join(spec.credentials)}" if spec.credentials else ""
        return f"平台 {name} 尚未实现：{spec.reason or '缺少出站适配器'}{needs}"
    return ""


def validate_deliver(deliver: str | None) -> str:
    """校验并规范化投递目标；不认识的值**显式报错**。

    三种合法形状：

    - ``none``（默认，不投递）；
    - ``file:<路径>``：把输出写进文件（不截断，给人看）；
    - ``platform:<平台>[:<频道>]``：走 ``app/gateway.py`` 的平台适配器投递。

    为什么必须报错而不是静默降级：静默接受一个没实现的平台会让用户以为"输出已经发到
    群里了"，实际什么都没发——定时任务的失败模式里，"以为发了"比"明确没发"贵得多。

    **未实现的平台在 ``create`` / ``edit`` 就被拒**（调用方都是先校验再落盘），
    而不是等到任务跑起来才失败：那时用户已经等了半小时。
    """
    value = (deliver or "").strip() or DELIVER_NONE
    if value == DELIVER_NONE:
        return DELIVER_NONE
    if value.startswith(DELIVER_FILE_PREFIX):
        target = value[len(DELIVER_FILE_PREFIX) :].strip()
        if not target:
            raise ValueError("投递目标 file: 后面要跟文件路径（例：--deliver file:data/cron/out.md）")
        return f"{DELIVER_FILE_PREFIX}{target}"
    if value.startswith(DELIVER_PLATFORM_PREFIX):
        rest = value[len(DELIVER_PLATFORM_PREFIX) :].strip()
        if not rest:
            raise ValueError(
                "投递目标 platform: 后面要跟平台名（例：--deliver platform:telegram:<chat_id>）"
            )
        name, _, channel = rest.partition(":")
        platform_name = name.strip().lower()
        if not platform_name:
            raise ValueError(f"投递目标缺少平台名：{value!r}")
        reason = _platform_rejection(platform_name)
        if reason:
            raise ValueError(reason)
        clean_channel = channel.strip()
        return (
            f"{DELIVER_PLATFORM_PREFIX}{platform_name}"
            + (f":{clean_channel}" if clean_channel else "")
        )
    raise ValueError(
        f"投递目标无法识别：{value!r}。平台投递未实现这种写法——平台投递要写成 "
        "platform:<平台>[:<频道>]（走 app/gateway.py 的适配器，属 W9 平台层）。"
        "当前支持 none、file:<路径>、platform:<平台>[:<频道>]。"
    )


def parse_deliver(deliver: str | None) -> tuple[str, str, str]:
    """把校验过的投递目标拆成 ``(模式, 目标, 频道)``。

    模式是 ``none`` / ``file`` / ``platform``；``目标`` 是文件路径或平台名；
    ``频道`` 只对平台投递有意义（空串 = 该平台的 home 频道）。
    与 :func:`validate_deliver` 配对使用：**先校验再解析**，避免两条路径各自宽松。
    """
    value = validate_deliver(deliver)
    if value == DELIVER_NONE:
        return DELIVER_NONE, "", ""
    if value.startswith(DELIVER_FILE_PREFIX):
        return "file", value[len(DELIVER_FILE_PREFIX) :].strip(), ""
    rest = value[len(DELIVER_PLATFORM_PREFIX) :]
    name, _, channel = rest.partition(":")
    return "platform", name.strip().lower(), channel.strip()


def deliver_to(
    platform: str,
    channel: str,
    text: str,
    *,
    data_dir: str | Path | None = None,
    gateway: Any = None,
    message_key: str = "",
    now: float | None = None,
) -> Any:
    """把文本投递到平台，并**把结论记进投递账本**（``app/delivery_ledger.py``）。

    走 ``app/gateway.py`` 的 ``deliver()``，不另写一套发送逻辑：账本格式、重试语义、
    平台未实现的报错形状都在那一层，重写一遍只会得到第二个互相矛盾的答案。

    幂等：``message_key`` 由调用方给（cron 用 ``cron:<job_id>:<run_id>``）。
    认领（``claim``）**不阻止发送**——定时任务的语义是"这一轮跑出来的东西必须投一次"，
    而账本的作用是"送没送到只有一个答案"。认领结果只用于留痕与去重查询。
    """
    from app import gateway as gw
    from app.delivery_ledger import STATUS_DELIVERED, STATUS_FAILED, DeliveryLedger

    name = str(platform or "").strip().lower()
    if not name:
        raise ValueError("平台投递需要平台名")
    key = str(message_key or "").strip()
    ledger: Any = None
    if data_dir is not None:
        ledger = DeliveryLedger(Path(data_dir))
        if gateway is None:
            # 必须用**同一个 data_dir** 建服务：不给的话 ``gw.deliver`` 会退回
            # ``Settings().data_dir``，于是账本与平面都写进真实数据目录——
            # 测试里看不见（事件"凭空消失"），真实环境里则是"数据目录 A 的任务
            # 往目录 B 记账"，排查起来极难。
            gateway = gw.service(Path(data_dir))
    if ledger is not None and key:
        ledger.claim(
            key,
            channel=str(channel or ""),
            platform=name,
            session_key=f"{name}:{str(channel or '').strip() or 'home'}",
            now=now,
        )
    result = gw.deliver(
        text,
        platform=name,
        channel=str(channel or ""),
        gateway=gateway,
        message_key=key,
    )
    if ledger is not None and key:
        ledger.mark(
            key,
            STATUS_DELIVERED if result.ok else STATUS_FAILED,
            result.error,
            channel=str(channel or ""),
            platform=name,
            message_id=result.message_id,
            now=now,
        )
    return result



# ── 与交互共享的权限设置（MATRIX 11.5） ────────────────────────────────────

#: 权限档的兜底值。与 ``app/config.py`` 的默认值一致：配置读不出来时**退回更窄的一档**
#: （``workspace`` = 仅工作文件夹、``off`` = 只拦硬地板），而不是反向放开。
DEFAULT_FILE_SCOPE = "workspace"
DEFAULT_APPROVAL_MODE = "off"


def read_permissions(data_dir: str | Path) -> dict[str, str]:
    """读**与交互同一份** ``<data_dir>/settings.json`` 的权限两档。

    为什么读文件而不是 ``Settings()``：交互侧（对话页）把权限改动写在
    ``<data_dir>/settings.json``（见 ``app/cli/context.py`` 的 ``hydrate``），
    而 cron 的执行体常常在**没有配置环境**的进程里跑（外部调度器起的 tick）。
    读同一份文件才能保证"我在对话页把权限调成仅工作文件夹"对定时任务同样生效——
    这正是 11.5 要的东西：不是再造一套 cron 专属权限，而是共用同一份。

    返回 ``{file_scope, approval_mode, source}``；``source`` 说明这档是从哪来的
    （``settings.json`` 还是默认值），事后追责时要能区分"用户设的"与"没设过"。

    坏值一律降级到更窄的一档：``file_scope`` 只认 ``workspace`` / ``machine``，
    ``approval_mode`` 只认 ``off`` / ``smart`` / ``manual``。两个合法取值表都从
    各自的模块**惰性导入**，免得在这里复制一份会漂移的常量。
    """
    from app.approvals import APPROVAL_MODES
    from app.config import normalize_file_scope

    path = Path(data_dir) / "settings.json"
    raw: Any = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
    data = raw if isinstance(raw, dict) else {}
    mode = str(data.get("approval_mode") or "").strip().lower()
    return {
        "file_scope": normalize_file_scope(data.get("file_scope", DEFAULT_FILE_SCOPE)),
        "approval_mode": mode if mode in APPROVAL_MODES else DEFAULT_APPROVAL_MODE,
        "source": str(path) if path.exists() else "默认值（settings.json 不存在）",
    }


# ── 调度表达式 ──────────────────────────────────────────────────────────────

#: 间隔单位 → 秒。
_UNIT_SECONDS: dict[str, int] = {"s": 1, "m": 60, "h": 3600, "d": 86400}

#: 间隔单位的英文写法 → 单字母。``every hour`` / ``every 30 minutes`` 都用得上。
_UNIT_WORDS: dict[str, str] = {
    "s": "s",
    "sec": "s",
    "secs": "s",
    "second": "s",
    "seconds": "s",
    "m": "m",
    "min": "m",
    "mins": "m",
    "minute": "m",
    "minutes": "m",
    "h": "h",
    "hr": "h",
    "hrs": "h",
    "hour": "h",
    "hours": "h",
    "d": "d",
    "day": "d",
    "days": "d",
}

_INTERVAL_RE = re.compile(r"^(?:every\s+)?(?P<num>\d+)\s*(?P<unit>[a-z]+)$", re.IGNORECASE)
_EVERY_WORD_RE = re.compile(r"^every\s+(?P<unit>[a-z]+)$", re.IGNORECASE)
_CRON_FIELD_RE = re.compile(r"^[0-9*,\-/]+$")

#: cron 写法向后搜索的天数上限。4 年足够覆盖"2 月 29 日"这类最稀疏的合法表达式，
#: 同时保证非法/永不触发的表达式在有限时间内报错而不是挂住。
_CRON_SEARCH_DAYS = 366 * 4


@dataclass(frozen=True)
class Interval:
    """固定间隔写法：``30m`` / ``2h`` / ``every 2h``。"""

    seconds: int


@dataclass(frozen=True)
class CronSpec:
    """5 字段 cron 写法（分 时 日 月 周）。周字段 0=周日，7 也接受。"""

    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]


Schedule = Interval | CronSpec


def _parse_number(text: str, low: int, high: int, *, name: str) -> int:
    text = text.strip()
    if not text.isdigit():
        raise ValueError(f"{name}字段不是数字：{text!r}")
    value = int(text)
    if not low <= value <= high:
        raise ValueError(f"{name}字段超出范围 {low}-{high}：{value}")
    return value


def _parse_field(text: str, low: int, high: int, *, name: str) -> frozenset[int]:
    """解析单个 cron 字段：``*`` / 数字 / ``a-b`` / ``a,b`` / ``*/n`` / ``a/n``。"""
    values: set[int] = set()
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            raise ValueError(f"{name}字段里有空的列表项：{text!r}")
        step = 1
        if "/" in chunk:
            chunk, _, raw_step = chunk.partition("/")
            if not raw_step.strip().isdigit() or int(raw_step) <= 0:
                raise ValueError(f"{name}字段的步长非法：{raw_step!r}")
            step = int(raw_step)
        if chunk == "*":
            start, end = low, high
        elif "-" in chunk:
            head, _, tail = chunk.partition("-")
            start = _parse_number(head, low, high, name=name)
            end = _parse_number(tail, low, high, name=name)
            if start > end:
                raise ValueError(f"{name}字段的范围倒序：{chunk!r}")
        else:
            start = _parse_number(chunk, low, high, name=name)
            # `5/15` 的语义是"从 5 开始每 15 到上限"，与 vixie cron 一致。
            end = high if step != 1 else start
        values.update(range(start, end + 1, step))
    if not values:
        raise ValueError(f"{name}字段没有取值：{text!r}")
    return frozenset(values)


def _normalize_weekdays(values: frozenset[int]) -> frozenset[int]:
    """周字段 0-6 与 7 都接受，7 折算成 0（周日），与 vixie cron 一致。"""
    return frozenset(0 if value == 7 else value for value in values)


def parse_schedule(schedule: str) -> Schedule:
    """把三种写法解析成 :class:`Interval` 或 :class:`CronSpec`。

    支持：``30m`` / ``90s`` / ``2h`` / ``1d``、``every 2h``（含 ``every hour``）、
    5 字段 cron ``0 9 * * 1-5``。

    **不支持** Hermes 的一次性写法（``in 30m``）与自然语言（``every monday 9am``）：
    它们的语义是"定点一次性触发"，需要另一套状态机；硬塞进"按 next_run_at 轮询"的模型
    里会给出错误结果（比如每轮都触发），所以宁可明确报错并说清替代写法。
    """
    text = (schedule or "").strip()
    if not text:
        raise ValueError("调度表达式不能为空")
    lowered = text.lower()
    if lowered.startswith("in "):
        raise ValueError(
            f"暂不支持一次性写法：{text!r}。请用间隔（'30m'）或 5 字段 cron（'0 9 * * *'）表达重复任务。"
        )
    fields = text.split()
    if len(fields) == 5:
        if not all(_CRON_FIELD_RE.match(item) for item in fields):
            raise ValueError(f"不是合法的 5 字段 cron（分 时 日 月 周）：{text!r}")
        minute, hour, day, month, weekday = fields
        return CronSpec(
            minutes=_parse_field(minute, 0, 59, name="分"),
            hours=_parse_field(hour, 0, 23, name="时"),
            days=_parse_field(day, 1, 31, name="日"),
            months=_parse_field(month, 1, 12, name="月"),
            weekdays=_normalize_weekdays(_parse_field(weekday, 0, 7, name="周")),
        )
    if len(fields) != 1 and not lowered.startswith("every"):
        raise ValueError(
            f"无法解析调度表达式：{text!r}。支持 '30m'、'every 2h'、'0 9 * * *' 三种写法。"
        )
    match = _INTERVAL_RE.match(text)
    if match:
        unit = _UNIT_WORDS.get(match.group("unit").lower())
        if unit is None:
            raise ValueError(
                f"不认识的间隔单位：{match.group('unit')!r}（可用 s/m/h/d，或 second/minute/hour/day）"
            )
        number = int(match.group("num"))
        if number <= 0:
            raise ValueError(f"间隔必须为正：{text!r}")
        return Interval(number * _UNIT_SECONDS[unit])
    word = _EVERY_WORD_RE.match(text)
    if word:
        unit = _UNIT_WORDS.get(word.group("unit").lower())
        if unit is None:
            raise ValueError(
                f"不认识的间隔单位：{word.group('unit')!r}（可用 second/minute/hour/day）"
            )
        return Interval(_UNIT_SECONDS[unit])
    raise ValueError(
        f"无法解析调度表达式：{text!r}。支持 '30m'、'every 2h'、'0 9 * * *' 三种写法。"
    )


def _day_matches(day: date, spec: CronSpec, *, day_restricted: bool, weekday_restricted: bool) -> bool:
    """日 / 周同时受限时按 vixie cron 的 OR 语义：任一匹配即触发。

    为什么不用 AND：``0 9 1 * 1``（每月 1 号**或**每周一）在标准 cron 里是"或"，
    用"与"会让绝大多数真实表达式算不出下一次触发，表现为"任务再也不跑"。
    """
    dom_ok = day.day in spec.days
    # ``date.weekday()`` 是周一=0，而 cron 周字段是周日=0，要错位一格。
    dow_ok = ((day.weekday() + 1) % 7) in spec.weekdays
    if day_restricted and weekday_restricted:
        return dom_ok or dow_ok
    if day_restricted:
        return dom_ok
    if weekday_restricted:
        return dow_ok
    return True


def _next_cron(spec: CronSpec, after: datetime) -> datetime:
    start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    hours, minutes = sorted(spec.hours), sorted(spec.minutes)
    day_restricted = spec.days != frozenset(range(1, 32))
    weekday_restricted = spec.weekdays != frozenset(range(7))
    for offset in range(_CRON_SEARCH_DAYS):
        day = (start + timedelta(days=offset)).date()
        if day.month not in spec.months:
            continue
        if not _day_matches(
            day, spec, day_restricted=day_restricted, weekday_restricted=weekday_restricted
        ):
            continue
        for hour in hours:
            for minute in minutes:
                candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=start.tzinfo)
                if candidate >= start:
                    return candidate
    raise ValueError(f"调度表达式在 {_CRON_SEARCH_DAYS} 天内没有触发点：{spec}")


def next_run(schedule: str, after: datetime) -> datetime:
    """算出 ``after`` 之后（不含）的下一个触发时刻。

    间隔写法返回 ``after + 间隔``；cron 写法返回严格大于 ``after`` 的下一个匹配分钟。
    单调性（后者严格大于前者）是这个函数唯一的硬契约——:meth:`CronRunner.run_job`
    靠它推进 ``next_run_at``，一旦不单调，任务会在同一分钟里被反复触发。
    """
    parsed = parse_schedule(schedule)
    moment = _aware(after)
    if isinstance(parsed, Interval):
        return moment + timedelta(seconds=parsed.seconds)
    return _next_cron(parsed, moment)


# ── 任务模型 ────────────────────────────────────────────────────────────────


@dataclass
class Job:
    """一条定时任务。字段与 Hermes 的 job 结构对齐（省略了本轮不做的部分）。"""

    id: str
    schedule: str
    name: str = ""
    prompt: str = ""
    deliver: str = DELIVER_NONE
    #: 次数上限。0 = 不限次（Hermes 的 "forever"）。
    repeat: int = 0
    skills: list[str] = field(default_factory=list)
    script: str = ""
    #: True = 脚本模式：脚本就是任务，不走模型（Hermes 的 ``--no-agent``）。
    no_agent: bool = False
    workdir: str = ""
    model: str = ""
    enabled: bool = True
    created_at: str = ""
    last_run_at: str = ""
    next_run_at: str = ""
    run_count: int = 0
    #: **连续**失败次数（成功一次即清零），不是历史失败总数。
    #: 自动停用看的是连续次数：一个偶尔失败但总能恢复的任务不该被停掉。
    failures: int = 0

    @property
    def display_name(self) -> str:
        return self.name or self.prompt.strip()[:40] or self.script.strip()[:40] or self.id

    @property
    def exhausted(self) -> bool:
        """次数上限是否已用尽。"""
        return bool(self.repeat) and self.run_count >= self.repeat

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["skills"] = list(self.skills)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        """宽松反序列化：手改过的 JSON 里多字段 / 少字段 / 类型不对都不该炸。

        手改 jobs.json 是这个子系统的**特性**而不是意外，所以这里的容错是功能需求：
        把 ``"repeat": "3"`` 当成 3，把 ``"enabled": "false"`` 当成 False。
        """

        def _text(key: str, default: str = "") -> str:
            value = data.get(key)
            return default if value is None else str(value)

        def _int(key: str, default: int = 0) -> int:
            value = data.get(key)
            if value is None:
                return default
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        def _bool(key: str, default: bool = True) -> bool:
            value = data.get(key, default)
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() not in {"", "0", "false", "no", "off"}
            return default

        raw_skills = data.get("skills")
        if isinstance(raw_skills, list):
            skills = [str(item) for item in raw_skills if str(item).strip()]
        elif isinstance(raw_skills, str) and raw_skills.strip():
            skills = [item.strip() for item in raw_skills.split(",") if item.strip()]
        else:
            skills = []
        return cls(
            id=_text("id"),
            schedule=_text("schedule"),
            name=_text("name"),
            prompt=_text("prompt"),
            deliver=_text("deliver", DELIVER_NONE) or DELIVER_NONE,
            repeat=max(0, _int("repeat")),
            skills=skills,
            script=_text("script"),
            no_agent=_bool("no_agent", False),
            workdir=_text("workdir"),
            model=_text("model"),
            enabled=_bool("enabled", True),
            created_at=_text("created_at"),
            last_run_at=_text("last_run_at"),
            next_run_at=_text("next_run_at"),
            run_count=max(0, _int("run_count")),
            failures=max(0, _int("failures")),
        )


def is_due(job: Job, now: datetime | None = None) -> bool:
    """到点判定：以 ``next_run_at`` 为准，不用 ``schedule`` 现算。

    为什么钉住 next_run_at：外部调度器每分钟来一次 tick，若这里现算，"错过的那一轮"
    会在每次 tick 都判成到点，任务实际按分钟跑而不是按调度跑。所以推进权在
    :meth:`CronRunner.run_job` 手里，这里只做比较。
    """
    if not job.enabled or job.exhausted:
        return False
    moment = _aware(now or now_utc())
    next_at = from_iso(job.next_run_at)
    if next_at is not None:
        return next_at <= moment
    # 缺 next_run_at（手改/老数据）：从创建时间推一次。缺创建时间就按"现在就到期"，
    # 因为用户的意图明显是"这条任务该跑"。
    anchor = from_iso(job.created_at) or moment
    try:
        computed = next_run(job.schedule, anchor)
    except ValueError:
        return False
    if computed > moment:
        job.next_run_at = to_iso(computed)  # 顺手补上，tick 落盘后就不必再推算
        return False
    return True


# ── 落盘 ────────────────────────────────────────────────────────────────────


class RunningRegistry:
    """``<data_dir>/cron/running.json``：正在跑的任务登记簿。

    存在的唯一理由是**紧急停止**（MATRIX 11.6）。``pause`` 能挡住"下一轮不开始"，
    但已经开始跑的那一轮还在别的进程里——跨进程强杀（``taskkill`` / 信号）风险等级
    完全不同：杀错 PID、杀掉外部调度器、留下半写的状态文件。所以这里只做两件事：

    1. 登记"谁在跑"（``run_id`` / ``job_id`` / ``pid`` / 开始时刻）；
    2. 收到紧急停止时给每条登记**打标记**，执行方在下一个可中断点自己退。

    崩溃留下的残条（进程已死）不在这里清理：判活需要 ``(pid, host_start_time)``
    二元组，那套判据在 ``app/orphan_reaper.py`` 与 ``app/gateway.py`` 里已经有一份，
    这里再写一遍只会多出一份会漂移的实现。残条只会让 ``--status`` 多报一条，
    而多报一条比漏报安全。
    """

    def __init__(self, root: str | Path) -> None:
        self.path = Path(root) / RUNNING_FILENAME

    def load(self) -> list[dict[str, Any]]:
        """读登记簿；坏文件当空表（与 ``jobs.json`` 同一个容错口径）。"""
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        items = raw.get("running") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    def save(self, items: Iterable[dict[str, Any]]) -> None:
        _atomic_write(
            self.path,
            json.dumps({"running": list(items)}, ensure_ascii=False, indent=2),
        )

    def register(
        self,
        run_id: str,
        job_id: str,
        *,
        started_at: str = "",
        pid: int | None = None,
    ) -> dict[str, Any]:
        """登记一条正在跑的任务（同 ``run_id`` 重复登记只保留一条）。"""
        items = [item for item in self.load() if str(item.get("run_id") or "") != run_id]
        record = {
            "run_id": run_id,
            "job_id": job_id,
            "pid": os.getpid() if pid is None else int(pid),
            "started_at": started_at or to_iso(now_utc()),
            "cancel_requested": False,
            "cancel_reason": "",
        }
        items.append(record)
        self.save(items)
        return record

    def clear(self, run_id: str) -> bool:
        """跑完就注销。返回是否真的删掉了一条（没登记过返回 False，不报错）。"""
        items = self.load()
        remaining = [item for item in items if str(item.get("run_id") or "") != run_id]
        if len(remaining) == len(items):
            return False
        self.save(remaining)
        return True

    def mark_cancelled(self, *, reason: str = "") -> list[str]:
        """给**所有**正在跑的任务打停止标记（不强杀）。返回被标记的 ``run_id``。"""
        items = self.load()
        marked: list[str] = []
        for item in items:
            if item.get("cancel_requested"):
                marked.append(str(item.get("run_id") or ""))
                continue
            item["cancel_requested"] = True
            item["cancel_reason"] = reason
            marked.append(str(item.get("run_id") or ""))
        if items:
            self.save(items)
        return [item for item in marked if item]

    def clear_cancelled(self) -> int:
        """解除停止标记（``resume`` 时调用）。返回清掉的条数。"""
        items = self.load()
        changed = 0
        for item in items:
            if item.get("cancel_requested"):
                item["cancel_requested"] = False
                item["cancel_reason"] = ""
                changed += 1
        if changed:
            self.save(items)
        return changed

    def cancelled(self, run_id: str) -> bool:
        """这条运行是否被标记过紧急停止（执行体自己查，自己决定在哪退）。"""
        for item in self.load():
            if str(item.get("run_id") or "") == run_id:
                return bool(item.get("cancel_requested"))
        return False


class CronStore:
    """``<data_dir>/cron/`` 下的全部落盘：任务表、执行流水、事故状态、暂停哨兵。"""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.root = Path(data_dir) / "cron"

    @property
    def jobs_path(self) -> Path:
        return self.root / "jobs.json"

    @property
    def runs_path(self) -> Path:
        return self.root / "runs.jsonl"

    @property
    def incidents_path(self) -> Path:
        return self.root / "incidents.json"

    @property
    def paused_path(self) -> Path:
        return self.root / "paused"

    @property
    def running(self) -> RunningRegistry:
        """正在跑的任务登记簿（见 :class:`RunningRegistry`）。"""
        return RunningRegistry(self.root)

    # ── 通用读 ──
    def _read_raw(self, path: Path) -> Any:
        """读 JSON。缺失或损坏都返回 ``None``，损坏的那份改名留证。"""
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._quarantine(path)
            return None

    def _quarantine(self, path: Path) -> None:
        """坏文件改名而不是就地覆盖：用户手改错了一行，原始内容还值得留下。"""
        try:
            os.replace(path, path.with_name(f"{path.name}.corrupt-{now_utc():%Y%m%d%H%M%S}"))
        except OSError:
            pass

    # ── 任务表 ──
    def load_jobs(self) -> list[Job]:
        """读任务表；坏文件当空表（并把坏文件留证），不抛异常。

        为什么不抛：``orbit cron tick`` 由外部调度器每分钟调一次，一个手抖写坏的 JSON
        不该让它从此永久失败——那等于整个定时子系统静默死亡，且没人会注意到。
        """
        raw = self._read_raw(self.jobs_path)
        if isinstance(raw, dict):
            raw = raw.get("jobs")
        if not isinstance(raw, list):
            return []
        jobs: list[Job] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                job = Job.from_dict(item)
            except (TypeError, ValueError):
                continue
            if job.id:
                jobs.append(job)
        return jobs

    def save_jobs(self, jobs: Iterable[Job]) -> None:
        payload = {"version": 1, "jobs": [job.to_dict() for job in jobs]}
        _atomic_write(self.jobs_path, json.dumps(payload, ensure_ascii=False, indent=2))

    # ── 执行流水 ──
    def append_run(self, record: dict[str, Any]) -> None:
        """追加一行。**追加而非重写**：runs.jsonl 是流水账，进程被杀也要留下已发生的部分。"""
        self.root.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with self.runs_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def load_runs(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """按时间正序读流水。坏行跳过——一行坏了不该让整份历史读不出来。"""
        if not self.runs_path.exists():
            return []
        try:
            text = self.runs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        records: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                records.append(item)
        if limit is not None and limit > 0:
            return records[-limit:]
        return records

    # ── 事故状态 ──
    def load_incidents(self) -> dict[str, dict[str, Any]]:
        raw = self._read_raw(self.incidents_path)
        if not isinstance(raw, dict):
            return {}
        items = raw.get("incidents")
        if not isinstance(items, dict):
            return {}
        return {str(key): value for key, value in items.items() if isinstance(value, dict)}

    def save_incidents(self, incidents: dict[str, dict[str, Any]]) -> None:
        _atomic_write(
            self.incidents_path,
            json.dumps({"incidents": incidents}, ensure_ascii=False, indent=2),
        )

    # ── 全局暂停哨兵 ──
    def paused(self) -> bool:
        return self.paused_path.exists()

    def set_paused(self, value: bool, *, reason: str = "") -> None:
        """全局紧急停止。

        为什么用**哨兵文件**而不是配置字段：照 Hermes ``hermes pause`` 的语义，紧急停止
        要能被任何进程（含运维脚本、另一个终端）一行命令生效，且 tick 侧只需一次
        ``exists()`` 判定——配置字段要读配置才生效，配置读不出来时反而停不下来。
        """
        if not value:
            self.paused_path.unlink(missing_ok=True)
            return
        _atomic_write(
            self.paused_path,
            json.dumps({"paused_at": to_iso(now_utc()), "reason": reason}, ensure_ascii=False),
        )


def _find_job(jobs: Sequence[Job], job_id: str) -> Job | None:
    for job in jobs:
        if job.id == job_id:
            return job
    return None


def _new_job_id(existing: Sequence[Job]) -> str:
    """生成短 id。``cron-`` 前缀让它在日志里一眼可辨，撞了就重抽。"""
    used = {job.id for job in existing}
    while True:
        candidate = f"cron-{uuid.uuid4().hex[:8]}"
        if candidate not in used:
            return candidate


# ── 任务 CRUD ───────────────────────────────────────────────────────────────


def create_job(
    store: CronStore,
    *,
    schedule: str,
    prompt: str = "",
    name: str = "",
    deliver: str = DELIVER_NONE,
    repeat: int = 0,
    skills: Sequence[str] = (),
    script: str = "",
    no_agent: bool = False,
    workdir: str = "",
    model: str = "",
    enabled: bool = True,
    now: datetime | None = None,
) -> Job:
    """建任务。**调度与投递先校验再落盘**，坏输入不该留下半个任务。"""
    moment = _aware(now or now_utc())
    parse_schedule(schedule)  # 只为校验：算不出来就别建
    target = validate_deliver(deliver)
    skill_list = [str(item).strip() for item in skills if str(item).strip()]
    if no_agent and not script.strip():
        raise ValueError(
            "--no-agent 必须配 --script：脚本模式里脚本就是任务本身（不走模型，输出直接投递）。"
        )
    if not no_agent and not prompt.strip() and not skill_list:
        raise ValueError("任务要么给 prompt，要么给至少一个 --skill，否则每轮无事可做。")
    jobs = store.load_jobs()
    normalized_schedule = schedule.strip()
    job = Job(
        id=_new_job_id(jobs),
        schedule=normalized_schedule,
        name=name.strip(),
        prompt=prompt,
        deliver=target,
        repeat=max(0, int(repeat or 0)),
        skills=skill_list,
        script=script.strip(),
        no_agent=bool(no_agent),
        workdir=workdir.strip(),
        model=model.strip(),
        enabled=bool(enabled),
        created_at=to_iso(moment),
        next_run_at=to_iso(next_run(normalized_schedule, moment)),
    )
    jobs.append(job)
    store.save_jobs(jobs)
    return job


def update_job(
    store: CronStore,
    job_id: str,
    *,
    schedule: str | None = None,
    prompt: str | None = None,
    name: str | None = None,
    deliver: str | None = None,
    repeat: int | None = None,
    skills: Sequence[str] | None = None,
    add_skills: Sequence[str] = (),
    remove_skills: Sequence[str] = (),
    clear_skills: bool = False,
    script: str | None = None,
    no_agent: bool | None = None,
    workdir: str | None = None,
    model: str | None = None,
    now: datetime | None = None,
) -> Job | None:
    """改任务。只改显式给了值的字段（``None`` = 不动），改完重算 ``next_run_at``。

    为什么调度一变就重算 next_run_at：否则"把 30m 改成 1d"之后，任务仍会按旧的到点时间
    立刻触发一次，用户会以为改错了。
    """
    moment = _aware(now or now_utc())
    jobs = store.load_jobs()
    job = _find_job(jobs, job_id)
    if job is None:
        return None
    schedule_changed = False
    if schedule is not None and schedule.strip() != job.schedule:
        parse_schedule(schedule)
        job.schedule = schedule.strip()
        schedule_changed = True
    if prompt is not None:
        job.prompt = prompt
    if name is not None:
        job.name = name.strip()
    if deliver is not None:
        job.deliver = validate_deliver(deliver)
    if repeat is not None:
        job.repeat = max(0, int(repeat))
    if clear_skills:
        job.skills = []
    if skills is not None:
        job.skills = [str(item).strip() for item in skills if str(item).strip()]
    if add_skills:
        for item in add_skills:
            text = str(item).strip()
            if text and text not in job.skills:
                job.skills.append(text)
    if remove_skills:
        dropping = {str(item).strip() for item in remove_skills}
        job.skills = [item for item in job.skills if item not in dropping]
    if script is not None:
        job.script = script.strip()
    if no_agent is not None:
        job.no_agent = bool(no_agent)
    if workdir is not None:
        job.workdir = workdir.strip()
    if model is not None:
        job.model = model.strip()
    if job.no_agent and not job.script:
        raise ValueError("--no-agent 需要 --script（或任务上已有脚本），否则每轮没有可执行的东西。")
    if not job.no_agent and not job.prompt.strip() and not job.skills:
        raise ValueError("任务要么有 prompt，要么有至少一个 skill，否则每轮无事可做。")
    if schedule_changed or from_iso(job.next_run_at) is None:
        job.next_run_at = to_iso(next_run(job.schedule, moment))
    store.save_jobs(jobs)
    return job


def remove_job(store: CronStore, job_id: str) -> bool:
    """删任务。找不到返回 False（让 CLI 报错，而不是假装删掉了）。"""
    jobs = store.load_jobs()
    remaining = [job for job in jobs if job.id != job_id]
    if len(remaining) == len(jobs):
        return False
    store.save_jobs(remaining)
    return True


# ── 执行 ────────────────────────────────────────────────────────────────────

#: 执行器签名：拿一条任务，返回 ``{"ok": bool, "output": str, "error": str}``。
#: 允许额外的 ``unavailable=True``——表示"执行器本身没接线"，不算任务失败（见 run_job）。
Runner = Callable[[Job], Awaitable[dict[str, Any]]]


@dataclass
class RunResult:
    """一轮 tick 里单个任务的执行结果，也是 ``runs.jsonl`` 一行的来源。"""

    job_id: str
    name: str
    ok: bool
    started_at: str
    finished_at: str
    duration_ms: int
    #: 本次运行的标识（``cron-xxxxxxxx-r1`` 形态）。平面事件的幂等键靠它，
    #: 也是"同一条任务的第几次运行"在日志里的锚点。
    run_id: str = ""
    output: str = ""
    error: str = ""
    delivered: str = ""
    #: 本轮因连续失败 / 次数用尽被自动停用。
    disabled: bool = False
    #: 执行器不可用（未接线），不是任务本身失败。
    unavailable: bool = False
    #: 运行期间收到的紧急停止标记（**只标记，不强杀**——见 :class:`RunningRegistry`）。
    cancel_requested: bool = False
    note: str = ""
    #: 运行时生效的权限档（读的是与交互同一份 ``settings.json``，MATRIX 11.5）。
    #: 记在这里是为了回答"这个定时任务当时有没有权限写工作目录外"。
    file_scope: str = ""
    approval_mode: str = ""

    def as_record(self) -> dict[str, Any]:
        return {
            "ts": self.finished_at,
            "job_id": self.job_id,
            "run_id": self.run_id,
            "name": self.name,
            "ok": self.ok,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "output": self.output[:OUTPUT_KEEP],
            "error": self.error,
            "delivered": self.delivered,
            "disabled": self.disabled,
            "unavailable": self.unavailable,
            "cancel_requested": self.cancel_requested,
            "note": self.note,
            "file_scope": self.file_scope,
            "approval_mode": self.approval_mode,
        }


class CronRunner:
    """挑出到点任务、逐个执行、记账。执行器由构造参数注入（测试不必真跑 agent）。"""

    def __init__(
        self,
        data_dir: str | Path,
        runner: Runner,
        *,
        max_failures: int = MAX_CONSECUTIVE_FAILURES,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.store = CronStore(data_dir)
        self.runner = runner
        self.max_failures = max(1, int(max_failures))
        #: 正在跑的任务登记簿（紧急停止只能标记它，见 :class:`RunningRegistry`）。
        self.running = RunningRegistry(self.store.root)

    # ── 查询 ──
    def jobs(self) -> list[Job]:
        return self.store.load_jobs()

    def get(self, job_id: str) -> Job | None:
        return _find_job(self.store.load_jobs(), job_id)

    def due_jobs(self, now: datetime | None = None) -> list[Job]:
        moment = _aware(now or now_utc())
        return [job for job in self.store.load_jobs() if is_due(job, moment)]

    # ── 一轮 ──
    async def tick(self, now: datetime | None = None) -> list[RunResult]:
        """跑一轮：挑出到点的任务、逐个执行、最后一次性落盘任务表。

        暂停（哨兵文件）优先于一切：紧急停止的语义就是"这一轮什么都别做"。
        """
        moment = _aware(now or now_utc())
        if self.store.paused():
            return []
        jobs = self.store.load_jobs()
        results: list[RunResult] = []
        for job in jobs:
            if not is_due(job, moment):
                continue
            results.append(await self.run_job(job, moment))
        # 整轮一次落盘：单条失败已在 run_job 里吃掉，这里不会因为某条任务而丢别人的状态。
        self.store.save_jobs(jobs)
        return results

    async def run_job(
        self,
        job: Job,
        now: datetime | None = None,
        *,
        run_id: str = "",
    ) -> RunResult:
        """执行一条任务并记账。

        **所有异常都在这里被吃掉**：一轮 tick 不能因为一条坏任务而整轮崩掉——外部调度器
        只看退出码，崩一次就可能再也不调了，其它健康任务跟着一起停。

        ``run_id`` 可注入：测试要能"用同一个 run_id 跑两次"来验证平面事件幂等。
        不给就现生成（``<job_id>-<8 位随机>``）。
        """
        started = _aware(now or now_utc())
        began = time.monotonic()
        ident = run_id.strip() or f"{job.id}-{uuid.uuid4().hex[:8]}"
        # 权限档在**开始那一刻**读一次并全程沿用：中途用户在对话页改了权限，
        # 也不该让这一轮"前半段在 workspace、后半段在 machine"。
        perms = read_permissions(self.data_dir)
        plane_errors: list[str] = []
        self._plane_write(
            job,
            ident,
            KIND_CRON_RUN_STARTED,
            started_at=to_iso(started),
            permissions=perms,
            errors=plane_errors,
        )
        self.running.register(ident, job.id, started_at=to_iso(started))
        ok, output, error, unavailable = False, "", "", False
        try:
            payload = await self.runner(job)
        except asyncio.CancelledError:
            self.running.clear(ident)
            raise  # 取消是调用方的意图，不该被记成任务失败
        except Exception as exc:  # noqa: BLE001 —— 执行器的任何异常都只算这一条失败
            error = f"{type(exc).__name__}: {exc}"
        else:
            data = payload if isinstance(payload, dict) else {}
            ok = bool(data.get("ok", True))
            output = str(data.get("output") or "")
            error = str(data.get("error") or "")
            unavailable = bool(data.get("unavailable", False))

        delivered, deliver_error = self._deliver(job, output, run_id=ident)
        if deliver_error:
            # 投递失败算任务失败："跑了但没人收到"在运维上等于没跑。
            ok = False
            error = f"{error}；{deliver_error}" if error else deliver_error

        # 运行期间有没有人喊停：只读标记，不改变结果——真正的"停"由执行体自己决定。
        cancel_requested = self.running.cancelled(ident)
        self.running.clear(ident)

        # 注入时钟优先：测试要能确定性地推进"下一次触发"，混进真实时间会让断言不可复现。
        # 真实跑（now=None）时才用当前时间，保证 finished 不早于 started。
        finished = started if now is not None else max(started, now_utc())
        duration_ms = int((time.monotonic() - began) * 1000)

        note = ""
        disabled = False
        job.last_run_at = to_iso(started)
        job.run_count += 1
        if ok:
            job.failures = 0
        elif unavailable:
            # 执行器没接线（例如 agent 轮次尚未接入）不是任务的错：照实记进流水，
            # 但**不计连续失败**——否则一次配置缺项会在三分钟里把用户的任务自动停用掉。
            note = error or "执行器不可用"
        else:
            job.failures += 1
            if job.failures >= self.max_failures:
                job.enabled = False
                disabled = True
                note = (
                    f"连续失败 {job.failures} 次，已自动停用；"
                    f"修好后用 `orbit cron resume {job.id}` 重新启用"
                )

        if cancel_requested:
            note = (note + "；" if note else "") + "运行期间收到紧急停止标记（只标记，未强杀）"

        if job.exhausted:
            job.enabled = False
            disabled = True
            job.next_run_at = ""
            note = note or f"已达重复上限 {job.repeat} 次，已停用"
        else:
            try:
                # 从**完成时刻**推下一次：长任务不该把自己的下一个槽位挤掉。
                job.next_run_at = to_iso(next_run(job.schedule, _aware(finished)))
            except ValueError as exc:
                job.enabled = False
                disabled = True
                job.next_run_at = ""
                note = note or f"调度表达式无法计算下一次触发：{exc}"

        if plane_errors:
            note = (note + "；" if note else "") + f"平面事件写入失败：{'、'.join(plane_errors)}"

        result = RunResult(
            job_id=job.id,
            name=job.display_name,
            ok=ok,
            started_at=to_iso(started),
            finished_at=to_iso(finished),
            duration_ms=duration_ms,
            run_id=ident,
            output=output,
            error=error,
            delivered=delivered,
            disabled=disabled,
            unavailable=unavailable,
            cancel_requested=cancel_requested,
            note=note,
            file_scope=perms["file_scope"],
            approval_mode=perms["approval_mode"],
        )
        # 结束事件在流水落盘**之前**写：即使 runs.jsonl 写失败（磁盘满等），
        # 平面上也留下了"这一轮结束了"的痕迹，不会永远停在 started 上。
        self._plane_write(
            job,
            ident,
            KIND_CRON_RUN_FINISHED,
            started_at=to_iso(started),
            finished_at=result.finished_at,
            duration_ms=duration_ms,
            ok=ok,
            unavailable=unavailable,
            delivered=delivered,
            permissions=perms,
            errors=plane_errors,
        )
        if not ok and not unavailable:
            # 失败**额外**再写一条：审计要能直接按 kind 捞失败，不必逐条解析 finished。
            # ``unavailable`` 不算失败（执行器没接线不是任务的错），所以不写 failed——
            # 否则"配置缺项"会在平面里堆成一串假的失败。
            self._plane_write(
                job,
                ident,
                KIND_CRON_RUN_FAILED,
                started_at=to_iso(started),
                finished_at=result.finished_at,
                error=error or "（执行器未给原因）",
                unavailable=unavailable,
                permissions=perms,
                errors=plane_errors,
            )
        self.store.append_run(result.as_record())
        return result

    # ── 平面（写侧统一：MATRIX 10.1） ──
    def _plane_write(
        self,
        job: Job,
        run_id: str,
        kind: str,
        *,
        permissions: dict[str, str],
        errors: list[str],
        **fields: Any,
    ) -> None:
        """写一条 ``cron.run.*`` 平面事件，会话键 ``cron:<job_id>``。

        幂等键 = ``cron:<job_id>:<run_id>:<阶段>``：同一个 ``run_id`` 重复调用
        （重放、测试里连跑两次）不会在平面上多出一行。

        写失败只收集到 ``errors`` 里、由调用方拼进 ``note``，**不抛**：平面是审计视图，
        它坏了不该让一条真正跑成功的定时任务变成失败。
        """
        phase = kind.rsplit(".", 1)[-1]
        event: dict[str, Any] = {
            "kind": kind,
            "session_id": plane_session_key(SOURCE_KEY_CRON, job.id),
            "job_id": job.id,
            "job_name": job.display_name,
            "run_id": run_id,
            "schedule": job.schedule,
            "file_scope": permissions.get("file_scope", ""),
            "approval_mode": permissions.get("approval_mode", ""),
            "permissions_source": permissions.get("source", ""),
        }
        event.update(fields)
        try:
            SessionPlane(self.data_dir).append_once(
                event, message_key=f"{SOURCE_KEY_CRON}:{job.id}:{run_id}:{phase}"
            )
        except Exception as exc:  # noqa: BLE001 —— 平面坏了不该影响任务结果
            errors.append(f"{phase}: {type(exc).__name__}: {exc}")

    # ── 投递 ──
    def _deliver(self, job: Job, output: str, *, run_id: str = "") -> tuple[str, str]:
        """投递输出，返回 ``(落点, 错误)``。空输出不落盘（照 Hermes "empty stdout = silent"）。

        非法投递目标（手改 ``jobs.json`` 写坏了）**记成这一条失败**，不往上抛：
        抛出去会让整轮 tick 崩掉，而一轮里其它健康任务不该被一条手写坏的目标拖死。
        """
        try:
            mode, target, channel = parse_deliver(job.deliver)
        except ValueError as exc:
            return "", str(exc)
        if mode == DELIVER_NONE:
            return "", ""
        if not output.strip():
            return "", ""
        if mode == "file":
            path = Path(target).expanduser()
            try:
                _atomic_write(path, output if output.endswith("\n") else output + "\n")
            except OSError as exc:
                return "", f"投递到 {path} 失败：{exc}"
            return str(path), ""
        # 平台投递：走 gateway 的 deliver()，结论记进投递账本（app/delivery_ledger.py）。
        landing = f"{target}:{channel or 'home'}"
        try:
            result = deliver_to(
                target,
                channel,
                output,
                data_dir=self.data_dir,
                message_key=f"cron:{job.id}:{run_id}" if run_id else "",
            )
        except Exception as exc:  # noqa: BLE001 —— 投递层的异常也只算这一条失败
            return "", f"投递到 {landing} 失败：{type(exc).__name__}: {exc}"
        if result.ok:
            return landing, ""
        return "", f"投递到 {landing} 失败：{result.error or '未给原因'}"

    # ── 暂停 / 恢复 ──
    def pause(self, job_id: str) -> bool:
        jobs = self.store.load_jobs()
        job = _find_job(jobs, job_id)
        if job is None:
            return False
        job.enabled = False
        self.store.save_jobs(jobs)
        return True

    def resume(
        self,
        job_id: str,
        *,
        at: datetime | None = None,
        run_now: bool = False,
        now: datetime | None = None,
    ) -> Job | None:
        """恢复任务。**顺手清零连续失败计数**：不清零的话，刚恢复的任务会被下一次失败
        立刻再停用（failures 已经是 3），用户会以为恢复命令没生效。"""
        moment = _aware(now or now_utc())
        jobs = self.store.load_jobs()
        job = _find_job(jobs, job_id)
        if job is None:
            return None
        job.enabled = True
        job.failures = 0
        if run_now:
            job.next_run_at = to_iso(moment)
        elif at is not None:
            job.next_run_at = to_iso(at)
        else:
            try:
                job.next_run_at = to_iso(next_run(job.schedule, moment))
            except ValueError:
                job.next_run_at = ""
        self.store.save_jobs(jobs)
        return job

    def pause_all(self, *, reason: str = "") -> CascadeStatus:
        """全局紧急停止：**级联**（cron + gateway + 正在跑的任务）。

        委托给模块级 :func:`pause_all`：命令行的 ``cron pause all`` 与
        ``orbit pause`` 必须停掉同一批东西，两处各写一份级联迟早只停一半。
        """
        return pause_all(self.data_dir, reason=reason)

    def resume_all(self) -> CascadeStatus:
        """解除全局紧急停止（级联）。"""
        return resume_all(self.data_dir)


# ── 紧急停止级联（MATRIX 11.6） ──────────────────────────────────────────────


@dataclass(frozen=True)
class CascadeStatus:
    """一次级联停止/恢复的现场：每个被管的子系统各一行。

    ``paused`` 是**总判定**：任一子系统仍停着就算"停着"。为什么不用"全部停着"：
    漏停一个子系统（比如 gateway 还在发消息）比多报一次"还停着"危险得多。
    """

    paused: bool
    reason: str = ""
    subsystems: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "paused": self.paused,
            "reason": self.reason,
            "subsystems": {name: dict(state) for name, state in self.subsystems.items()},
        }

    def paused_names(self) -> list[str]:
        """当前停着的子系统名（``orbit pause --status`` 直接列它）。"""
        return [name for name, state in self.subsystems.items() if state.get("paused")]


def _gateway_pause_paths(data_dir: Path) -> tuple[Path, Any, Any]:
    """拿到 gateway 哨兵路径与两个写盘函数（**不建 GatewayService**）。

    为什么不实例化服务：构造 ``GatewayService`` 会建一整套平台适配器（含 httpx 客户端）
    并探测本进程身份——对"写一个哨兵文件"来说完全是浪费，而且会把 httpx 拖进
    ``orbit pause`` 这种本该零依赖的命令。``GatewayPaths`` 是个纯路径对象，够用了。
    """
    from app.gateway import GatewayPaths, write_json

    paths = GatewayPaths(Path(data_dir))
    return paths.paused, write_json, paths


def pause_all(data_dir: str | Path, *, reason: str = "") -> CascadeStatus:
    """紧急停止级联：cron 哨兵 + gateway 哨兵 + 正在跑的任务（只标记）。

    三个子系统，为什么都得管：

    - **cron**：挡"下一轮 tick 不开始"；
    - **gateway**：挡"新网关轮次与出站投递"——只停 cron 等于停了半台机器；
    - **正在跑的任务**：已经开始的那一轮在**别的进程**里，只能打标记让它自己退。
      跨进程强杀（``taskkill`` / 信号）会引入"杀错 PID / 杀掉外部调度器"的风险，
      那比"多跑完这一轮"贵得多。
    """
    root = Path(data_dir)
    store = CronStore(root)
    store.set_paused(True, reason=reason)
    running = store.running
    marked = running.mark_cancelled(reason=reason)

    paused_path, write_json, _paths = _gateway_pause_paths(root)
    write_json(
        paused_path,
        {
            "paused_at": to_iso(now_utc()),
            "reason": reason,
            "source": "orbit pause（级联）",
        },
    )
    return cascade_status(root, reason=reason, marked=marked)


def resume_all(data_dir: str | Path) -> CascadeStatus:
    """解除级联停止：清 cron 哨兵、清 gateway 哨兵、解除正在跑的任务的标记。"""
    root = Path(data_dir)
    store = CronStore(root)
    store.set_paused(False)
    cleared = store.running.clear_cancelled()
    paused_path, _write_json, _paths = _gateway_pause_paths(root)
    try:
        paused_path.unlink(missing_ok=True)
    except OSError:
        pass
    status = cascade_status(root)
    # 把"本次解除了几条标记"留在现场里，便于人确认 resume 真的做了事。
    status.subsystems["running_jobs"]["unmarked"] = cleared
    return status


def cascade_status(
    data_dir: str | Path,
    *,
    reason: str = "",
    marked: list[str] | None = None,
) -> CascadeStatus:
    """读三个子系统的当前状态（不改变任何东西）。

    ``--status`` 的价值在于"我到底停了什么"：紧急停止最怕的不是没停，而是
    **以为停了、其实只停了一半**。所以这里逐个报，而不是只给一个布尔。
    """
    root = Path(data_dir)
    store = CronStore(root)
    cron_paused = store.paused()
    paused_path, _write_json, _paths = _gateway_pause_paths(root)
    gateway_paused = paused_path.exists()
    running_items = store.running.load()
    running_marked = [item for item in running_items if item.get("cancel_requested")]
    subsystems: dict[str, dict[str, Any]] = {
        "cron": {
            "paused": cron_paused,
            "path": str(store.paused_path),
            "detail": "tick 不执行任何任务" if cron_paused else "正常调度",
        },
        "gateway": {
            "paused": gateway_paused,
            "path": str(paused_path),
            "detail": "新轮次与出站投递被挡住" if gateway_paused else "正常收发",
        },
        "running_jobs": {
            "paused": bool(running_marked),
            "path": str(store.running.path),
            "detail": (
                f"{len(running_marked)} 条已标记停止（只标记，不强杀）"
                if running_marked
                else f"{len(running_items)} 条在跑，未标记"
            ),
            "running": len(running_items),
            "marked": [str(item.get("run_id") or "") for item in running_marked],
        },
    }
    if marked is not None:
        subsystems["running_jobs"]["marked"] = marked
    paused = any(state.get("paused") for state in subsystems.values())
    return CascadeStatus(paused=paused, reason=reason, subsystems=subsystems)


# ── 事故（incidents）─────────────────────────────────────────────────────────


@dataclass
class Incident:
    """一条事故 = 某个任务当前这段连续失败。三态：detected / alerted / closed。"""

    id: str
    job_id: str
    job_name: str
    state: str = "detected"
    failures: int = 0
    first_failure_at: str = ""
    last_failure_at: str = ""
    last_error: str = ""
    detected_at: str = ""
    alerted_at: str = ""
    closed_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalize_state(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in INCIDENT_STATES else "detected"


def _incident_from_dict(data: dict[str, Any]) -> Incident:
    raw_failures = data.get("failures")
    try:
        failures = max(0, int(raw_failures or 0))
    except (TypeError, ValueError):
        failures = 0
    return Incident(
        id=str(data.get("id") or ""),
        job_id=str(data.get("job_id") or ""),
        job_name=str(data.get("job_name") or data.get("job_id") or ""),
        state=_normalize_state(data.get("state")),
        failures=failures,
        first_failure_at=str(data.get("first_failure_at") or ""),
        last_failure_at=str(data.get("last_failure_at") or ""),
        last_error=str(data.get("last_error") or ""),
        detected_at=str(data.get("detected_at") or ""),
        alerted_at=str(data.get("alerted_at") or ""),
        closed_at=str(data.get("closed_at") or ""),
    )


def _failure_streaks(
    runs: Iterable[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, bool]]:
    """按任务切出"当前这段连续失败"，并给出每个任务最近一次是否成功。

    为什么按"连续"：事故的生命周期是"从开始坏到现在还坏着"，中间成功一次就该结案。
    按累计失败数聚合的话，一条三天前就修好的任务会永远挂在事故列表里，列表很快就没人看了。
    """
    streaks: dict[str, list[dict[str, Any]]] = {}
    last_ok: dict[str, bool] = {}
    for record in runs:
        job_id = str(record.get("job_id") or "")
        if not job_id or record.get("unavailable"):
            continue
        if record.get("ok"):
            last_ok[job_id] = True
            streaks[job_id] = []  # 成功一次：这段连续失败结束
        else:
            last_ok[job_id] = False
            streaks.setdefault(job_id, []).append(record)
    return ({job_id: items for job_id, items in streaks.items() if items}, last_ok)


def sync_incidents(store: CronStore, *, now: datetime | None = None) -> list[Incident]:
    """从 ``runs.jsonl`` 聚合事故，并把三态落到 ``incidents.json``。

    为什么状态要单独存：流水只记录"发生过什么"，而"这条事故已被人看到/已处理"是人的
    决定，只能存在别处。合并规则刻意保守——**自动**只会把事故推进到 closed（任务后来
    成功了），``alerted`` 是人工 ack 的结果，绝不自动改回去。
    """
    moment = _aware(now or now_utc())
    stored = store.load_incidents()
    streaks, last_ok = _failure_streaks(store.load_runs())

    incidents: list[Incident] = []
    seen: set[str] = set()
    for job_id, failures in streaks.items():
        first = str(failures[0].get("ts") or "")
        incident_id = f"{job_id}@{first}"
        previous = stored.get(incident_id) or {}
        incident = Incident(
            id=incident_id,
            job_id=job_id,
            job_name=str(failures[-1].get("name") or job_id),
            state=_normalize_state(previous.get("state")),
            failures=len(failures),
            first_failure_at=first,
            last_failure_at=str(failures[-1].get("ts") or ""),
            last_error=str(failures[-1].get("error") or "")[:500],
            detected_at=str(previous.get("detected_at") or first),
            alerted_at=str(previous.get("alerted_at") or ""),
            closed_at=str(previous.get("closed_at") or ""),
        )
        incidents.append(incident)
        seen.add(incident_id)

    # 已恢复的任务：把它的旧事故自动结案，但**保留记录**——"曾经坏过"是运维信息，
    # 删掉等于让下次同类故障看起来像第一次发生。
    for incident_id, previous in stored.items():
        if incident_id in seen:
            continue
        incident = _incident_from_dict(previous)
        if incident.state != "closed" and last_ok.get(incident.job_id, False):
            incident.state = "closed"
            incident.closed_at = incident.closed_at or to_iso(moment)
        incidents.append(incident)

    incidents.sort(key=lambda item: item.last_failure_at, reverse=True)
    store.save_incidents({item.id: item.as_dict() for item in incidents})
    return incidents


def acknowledge_incident(
    store: CronStore, incident_id: str, *, now: datetime | None = None
) -> Incident | None:
    """人工 ack：``detected`` → ``alerted``。找不到返回 ``None``（由 CLI 报错）。"""
    moment = _aware(now or now_utc())
    incidents = sync_incidents(store, now=moment)
    target = next((item for item in incidents if item.id == incident_id), None)
    if target is None:
        return None
    if target.state == "closed":
        return target  # 已结案的不改状态：结案是事实，不是可以回退的标记
    target.state = "alerted"
    target.alerted_at = target.alerted_at or to_iso(moment)
    stored = store.load_incidents()
    stored[target.id] = target.as_dict()
    store.save_incidents(stored)
    return target
