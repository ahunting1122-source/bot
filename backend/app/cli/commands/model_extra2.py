"""模型适配层第二组运行时命令：能力元数据 / 语音 / 本地代理。

三块功能对应 MATRIX 的三条（4.10 / 4.12 / 4.7），彼此不依赖，所以单独一个模块：

- `model --capabilities`：三级能力元数据（override → cache → snapshot），**每个数字
  都标出处**；
- `model --voice`：11 家 TTS + 8 家 STT 的可用性清单，只有 `command` 通道能离线跑；
- `proxy {status,providers,plan,serve,start}`：本地 OpenAI 兼容代理的计划与启动。

## 接线方式（与 `model_extra.py` 同一套手法）

1. `register_handler(path, handler)` / 直接填 handler：声明表里**已经写了**这条路径
   （`proxy start` / `proxy status` / `proxy providers`），只是状态还是 `todo`；
2. `parent.subcommands.append(...)`：父命令在、但表里没有这条子命令（`proxy plan` /
   `proxy serve`）。`spec.py` 的 docstring 明确说子命令列表本来就是分步拼装的；
3. `parent.options.extend(...)` + 包一层 dispatch：`model` 是**有位置参数**的节点
   （`orbit model deepseek/deepseek-chat`），不能再挂子命令。

两条路径都写进 `WIRING`，测试据此断言"到底接上了没有、用的是哪种方式"。

## 为什么 `model capabilities` 做成了 `--capabilities`

`orbit model` 带一个位置参数 `selection`。argparse 的子解析器是**位置参数**，会把
第一个 token 抢走——实测给 `model` 挂上子命令后，`orbit model deepseek/x` 直接报
`argument {capabilities}: invalid choice`，既有的选模型用法当场坏掉。所以这两条按
仓库既有约定接成开关，与 `--route` / `--reasoning` / `--aux` 完全一致
（见 `model_extra.py` 的 `_attach_model_flags`）。`proxy` 没有位置参数，子命令照常挂。

## 导入位置（重要）

本模块**必须在 `hermes_parity` 之后**导入：它自己 `import hermes_parity`，因为
`proxy` 的子命令要挂到表里已经组装好的父节点上。与 `session_extra.py` 同一档——
"只往已有父节点挂子命令的模块可以排在表组装之后"。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from app import capabilities as cap
from app import providers as pv
from app import proxy as px
from app import voice as vc
from app.cli.commands import hermes_parity  # noqa: F401 —— 需要表先组装好
from app.cli.commands._table import register_handler
from app.cli.context import CliError, Context
from app.cli.spec import REGISTRY, Command, Handler, Option

#: 路径 → 接线方式（`claimed` / `appended` / `deferred` / `flags` / `already`）。
WIRING: dict[str, str] = {}

_ATTACHED = False


def _attach(path: str, command: Command) -> str:
    """把一条命令挂进注册表，返回实际采用的接线方式。

    三种情形都要处理，因为导入顺序不由本模块决定：

    - `claimed`：父节点在、该子命令也在（表里已登记但没实现）→ 直接换 handler，
      顺手把状态改成 `done`；
    - `appended`：父节点在、该子命令不在 → 追加；
    - `deferred`：父节点还没登记 → 交给 `_table.HANDLERS`，等声明表组装时填进去。
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


def _refresh_state(parent: Command) -> None:
    """父节点的状态由子节点汇总（与 `_table.Spec.build` 的口径一致）。

    不刷新的话，`proxy` 会永远显示 `todo`——哪怕它的五个子命令全都 `done`。
    完成度被系统性低估，正是 `skin` 当初踩过的坑。
    """
    if not parent.subcommands:
        return
    states = {child.state for child in parent.subcommands}
    if states == {"done"}:
        parent.state = "done"
    elif states & {"done", "partial"}:
        parent.state = "partial"
    else:
        parent.state = "todo"


# ── 小工具 ──────────────────────────────────────────────────────────────────


