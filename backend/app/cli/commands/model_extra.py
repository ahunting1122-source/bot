"""模型适配层的**运行时命令**：per-model 路由 / 推理线格式 / 辅助槽位 / MoA 执行。

## 为什么不另立顶层命令

`orbit model` 与 `orbit moa` 已经由 `model.py` 登记过，`hermes_parity` 的声明表里
同名条目也会被自动跳过（见 `_table.register_specs`）。再 `REGISTRY.add` 一个 `model`
会直接抛"命令重复登记"。所以本模块只做两件事：

1. `register_handler(path, handler)`：声明表里**已经写了**这条路径、只是还没实现时，
   把 handler 填进去（`_table.HANDLERS` 机制）；
2. `parent.subcommands.append(...)`：父命令已存在、但表里压根没有这条子命令时，
   把它作为子命令挂上去——`spec.py` 的 docstring 明确说子命令列表本来就是
   "分步拼装"的（`config` 先登记、`config get` 后补）。

两条路径都写进 `WIRING`，测试可以断言"到底接上了没有、用的是哪种方式"。

## 导入位置

本模块**必须在 `model` 之后**导入（它要 `REGISTRY.get("model")` 拿到父节点；
导入本模块时会顺带 import `model`，所以这一点由 Python 的模块缓存保证）。
若要 `register_handler` 生效，则必须在 `hermes_parity` **之前**导入——与其它
功能域模块同一条规则。
"""

from __future__ import annotations

from typing import Any

from app import aux_slots as aux
from app import moa, reasoning
from app import model_router as router
from app.cli.commands._table import register_handler
from app.cli.commands.model import (
    MOA_DEFAULT_KEY,
    MOA_PRESETS_KEY,
    _runtime_settings,
    _write_runtime,
)
from app.cli.context import CliError, Context
from app.cli.spec import REGISTRY, Command, Handler, Option

#: 路径 → 接线方式（`appended` / `claimed` / `deferred`）。测试与排障都用它。
WIRING: dict[str, str] = {}


def _attach(path: str, command: Command) -> str:
    """把一条命令挂进注册表，返回实际采用的接线方式。

    三种情形都要处理，因为导入顺序不由本模块决定：

    - `claimed`：父节点在、该子命令也在（声明表里已登记但没实现）→ 直接换 handler，
      顺手把状态改成 `done`；
    - `appended`：父节点在、该子命令不在 → 追加（子命令列表本来就能分步拼装）；
    - `deferred`：父节点都还没登记 → 交给 `_table.HANDLERS`，等声明表组装时填进去。
    """
    head, _, tail = path.partition(" ")
    parent = REGISTRY.get(head)
    if parent is None:
        if command.handler is None:
            raise ValueError(f"{path}：没有 handler，无法延迟接线")
        register_handler(path, command.handler)
        WIRING[path] = "deferred"
        return "deferred"
    existing = REGISTRY.resolve_child(parent, tail) if tail else parent
    if existing is not None:
        if command.handler is not None:
            existing.handler = command.handler
            existing.state = "done"
        WIRING[path] = "claimed"
        return "claimed"
    parent.subcommands.append(command)
    WIRING[path] = "appended"
    return "appended"


# ── model routing ──────────────────────────────────────────────────────────


def run_routing(context: Context, args) -> int:
    """给定模型名，说明它会走哪家、哪个端点、有哪些能力。

    这是 MATRIX 4.3 的用户面：反查有歧义时**不猜**，直接告诉用户去显式指定。
    """
    name = (
        getattr(args, "route", None) or getattr(args, "model", None) or ""
    ).strip() or str(context.settings.model or "")
    if not name:
        raise CliError(
            "用法：`orbit model routing <模型名>`（或先 `orbit model <provider>/<模型>` 设主模型）"
        )
    decision = router.route(name, context.settings, data_dir=context.settings.data_dir)
    if not decision.supported:
        raise CliError(f"`{name}` 无法路由：{decision.reason}")
    context.out(f"模型：{decision.model}")
    context.out(f"provider：{decision.provider}（判定依据：{decision.source}）")
    context.out(f"线协议：{decision.transport}")
    context.out(f"端点：{decision.base_url}")
    context.out(f"上下文窗口：{decision.context_window:,} token")
    context.out(
        "能力："
        f"tools={'是' if decision.supports_tools else '否'} · "
        f"reasoning={'是' if decision.supports_reasoning else '否'} · "
        f"vision={'是' if decision.supports_vision else '否'}"
    )
    sources = "、".join(f"{key}={value}" for key, value in decision.capability_sources.items())
    if sources:
        context.note(f"能力来源：{sources}（manual = 手工表，cache = 运行时缓存，factory = 出厂兜底）")
    context.emit({"ok": True, **decision.to_dict()})
    return 0


