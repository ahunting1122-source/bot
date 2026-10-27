"""命令注册表：一条命令 = 一个 :class:`Command` 对象。

设计要点（改这里之前先读）：

- **不可变优先**。``Command`` 用普通 dataclass 而不是 frozen，是因为子命令列表在
  各模块里分步拼装（``config`` 先登记，``config get`` 后补）。除此之外不要在运行时
  改字段——注册表是"编译期"结构。
- **handler 只认 ``(ctx, args) -> int``**。返回码语义照 Unix：0 成功、非 0 失败。
  抛 ``CliError`` 等价于"打印这条消息 + 返回码"，别在 handler 里 ``print`` 错误再
  ``return 1``，那样 ``--json`` 模式下会混进非结构化文本。
- **归类（group）是给人看的，优先级（parity）是给核对用的**。两者都不影响 argparse。
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

#: handler 签名。返回进程退出码。
Handler = Callable[[Any, argparse.Namespace], int]

#: 命令归类。顺序即 ``orbit --help`` 里的展示顺序。
GROUPS: dict[str, str] = {
    "core": "核心（配置 / 诊断 / 会话 / 记忆 / 技能 / 工具 / MCP）",
    "run": "运行（对话 / 服务 / 网关 / 定时 / 委派）",
    "ops": "运维（备份 / 迁移 / 更新 / 日志 / 监控）",
    "platform": "平台集成（消息平台 / Webhook / 配对 / 对等节点）",
    "ext": "扩展（插件 / 模型供应商 / 观测导出）",
    "internal": "内部命令（供其它进程调用，不作为用户入口）",
}

#: 对标优先级。P0 = 没有它就不算对齐 Hermes；P1 = 应做；P2 = 可选/内部。
PARITY_LEVELS: tuple[str, ...] = ("P0", "P1", "P2")

#: 对标状态。``done`` 有实现且有验证证据；``partial`` 实现但缺项；``todo`` 仅登记。
PARITY_STATES: tuple[str, ...] = ("done", "partial", "todo")


@dataclass(frozen=True)
class Option:
    """一条命令行选项。

    只支持 argparse 里**真正常用**的那几种 action。刻意不暴露任意 ``**kwargs``：
    一旦允许，选项行为就会散落各处，``--help`` 的一致性立刻失守。
    """

    flags: tuple[str, ...]
    help: str = ""
    action: str | None = None
    default: Any = None
    metavar: str | None = None
    choices: tuple[str, ...] | None = None
    dest: str | None = None
    const: Any = None
    nargs: str | int | None = None

    def add(self, parser: argparse.ArgumentParser) -> None:
        kwargs: dict[str, Any] = {"help": self.help}
        # argparse 把 ``default=None`` 和"不给 default"当同一件事，但 ``action="store_true"``
        # 的默认值必须是 False，否则 ``args.flag`` 会是 None 而不是布尔。显式区分。
        if self.action is not None:
            kwargs["action"] = self.action
        if self.default is not None:
            kwargs["default"] = self.default
        elif self.action == "store_true":
            kwargs["default"] = False
        if self.metavar is not None:
            kwargs["metavar"] = self.metavar
        if self.choices is not None:
            kwargs["choices"] = self.choices
        if self.dest is not None:
            kwargs["dest"] = self.dest
        if self.const is not None:
            kwargs["const"] = self.const
        if self.nargs is not None:
            kwargs["nargs"] = self.nargs
        parser.add_argument(*self.flags, **kwargs)


@dataclass
class Command:
    """一条命令或子命令。"""

    name: str
    help: str
    handler: Handler | None = None
    options: list[Option] = field(default_factory=list)
    subcommands: list[Command] = field(default_factory=list)
    group: str = "core"
    #: 对标 Hermes 的哪条命令（留空 = 同名）。
    hermes: str = ""
    parity: str = "P0"
    state: str = "todo"
    aliases: tuple[str, ...] = ()
    description: str = ""
    epilog: str = ""
    #: True = 需要 ``settings.prepare()``（会建目录）；纯读命令设 False 更安全。
    needs_settings: bool = True
    #: True = 需要打开数据库。为 False 时即使数据文件损坏也能给出可读错误。
    needs_store: bool = False

    def __post_init__(self) -> None:
        if self.parity not in PARITY_LEVELS:
            raise ValueError(f"{self.name}: 未知优先级 {self.parity!r}")
        if self.state not in PARITY_STATES:
            raise ValueError(f"{self.name}: 未知状态 {self.state!r}")
        if self.group not in GROUPS:
            raise ValueError(f"{self.name}: 未知归类 {self.group!r}")

    @property
    def path(self) -> str:
        """人类可读的命令路径，如 ``config get``。"""
        return self.name

    def child(self, name: str) -> Command | None:
        for sub in self.subcommands:
            if sub.name == name or name in sub.aliases:
                return sub
        return None


class Registry:
    """命令注册表。模块导入时登记，``main`` 时一次性编译。"""

    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}
        self._aliases: dict[str, str] = {}

    def add(self, *commands: Command) -> None:
        for command in commands:
            if command.name in self._commands:
                raise ValueError(f"命令重复登记：{command.name}")
            self._commands[command.name] = command
            for alias in command.aliases:
                if alias in self._aliases or alias in self._commands:
                    raise ValueError(f"别名冲突：{alias}")
                self._aliases[alias] = command.name
            # 子命令的别名也要登记，否则 ``orbit config ls`` 找不到 ``list``。
            for sub in command.subcommands:
                for alias in sub.aliases:
                    if alias in self._aliases:
                        raise ValueError(f"子命令别名冲突：{command.name} {alias}")
                    self._aliases[f"{command.name}:{alias}"] = sub.name

    def get(self, name: str) -> Command | None:
        if name in self._commands:
            return self._commands[name]
        target = self._aliases.get(name)
        return self._commands.get(target) if target else None

    def resolve_child(self, parent: Command, name: str) -> Command | None:
        child = parent.child(name)
        if child is not None:
            return child
        target = self._aliases.get(f"{parent.name}:{name}")
        return parent.child(target) if target else None

    def all(self) -> list[Command]:
        """按登记顺序返回全部命令。顺序 = 模块导入顺序，保持稳定即可。"""
        return list(self._commands.values())

    def by_group(self, group: str) -> list[Command]:
        return [command for command in self._commands.values() if command.group == group]

    def parity_rows(self) -> list[dict[str, str]]:
        """对标矩阵的数据源：顶层命令 + 子命令展平。"""
        rows: list[dict[str, str]] = []

        def walk(command: Command, prefix: str) -> None:
            path = f"{prefix} {command.name}".strip()
            rows.append(
                {
                    "command": path,
                    "hermes": command.hermes or path,
                    "group": command.group,
                    "parity": command.parity,
                    "state": command.state,
                    "help": command.help,
                }
            )
            for sub in command.subcommands:
                walk(sub, path)

        for command in self._commands.values():
            walk(command, "")
        return rows


#: 全局唯一注册表。各 ``commands/*.py`` 在导入时往里登记。
REGISTRY = Registry()


def flatten(argv: Sequence[str]) -> list[str]:
    """把 ``["config", "get"]`` 这样的调用路径原样返回（占位，便于测试替换）。"""
    return list(argv)