def _fmt_value(name: str, value: Any) -> str:
    """能力值的人话格式。`None` 一律显示"未知"——**不许**把它显示成 0 或 False。"""
    if value is None:
        return "未知"
    if isinstance(value, bool):
        return "是" if value else "否"
    if name in {"input_price", "output_price"}:
        return f"${value}/M"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _current_model(context: Context) -> tuple[str, str]:
    """当前 `(模型, provider)`。

    **provider 只认显式配置的那一个**（`Settings.model_provider` / 环境变量）。
    反查出来的 provider 只是"从 base_url 猜的"，用它去查能力表反而会查空：
    `model=deepseek-v4-flash` 配着默认的 OpenAI 端点时，反查得到 `openai`，
    而 openai 的表里没有这个模型。留空交给 `capabilities.resolve()` 按模型名反查
    才是对的。
    """
    settings = context.settings
    model = str(getattr(settings, "model", "") or "").strip()
    provider = str(getattr(settings, "model_provider", "") or "").strip()
    return model, provider


# ── `model --capabilities` ─────────────────────────────────────────────────


def run_model_capabilities(context: Context, args) -> int:
    """打印一个模型的能力表：值 + **每个字段的来源**。

    这张表存在的理由就是"这个数字哪来的"：`context_window=131072` 是用户手填的、
    昨天刷的还是出厂兜底的，可信度完全不同。只给值等于让人赌。
    """
    if getattr(args, "cap_refresh", False):
        try:
            cap.refresh_cache(data_dir=context.settings.data_dir)
        except cap.NetworkUnavailable as exc:
            raise CliError(str(exc)) from exc

    default_model, default_provider = _current_model(context)
    model = (getattr(args, "cap_model", None) or default_model).strip()
    provider = default_provider
    if not model:
        raise CliError(
            "没有模型名。用 `orbit model --capabilities --cap-model <模型名>`，"
            "或先 `orbit model <provider>/<模型>` 设主模型。"
        )

    result = cap.resolve(model, provider, data_dir=context.settings.data_dir)
    context.out(f"模型：{result.model}" + (f"（provider：{result.provider}）" if result.provider else ""))
    context.out()
    rows = [
        (
            field,
            _fmt_value(field, result.value(field)),
            cap.describe_source(result.field_sources.get(field, "unknown")),
        )
        for field in cap.CAPABILITY_FIELDS
    ]
    context.table(("字段", "值", "来源"), rows)
    context.out()
    context.note(f"结论来源：{result.source}（{cap.describe_source(result.source)}）")
    if not result.known:
        context.warn(
            "三层（手工覆盖 / 缓存 / 出厂 snapshot）都没有这个模型的数据。"
            "**不给默认值**：七个字段全部留空。要补就写 `<data>/capabilities.json`"
            "（不需要网络），或用 `orbit model --cap-refresh`（需要联网）。"
        )
    unknown = result.unknown_fields()
    if result.known and unknown:
        context.note(f"仍未知的字段：{'、'.join(unknown)}（同样不猜）")
    context.emit({"ok": True, **result.to_dict()})
    return 0


# ── `model --voice` ────────────────────────────────────────────────────────


def run_model_voice(context: Context, args) -> int:
    """语音层：列 19 家可用性 / 写配置 / 真跑一次命令通道。"""
    data_dir = context.settings.data_dir
    setting = (getattr(args, "voice_set", None) or "").strip()
    if setting:
        return _apply_voice_setting(context, setting)

    rows = vc.describe_all(data_dir=data_dir)
    tts_rows = [row for row in rows if row["kind"] in {"command", "tts"}]
    stt_rows = [row for row in rows if row["kind"] == "stt"]
    context.out("TTS（11 家 + command 通道）")
    context.table(
        ("provider", "可用", "说明"),
        [(str(row["name"]), "是" if row["available"] else "否", str(row["describe"])) for row in tts_rows],
    )
    context.out()
    context.out("STT（8 家）")
    context.table(
        ("provider", "可用", "说明"),
        [(str(row["name"]), "是" if row["available"] else "否", str(row["describe"])) for row in stt_rows],
    )
    context.out()
    available = [row for row in rows if row["available"]]
    if not available:
        context.note(
            "当前没有可用的 provider。本机无网络，19 家里只有 `command` / "
            "`local_command` 能在配好外部命令后离线工作。"
        )

    code = 0
    say = (getattr(args, "voice_say", None) or "").strip()
    if say:
        code = _synthesize_once(context, say, data_dir)
    listen = (getattr(args, "voice_listen", None) or "").strip()
    if listen:
        code = _transcribe_once(context, listen, data_dir)
    context.emit({"ok": code == 0, "providers": rows})
    return code