# ── model reasoning ────────────────────────────────────────────────────────


def run_reasoning(context: Context, args) -> int:
    """列出/查看各家推理线格式（MATRIX 4.4 的用户面）。"""
    provider = (getattr(args, "provider", None) or "").strip().lower()
    level = (getattr(args, "level", None) or "").strip().lower()

    if provider:
        try:
            info = reasoning.describe(provider)
        except reasoning.NotSupported as exc:
            raise CliError(str(exc)) from exc
        context.out(f"provider：{info['provider']}")
        context.out(f"响应落点：{info['field']}")
        context.out(f"可用档位：{' / '.join(info['levels'])}")
        context.out(f"注意：{info['note']}")
        payload: dict[str, Any] = {"ok": True, "provider": info["provider"], "info": info}
        if level:
            try:
                request = reasoning.to_request(level, provider)
            except ValueError as exc:
                raise CliError(str(exc)) from exc
            context.out()
            context.out(f"level={level} 时该发的请求参数：")
            context.out(f"  extra_body：{request['extra_body']}")
            context.out(f"  顶层字段：{request['top_level']}")
            payload["request"] = request
        context.emit(payload)
        return 0

    rows = []
    for name in sorted(reasoning.TRANSLATORS):
        info = reasoning.describe(name)
        rows.append((info["provider"], info["field"], " / ".join(info["levels"])))
    context.out("各家推理线格式（响应落点 / 请求参数落点逐家不同，调研 06 §3.5）")
    context.out()
    context.table(("provider", "响应落点", "可用档位"), rows)
    context.out()
    context.note("看细节：`orbit model --reasoning-provider deepseek`")
    context.note("看请求参数：`orbit model --reasoning-provider deepseek --reasoning-level high`")
    context.emit(
        {"ok": True, "translators": {name: reasoning.describe(name) for name in reasoning.TRANSLATORS}}
    )
    return 0


# ── model aux ──────────────────────────────────────────────────────────────


def _set_slot(context: Context, spec: str, args) -> int:
    """`--set summary=deepseek/deepseek-v4-flash`：写进 settings.json 的 `auxiliary`。"""
    name, _, target = spec.partition("=")
    name = name.strip().lower()
    target = target.strip()
    if not name or not target:
        raise CliError("用法：`--set <槽位>=<provider>[/<模型>]`，如 `--set title=deepseek/deepseek-v4-flash`")
    if name not in aux.SLOTS:
        raise CliError(f"未知槽位 `{name}`（可用：{' / '.join(aux.SLOTS)}）")
    provider, _, model = target.partition("/")
    entry: dict[str, Any] = {"provider": provider.strip().lower(), "model": model.strip()}
    timeout = getattr(args, "aux_timeout", None)
    if timeout is not None:
        try:
            entry["timeout"] = int(timeout)
        except (TypeError, ValueError) as exc:
            raise CliError(f"--aux-timeout 需要整数，收到：{timeout!r}") from exc
    effort = (getattr(args, "reasoning_effort", None) or "").strip()
    if effort:
        entry["reasoning_effort"] = effort
    # 读-改-写整个 `auxiliary` 表：只写一个键会把其它槽位的配置抹掉。
    table = _runtime_settings(context).get("auxiliary")
    table = dict(table) if isinstance(table, dict) else {}
    table[name] = entry
    path = _write_runtime(context, "auxiliary", table)
    context.out(f"已设置槽位 `{name}` → {provider.strip()}/{model.strip() or '（该家默认）'}")
    context.out(f"  写入：{path}")
    context.emit({"ok": True, "slot": name, "config": entry, "path": str(path)})
    return 0


