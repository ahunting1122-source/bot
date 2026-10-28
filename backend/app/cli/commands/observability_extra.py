"""`orbit monitoring` 的可观测性扩展：exporter 插件与 metrics schema（MATRIX 15.4 / 15.5）。

`monitoring status` / `monitoring metrics` 已经在 `observability.py` 里。这里补的是
**往外送**与**契约版本**两块：

- `monitoring exporters`：列出 exporter 池（哪个能真的送、哪个只能 spool、缺什么凭证）
- `monitoring flush <name>`：把 spool 送出去。本机无网络 → 远端 exporter 明确失败，
  jsonl 直接成功
- `monitoring schema [--version N] [--migrate]`：打印 schema 并可选把 v1 迁到 v2

**为什么 exporter 只落盘不假装上报**：配置好之后什么都收不到、日志里却全是
"发送成功"，是最坏的结果——排查时没有任何线索。所以 `describe()` 必须写清缺什么，
`flush()` 必须抛错而不是返回成功。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app import metrics_schema
from app.cli.context import CliError, Context
from app.cli.shorthand import to_option
from app.cli.spec import REGISTRY, Command
from app.exporters import ExportError, default_exporters

#: "未实现 / 不可用"的退出码。与 `_table._pending` / `mcp.py::UNIMPLEMENTED_CODE` 一致。
UNAVAILABLE_CODE = 3


def _exporters(context: Context) -> dict[str, Any]:
    return default_exporters(context.data_dir)


def run_exporters(context: Context, args: argparse.Namespace) -> int:
    """列出 exporter 池与各自的真实能力。"""
    pool = _exporters(context)
    rows = [info.describe() for info in pool.values()]
    if getattr(args, "json", False):
        context.out(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return 0

    context.out("可观测性 exporter（spool = 本地待发队列，网络恢复后可补发）\n")
    headers = ["名称", "类型", "可用", "缺什么 / 说明"]
    table: list[list[str]] = []
    for info in rows:
        if info["available"]:
            note = info.get("note", "")
        else:
            missing = info.get("missing_env") or []
            note = f"缺环境变量：{'、'.join(missing)}" if missing else info.get("note", "")
        table.append(
            [
                str(info["name"]),
                str(info["kind"]),
                "是" if info["available"] else "否",
                note,
            ]
        )
    context.table(headers, table)
    spool = context.data_dir / "exports" / "spool"
    pending = sum(len(info.get("spool", "")) and 1 for info in rows)
    context.out(f"\nspool 目录：{spool}")
    context.out(
        "本机无网络：远端 exporter 只能 spool，不能上报。"
        "这不是配置问题，是环境限制——`flush` 会明确报错而不是假装成功。"
    )
    del pending
    return 0


def run_flush(context: Context, args: argparse.Namespace) -> int:
    """把某个 exporter 的 spool 送出去。送不出去就如实失败。"""
    name = args.name
    pool = _exporters(context)
    if name not in pool:
        raise CliError(f"没有名为 {name} 的 exporter（可选：{'、'.join(pool)}）")
    exporter = pool[name]
    pending = len(exporter.pending()) if hasattr(exporter, "pending") else 0
    try:
        sent = exporter.flush()
    except ExportError as exc:
        # 退出码 3：这是"环境不具备"而不是"参数写错了"。
        context.warn(f"orbit monitoring flush {name}: {exc}")
        context.warn(f"spool 里还有 {pending} 条，未丢失。")
        return UNAVAILABLE_CODE
    context.out(f"{name}：已送出 {sent} 条（spool 里原有 {pending} 条）")
    return 0


def run_schema(context: Context, args: argparse.Namespace) -> int:
    """打印 schema；`--migrate` 把一份 v1 载荷迁到 v2 并输出。"""
    if args.migrate:
        source = Path(args.migrate)
        if not source.exists():
            raise CliError(f"找不到文件：{source}")
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CliError(f"{source} 不是合法 JSON：{exc}") from exc
        version = metrics_schema.validate(payload)
        if version == metrics_schema.CURRENT_VERSION:
            context.out(f"已经是 v{version}，无需迁移。")
            return 0
        migrated = metrics_schema.migrate_v1_to_v2(payload)
        context.out(metrics_schema.dumps(migrated))
        return 0

    version = int(args.version or metrics_schema.CURRENT_VERSION)
    if version not in metrics_schema.SUPPORTED_VERSIONS:
        raise CliError(
            f"不支持的 schema 版本 {version}（支持 {metrics_schema.SUPPORTED_VERSIONS}）"
        )
    if version == 1:
        context.out(
            json.dumps(
                {
                    "schema_version": 1,
                    "metrics": {"<指标名>": "<数字>"},
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        context.warn(
            "注意：v1 的 0 分不清「有数据且为 0」与「缺数据」。新代码请用 v2。"
        )
        return 0
    context.out(
        json.dumps(
            {
                "schema_version": 2,
                "collected_at": "<ISO8601>",
                "metrics": {
                    "<指标名>": {
                        "value": "<数字>",
                        "available": True,
                        "source": "<这个数从哪来>",
                        "help": "<一句话说明>",
                    }
                },
                "degraded": ["<不可用的指标名>"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    context.warn(
        "`available=false` 时必须写 source 或 help 说明原因——"
        "标了不可用却不说为什么，等于让用户自己去猜。"
    )
    return 0


# ── 接线 ────────────────────────────────────────────────────────────────────


def _extra_subcommands() -> list[Command]:
    return [
        Command(
            name="exporters",
            help="列出可观测性 exporter（哪个能真的送、哪个只能 spool、缺什么凭证）",
            handler=run_exporters,
            group="ext",
            hermes="monitoring exporters",
            parity="P2",
            state="done",
            options=[to_option("--json", "以 JSON 输出")],
        ),
        Command(
            name="flush",
            help="把 exporter 的 spool 送出去（本机无网络时远端 exporter 明确失败）",
            handler=run_flush,
            group="ext",
            hermes="monitoring flush",
            parity="P2",
            state="done",
            options=[to_option("name", "exporter 名（jsonl / langfuse / nemo_relay）")],
        ),
        Command(
            name="schema",
            help="打印 metrics 载荷的 schema（v1/v2），或把一份 v1 载荷迁到 v2",
            handler=run_schema,
            group="ext",
            hermes="monitoring schema",
            parity="P2",
            state="done",
            options=[
                to_option("--version N", "要打印的 schema 版本（默认当前版本 v2）"),
                to_option("--migrate FILE", "把文件里的 v1 载荷迁到 v2 并输出"),
            ],
        ),
    ]


def extend_registry() -> list[str]:
    """把新子命令追加到 `monitoring`（幂等，返回新增路径）。"""
    parent = REGISTRY.get("monitoring")
    if parent is None:
        return []
    existing = {sub.name for sub in parent.subcommands}
    added: list[str] = []
    for child in _extra_subcommands():
        if child.name in existing:
            continue
        parent.subcommands.append(child)
        added.append(f"monitoring {child.name}")
    return added


def _install() -> None:
    """父命令还没组装就推迟到声明表组装完（与 `tools_extra2.py` 同一套做法）。"""
    if REGISTRY.get("monitoring") is not None:
        extend_registry()
        return
    # `wrap_register_specs` 在 `tools_extra2` 里（同一套接缝，别复制第二份）
    from app.cli.commands import _table
    from app.cli.commands.tools_extra2 import wrap_register_specs

    wrap_register_specs(_table, extend_registry)


_install()

__all__ = ["extend_registry", "run_exporters", "run_flush", "run_schema"]