def _apply_voice_setting(context: Context, setting: str) -> int:
    """`--voice-set key=value`：只认 `voice.json` 里的四个键，别的直接拒绝。"""
    key, _, value = setting.partition("=")
    key = key.strip().lower()
    allowed = {"tts_command", "stt_command", "voice", "timeout"}
    if key not in allowed:
        raise CliError(f"未知语音配置键 `{key}`（可用：{' / '.join(sorted(allowed))}）")
    data_dir = context.settings.data_dir
    current = vc.VoiceConfig.load(data_dir)
    tts_command = current.tts_command
    stt_command = current.stt_command
    voice_name = current.voice
    timeout = current.timeout
    if key == "timeout":
        try:
            timeout = float(value)
        except ValueError as exc:
            raise CliError(f"timeout 需要数字，收到：{value!r}") from exc
    elif key == "tts_command":
        tts_command = value.strip()
    elif key == "stt_command":
        stt_command = value.strip()
    else:
        voice_name = value.strip()
    config = vc.VoiceConfig(
        tts_command=tts_command,
        stt_command=stt_command,
        voice=voice_name,
        timeout=timeout,
    )
    path = vc.save_config(data_dir, config)
    context.out(f"已写入 {path}")
    context.note("命令通道现在可用性：" + ("可用" if config.tts_command or config.stt_command else "未配置"))
    context.emit({"ok": True, "path": str(path), "config": config.to_dict()})
    return 0


def _synthesize_once(context: Context, text: str, data_dir: str | Path) -> int:
    """用 `command` 通道真跑一次合成，把音频落到 `<data>/voice/`。"""
    provider = vc.CommandTts(data_dir=data_dir)
    try:
        audio = provider.synthesize(text)
    except vc.VoiceError as exc:
        raise CliError(str(exc)) from exc
    target = Path(data_dir) / "voice" / f"tts-{int(time.time())}.wav"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(audio)
    context.out(f"已合成 {len(audio)} 字节 → {target}")
    return 0


def _transcribe_once(context: Context, path: str, data_dir: str | Path) -> int:
    """用 `local_command` 通道真跑一次转写。"""
    source = Path(path).expanduser()
    if not source.exists():
        raise CliError(f"找不到音频文件：{source}")
    provider = vc.CommandStt(data_dir=data_dir)
    try:
        text = provider.transcribe(source.read_bytes())
    except vc.VoiceError as exc:
        raise CliError(str(exc)) from exc
    context.out(text)
    return 0


# ── `proxy …` ─────────────────────────────────────────────────────────────


def _proxy_plan_from_args(args) -> px.ProxyPlan:
    """从命令行参数造计划。`--map` 的写法是 `客户端模型=上游模型`（可重复用逗号分隔）。"""
    provider = (getattr(args, "provider", None) or "anthropic").strip().lower()
    host = (getattr(args, "host", None) or px.DEFAULT_HOST).strip() or px.DEFAULT_HOST
    raw_port = getattr(args, "port", None)
    try:
        port = px.DEFAULT_PORT if raw_port is None else max(0, int(raw_port))
    except (TypeError, ValueError):
        port = px.DEFAULT_PORT
    model_map: dict[str, str] = {}
    raw_map = (getattr(args, "map", None) or "").strip()
    for pair in raw_map.split(","):
        pair = pair.strip()
        if not pair:
            continue
        left, _, right = pair.partition("=")
        if not left.strip() or not right.strip():
            raise CliError(f"`--map` 需要 `客户端模型=上游模型`，收到：{pair!r}")
        model_map[left.strip()] = right.strip()
    try:
        return px.plan_for(provider, host=host, port=port, model_map=model_map)
    except px.ProxyError as exc:
        raise CliError(str(exc)) from exc


def run_proxy_status(context: Context, args) -> int:
    """代理状态：每个可用上游的计划 + 端口占用 + **本层没做到什么**。"""
    rows: list[tuple[str, str, str, str]] = []
    for provider in px.SUPPORTED_UPSTREAMS:
        plan = px.plan_for(provider)
        busy = px.port_in_use(plan.listen_host, plan.listen_port)
        rows.append(
            (
                provider,
                plan.transport,
                plan.base_url,
                f"占用（{plan.listen_host}:{plan.listen_port}）" if busy else "空闲",
            )
        )
    context.out(px.describe())
    context.out()
    context.table(("上游", "线协议", "端点", f"端口 {px.DEFAULT_PORT}"), rows)
    context.out()
    context.note("启动：`orbit proxy serve --provider anthropic --port 8645`")
    context.note("看计划：`orbit proxy plan --provider anthropic --map gpt-4o-mini=claude-sonnet-5`")
    context.emit({"ok": True, "upstreams": [row[0] for row in rows]})
    return 0