def _unset_slot(context: Context, name: str) -> int:
    name = (name or "").strip().lower()
    data = _runtime_settings(context)
    table = data.get("auxiliary")
    if not isinstance(table, dict) or name not in table:
        raise CliError(f"槽位 `{name}` 本来就没配（当前是继承主模型）")
    table.pop(name)
    path = _write_runtime(context, "auxiliary", table)
    context.out(f"已取消槽位 `{name}` 的专用配置，回退为继承主模型")
    context.out(f"  写入：{path}")
    context.emit({"ok": True, "slot": name, "unset": True, "path": str(path)})
    return 0


def run_aux(context: Context, args) -> int:
    """列出辅助槽位 / 设置 / 取消 / 估算成本（MATRIX 4.9 的用户面）。"""
    if getattr(args, "set", None):
        return _set_slot(context, args.set, args)
    if getattr(args, "unset", None):
        return _unset_slot(context, args.unset)

    if getattr(args, "cost", None):
        tokens = getattr(args, "tokens", None)
        if tokens is None:
            raise CliError("`--cost` 需要 `--tokens N`（总 token 数，或 input:output）")
        try:
            parsed: Any = (
                {k: int(v) for k, v in (part.split(":") for part in str(tokens).split(","))}
                if ":" in str(tokens)
                else int(tokens)
            )
        except (TypeError, ValueError) as exc:
            raise CliError(f"--tokens 需要整数或 input:output，收到：{tokens!r}") from exc
        try:
            estimate = aux.estimate_cost(args.cost, parsed, context.settings)
        except ValueError as exc:
            raise CliError(str(exc)) from exc
        context.out(f"槽位：{estimate.slot} → {estimate.provider}/{estimate.model}")
        context.out(
            f"估算：输入 {estimate.input_tokens:,} token × ${estimate.input_price}/M + "
            f"输出 {estimate.output_tokens:,} token × ${estimate.output_price}/M"
        )
        context.out(f"      ≈ ${estimate.usd:.6f}")
        if estimate.inherited:
            context.out("注意：该槽位**未单独配置**，跑的是主模型（inherited=True）")
        if not estimate.priced:
            context.warn(estimate.note)
        context.note("单价来自公开标价（models.dev 快照），会变；只用于量级判断，不是账单。")
        context.emit({"ok": True, **estimate.to_dict()})
        return 0

    rows = []
    for slot in aux.resolve_all(context.settings):
        rows.append(
            (
                slot.slot,
                slot.label() or "（未设）",
                "继承（未配置）" if slot.inherited else "专用",
                str(slot.timeout),
                slot.purpose,
            )
        )
    context.out("辅助模型槽位（压缩/摘要/标题/分类不该烧主模型）")
    context.out()
    context.table(("槽位", "模型", "来源", "超时(s)", "用途"), rows)
    context.out()
    context.note("设置：`orbit model --aux-set title=deepseek/deepseek-v4-flash`")
    context.note("取消：`orbit model --aux-unset title`")
    context.note("估成本：`orbit model --aux-cost compression --aux-tokens 20000`")
    context.emit(
        {"ok": True, "slots": [slot.to_dict() for slot in aux.resolve_all(context.settings)]}
    )
    return 0


# ── moa run ────────────────────────────────────────────────────────────────


def _load_plan(context: Context, name: str) -> moa.MoAPlan:
    presets = _runtime_settings(context).get(MOA_PRESETS_KEY)
    presets = presets if isinstance(presets, dict) else {}
    if not presets:
        raise CliError(
            "还没有任何 MoA 预设。先建一个：`orbit moa configure default "
            "--reference deepseek/deepseek-v4-flash --aggregator openai/gpt-5.4`"
        )
    preset_name = name or str(_runtime_settings(context).get(MOA_DEFAULT_KEY) or "default")
    preset = presets.get(preset_name)
    if not isinstance(preset, dict):
        raise CliError(f"没有名为 `{preset_name}` 的预设（现有：{', '.join(sorted(presets))}）")
    if not preset.get("enabled", True):
        raise CliError(f"预设 `{preset_name}` 已停用（`orbit moa configure {preset_name}` 重新启用）")
    try:
        return moa.plan_from_preset(preset, preset_name)
    except ValueError as exc:
        raise CliError(str(exc)) from exc


