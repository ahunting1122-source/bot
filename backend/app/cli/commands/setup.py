"""`orbit setup` —— 首次配置向导。

为什么需要一个向导而不是只给 `config set`：Orbit 的配置分布在**三个平面**
（`backend/.env` 环境变量、`data/settings.json` 运行时键、系统 keyring 密钥），
新用户不知道哪个键该写哪里，也不知道"密钥绝不进配置文件"这条规矩。
向导的价值就是把这三者按正确的落点写一遍，并在写之前把默认值摆出来。

三条硬规矩：

1. **非交互环境不许假装在交互**。`stdin` 不是 TTY 时直接拒绝并给出等价命令，
   而不是 `input()` 拿到 EOF 后一路用默认值把配置写歪。
2. **密钥只走 keyring，且不回显**。用 `getpass` 读，读完只说"已保存（末 4 位）"。
3. **不实现的 section 要明说**。Hermes 的 setup 有 7 个 section，Orbit 现在只有
   `model` / `terminal` / `agent` 有意义；其余明确报"未实现 + 原因"，不留一个
   "跑完了但什么也没配"的空壳。
"""

from __future__ import annotations

import getpass
import json
import sys
from pathlib import Path
from typing import Any

from app.cli.context import CliError, Context
from app.cli.spec import REGISTRY, Command, Option

#: 有真实现的 section。键是命令行取值，值是中文标题。
SECTIONS: dict[str, str] = {
    "model": "模型与供应商",
    "terminal": "权限级别与命令执行",
    "agent": "审批模式与预算",
}

#: Hermes 有、Orbit 暂未实现的 section → 一句能行动的原因。
UNIMPLEMENTED: dict[str, str] = {
    "tts": "语音合成层未实现（Orbit 目前没有 TTS 工具）",
    "gateway": "消息网关层未实现（属 W9 平台层）",
    "tools": "per-platform 工具集配置未接线（见 `orbit tools`，W4）",
    "telemetry": "遥测/OTLP 导出未实现（Orbit 的 monitoring 是本地只读，不外发）",
}


def _is_interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _ask(prompt: str, default: str = "", *, secret: bool = False) -> str:
    """读一个值。空输入用默认值。secret=True 时不回显、也不进日志。"""
    suffix = f" [{default}]" if default else ""
    try:
        if secret:
            value = getpass.getpass(f"{prompt}{suffix}: ")
        else:
            value = input(f"{prompt}{suffix}: ")
    except EOFError as exc:
        raise CliError(
            "读不到输入（非交互环境）。请改用 --non-interactive，"
            "或直接用 `orbit config set <键> <值>`。"
        ) from exc
    value = value.strip()
    return value or default


def _write_runtime(context: Context, updates: dict[str, Any]) -> Path:
    path = context.settings_path
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    data.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _keyring_store(context: Context, account: str, value: str) -> bool:
    """把密钥写进系统 keyring。写不进去返回 False（调用方如实报出来）。"""
    try:
        import keyring
    except Exception:
        return False
    service = f"orbit:{context.data_dir.resolve()}"
    try:
        keyring.set_password(service, account, value)
    except Exception:
        return False
    return True


def _section_model(context: Context, *, interactive: bool, quick: bool) -> list[str]:
    from app.providers import PROVIDERS

    current = context.settings
    changed: list[str] = []
    if not interactive:
        if not current.model.strip():
            raise CliError(
                "非交互模式下模型名未配置。请先 `orbit config set model <名称>`，"
                "或带 --non-interactive 之前用环境变量 ORBIT_MODEL 指定。"
            )
        return changed

    names = ", ".join(sorted(PROVIDERS))
    provider = _ask(f"供应商（{names}）", current.model_provider or "deepseek")
    if provider not in PROVIDERS:
        raise CliError(f"未知供应商：{provider}（可选：{names}）")
    profile = PROVIDERS[provider]
    base_url = _ask("API 地址", current.model_base_url or profile.default_base_url)
    model = _ask("模型名", current.model or profile.default_model)
    updates: dict[str, Any] = {
        "model_provider": provider,
        "model_base_url": base_url,
        "model": model,
    }
    path = _write_runtime(context, updates)
    changed.extend(f"{key}={value}" for key, value in updates.items())
    changed.append(f"→ {path}")

    if not quick or not current.model_api_key.get_secret_value():
        key = _ask(f"{provider} 的 API Key（留空跳过）", secret=True)
        if key:
            if _keyring_store(context, "model_api_key", key):
                tail = key[-4:] if len(key) >= 4 else "***"
                changed.append(f"密钥已存入系统 keyring（…{tail}）")
            else:
                raise CliError(
                    "系统 keyring 不可用，密钥没有保存。请配置一个可用的密钥后端后重试"
                    "（Orbit 刻意不把密钥写进配置文件）。"
                )
    return changed