def run_proxy_providers(context: Context, args) -> int:
    """列出能当代理上游的 provider（只有协议与 OpenAI 不同的那两家）。"""
    rows = []
    for provider in px.SUPPORTED_UPSTREAMS:
        profile = pv.PROVIDERS[provider]
        rows.append(
            (
                profile.id,
                profile.name,
                px.UPSTREAM_TRANSPORTS[provider],
                profile.default_model,
                profile.key_env or "（无）",
            )
        )
    context.table(("provider", "名称", "线协议", "默认模型", "key 环境变量"), rows)
    context.out()
    context.note(
        "其余 14 家本身就是 OpenAI 兼容面，客户端直连即可 —— 套一层代理只是多一次转发。"
    )
    context.emit({"ok": True, "upstreams": [row[0] for row in rows]})
    return 0


def run_proxy_plan(context: Context, args) -> int:
    """只打印计划，不占端口、不发请求（`--json` 下给结构化计划）。"""
    plan = _proxy_plan_from_args(args)
    context.out(plan.describe())
    busy = px.port_in_use(plan.listen_host, plan.listen_port)
    if busy:
        context.warn(
            f"端口 {plan.listen_port} 已被占用：真启动会失败。换 `--port` 或先停掉占用者。"
        )
    context.emit({"ok": True, **plan.to_dict(), "port_in_use": busy})
    return 0


def run_proxy_serve(context: Context, args) -> int:
    """真起服务（**阻塞**）。`proxy start` 与 `proxy serve` 都走这里。"""
    plan = _proxy_plan_from_args(args)
    if px.port_in_use(plan.listen_host, plan.listen_port):
        raise CliError(
            f"端口 {plan.listen_port} 已被占用（{plan.listen_host}）；换一个 `--port`。"
        )
    profile = pv.PROVIDERS[plan.upstream_provider]
    try:
        api_key = pv.resolve_api_key(profile, context.settings)
    except pv.ProviderError:  # 本地端点没有 key，不算错
        api_key = ""
    server = px.ProxyServer(
        plan,
        forward=px.http_forward(plan, api_key=api_key),
        headers=px.auth_headers(plan, api_key),
    )
    context.out(server.describe())
    if not api_key and profile.auth == "api_key":
        context.warn(
            f"{plan.upstream_provider} 需要 API key：`orbit login --provider "
            f"{plan.upstream_provider}`。现在启动的话每个请求都会 401。"
        )
    context.out()
    context.out(f"监听 {plan.url}（Ctrl-C 停止）")
    try:
        server.serve()
    except KeyboardInterrupt:  # pragma: no cover - 交互式中断
        context.note("已停止")
    except OSError as exc:
        raise CliError(f"起服务失败：{exc}") from exc
    return 0


# ── 接线 ────────────────────────────────────────────────────────────────────

#: 挂到 `model` 上的开关。`model` 有位置参数 `selection`，不能挂子命令（见模块
#: docstring）。dest 都带前缀（`cap_` / `voice_`），避免与 `--reasoning-provider`
#: 这类已有开关的 dest 撞车——撞了会让 `--aux` 的分派逻辑误判。
_MODEL_OPTIONS: list[Option] = [
    Option(
        ("--capabilities",),
        "查看模型能力元数据（三级来源 + 每个字段的出处）",
        action="store_true",
    ),
    Option(
        ("--cap-model",),
        "配合 --capabilities：要查的模型名（默认取当前主模型）",
        metavar="MODEL",
        dest="cap_model",
    ),
    Option(
        ("--cap-refresh",),
        "刷新能力缓存（需要联网；本机会明确报错并说明用的是 snapshot）",
        action="store_true",
        dest="cap_refresh",
    ),
    Option(("--voice",), "查看 TTS 11 家 / STT 8 家与 command 通道的可用性", action="store_true"),
    Option(
        ("--voice-say",),
        "用 command 通道合成一段语音（需先配 tts_command）",
        metavar="TEXT",
        dest="voice_say",
    ),
    Option(
        ("--voice-listen",),
        "用 command 通道转写一个音频文件（需先配 stt_command）",
        metavar="PATH",
        dest="voice_listen",
    ),
    Option(
        ("--voice-set",),
        "写 voice.json：`--voice-set tts_command=<命令>`",
        metavar="KEY=VALUE",
        dest="voice_set",
    ),
]


