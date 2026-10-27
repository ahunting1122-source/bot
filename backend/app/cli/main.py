"""把命令注册表编译成 argparse 并分发。

为什么用 argparse 的嵌套 subparser 而不是手写分发：``hermes --help`` 的版式就是
argparse 的版式（``positional arguments: {a,b,c}`` + 缩进的子命令帮助）。要对齐它，
最省事也最不容易走样的做法就是用同一套机制，而不是仿造输出。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from typing import Any

from app.cli.context import CliError, Context
from app.cli.spec import GROUPS, REGISTRY, Command

PROG = "orbit"

#: 描述串对齐 Hermes 的定位句式，让人一眼看出这是同一个东西的对标实现。
DESCRIPTION = "Orbit - 网页操作特化的本地 Agent（对标 Hermes 的命令面）"


def orbit_version() -> str:
    """包版本。装成可编辑包时读元数据，直接跑源码时退回常量。"""
    try:
        return package_version("orbit-personal-agent")
    except PackageNotFoundError:
        return "0.1.0"


def _global_options() -> argparse.ArgumentParser:
    """全局选项。做成 parents 让它们既能写在命令前也能写在命令后。

    ``add_help=False``：帮助由每个 parser 自己管，parent 只提供参数定义，
    否则父子两个 parser 会为 ``-h`` 打架。
    """
    parser = argparse.ArgumentParser(add_help=False)
    # 默认值必须是 SUPPRESS 而不是 False/None：子命令解析器会把**自己的默认值**整体
    # 覆盖回父命名空间，于是 `orbit --json status` 里的 `--json` 会被子命令的默认值
    # 抹成 False。SUPPRESS 让"没给这个参数"等于"不写这个键"，父级的值才保得住。
    suppressed = argparse.SUPPRESS
    parser.add_argument(
        "--json", action="store_true", default=suppressed, help="以 JSON 输出（脚本友好）"
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true", default=suppressed, help="只输出必要信息"
    )
    parser.add_argument(
        "--data-dir",
        metavar="PATH",
        default=suppressed,
        help="覆盖数据目录（默认取 ORBIT_DATA_DIR 或仓库 data/）",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        default=suppressed,
        help="禁用彩色输出（本实现默认无色）",
    )
    # oneshot 是**全局** flag 而不是 `chat` 的子选项：对齐 `hermes -z "prompt"` 的用法，
    # 脚本里不必记得先写命令名。
    parser.add_argument(
        "-z",
        "--oneshot",
        metavar="PROMPT",
        default=suppressed,
        help="单次提问：只把最终回答打到 stdout 后退出（脚本/管道友好）",
    )
    return parser


def _attach_globals(
    parser: argparse.ArgumentParser,
    command: Command,
    global_parser: argparse.ArgumentParser | None,
) -> None:
    """把全局选项挂到子命令上，**跳过与子命令自己声明的 flag 冲突的那些**。

    为什么不能直接用 ``parents=[global_parser]``：Hermes 的很多子命令**自己就有**
    ``--json``（实测 ``approvals allow list`` 就是），再挂一份全局 parent 会直接
    ``argparse.ArgumentError: conflicting option string: --json``，整个命令树编译不出来。

    为什么要挂：不挂的话 ``orbit security audit --json`` 报
    ``unrecognized arguments``，只有 ``orbit --json security audit`` 能用——
    而人写命令时习惯把 ``--json`` 放最后。这个不对称是实测出来的。

    共享 action 对象本身没问题：argparse 的 ``parents`` 机制做的就是这件事，
    而全局选项的默认值是 ``SUPPRESS``，挂上去不会反过来覆盖父级已解析的值。
    """
    if global_parser is None:
        return
    taken = {flag for option in command.options for flag in option.flags}
    for action in global_parser._actions:
        if isinstance(action, argparse._HelpAction):
            continue
        if taken & set(action.option_strings):
            continue
        parser._add_action(action)


def _add_command(
    parser: argparse.ArgumentParser,
    command: Command,
    prefix: str,
    global_parser: argparse.ArgumentParser | None = None,
) -> None:
    """把一个 Command 挂到 parser 的 subparsers 上，并递归处理子命令。

    ``prefix`` 一路往下传，是为了让报错能显示**完整命令路径**（``approvals allow add``），
    而不是只显示末级名字（``add``）——后者在 74 个命令的规模下等于没说。
    """
    subparsers = parser.add_subparsers(
        dest=f"_{command.name}_command",
        metavar="{" + ",".join(sub.name for sub in command.subcommands) + "}",
    )
    for sub in command.subcommands:
        path = f"{prefix} {sub.name}"
        child = subparsers.add_parser(
            sub.name,
            help=sub.help,
            description=sub.description or sub.help,
            epilog=sub.epilog or None,
            aliases=list(sub.aliases),
        )
        child.set_defaults(_command=sub, _command_path=path)
        _attach_globals(child, sub, global_parser)
        for option in sub.options:
            option.add(child)
        if sub.subcommands:
            _add_command(child, sub, path, global_parser)


def build_parser() -> argparse.ArgumentParser:
    """编译整个命令树。每次调用都重新编译，测试里可以安全地反复调用。"""
    global_parser = _global_options()
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=DESCRIPTION,
        parents=[global_parser],
        epilog=_grouped_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", "-V", action="version", version=f"orbit {orbit_version()}")
    subparsers = parser.add_subparsers(
        dest="command",
        metavar="{" + ",".join(command.name for command in REGISTRY.all()) + "}",
        title="命令",
    )
    for command in REGISTRY.all():
        child = subparsers.add_parser(
            command.name,
            help=command.help,
            description=command.description or command.help,
            epilog=command.epilog or None,
            aliases=list(command.aliases),
            parents=[global_parser],
        )
        child.set_defaults(_command=command, _command_path=command.name)
        for option in command.options:
            option.add(child)
        if command.subcommands:
            _add_command(child, command, command.name, global_parser)
    return parser


def _grouped_epilog() -> str:
    """按归类列出命令，方便人读；``--help`` 顶部的 argparse 列表保持字母无关的登记序。"""
    lines = ["命令归类："]
    for key, label in GROUPS.items():
        names = [command.name for command in REGISTRY.by_group(key)]
        if names:
            lines.append(f"  {label}")
            lines.append(f"    {' '.join(names)}")
    return "\n".join(lines)


def _select(argv: Sequence[str]) -> Command | None:
    """解析出要执行哪条命令（支持两级）。"""
    try:
        return REGISTRY.get(argv[0])
    except IndexError:
        return None


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口。返回进程退出码。"""
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not arguments:
        parser.print_help()
        return 1
    args = parser.parse_args(arguments)
    command: Command | None = getattr(args, "_command", None)
    if command is None and getattr(args, "oneshot", None):
        # `orbit -z "prompt"` 等价于 `orbit chat -z "prompt"`。
        command = REGISTRY.get("chat")
        if command is None:  # chat 模块没被导入（不该发生，防御一下）
            print("orbit: oneshot 模式不可用（chat 命令未注册）", file=sys.stderr)
            return 1
        args._command = command
        args._command_path = "chat"
    if command is None:
        parser.print_help()
        return 1

    from pathlib import Path

    raw_dir = getattr(args, "data_dir", None)
    data_dir = Path(raw_dir).expanduser() if raw_dir else None
    context = Context(
        json_output=bool(getattr(args, "json", False)),
        quiet=bool(getattr(args, "quiet", False)),
        data_dir=data_dir,
    )
    code = 0
    try:
        if command.needs_settings:
            context.settings.prepare()
        # 所有命令都先水合配置：密钥在系统 keyring 里，不读就永远是"未配置"。
        context.hydrate()
        if command.needs_store:
            context.require_db()
        if command.handler is None:
            # 只有子命令的父命令被单独调用：打印它自己的帮助，按"缺参数"返回 1。
            _print_command_help(parser, arguments[0])
            return 1
        code = command.handler(context, args) or 0
    except CliError as exc:
        _report(context, getattr(args, "_command_path", command.name), str(exc))
        code = exc.code
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        code = 130
    finally:
        if code == 0:
            context.finish()
        context.close()
    return code


def _print_command_help(parser: argparse.ArgumentParser, name: str) -> None:
    """打印某个命令的帮助而不退出进程。"""
    for action in parser._actions:  # noqa: SLF001 —— argparse 没给公开的 subparser 访问器
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict) and name in choices:
            choices[name].print_help()
            return
    parser.print_help()


def _report(context: Context, path: str, message: str) -> None:
    """统一错误出口：json 模式给结构化错误，否则给人读的一行。"""
    if context.json:
        import json

        print(
            json.dumps(
                {"ok": False, "command": path, "error": message},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"orbit {path}: {message}", file=sys.stderr)


def run(argv: Sequence[str] | None = None) -> Any:
    """给测试用的别名（不吞 SystemExit）。"""
    return main(argv)


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    raise SystemExit(main())