def run_moa_run(context: Context, args) -> int:
    """真正执行一次 MoA：N 个 proposer 并发作答 → aggregator 汇总。

    之前 `orbit moa` 只存配置不执行（`model.py` 的 `run_moa_list` 里那句
    "运行时聚合未实现"就是这个现状），本命令把它接上。
    """
    plan = _load_plan(context, (getattr(args, "preset", None) or "").strip())
    budget = getattr(args, "budget", None)
    if budget is not None:
        try:
            plan = moa.MoAPlan(
                proposers=plan.proposers,
                aggregator=plan.aggregator,
                max_tokens=plan.max_tokens,
                budget_tokens=int(budget),
                name=plan.name,
            )
        except (TypeError, ValueError) as exc:
            raise CliError(f"--budget 需要正整数，收到：{budget!r}") from exc

    prompt = (getattr(args, "prompt", None) or "").strip()
    if getattr(args, "dry_run", False):
        context.out(f"预设：{plan.name}（dry-run：只展示计划，不发起调用）")
        context.table(
            ("角色", "模型", "temperature", "上限(token)"),
            [
                *[("proposer", item.label(), str(item.temperature), str(item.max_tokens or plan.max_tokens))
                  for item in plan.proposers],
                ("aggregator", plan.aggregator.label(), str(plan.aggregator.temperature), str(plan.max_tokens)),
            ],
        )
        context.out()
        context.note(f"每槽实际上限：{moa.per_slot_cap(plan)} token（总预算 {plan.budget_tokens or '不限'}）")
        context.emit({"ok": True, "dry_run": True, "plan": plan.to_dict()})
        return 0

    if not prompt:
        raise CliError(
            "用法：`orbit moa run <提示词> [--preset 名字] [--budget N] [--dry-run]`"
        )
    try:
        result = context.run(moa.run(plan, prompt, settings=context.settings))
    except moa.MoAFailed as exc:
        raise CliError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 —— 模型层异常统一转成可读的 CLI 报错
        raise CliError(f"MoA 执行失败：{exc}") from exc

    if result.missing:
        context.warn(f"有 {len(result.missing)} 个 proposer 未作答：{', '.join(result.missing)}")
    context.out(result.text)
    context.out()
    context.note(
        f"aggregator：{result.aggregator} · 用量约 {result.used_tokens} token"
        + ("（受预算限制已截断）" if result.budget_limited else "")
    )
    context.emit({"ok": True, **result.to_dict()})
    return 0


# ── 接线 ────────────────────────────────────────────────────────────────────

_ATTACHED = False

#: `orbit model` 上新加的开关。**为什么不加子命令**：`model` 已经有一个位置参数
#: `selection`（`orbit model deepseek/deepseek-chat`），argparse 会把第一个词先喂给
#: 它——`orbit model routing` 会被解析成 `selection="routing"`，子命令永远走不到
#: （实测：`invalid choice: 'gpt-5.4'`）。所以这一族只能用开关挂在 `model` 上，
#: 再由 `_dispatch` 前置拦截。
_MODEL_OPTIONS: list[Option] = [
    Option(
        ("--route",),
        "查看某个模型会走哪家 provider / 哪个端点 / 有哪些能力（不给值则用当前主模型）",
        metavar="MODEL",
        dest="route",
        nargs="?",
        const="",
    ),
    Option(
        ("--reasoning",),
        "查看各家推理线格式（响应字段 / 请求参数落点）",
        action="store_true",
    ),
    Option(
        ("--reasoning-provider",),
        "只看这一家的推理格式（deepseek / anthropic / gemini / openai …）",
        metavar="ID",
        dest="provider",
    ),
    Option(
        ("--reasoning-level",),
        "同时展示该档位要发的请求参数（low / medium / high / off）",
        metavar="LEVEL",
        dest="level",
    ),
    Option(("--aux",), "查看辅助模型槽位（压缩 / 摘要 / 标题 / 分类）", action="store_true"),
    Option(
        ("--aux-set",),
        "设置槽位，如 `--aux-set title=deepseek/deepseek-v4-flash`",
        metavar="SLOT=TARGET",
        dest="set",
    ),
    Option(
        ("--aux-unset",),
        "取消槽位的专用配置（回退为继承主模型）",
        metavar="SLOT",
        dest="unset",
    ),
    Option(
        ("--aux-cost",),
        "估算某个槽位一次调用的花费（配合 --aux-tokens）",
        metavar="SLOT",
        dest="cost",
    ),
    Option(
        ("--aux-tokens",),
        "配合 --aux-cost：总 token 数，或 input:output",
        metavar="N",
        dest="tokens",
    ),
    Option(
        ("--aux-timeout",),
        "配合 --aux-set：该槽位超时秒数",
        metavar="SECONDS",
        dest="aux_timeout",
    ),
]


