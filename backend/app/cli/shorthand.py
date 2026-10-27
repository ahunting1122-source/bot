"""把 ``hermes <cmd> --help`` 的用法行压缩成声明式选项字符串。

动机：Hermes 的命令面有 **79 个顶层命令 + 约 250 条子命令 + 上千个选项**。
如果每条命令都手写 ``Command(...)`` 与 ``Option(...)``，代码量会淹掉真正的逻辑，
而且没法逐条核对"有没有抄漏"。所以选项用**用法行同构**的短字符串声明：

    "--name NAME"        → 带值的选项（metavar=NAME）
    "--all"              → 开关（store_true）
    "--mode {a,b}"       → 带候选值的选项
    "-f"                 → 短开关（与 "--force" 合写成 "-f, --force"）
    "schedule"           → 必填位置参数
    "[prompt]"           → 可选位置参数（nargs="?"）

这套语法与 argparse 的 usage 行一一对应，抄写时几乎不需要思考，
漏抄也容易看出来（usage 行里有、短字符串里没有）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.cli.spec import Option

#: ``--flag {a,b,c}`` / ``--flag VALUE`` / ``--flag``
_OPTION = re.compile(r"^(?P<flags>(?:-{1,2}[A-Za-z0-9][A-Za-z0-9_-]*)(?:\s*,\s*-{1,2}[A-Za-z0-9][A-Za-z0-9_-]*)*)"
                     r"(?:\s+(?P<value>\{[^}]*\}|[A-Z][A-Z0-9_]*))?$")
#: ``[name]`` 可选位置参数
_OPTIONAL_POSITIONAL = re.compile(r"^\[(?P<name>[A-Za-z][A-Za-z0-9_-]*)\]$")
#: ``name`` 必填位置参数
_POSITIONAL = re.compile(r"^(?P<name>[A-Za-z][A-Za-z0-9_-]*)$")


@dataclass(frozen=True)
class ParsedOption:
    """短字符串解析结果。``Option`` 由 :func:`to_option` 再生成。"""

    flags: tuple[str, ...]
    takes_value: bool
    metavar: str | None
    choices: tuple[str, ...] | None
    positional: bool
    required: bool


def parse_shorthand(text: str) -> ParsedOption:
    """解析一条短字符串。看不懂就抛 ``ValueError``——**不允许静默跳过**，
    否则抄漏的选项会静默消失，``--help`` 看起来正常但功能缺失。"""
    raw = text.strip()
    if not raw:
        raise ValueError("空的选项声明")

    optional_positional = _OPTIONAL_POSITIONAL.match(raw)
    if optional_positional:
        name = optional_positional.group("name")
        return ParsedOption(
            flags=(name,), takes_value=True, metavar=None, choices=None,
            positional=True, required=False,
        )

    option = _OPTION.match(raw)
    if option:
        flags = tuple(part.strip() for part in option.group("flags").split(","))
        value = option.group("value")
        if value is None:
            return ParsedOption(
                flags=flags, takes_value=False, metavar=None, choices=None,
                positional=False, required=True,
            )
        if value.startswith("{"):
            choices = tuple(item.strip() for item in value.strip("{}").split(",") if item.strip())
            return ParsedOption(
                flags=flags, takes_value=True, metavar=None, choices=choices,
                positional=False, required=True,
            )
        return ParsedOption(
            flags=flags, takes_value=True, metavar=value, choices=None,
            positional=False, required=True,
        )

    positional = _POSITIONAL.match(raw)
    if positional:
        return ParsedOption(
            flags=(positional.group("name"),), takes_value=True, metavar=None,
            choices=None, positional=True, required=True,
        )

    raise ValueError(f"无法解析的选项声明：{text!r}")


def to_option(text: str, help_text: str = "") -> Option:
    """短字符串 → :class:`app.cli.spec.Option`。"""
    parsed = parse_shorthand(text)
    if parsed.positional:
        name = parsed.flags[0]
        return Option(
            (name,),
            help_text,
            metavar=name.upper(),
            nargs=None if parsed.required else "?",
        )
    if parsed.choices is not None:
        return Option(parsed.flags, help_text, choices=parsed.choices)
    if parsed.takes_value:
        return Option(parsed.flags, help_text, metavar=parsed.metavar or "VALUE")
    return Option(parsed.flags, help_text, action="store_true")


def to_options(items: list[str] | tuple[str, ...]) -> list[Option]:
    """批量转换。支持 ``"--flag 说明文字"`` 的写法吗？不支持——帮助文本走单独字段，
    免得在短字符串里猜"哪一段是说明"。"""
    return [to_option(item) for item in items]