def _wants_capabilities(args) -> bool:  # noqa: ANN001
    return bool(
        getattr(args, "capabilities", False)
        or getattr(args, "cap_model", None)
        or getattr(args, "cap_refresh", False)
    )


def _wants_voice(args) -> bool:  # noqa: ANN001
    return bool(
        getattr(args, "voice", False)
        or getattr(args, "voice_say", None)
        or getattr(args, "voice_listen", None)
        or getattr(args, "voice_set", None)
    )


def _dispatch(original: Handler | None) -> Handler:
    """包住 `model` 现有的 handler：认我们的开关，其余原样交给上游 dispatch。

    **只前置拦截，不替换语义**：`orbit model deepseek/x`、`--list`、`--aux` 的既有
    行为一个字节都不改（最终仍然落到 `model.py` 的 `run_model`）。
    """

    def handler(context: Context, args) -> int:
        if _wants_capabilities(args):
            return run_model_capabilities(context, args)
        if _wants_voice(args):
            return run_model_voice(context, args)
        if original is None:
            raise CliError("`orbit model` 没有可用实现（注册表里 handler 为空）")
        return original(context, args)

    return handler


def _attach_model_flags() -> str:
    """把能力/语音开关挂到已存在的 `model` 命令上。"""
    parent = REGISTRY.get("model")
    if parent is None:
        raise RuntimeError(
            "`model_extra2` 必须在 `app.cli.commands.model` 之后导入"
            "（它要挂到已存在的 `model` 命令上）"
        )
    if any(option.flags[0] == "--capabilities" for option in parent.options):
        WIRING["model flags"] = "already"
        return "already"
    parent.options.extend(_MODEL_OPTIONS)
    parent.handler = _dispatch(parent.handler)
    WIRING["model flags"] = "flags"
    return "flags"


def attach() -> dict[str, str]:
    """接线：`proxy` 的 5 条路径 + `model` 的开关。**幂等**（重复调用不会挂两遍）。"""
    global _ATTACHED
    if _ATTACHED:
        return dict(WIRING)
    _attach_model_flags()

    # 声明表里已有、状态 todo 的三条：直接接管。
    _attach("proxy status", Command(name="status", help="查看哪些上游已就绪", handler=run_proxy_status))
    _attach(
        "proxy providers",
        Command(name="providers", help="列出可用的代理上游供应商", handler=run_proxy_providers),
    )
    # `proxy start` 是表里的名字（前台运行代理）；`serve` 是 Orbit 的别名，两者同一实现。
    _attach(
        "proxy start",
        Command(name="start", help="前台运行代理", handler=run_proxy_serve),
    )
    _attach(
        "proxy serve",
        Command(
            name="serve",
            help="前台运行代理（与 start 同一实现）",
            handler=run_proxy_serve,
            group="ops",
            parity="P1",
            state="done",
            options=[
                Option(("--provider",), "上游 provider（anthropic / gemini）", metavar="PROVIDER"),
                Option(("--host",), "监听地址（默认 127.0.0.1）", metavar="HOST"),
                Option(("--port",), "监听端口（默认 8645，0 = 系统分配）", metavar="PORT"),
                Option(
                    ("--map",),
                    "模型映射，`客户端模型=上游模型`（逗号分隔）",
                    metavar="PAIRS",
                ),
            ],
        ),
    )
    _attach(
        "proxy plan",
        Command(
            name="plan",
            help="只打印代理计划（不占端口、不发请求）",
            handler=run_proxy_plan,
            group="ops",
            parity="P1",
            state="done",
            options=[
                Option(("--provider",), "上游 provider（anthropic / gemini）", metavar="PROVIDER"),
                Option(("--host",), "监听地址（默认 127.0.0.1）", metavar="HOST"),
                Option(("--port",), "监听端口（默认 8645，0 = 系统分配）", metavar="PORT"),
                Option(
                    ("--map",),
                    "模型映射，`客户端模型=上游模型`（逗号分隔）",
                    metavar="PAIRS",
                ),
            ],
        ),
    )

    parent = REGISTRY.get("proxy")
    if parent is not None:
        _refresh_state(parent)
    _ATTACHED = True
    return dict(WIRING)


attach()


__all__ = [
    "WIRING",
    "attach",
    "run_model_capabilities",
    "run_model_voice",
    "run_proxy_plan",
    "run_proxy_providers",
    "run_proxy_serve",
    "run_proxy_status",
]