def _wants_aux(args) -> bool:  # noqa: ANN001
    return any(
        (
            getattr(args, "aux", False),
            getattr(args, "set", None),
            getattr(args, "unset", None),
            getattr(args, "cost", None),
        )
    )


def _dispatch(original) -> Handler:  # noqa: ANN001
    """包住 `model` 原来的 handler：认我们的开关，其余原样交给 `run_model`。

    **只前置拦截，不替换语义**：`orbit model deepseek/x`、`--list`、`--refresh`
    的既有行为一个字都不变（`run_model` 还是 `model.py` 里那个函数）。
    """

    def handler(context: Context, args) -> int:
        if getattr(args, "route", None) is not None:
            return run_routing(context, args)
        if (
            getattr(args, "reasoning", False)
            or getattr(args, "provider", None)
            or getattr(args, "level", None)
        ):
            return run_reasoning(context, args)
        if _wants_aux(args):
            return run_aux(context, args)
        if original is None:
            raise CliError("`orbit model` 没有可用实现（注册表里 handler 为空）")
        return original(context, args)

    return handler


def _attach_model_flags() -> str:
    """把四个运行时能力挂到已存在的 `model` 命令上（开关 + handler 前置拦截）。"""
    parent = REGISTRY.get("model")
    if parent is None:
        # 父命令还没登记（导入顺序被排到了 `model` 之前）→ 明确失败，不要静默丢功能。
        raise RuntimeError(
            "`model_extra` 必须在 `app.cli.commands.model` 之后导入"
            "（它要挂到已存在的 `model` 命令上）"
        )
    if any(option.flags[0] == "--route" for option in parent.options):
        WIRING["model flags"] = "already"
        return "already"
    parent.options.extend(_MODEL_OPTIONS)
    parent.handler = _dispatch(parent.handler)
    WIRING["model flags"] = "flags"
    return "flags"


def attach() -> dict[str, str]:
    """接线：`model` 加开关、`moa` 加 `run` 子命令。幂等（重复调用不会挂两遍）。"""
    global _ATTACHED
    if _ATTACHED:
        return dict(WIRING)
    _attach_model_flags()
    # `moa` 没有位置参数（只有子命令），所以 `orbit moa run <prompt>` 能正常解析。
    _attach(
        "moa run",
        Command(
            name="run",
            help="执行一次 MoA（N 个 proposer 并发作答 → aggregator 汇总）",
            handler=run_moa_run,
            group="core",
            parity="P1",
            state="done",
            options=[
                Option(("prompt",), "提示词", nargs="?"),
                Option(("--preset",), "用哪个预设（默认取 moa_default_preset）", metavar="NAME"),
                Option(("--budget",), "整批总预算（token），0 = 不限", metavar="N"),
                Option(("--dry-run",), "只展示计划，不发起调用", action="store_true"),
            ],
        ),
    )
    _ATTACHED = True
    return dict(WIRING)


attach()


__all__ = [
    "WIRING",
    "attach",
    "run_aux",
    "run_moa_run",
    "run_reasoning",
    "run_routing",
]
