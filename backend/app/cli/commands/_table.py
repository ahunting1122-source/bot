"""声明式命令表的构建器：把"数据"翻译成注册表里的 ``Command``。

分工（这是本文件存在的理由）：

- **数据**放在 ``hermes_parity.py``：一张大表，逐条抄 Hermes 的命令面。
- **逻辑**放在 ``app/cli/commands/*.py``：真实现的命令，各自 ``REGISTRY.add(...)``。
- **本文件**只做翻译，并负责"已实现的命令不要被表重复登记"。

迁移规则：某个命令一旦有了真实现，就把它从表里删掉、在功能域模块里登记。
表里剩下的就是"还没做的"，所以 ``orbit --help`` 里看到的命令永远是真的，
矩阵里的 ``todo`` 永远是诚实的。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.cli.context import CliError, Context
from app.cli.shorthand import to_option
from app.cli.spec import REGISTRY, Command


@dataclass
class Spec:
    """一条命令的声明。字段刻意少——只放 Hermes 命令面里真实存在的东西。"""

    name: str
    help: str
    group: str = "core"
    parity: str = "P1"
    hermes: str = ""
    aliases: tuple[str, ...] = ()
    options: tuple[str, ...] = ()
    subcommands: tuple[Spec, ...] = field(default_factory=tuple)
    #: 该命令计划在哪个波次落地（W3/W5/…）。写进未实现的报错里，便于排期。
    wave: str = ""
    #: 需要数据库/后端已初始化。默认 False：登记的命令多数是纯配置/文件操作。
    needs_store: bool = False

    def build(self) -> Command:
        return Command(
            name=self.name,
            help=self.help,
            handler=None if self.subcommands else _pending(self),
            options=[to_option(item) for item in self.options],
            subcommands=[sub.build() for sub in self.subcommands],
            group=self.group,
            hermes=self.hermes or self.name,
            parity=self.parity,
            state="todo",
            aliases=self.aliases,
            needs_store=self.needs_store,
        )


def _pending(spec: Spec):
    """未实现命令的 handler：给一句能行动的话，而不是一个空壳。"""

    def handler(context: Context, args) -> int:
        where = f"（计划波次 {spec.wave}）" if spec.wave else ""
        raise CliError(
            f"`orbit {spec.name}` 尚未实现{where}。"
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
