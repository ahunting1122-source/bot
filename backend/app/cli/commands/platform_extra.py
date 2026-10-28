"""``orbit peer`` 与 ``orbit gateway`` 的剩余子命令（MATRIX 12.9 / 12.10）。

接线方式照 ``_table.py`` 的约定：**表不动**，本模块只声明"这些路径我接了"
（:func:`app.cli.commands._table.register_handler`）。表里 ``peer`` 有 7 条、``gateway``
有 11 条，另立同名 Command 会把剩下没实现的整片挤掉，``--help`` 里就再也看不见它们。

本模块补的 9 条，按**能不能真的做**分两档——这一档划分是本文件最重要的信息：

| 路径 | 状态 | 为什么 |
|---|---|---|
| ``peer dm`` | 真实现（发送侧） | 一次 HTTP POST + Bearer，形状自洽、可离线用 ``httpx.MockTransport`` 验证 |
| ``gateway run`` | 真实现 | 前台起 dispatcher（拿锁 + 四个状态文件），Ctrl+C / ``orbit pause`` 退出 |
| ``gateway list`` | 部分 | 能列出本机网关实例，但 Orbit 是**单数据目录**，没有 Hermes 的 profile 多实例 |
| ``gateway migrate-legacy`` | 部分 | 能扫出遗留单元文件并报告；**删除系统单元文件未实现**（只写 data_dir） |
| ``peer run`` / ``peer stop`` | 未实现 | 异步 run 需要远端 ``api_server`` 平台的 run 账本与查询/停止端点，Orbit 两端都没有 |
| ``gateway enroll`` | 未实现 | 中继（relay）注册要可达的 connector 服务端 + 令牌签发，本机无网络也没有该服务 |
| ``gateway install`` / ``uninstall`` | 未实现 | systemd / launchd / Windows 服务安装是**写系统级单元**，超出 Orbit 的写入范围 |

**"未实现"一律 ``CliError(..., code=3)``**：退出码 3 是本仓库约定的"命令面在、功能不在"
（``_table._pending`` 用的也是 3），脚本据此区分"失败重试"与"这条根本没做"。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx

from app import gateway as gw
from app.cli.commands import _table
from app.cli.commands._table import register_handler
from app.cli.context import CliError, Context
from app.cli.spec import REGISTRY
from app.platforms import IMPLEMENTED_PLATFORMS, PLATFORMS

#: 对等节点客户端用的 transport。**必须可替换**：本机无网络，测试用
#: ``httpx.MockTransport`` 造成功/失败两种响应，否则这条命令在离线环境里
#: 只能"看着像实现了"（一次都没真跑过）。
PEER_TRANSPORT: httpx.BaseTransport | None = None

#: 对等节点请求的超时（秒）。比 webhook 的 5s 长：对端可能要跑一轮 agent。
PEER_TIMEOUT = 30.0

#: ``peer dm`` 的端点路径。**这是 Orbit 自己的约定**——Hermes 的 peer dm 走的是
#: 对端 ``api_server`` 平台，那个平台的端点形状在调研里没有给出（只有一句"必须运行
#: api_server 平台"），所以照抄不了，只能定一个自洽的形状并写在这里。
PEER_DM_PATH = "/peer/dm"

#: ``gateway run`` 的有界运行开关：给离线验证 / CI 用，跑 n 个 tick 就退出。
RUN_TICKS_ENV = "ORBIT_GATEWAY_RUN_TICKS"

#: 一个 tick 的间隔（秒）。前台运行时用真实节奏；有界运行时置 0，免得测试白等。
RUN_TICK_SECONDS = 1.0

#: 遗留服务单元文件（Hermes 旧版可能留在这些位置）。**只扫描，不删除**。
LEGACY_UNIT_PATHS: tuple[str, ...] = (
    "~/.config/systemd/user/hermes.service",
    "~/.config/systemd/user/hermes-gateway.service",
    "/etc/systemd/system/hermes.service",
    "/etc/systemd/system/hermes-gateway.service",
    "~/Library/LaunchAgents/com.hermes.gateway.plist",
    "~/Library/LaunchAgents/com.nousresearch.hermes.plist",
)


# ── 公共小工具 ──────────────────────────────────────────────────────────────


def _subcommand_name(args) -> str:
    """当前子命令名。用 ``args._command`` 而不是 argparse 的 ``_<parent>_command`` dest。"""
    command = getattr(args, "_command", None)
    return str(getattr(command, "name", "") or "")


def _text(args, name: str, default: str = "") -> str:
    value = getattr(args, name, None)
    return default if value is None else str(value)


def _flag(args, name: str) -> bool:
    return bool(getattr(args, name, False))


def _peers(context: Context) -> gw.PeerRegistry:
    return gw.PeerRegistry(context.data_dir / gw.GATEWAY_DIRNAME / gw.PEERS_FILENAME)


def _split_peer(target: str) -> tuple[str, str]:
    """``<peer>[/<agent>]``：``/`` 之后是多路复用 peer 上的具名 profile。"""
    text = str(target or "").strip()
    name, _, agent = text.partition("/")
    return name.strip().lower(), agent.strip()


def _unimplemented(message: str) -> CliError:
    """统一的"命令面在、功能不在"报错（退出码 3）。"""
    return CliError(message, code=3)


# ── peer dm ─────────────────────────────────────────────────────────────────


def _read_message(args) -> str:
    """正文：位置参数优先，省略则读 stdin（照 Hermes 的 ``[message]`` 语义）。"""
    text = _text(args, "message")
    if text.strip():
        return text
    import sys

    if sys.stdin is not None and not sys.stdin.isatty():
        piped = sys.stdin.read()
        if piped.strip():
            return piped
    raise CliError(
        "peer dm 需要消息正文：`orbit peer dm <peer>[/<agent>] \"...\"`，或从 stdin 管道喂入",
        code=2,
    )


def run_peer_dm(context: Context, args) -> int:
    """``peer dm <peer>[/<agent>] [message]``：同步发一条并打印对端回复。

    形状：``POST <peer_url>/peer/dm``，``Authorization: Bearer <key>``，
    body ``{"agent": <具名 profile 或空>, "message": <正文>}``。

    **诚实边界**：Orbit 本轮只实现了**发送侧**。对端要有 ``/peer/dm`` 这个端点才有回复，
    而 Orbit 自己没有 api_server 平台（没有 HTTP 入站服务），所以"两台 Orbit 互相 dm"
    暂时不成立。发送失败时如实报"连不上/HTTP 4xx/5xx"，不假装成功。
    """
    name, agent = _split_peer(_text(args, "target"))
    if not name:
        raise CliError("用法：orbit peer dm <peer>[/<agent>] [message]", code=2)
    peers = _peers(context).load()
    record = peers.get(name)
    if record is None:
        raise CliError(
            f"没有这个对等节点：{name}（先 `orbit peer add {name} --url http://host:port`）",
            code=1,
        )
    base = str(record.get("url", "")).rstrip("/")
    if not base:
        raise CliError(f"对等节点 {name} 没有登记 URL（重新 `peer add`）", code=1)
    message = _read_message(args)
    url = base + PEER_DM_PATH
    key = str(record.get("key", ""))
    headers = {"User-Agent": "orbit-gateway/1", "Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {"agent": agent, "message": message}
    try:
        with httpx.Client(transport=PEER_TRANSPORT, timeout=PEER_TIMEOUT) as client:
            response = client.post(url, headers=headers, json=body)
    except httpx.TimeoutException as exc:
        raise CliError(
            f"对等节点 {name} 超时（{PEER_TIMEOUT:.0f}s）：{exc!r}。"
            "对端要有 /peer/dm 端点（Orbit 本轮只实现了发送侧）",
            code=1,
        ) from exc
    except httpx.HTTPError as exc:
        raise CliError(
            f"连不上对等节点 {name}（{url}）：{exc!r}。"
            "检查 `orbit peer list` 里的 URL，以及对端是否在跑带 /peer/dm 的服务",
            code=1,
        ) from exc
    if response.status_code >= 400:
        raise CliError(
            f"对等节点 {name} 返回 HTTP {response.status_code}：{response.text[:200]}",
            code=1,
        )
    reply = ""
    payload: dict[str, Any] = {}
    try:
        parsed = response.json()
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        payload = parsed
        reply = str(parsed.get("reply") or parsed.get("text") or "")
    else:
        reply = response.text
    context.out(f"→ {name}{'/' + agent if agent else ''}")
    context.out(reply or "（对端返回 200，但没有可读的回复正文）")
    context.emit(
        {
            "ok": True,
            "peer": name,
            "agent": agent,
            "status": response.status_code,
            "reply": reply,
            "raw": payload,
        }
    )
    return 0


# ── peer run / stop（未实现）────────────────────────────────────────────────


def run_peer_run(context: Context, args) -> int:
    """``peer run``：在对等节点上起一个**异步长任务**并立刻返回 run id。

    未实现，原因不是"没时间写"：它要求对端
    ① 有一个 ``api_server`` 风格的入站服务，② 有一份**可查询的 run 账本**（
    ``peer status`` 要按 run id 读状态与最终输出），③ 有"停掉单个 run 而不影响其它 turn"
    的控制端点。Orbit 三样都没有：本轮的平台适配器只做出站文本，gateway 也不监听端口。
    只发出去一半（发得出去、查不回来）比不发更坏——用户会拿到一个永远查不到的 run id。
    """
    name, agent = _split_peer(_text(args, "target"))
    if not name:
        raise CliError("用法：orbit peer run <peer>[/<agent>] [message]", code=2)
    if _peers(context).load().get(name) is None:
        raise CliError(
            f"没有这个对等节点：{name}（先 `orbit peer add {name} --url http://host:port`）",
            code=1,
        )
    raise _unimplemented(
        f"peer run 未实现：{name}{'/' + agent if agent else ''} 的异步任务需要远端有 "
        "api_server 平台的 run 账本 + 查询/停止端点，Orbit 本轮两端都没有（适配器只做出站文本，"
        "gateway 不监听端口）。同一原因也让 `peer status` 报未实现——"
        "只发出去、查不回来比不发更坏。登记信息本身是好的（`peer list` 可见）。"
    )


def run_peer_stop(context: Context, args) -> int:
    """``peer stop <peer>[/<agent>] <run_id>``：停掉单个异步 run。"""
    name, agent = _split_peer(_text(args, "target"))
    run_id = _text(args, "run_id").strip()
    if not name or not run_id:
        raise CliError("用法：orbit peer stop <peer>[/<agent>] <run_id>", code=2)
    if _peers(context).load().get(name) is None:
        raise CliError(
            f"没有这个对等节点：{name}（先 `orbit peer add {name} --url http://host:port`）",
            code=1,
        )
    raise _unimplemented(
        f"peer stop 未实现：停 {name}{'/' + agent if agent else ''} 上的 run {run_id!r} "
        "需要远端提供「按 run id 取消单个 turn」的控制端点，而 Orbit 没有 run 账本、"
        "也没有可取消的远端 turn（`peer run` 本身未实现，所以不存在可停的 run）。"
    )


# ── gateway run ─────────────────────────────────────────────────────────────


def run_gateway_run(context: Context, args) -> int:
    """``gateway run``：**前台**运行网关（拿锁 + 写四个状态文件 + tick 循环）。

    与 ``gateway start`` 的区别照 Hermes：``start`` 是"启动已安装的后台服务"，
    ``run`` 是"就在这个终端里跑"，Ctrl+C 即退出。Orbit 没有系统服务（``install`` 未实现），
    所以 ``run`` 是**唯一**真能让 dispatcher 常驻的方式。

    两个退出条件，都走已有的机制而不是新造哨兵：
    - ``KeyboardInterrupt``（Ctrl+C）；
    - ``<data>/gateway/paused`` 出现（``orbit pause`` 写的就是它）。

    ``ORBIT_GATEWAY_RUN_TICKS=n``：只跑 n 个 tick 就退出，**给离线验证 / CI 用**。
    没有它，这条命令在测试里会永远阻塞（前台运行本来就是阻塞的）。
    """
    service = gw.GatewayService(context.data_dir)
    force = _flag(args, "replace") or _flag(args, "force")
    outcome = service.start(force=force)
    if not outcome.ok:
        raise CliError(f"前台启动被拒：{outcome.reason}", code=1)
    if _flag(args, "verbose"):
        context.out(f"dispatcher 已就绪：pid={outcome.pid}，数据目录={context.data_dir}")
        if outcome.reclaimed:
            context.out(f"已回收孤儿锁：{outcome.reason}")
    context.out(
        f"网关前台运行中（pid={outcome.pid}）。Ctrl+C 退出，或另开终端 `orbit pause` 停止。"
    )
    ticks = _run_ticks()
    served = 0
    try:
        while True:
            if service.paused():
                context.out("检测到 pause 哨兵：退出前台运行（dispatcher 锁已释放）。")
                break
            served += 1
            if ticks and served >= ticks:
                break
            time.sleep(0.0 if ticks else RUN_TICK_SECONDS)
    except KeyboardInterrupt:  # 前台运行的正常退出方式，不是错误
        context.out("收到 Ctrl+C：退出前台运行。")
    finally:
        service.stop()
    context.emit({"ok": True, "pid": outcome.pid, "ticks": served, "foreground": True})
    return 0


def _run_ticks() -> int:
    """``ORBIT_GATEWAY_RUN_TICKS`` 的解析：坏值按 0 处理（= 无界运行）。"""
    raw = os.environ.get(RUN_TICKS_ENV, "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


# ── gateway list ────────────────────────────────────────────────────────────


def run_gateway_list(context: Context, args) -> int:
    """``gateway list``：列出本机网关实例与它们的 dispatcher 状态。

    **诚实边界**：Hermes 的 ``gateway list`` 是"列出各 profile 的网关"（多实例）。
    Orbit 是**单数据目录**（没有 profile 概念），所以这里只会有一行——这不是缺项，
    是事实。``--system`` / ``--all`` 的多实例枚举未实现（没有系统服务可枚举）。
    """
    if _flag(args, "system"):
        context.warn("--system 在 Orbit 里没有对应物：本轮不安装系统级服务，忽略该选项。")
    rows: list[tuple[str, str, str, str]] = []
    for data_dir in _known_data_dirs(context):
        service = gw.GatewayService(data_dir)
        status = service.status()
        dispatcher = status["dispatcher"]
        if dispatcher["running"]:
            state = f"运行中（pid={dispatcher['holder'].get('pid')}）"
        elif dispatcher["holder"]:
            state = f"未运行（残留锁 pid={dispatcher['holder'].get('pid')}）"
        else:
            state = "未运行"
        rows.append(
            (
                str(data_dir),
                state,
                f"{len(IMPLEMENTED_PLATFORMS)}/{len(PLATFORMS)}",
                f"{status['starts']['total_starts']} 次",
            )
        )
    context.table(("数据目录", "dispatcher", "已实现平台", "启动次数"), rows)
    context.out(
        "说明：Orbit 只有一个数据目录（没有 Hermes 的 profile 多实例）；"
        "`--system` / `--all` 的多实例枚举未实现。"
    )
    context.emit(
        {
            "ok": True,
            "instances": [
                {"data_dir": row[0], "dispatcher": row[1], "platforms": row[2], "starts": row[3]}
                for row in rows
            ],
        }
    )
    return 0


def _known_data_dirs(context: Context) -> list[Path]:
    """本机"已知的"网关数据目录：当前数据目录，加上仓库默认 data 目录（若不同）。"""
    dirs = [context.data_dir]
    try:
        from app.config import ROOT

        default = Path(ROOT) / "data"
        if default.resolve() != context.data_dir.resolve() and default.is_dir():
            dirs.append(default)
    except Exception:  # noqa: BLE001 - 枚举已知目录失败不该让 list 崩
        pass
    return dirs


# ── gateway migrate-legacy ──────────────────────────────────────────────────


def run_gateway_migrate_legacy(context: Context, args) -> int:
    """``gateway migrate-legacy``：扫出旧版 hermes 服务单元并给出处置建议。

    **能做到哪一步**：扫描 + 报告 + 给出**手工**删除命令。
    **做不到**：自动删除——那些路径在 ``data_dir`` 之外，而 Orbit 的写入范围只到
    ``data_dir``（``app/plugins.py`` 的扫描器把"写系统目录"直接判成 dangerous，是同一个
    原则）。``--dry-run`` 与 ``-y/--yes`` 都照 Hermes 接受，但 ``--yes`` 只会让输出更明确，
    不会真的 ``unlink``：一条"清理脚本"把用户的服务单元删错，比留着它糟得多。
    """
    found: list[Path] = []
    for raw in LEGACY_UNIT_PATHS:
        path = Path(raw).expanduser()
        if path.exists():
            found.append(path)
    if not found:
        context.out("没有遗留的 hermes 服务单元（systemd / launchd 都没找到）。")
        context.emit({"ok": True, "found": [], "removed": [], "dry_run": True})
        return 0
    context.table(("遗留单元", "建议"), [(str(path), "手工删除") for path in found])
    if _flag(args, "dry_run"):
        context.out("--dry-run：只报告，未做任何改动。")
    if _flag(args, "yes"):
        context.out(
            "注意：--yes 不会真的删除。这些文件在 data_dir 之外，Orbit 只写 data_dir。"
        )
    context.out("手工清理（确认后再执行）：")
    for path in found:
        context.out(f"  rm -f {path}" if os.name != "nt" else f'  Remove-Item "{path}"')
    context.emit(
        {
            "ok": True,
            "found": [str(path) for path in found],
            "removed": [],
            "dry_run": True,
            "note": "自动删除未实现：系统单元文件在 data_dir 之外",
        }
    )
    return 0


# ── gateway enroll / install / uninstall（未实现）────────────────────────────


def run_gateway_enroll(context: Context, args) -> int:
    """``gateway enroll``：向中继（relay）连接器注册本网关。未实现。"""
    connector = _text(args, "connector_url").strip()
    where = f"（connector={connector}）" if connector else ""
    raise _unimplemented(
        f"gateway enroll 未实现{where}：中继注册要 ① 可达的 connector 服务端与它签发的 "
        "一次性令牌，② 公网可达的 wake URL（对端要能反向唤醒本机网关），③ 一套"
        "网关身份/令牌轮换协议。本机无网络，也没有 connector 服务，三样都无法验证；"
        "照抄一个「看起来像注册成功」的假实现只会让对端以为这台机器在线。"
    )


def run_gateway_install(context: Context, args) -> int:
    """``gateway install``：把网关装成后台服务。未实现。"""
    raise _unimplemented(
        "gateway install 未实现：它要写**系统级**服务单元（systemd user unit / "
        "launchd plist / Windows 服务），这些路径在 data_dir 之外，而 Orbit 的写入范围"
        "只到 data_dir；安装服务还需要提权与开机自启的注册。本轮请用 `orbit gateway run` "
        "在前台运行（或用你自己的进程管理器托管它）。"
    )


def run_gateway_uninstall(context: Context, args) -> int:
    """``gateway uninstall``：卸载后台服务。未实现（与 install 同因）。"""
    raise _unimplemented(
        "gateway uninstall 未实现：没有 install 就没有可卸载的服务单元；"
        "而且删除系统级单元文件同样超出 Orbit 的写入范围（只写 data_dir）。"
    )


# ── 接线 ────────────────────────────────────────────────────────────────────


def _bind(path: str, handler: Any, *, state: str = "done") -> None:
    """把实现登记到声明表的某条命令路径上（两种导入顺序都要成立）。

    与 ``app/cli/commands/gateway.py::_bind`` 同一个理由：本模块可能排在
    ``hermes_parity`` 之前（表还没组装，交给 ``register_specs`` 去填），也可能排在
    之后（表已组装，必须就地回填 handler，否则命令停在"尚未实现"的占位实现上）。

    ``state`` 默认 ``done``；**只如实报未实现的路径必须传 ``partial``**——
    ``Spec.build()`` 见到有 handler 就标 ``done``，于是"如实说没有"会被算成"有"，
    那正是对标矩阵最容易被高估的一处（``tools_extra2.mark_proxy_paths_partial``
    踩过同一个坑）。
    """
    register_handler(path, handler)
    head, _, leaf = path.partition(" ")
    parent = REGISTRY.get(head)
    target = parent.child(leaf) if (parent is not None and leaf) else parent
    if target is not None and not target.subcommands:
        target.handler = handler
        target.state = state


#: 只如实报"未实现"的路径：状态必须是 ``partial``，不能算 ``done``。
_UNIMPLEMENTED_PATHS: tuple[str, ...] = (
    "peer run",
    "peer stop",
    "gateway enroll",
    "gateway install",
    "gateway uninstall",
)

#: "能做一半"的路径：同样不许算 ``done``（多 profile 枚举、自动删除都没做）。
_PARTIAL_PATHS: tuple[str, ...] = (
    "gateway list",
    "gateway migrate-legacy",
)

_bind("peer dm", run_peer_dm)
# `peer run` / `peer stop` **不在这里占位**：`app/peer_client.py` 与 `peer_extra.py`
# 已有真实现。占位的作用是「在真实现出现之前如实报未实现」，真实现出现了就该让位——
# 否则 `register_handler` 的重复登记门禁会让真实现永远接管不了，而用户看到的
# 永远是「未实现」。`run_peer_run` / `run_peer_stop` 两个函数保留，供 peer_extra
# 在对端不支持时复用同一套措辞。
_bind("gateway run", run_gateway_run)
# `list` / `migrate-legacy` 是"能做一半"：单实例列表与遗留扫描可用，多 profile 枚举与
# 自动删除未实现——标 done 会让矩阵以为 Hermes 的多实例语义也齐了。
_bind("gateway list", run_gateway_list, state="partial")
_bind("gateway migrate-legacy", run_gateway_migrate_legacy, state="partial")
_bind("gateway enroll", run_gateway_enroll, state="partial")
_bind("gateway install", run_gateway_install, state="partial")
_bind("gateway uninstall", run_gateway_uninstall, state="partial")


def mark_unimplemented_paths_partial() -> list[str]:
    """把"只如实报未实现 / 只做一半"的路径状态钉成 ``partial``。

    为什么需要这一步：如果本模块在 ``hermes_parity`` **之前**导入（生产接线的常规位置），
    ``_bind`` 时表还没组装，``REGISTRY.get("peer")`` 是 ``None``，回填什么也做不了；
    ``register_specs`` 随后按 ``HANDLERS`` 填 handler 并把状态一律标成 ``done``。
    于是"如实说没有"会被算成"有"——这正是对标矩阵最容易被高估的一处
    （``tools_extra2.mark_proxy_paths_partial`` 踩过同一个坑）。

    本模块在导入时把 :func:`_install_state_hook` 挂到 ``register_specs`` 上，所以表组装
    完成的那一刻会自动调用本函数；接线顺序（前/后）都不影响状态的诚实性。
    """
    changed: list[str] = []
    for path in (*_UNIMPLEMENTED_PATHS, *_PARTIAL_PATHS):
        head, _, leaf = path.partition(" ")
        parent = REGISTRY.get(head)
        target = parent.child(leaf) if parent is not None else None
        if target is not None and target.state != "partial":
            target.state = "partial"
            changed.append(path)
    return changed


def _install_state_hook() -> None:
    """包一层 ``register_specs``：表组装完立刻校正状态（接线顺序无关）。"""
    current = _table.register_specs
    if getattr(current, "_orbit_platform_extra_hook", False):
        return

    def wrapper(specs: Any) -> list[str]:
        result = current(specs)
        mark_unimplemented_paths_partial()
        return result

    setattr(wrapper, "_orbit_platform_extra_hook", True)  # noqa: B010 - 打标记防重复包装
    _table.register_specs = wrapper


_install_state_hook()


__all__ = [
    "LEGACY_UNIT_PATHS",
    "PEER_DM_PATH",
    "PEER_TIMEOUT",
    "PEER_TRANSPORT",
    "RUN_TICKS_ENV",
    "mark_unimplemented_paths_partial",
    "run_gateway_enroll",
    "run_gateway_install",
    "run_gateway_list",
    "run_gateway_migrate_legacy",
    "run_gateway_run",
    "run_gateway_uninstall",
    "run_peer_dm",
    "run_peer_run",
    "run_peer_stop",
]
