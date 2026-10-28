"""`orbit tools probe`：工具可用性探针（对标 Hermes 的 `check_fn`，MATRIX 5.3）。

**为什么单独一个文件**：`app/cli/commands/tools.py` 的接线由统一入口负责（本波次约定
不改它），所以这里以**追加子命令**的方式挂到已登记的 ``tools`` 命令上——导入即生效，
既有文件一个字都不用动。

**为什么不把探针塞进 `tools list`**：两者回答的问题不同。

- ``tools list``：**配置**允许暴露什么（禁用集 / 白名单 / 未归类）。它是确定性的，
  同一份配置永远给同一份答案。
- ``tools probe``：**这台机器此刻**能不能真的用（chromium 在不在、token 配没配、
  provider 通不通）。它会变，所以单独一条命令、单独一份输出。

混在一起最坏的后果是把"此刻恰好不可用"当成"被禁用了"——用户会去翻 `tools.json`
找一个根本不存在的禁用项。``post_setup_snapshot`` 也正是因此走
``toolsets._config_exposure``（只看配置）而不是 ``resolve``。
"""

from __future__ import annotations

import logging

import app.cli.commands.tools as tools_commands  # 导入即登记 `tools`（与本模块的导入顺序无关）
from app import toolsets
from app.cli.context import Context
from app.cli.spec import REGISTRY, Command, Option

log = logging.getLogger("orbit.cli.tools_extra")

PLATFORM_HELP = "平台（默认 cli；目前探针与平台无关，保留参数是为了与 `tools list` 一致）"


def run_probe(context: Context, args) -> int:
    """逐个工具跑一次真实探针，打印"能不能用、为什么不能用"。"""
    platform = str(getattr(args, "platform", None) or toolsets.DEFAULT_PLATFORM)
    names = tools_commands._catalog_names()
    rows = toolsets.probe_all(names, context.settings)
    config = toolsets.load_config(toolsets.config_path(context.data_dir))
    # 再按配置看一遍：一个工具可能"配置允许但此刻不可用"，也可能"配置就禁了"，
    # 两种原因必须分开讲，否则用户不知道该改 tools.json 还是该装依赖。
    configured = {
        row["name"]: row
        for row in toolsets.describe(platform, config, names, settings=context.settings)
    }
    unavailable = [row for row in rows if not row["available"]]
    probed = [row for row in rows if row["requirement"]]

    context.out(f"工具可用性探针（平台：{platform}）")
    context.out()
    context.table(
        ("工具", "toolset", "需求", "可用", "说明"),
        [
            (
                row["name"],
                row["toolset"],
                row["requirement"] or "—",
                "是" if row["available"] else "否",
                row["reason"] or "",
            )
            for row in probed
        ],
    )
    context.out()
    context.out(
        f"共探测 {len(probed)} 个有依赖的工具，可用 {len(probed) - len(unavailable)} 个，"
        f"不可用 {len(unavailable)} 个；其余 {len(rows) - len(probed)} 个工具无外部依赖。"
    )
    if unavailable:
        # 不可用**不是**禁用：给出"下一步怎么做"，否则用户只会以为工具坏了。
        context.warn(
            "以下工具当前不可用（不代表被禁用，补齐依赖或配置后自动恢复）："
            + "、".join(row["name"] for row in unavailable)
        )
    context.note(
        "探针结果带 30 秒 TTL 与 60 秒抖动宽限：一次探测失败不会立刻把工具从暴露面摘掉"
        "（否则网络抖一下整个 toolset 就消失了）。"
    )
    context.emit(
        {
            "ok": True,
            "platform": platform,
            "probed": len(probed),
            "unavailable": [row["name"] for row in unavailable],
            "tools": [
                {
                    **row,
                    "exposed": configured.get(row["name"], {}).get("exposed", True),
                }
                for row in rows
            ],
        }
    )
    return 0


PROBE_COMMAND = Command(
    name="probe",
    help="探测工具可用性（依赖 / 密钥 / provider 是否齐）",
    handler=run_probe,
    group="ext",
    parity="P1",
    state="done",
    needs_settings=False,
    options=[
        Option(
            ("--platform",),
            PLATFORM_HELP,
            default=toolsets.DEFAULT_PLATFORM,
            metavar="PLATFORM",
        )
    ],
)


def _register(command: Command) -> None:
    """把 ``probe`` 挂到 ``tools`` 下；万一 ``tools`` 还没登记就退化成独立命令。

    退化分支不是"应该不会发生"的防御性代码：``tools_extra`` 可能被
    ``commands/__init__.py`` 排在 ``tools`` **之前**导入。那时直接 ``REGISTRY.add``
    一条同名子命令是不可能的（子命令不是顶层命令），所以先 import ``tools`` 模块
    把父命令登记出来；真出意外也只会少一个父命令，而不是把 CLI 启动打崩。
    """
    parent = REGISTRY.get("tools")
    if parent is None:
        REGISTRY.add(
            Command(
                name="tools-probe",
                help=command.help,
                handler=command.handler,
                group=command.group,
                parity=command.parity,
                state=command.state,
                needs_settings=command.needs_settings,
                options=command.options,
            )
        )
        return
    if parent.child(command.name) is None:
        parent.subcommands.append(command)


_register(PROBE_COMMAND)