def _section_terminal(context: Context, *, interactive: bool, quick: bool) -> list[str]:
    current = context.settings
    if not interactive:
        return []
    changed: list[str] = []
    scope = _ask("权限级别（workspace=仅工作文件夹 / machine=完全访问）", current.file_scope)
    if scope not in {"workspace", "machine"}:
        raise CliError(f"权限级别只能是 workspace 或 machine，收到：{scope}")
    if scope == "machine":
        confirm = _ask("完全访问档会让 agent 读写整机任意文件并执行任意命令。输入 yes 确认", "")
        if confirm.lower() != "yes":
            scope = "workspace"
            changed.append("未确认，权限级别保持 workspace")
    updates = {
        "file_scope": scope,
        "command_timeout": _ask("命令超时（秒）", str(current.command_timeout)),
    }
    path = _write_runtime(context, updates)
    changed.extend(f"{key}={value}" for key, value in updates.items())
    changed.append(f"→ {path}")
    return changed


def _section_agent(context: Context, *, interactive: bool, quick: bool) -> list[str]:
    from app import approvals

    current_mode = approvals.resolve_mode(context.settings)
    if not interactive:
        return []
    mode = _ask(f"审批模式（{'/'.join(approvals.APPROVAL_MODES)}）", current_mode)
    if mode not in approvals.APPROVAL_MODES:
        raise CliError(f"未知审批模式：{mode}")
    updates: dict[str, Any] = {"approval_mode": mode}
    hardline = _ask(
        "完全访问档是否也守硬地板（yes/no，默认 no，对齐 Orbit 既有产品决策）",
        "yes" if approvals.resolve_hardline_in_machine(context.settings) else "no",
    )
    updates["hardline_in_machine"] = hardline.strip().lower() in {"yes", "y", "1", "true", "on"}
    path = _write_runtime(context, updates)
    changed = [f"{key}={value}" for key, value in updates.items()]
    changed.append(f"→ {path}")
    return changed


_SECTION_HANDLERS = {
    "model": _section_model,
    "terminal": _section_terminal,
    "agent": _section_agent,
}


def run_setup(context: Context, args) -> int:
    section = getattr(args, "section", None)
    non_interactive = bool(getattr(args, "non_interactive", False))
    reset = bool(getattr(args, "reset", False))
    quick = bool(getattr(args, "quick", False))

    if getattr(args, "portal", False):
        raise CliError(
            "Orbit 没有 Nous Portal 集成（那是 Hermes 自家生态的 OAuth 入口）。"
            "用 `orbit setup model` 配置任意 OpenAI 兼容供应商即可。"
        )

    if section and section in UNIMPLEMENTED:
        raise CliError(f"`setup {section}` 未实现：{UNIMPLEMENTED[section]}")
    if section and section not in SECTIONS:
        raise CliError(f"未知 section：{section}（可选：{'、'.join(SECTIONS)}）")

    interactive = not non_interactive
    if interactive and not _is_interactive():
        raise CliError(
            "当前不是交互终端，向导会拿不到输入。请加 --non-interactive（用现有配置/默认值），"
            "或直接用 `orbit config set <键> <值>`。"
        )

    context.out(f"Orbit 配置向导 · {context.data_dir}")
    context.out()

    if reset:
        # `--reset` 是**替换**语义，不是合并：`_write_runtime` 会把已有键保留下来，
        # 用它来做重置只会把旧配置原样写回去（实测踩过）。所以这里直接写空对象。
        path = context.settings_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        context.out(f"已重置运行时配置：{path}")
        context.out()

    targets = [section] if section else list(SECTIONS)
    all_changes: list[str] = []
    for name in targets:
        context.out(f"── {SECTIONS[name]} ──")
        changes = _SECTION_HANDLERS[name](context, interactive=interactive, quick=quick)
        for line in changes:
            context.out(f"  · {line}")
        if not changes:
            context.out("  （无改动）")
        all_changes.extend(changes)
        context.out()

    context.settings.prepare()
    created_db = False
    if not context.db_path.exists():
        try:
            from app.main import migrate

            migrate(context.settings)
            created_db = True
        except Exception as exc:  # 迁移失败要说清楚
            context.out(f"数据库创建失败：{exc}")
    context.out(f"数据目录：{context.data_dir}")
    context.out(f"数据库：{'已创建并迁移' if created_db else '已存在'}")
    context.out()
    context.out("下一步：")
    context.out("  1. orbit doctor          检查配置与依赖")
    context.out("  2. orbit status          看各组件状态")
    context.out("  3. 启动后端：scripts\\start.ps1")
    context.emit(
        {
            "ok": True,
            "sections": targets,
            "changes": all_changes,
            "data_dir": str(context.data_dir),
            "database_created": created_db,
        }
    )
    return 0


REGISTRY.add(
    Command(
        name="setup",
        help="交互式配置向导（模型 / 权限级别 / 审批模式）",
        handler=run_setup,
        group="core",
        state="done",
        options=[
            Option(
                ("section",),
                "只跑某一个 section：model / terminal / agent（tts·gateway·tools·telemetry 未实现）",
                nargs="?",
            ),
            Option(("--non-interactive",), "非交互模式：用现有配置与默认值，不提问", action="store_true"),
            Option(("--reset",), "先把运行时配置重置为空再继续", action="store_true"),
            Option(("--reconfigure",), "重跑完整向导（默认行为，保留该 flag 以对齐 Hermes）", action="store_true"),
            Option(("--quick",), "只补缺失项（如已配置模型就不再问密钥）", action="store_true"),
            Option(("--portal",), "Nous Portal OAuth 一键配置（Orbit 无此集成，会明确报错）", action="store_true"),
        ],
    ),
)
