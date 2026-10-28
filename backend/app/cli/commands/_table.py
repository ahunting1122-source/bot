"""声明式命令表的构建器：把"数据"翻译成注册表里的 ``Command``。

分工（这是本文件存在的理由）：

- **数据**放在 ``hermes_parity.py``：一张大表，逐条抄 Hermes 的命令面。
- **逻辑**放在 ``app/cli/commands/*.py``：真实现的命令，各自 ``REGISTRY.add(...)``。
- **本文件**只做翻译，并负责"已实现的命令不要被表重复登记"。

迁移规则：某个命令一旦有了真实现，就把它从表里删掉、在功能域模块里登记。
表里剩下的就是"还没做的"，所以 ``orbit --help`` 里看到的命令永远是真的，
矩阵里的 ``todo`` 永远是诚实的。

选项的两种写法（都支持，按信息量选）：

    "--source SOURCE"                          # 只有形状，没有说明
    ("--source SOURCE", "按来源过滤（cli/…）")   # 形状 + 说明 ← **优先用这个**

只有形状没有说明时，``--help`` 会显示一列光秃秃的选项——这正是"抄了命令名但没抄语义"
的典型症状。所以 :func:`register_specs` 会统计"无说明选项"的数量，测试用它当门禁。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.cli.context import CliError, Context
from app.cli.shorthand import to_option
from app.cli.spec import REGISTRY, Command, Handler

#: 选项声明的两种形态：短字符串，或 (短字符串, 中文说明)。
OptionSpec = str | tuple[str, str]


#: 命令路径 → handler。让**声明表里的命令**可以被功能域模块接管实现。
#:
#: 为什么需要这个机制：`sessions` 在表里有 19 个子命令，本轮只实现其中 6 个。
#: 如果把这 6 个"搬出表"另立一个 `sessions` Command，表里剩下 13 条会整片消失
#: ——命令面反而缩水，矩阵也会跟着丢条目。正确的做法是：表不动，实现方只声明
#: "这个路径我接了"，`register_specs` 组装时把 handler 填进去、状态改 `done`。
#:
#: 使用方式（在功能域模块里，导入顺序必须在 `hermes_parity` 之前）：
#:
#:     from app.cli.commands._table import register_handler
#:     register_handler("sessions list", run_list)
#:     register_handler("sessions browse", run_browse)
HANDLERS: dict[str, Handler] = {}


def register_handler(path: str, handler: Handler) -> None:
    """登记某条命令路径的真实现。路径写法与 `orbit --help` 里的层级一致。"""
    key = " ".join(path.split())
    if key in HANDLERS:
        raise ValueError(f"命令实现重复登记：{key}")
    HANDLERS[key] = handler


@dataclass
class Spec:
    """一条命令的声明。字段刻意少——只放 Hermes 命令面里真实存在的东西。"""

    name: str
    help: str
    group: str = "core"
    parity: str = "P1"
    hermes: str = ""
    aliases: tuple[str, ...] = ()
    options: tuple[OptionSpec, ...] = ()
    subcommands: tuple[Spec, ...] = field(default_factory=tuple)
    #: 该命令计划在哪个波次落地（W3/W5/…）。写进未实现的报错里，便于排期。
    wave: str = ""
    #: 需要数据库/后端已初始化。默认 False：登记的命令多数是纯配置/文件操作。
    needs_store: bool = False

    def build(self, prefix: str = "") -> Command:
        path = f"{prefix} {self.name}".strip()
        children = [sub.build(path) for sub in self.subcommands]
        claimed = HANDLERS.get(path)
        if claimed is not None:
            # 本节点自己有动作。可能是叶子，也可能是「裸命令 + 子命令」并存的节点
            # （`orbit dashboard` 起面板、`orbit dashboard register` 注册插件页面）。
            # 早先的实现一律不给中间节点 handler，于是 `orbit dashboard` 只能打印帮助。
            handler, state = claimed, "done"
        elif children:
            # 纯分组节点：自己不承接动作，状态由子节点汇总。
            # 早先一律标 todo，导致「子命令全实现了的父节点仍显示未实现」——
            # 完成度被系统性低估（`skin` 就是这么被低估的）。
            states = {child.state for child in children}
            if states == {"done"}:
                state = "done"
            elif states & {"done", "partial"}:
                state = "partial"
            else:
                state = "todo"
            handler = None
        else:
            handler, state = _pending(path, self.wave), "todo"
        return Command(
            name=self.name,
            help=self.help,
            handler=handler,
            options=[_option(item) for item in self.options],
            subcommands=children,
            group=self.group,
            hermes=self.hermes or path,
            parity=self.parity,
            state=state,
            aliases=self.aliases,
            needs_store=self.needs_store,
        )


def _option(item: OptionSpec):
    if isinstance(item, tuple):
        return to_option(item[0], item[1])
    return to_option(item)


def _pending(path: str, wave: str):
    """未实现命令的 handler：给一句能行动的话，而不是一个空壳。"""

    def handler(context: Context, args) -> int:
        where = f"（计划波次 {wave}）" if wave else ""
        raise CliError(
            f"`orbit {path}` 尚未实现{where}。"
            f"命令面已登记，实现在 handoff/hermes-parity/ROADMAP.md 里排期。",
            code=3,
        )

    return handler


def register_specs(specs: tuple[Spec, ...]) -> list[str]:
    """登记一批声明。返回被跳过的名字（= 已有真实现）。

    跳过而不是报错：迁移期两处并存是正常的，重复登记才是 bug。
    """
    skipped: list[str] = []
    fresh: list[Command] = []
    for spec in specs:
        if REGISTRY.get(spec.name) is not None:
            skipped.append(spec.name)
            continue
        fresh.append(spec.build())
    if fresh:
        REGISTRY.add(*fresh)
    return skipped


def undocumented_options(specs: tuple[Spec, ...], prefix: str = "") -> list[str]:
    """列出"只有形状、没有说明"的选项，形如 ``sessions list --source``。

    这是给测试用的门禁：命令面抄全了但选项说明全空，``--help`` 依然不可用。
    """
    missing: list[str] = []
    for spec in specs:
        path = f"{prefix} {spec.name}".strip()
        for item in spec.options:
            if isinstance(item, tuple):
                continue
            # 位置参数的说明同样重要（`log_name` 光有个名字谁也猜不到可选值）
            missing.append(f"{path} {item}")
        missing.extend(undocumented_options(spec.subcommands, path))
    return missing
