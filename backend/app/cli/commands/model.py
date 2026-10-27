"""模型层命令：`model` / `fallback` / `auth` / `login` / `logout` / `moa`。

## 命令面来源

选项集逐条对着 `handoff/hermes-parity/research/_raw/` 里的原文抄：
`cmd-02-model.txt`、`cmd-03-moa.txt`、`cmd-04-fallback.txt`、`cmd-18-login.txt`、
`cmd-19-logout.txt`、`cmd-20-auth.txt`，以及 `_raw/sub/` 下的
`auth__*.txt` / `fallback__*.txt` / `moa__*.txt`。

**少一个选项就是功能缺失，多一个选项就是行为差异**——所以下面的 `Option(...)`
与那份 `usage:` 行一一对得上；凡是 Orbit 自己的扩展（`model --list`、
`model --base-url`、`model --env`）都在 help 文本里标了「Orbit 扩展」。

## 三个存储面（这是本模块最容易搞混的地方）

| 存什么 | 落在哪 | 谁读它 |
|---|---|---|
| 当前 provider / 模型 / 回退链 / MoA 预设 | `<data_dir>/settings.json`（运行时键） | 后端每次读配置时刷新 |
| 环境级默认（进程启动时读一次） | `backend/.env` | `Settings` 在进程启动时 |
| **密钥** | 系统 keyring，`service = f"orbit:{data_dir}"` | `Context.hydrate()` |

密钥**绝不**进前两者：`backend/.env` 是明文文件，写进去等于把密钥摊在磁盘上；
`settings.json` 会被前端回显。这条红线与 `doctor` 的 ORBIT-002 通告是同一个承诺。

## 关于"运行时聚合未实现"

`moa` 只做配置读写，`auth` 只做凭证管理。真正把多模型聚合成答案（`/moa`）、把凭证池
接进 agent 循环，都需要改 `app/agent.py` / `app/graph/*`（本轮禁改），所以命令里
**如实说明**而不是假装能跑——假装的结果是用户配了半天发现没生效。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from app import fallback as fb
from app import providers as pv
from app.cli.context import CliError, Context
from app.cli.spec import REGISTRY, Command, Option
from app.config import ROOT

# ── 小工具（照 core.py 的既有约定）───────────────────────────────────────────

#: `.env` 的落点。
#:
#: 刻意**不用** `core.py` 里的 `Path(__file__).resolve().parents[3] / "backend" / ".env"`：
#: `core.py` 在 `app/cli/commands/` 下，`parents[3]` 已经是 `backend/`，再拼一层
#: `backend` 会得到 `backend/backend/.env`（一个永远不存在的路径）。这里直接复用
#: `config.ROOT`，保证与 `Settings` 读的是**同一个文件**——两处各算一次路径，
#: 迟早会漂成两个文件。
ENV_FILE = ROOT / "backend" / ".env"

#: 本模块写的运行时键。`config check` 的已知键清单在 `core.py`（禁改），所以这三个
#: 键会被它报成"未知"——这是已知的显示瑕疵，不影响功能，已在交付说明里列出。
CHAIN_KEY = "fallback_chain"
MOA_PRESETS_KEY = "moa_presets"
MOA_DEFAULT_KEY = "moa_default_preset"


def _runtime_settings(context: Context) -> dict[str, Any]:
    """读运行时设置文件。坏文件当空——配置文件坏了只该退回默认，不该让命令栈崩。"""
    path = context.settings_path
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_runtime(context: Context, key: str, value: Any) -> Path:
    """写一个运行时键（任意 JSON 值）。保留其它键，重排为缩进 2 的可读格式。"""
    path = context.settings_path
    data = _runtime_settings(context)
    data[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _pop_runtime(context: Context, key: str) -> bool:
    """删一个运行时键，返回是否真的删掉了。"""
    path = context.settings_path
    data = _runtime_settings(context)
    if key not in data:
        return False
    data.pop(key)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def _write_env(key: str, value: str) -> Path:
    """把 `ORBIT_<KEY>` 写进 `backend/.env`：替换已有行，没有就追加。

    保留原有行序与注释——`.env` 是给人读的文件，重排会毁掉上下文注释。
    **只用来写非密钥项**：密钥写这里等于明文落盘。
    """
    env_key = f"ORBIT_{key.upper()}"
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    replaced = False
    for index, line in enumerate(lines):
        if line.strip().startswith(f"{env_key}="):
            lines[index] = f"{env_key}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{env_key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ENV_FILE


def _profile(name: str) -> pv.ProviderProfile:
    """按 id 取 profile。名字大小写不敏感（用户手打时不会注意）。"""
    key = (name or "").strip().lower()
    profile = pv.PROVIDERS.get(key)
    if profile is None:
        raise CliError(f"未知 provider：{name or '（空）'}（可用：{', '.join(pv.PROVIDERS)}）")
    return profile


def _split_selection(text: str) -> tuple[str, str]:
    """`provider/model` 或 `provider:model` → (provider, model)。

    只切**第一个**分隔符：OpenRouter 的模型名本身就带斜杠
    （`openrouter/anthropic/claude-sonnet-4.6`），全切会把模型名拆碎。
    """
    raw = (text or "").strip()
    for sep in ("/", ":"):
        if sep in raw:
            head, _, tail = raw.partition(sep)
            return head.strip(), tail.strip()
    return raw, ""


def _current(context: Context) -> pv.ProviderProfile:
    """当前 provider。显式覆盖 → base_url 反查 → custom（见 `resolve_provider`）。"""
    try:
        return pv.resolve_provider(context.settings)
    except pv.UnknownProvider as exc:
        raise CliError(str(exc)) from exc


def _pool(context: Context) -> fb.CredentialPool:
    """凭证池。`service` 与 `ConnectorConfig` / `Context.hydrate` 完全一致，
    否则同一条密钥会在两个 service 名下各存一份，谁也读不到谁。"""
    service = f"orbit:{Path(context.settings.data_dir).resolve()}"
    return fb.CredentialPool(fb.KeyringStore(service))


def _primary_account() -> str:
    """运行时的**主凭证** account 名。

    这个字符串不是随便起的：`Context.hydrate()` 与 `ConnectorConfig.load()` 都只读
    `model_api_key` 这一个 account。改它等于让 `orbit login` 写进去的 key 不被运行时
    看见——"登录成功了但用不了"。
    """
    return "model_api_key"


def _store_primary_key(context: Context, secret: str) -> None:
    """写主凭证（keyring）。只写 keyring，绝不落配置文件。"""
    service = f"orbit:{Path(context.settings.data_dir).resolve()}"
    fb.KeyringStore(service).set(_primary_account(), secret)


def _delete_primary_key(context: Context) -> bool:
    service = f"orbit:{Path(context.settings.data_dir).resolve()}"
    store = fb.KeyringStore(service)
    if store.get(_primary_account()) is None:
        return False
    store.delete(_primary_account())
    return True


def _prompt_secret(prompt: str) -> str:
    """安全读密钥。

    非交互环境（管道 / CI / 被别的进程调起）直接报错要 `--api-key`，而不是让
    `getpass` 去读 stdin——那会把上一行的输出当成密钥，然后静默存进去。
    """
    if not sys.stdin.isatty():
        raise CliError(
            "当前不是交互终端，无法安全读取密钥。请用 `--api-key`（或 "
            "`orbit auth add <provider> --api-key ...`）。"
        )
    import getpass

    try:
        return getpass.getpass(prompt)
    except (EOFError, OSError) as exc:  # 终端被关掉 / 无控制台
        raise CliError(f"读取密钥失败：{exc}") from exc


def _mask(fingerprint_text: str) -> str:
    """指纹展示。空指纹（旧条目/坏数据）显示"未知"而不是空白。"""
    return fingerprint_text or "（未知）"


def _yes_no(value: bool) -> str:
    return "是" if value else "否"


# ── model ───────────────────────────────────────────────────────────────────


def _provider_rows(context: Context) -> list[tuple[str, ...]]:
    """provider 总表：身份 + 端点 + 鉴权 + key 变量名。"""
    current = _current(context)
    rows: list[tuple[str, ...]] = []
    for profile in pv.PROVIDERS.values():
        rows.append(
            (
                f"{profile.id}{' *' if profile.id == current.id else ''}",
                profile.name,
                profile.transport,
                profile.default_base_url or "（用户填）",
                profile.auth,
                profile.key_env or "（无）",
            )
        )
    return rows


def _capability_rows(context: Context) -> list[tuple[str, ...]]:
    """模型能力表。四级来源都标出来（manual / cache / factory），免得把兜底当真相。"""
    data_dir = context.data_dir
    rows: list[tuple[str, ...]] = []
    for profile in pv.PROVIDERS.values():
        for model in profile.models:
            caps = pv.capabilities(profile, model, data_dir=data_dir)
            sources = {source for _, source in caps.values()}
            rows.append(
                (
                    f"{profile.id}/{model}",
                    _yes_no(bool(caps["supports_tools"][0])),
                    _yes_no(bool(caps["supports_reasoning"][0])),
                    _yes_no(bool(caps["supports_vision"][0])),
                    str(caps["context_window"][0]),
                    "/".join(sorted(sources)),
                )
            )
    return rows


def _list_providers(context: Context) -> int:
    current = _current(context)
    context.out(f"模型供应商（共 {len(pv.PROVIDERS)} 家，* = 当前）")
    context.out()
    context.table(("id", "名称", "线协议", "默认端点", "鉴权", "key 环境变量"), _provider_rows(context))
    context.out()
    context.out("模型能力（出厂声明；来源 manual = 本仓库手工表，cache = 运行时缓存，factory = 兜底）")
    context.out()
    context.table(("provider/模型", "tools", "reasoning", "vision", "上下文", "来源"), _capability_rows(context))
    unsupported = {
        profile.id: profile.transport
        for profile in pv.PROVIDERS.values()
        if not profile.supported
    }
    if unsupported:
        context.out()
        for pid, transport in unsupported.items():
            reason = pv.UNSUPPORTED_TRANSPORTS.get(transport, "该线协议未实现")
            context.out(f"注意：{pid} 的线协议 {transport} 未实现 —— {reason}")
    context.emit(
        {
            "ok": True,
            "current": current.id,
            "providers": [
                {
                    "id": profile.id,
                    "name": profile.name,
                    "transport": profile.transport,
                    "supported": profile.supported,
                    "default_base_url": profile.default_base_url,
                    "auth": profile.auth,
                    "key_env": profile.key_env,
                    "key_env_aliases": list(profile.key_env_aliases),
                    "default_model": profile.default_model,
                    "models": profile.models,
                }
                for profile in pv.PROVIDERS.values()
            ],
        }
    )
    return 0


def _refresh_cache(context: Context) -> int:
    """`--refresh`：清掉模型选择器的磁盘缓存。

    Hermes 清完会重新拉所有 provider 的 live `/v1/models`。Orbit **不拉**：
    本机无网络，拉取必然失败，然后用户会以为 `--refresh` 坏了。所以这里只清本地
    缓存并如实说明"没有发起网络请求"。
    """
    path = pv.cache_path(context.data_dir)
    if path.exists():
        path.unlink()
        context.out(f"已清除模型缓存：{path}")
    else:
        context.out(f"模型缓存本来就不存在：{path}")
    context.note("本次未发起任何网络请求（本机无网络）；缓存会在有真实能力数据时重新生成。")
    context.emit({"ok": True, "cleared": str(path), "network": False})
    return 0


def _apply_selection(
    context: Context, profile: pv.ProviderProfile, model: str, base_url: str
) -> int:
    """把选择写进运行时设置。**不写密钥**（密钥只走 `orbit login` / keyring）。"""
    if not profile.supported:
        reason = pv.UNSUPPORTED_TRANSPORTS.get(profile.transport, "该线协议未实现")
        raise CliError(
            f"{profile.id} 不能用：线协议 {profile.transport} 未实现 —— {reason}"
        )
    target = model or profile.default_model
    if not target:
        raise CliError(
            f"{profile.id} 没有出厂默认模型，请显式指定：`orbit model {profile.id}/<模型>`"
        )
    endpoint = base_url or profile.default_base_url
    if not endpoint:
        raise CliError(
            f"{profile.id} 没有默认端点，请用 `--base-url` 指定（例：`--base-url "
            f"http://127.0.0.1:8000/v1`）。"
        )
    path = _write_runtime(context, "model_provider", profile.id)
    _write_runtime(context, "model", target)
    _write_runtime(context, "model_base_url", endpoint)
    context.out(f"已选择 {profile.id}/{target}")
    context.out(f"  端点：{endpoint}")
    context.out(f"  写入：{path}（model_provider / model / model_base_url）")
    if profile.auth == "api_key":
        has_key = bool(pv.resolve_api_key(profile, context.settings))
        if not has_key:
            context.warn(
                f"还没有 {profile.id} 的密钥。运行 `orbit login --provider {profile.id}` "
                f"写入系统 keyring，或设置环境变量 {profile.key_env}。"
            )
        else:
            context.note("密钥已就位（来自系统 keyring 或环境变量）。")
    elif profile.auth == "none":
        context.note("该 provider 不需要密钥。")
    else:
        context.warn(
            f"{profile.id} 的鉴权方式 {profile.auth} 需要外部凭证链，本机未实现（见 `orbit login`）。"
        )
    context.emit(
        {
            "ok": True,
            "provider": profile.id,
            "model": target,
            "base_url": endpoint,
            "path": str(path),
        }
    )
    return 0


def _interactive_select(context: Context) -> int:
    """交互式选择：先挑 provider，再挑模型。

    只用 `input()`，不引入 TUI 库（本机不许装依赖）。选择器读不到输入就直接报错，
    而不是默默用默认值——静默改配置是最难排查的一类问题。
    """
    supported = [profile for profile in pv.PROVIDERS.values() if profile.supported]
    context.out("可用供应商：")
    for index, profile in enumerate(supported, start=1):
        context.out(f"  {index}. {profile.id}（{profile.name}）")
    try:
        raw = input("选择供应商序号（回车取消）：").strip()
    except EOFError:
        raise CliError("没有读到输入，已取消。") from None
    if not raw:
        context.out("已取消。")
        return 0
    if not raw.isdigit() or not 1 <= int(raw) <= len(supported):
        raise CliError(f"无效序号：{raw}")
    profile = supported[int(raw) - 1]
    models = list(profile.models) or ([profile.default_model] if profile.default_model else [])
    if models:
        context.out(f"{profile.id} 的模型：")
        for index, name in enumerate(models, start=1):
            context.out(f"  {index}. {name}")
        try:
            raw_model = input("选择模型序号（回车用默认）：").strip()
        except EOFError:
            raise CliError("没有读到输入，已取消。") from None
        model = ""
        if raw_model:
            if not raw_model.isdigit() or not 1 <= int(raw_model) <= len(models):
                raise CliError(f"无效序号：{raw_model}")
            model = models[int(raw_model) - 1]
    else:
        model = ""
    return _apply_selection(context, profile, model, "")


def run_model(context: Context, args) -> int:
    if getattr(args, "refresh", False):
        return _refresh_cache(context)
    if getattr(args, "list", False):
        return _list_providers(context)

    selection = (getattr(args, "selection", None) or "").strip()
    base_url = (getattr(args, "base_url", None) or "").strip()
    if selection:
        provider_name, model = _split_selection(selection)
        profile = _profile(provider_name)
        code = _apply_selection(context, profile, model, base_url)
    elif sys.stdin.isatty():
        code = _interactive_select(context)
    else:
        raise CliError(
            "用法：`orbit model <provider>[/<模型>]`、`orbit model --list`、"
            "或 `orbit model --refresh`。"
        )

    if getattr(args, "env", False):
        # `--env` 是 Orbit 扩展：把 provider 固定到进程启动配置里，让后端重启后也用同一家。
        # 只写 provider 与端点，**不写密钥**。
        #
        # 从 settings.json 回读而不是用 `context.settings`：`_apply_selection` 刚写的是
        # 磁盘上的运行时键，`context.settings` 还是启动时那一份——不回读就会把**旧的**
        # provider 写进 .env（`--env` 于是看起来"没生效"）。
        runtime = _runtime_settings(context)
        provider_id = str(runtime.get("model_provider") or _current(context).id)
        base_url_value = str(runtime.get("model_base_url") or context.settings.model_base_url)
        _write_env("model_provider", provider_id)
        _write_env("model_base_url", base_url_value)
        context.out(f"已写入 {ENV_FILE}：ORBIT_MODEL_PROVIDER={provider_id}")
        context.note("环境变量在进程启动时读取，改完需要重启后端。")
    return code


# ── fallback ────────────────────────────────────────────────────────────────


def _load_chain(context: Context) -> list[fb.FallbackTarget]:
    return fb.targets_from_config(_runtime_settings(context).get(CHAIN_KEY))


def _save_chain(context: Context, targets: list[fb.FallbackTarget]) -> Path:
    return _write_runtime(context, CHAIN_KEY, fb.targets_to_config(targets))


def _primary_target(context: Context) -> fb.FallbackTarget:
    """主模型这一站。链里**不存**它——主模型永远是 `settings.model`，
    存两份就会出现"改了主模型但回退链里还是旧的那条"的错位。"""
    profile = _current(context)
    return fb.FallbackTarget(
        provider=profile.id,
        model=context.settings.model or profile.default_model,
        base_url=context.settings.model_base_url,
    )


def run_fallback_list(context: Context, args) -> int:
    primary = _primary_target(context)
    chain = _load_chain(context)
    context.out("回退链（主模型失败时按序尝试；触发条件：429 / 529 / 503 / 连接失败）")
    context.out()
    rows: list[tuple[str, ...]] = [
        ("0（主）", primary.provider, primary.model or "（未设）", primary.base_url or "（默认）", "—"),
    ]
    for index, target in enumerate(chain, start=1):
        rows.append(
            (
                str(index),
                target.provider,
                target.model or "（该家默认）",
                target.base_url or "（默认）",
                target.key_env or "（默认）",
            )
        )
    context.table(("序号", "provider", "模型", "端点", "key 环境变量"), rows)
    if not chain:
        context.out()
        context.out("（回退链为空：主模型 429/503 时没有接班者）")
    context.emit(
        {
            "ok": True,
            "primary": primary.to_config(),
            "chain": fb.targets_to_config(chain),
            "trigger_status": sorted(fb.FAILOVER_STATUS),
        }
    )
    return 0


def run_fallback(context: Context, args) -> int:
    """无子命令 = 看链（对齐 `hermes fallback` 的 "default when no subcommand"）。"""
    return run_fallback_list(context, args)


def run_fallback_add(context: Context, args) -> int:
    raw = (getattr(args, "provider", None) or "").strip()
    if not raw:
        raise CliError(
            "用法：`orbit fallback add <provider>[/<模型>]`。"
            "（Hermes 这里打开模型选择器；Orbit 非交互，显式给参数。）"
        )
    provider_name, model = _split_selection(raw)
    profile = _profile(provider_name)
    if not profile.supported:
        reason = pv.UNSUPPORTED_TRANSPORTS.get(profile.transport, "该线协议未实现")
        raise CliError(f"{profile.id} 不能加入回退链：{reason}")
    target = fb.FallbackTarget(
        provider=profile.id,
        model=(getattr(args, "model", None) or model or profile.default_model).strip(),
        base_url=(getattr(args, "base_url", None) or "").strip(),
        key_env=(getattr(args, "key_env", None) or "").strip(),
    )
    chain = _load_chain(context)
    if any(item.provider == target.provider and item.model == target.model for item in chain):
        raise CliError(f"回退链里已有 {target.label()}（`orbit fallback list` 查看）")
    chain.append(target)
    path = _save_chain(context, chain)
    context.out(f"已追加到回退链：{target.label()}")
    context.out(f"  写入：{path}（{CHAIN_KEY}，共 {len(chain)} 项）")
    context.emit({"ok": True, "chain": fb.targets_to_config(chain), "path": str(path)})
    return 0


def run_fallback_remove(context: Context, args) -> int:
    chain = _load_chain(context)
    raw = (getattr(args, "index", None) or "").strip()
    if not raw:
        rows = [
            (str(index), target.label(), target.base_url or "（默认）")
            for index, target in enumerate(chain, start=1)
        ]
        if not chain:
            context.out("回退链为空，没有可删除的项。")
            context.emit({"ok": True, "chain": []})
            return 0
        context.table(("序号", "目标", "端点"), rows)
        raise CliError("用法：`orbit fallback remove <序号>`（上面的序号）")
    if not raw.isdigit() or not 1 <= int(raw) <= len(chain):
        raise CliError(f"无效序号：{raw}（现有 {len(chain)} 项）")
    removed = chain.pop(int(raw) - 1)
    path = _save_chain(context, chain)
    context.out(f"已删除 {removed.label()}")
    context.out(f"  写入：{path}（{CHAIN_KEY}，剩 {len(chain)} 项）")
    context.emit(
        {"ok": True, "removed": removed.to_config(), "chain": fb.targets_to_config(chain)}
    )
    return 0


def run_fallback_clear(context: Context, args) -> int:
    chain = _load_chain(context)
    path = _save_chain(context, [])
    context.out(f"已清空回退链（原 {len(chain)} 项）")
    context.out(f"  写入：{path}")
    context.emit({"ok": True, "cleared": len(chain), "path": str(path)})
    return 0


# ── auth ────────────────────────────────────────────────────────────────────


def _require_api_key_type(raw: str | None) -> None:
    """`--type oauth` 明确报未实现。

    为什么不"先存起来等以后支持"：OAuth 需要浏览器回调 + PKCE + token 刷新，存一个
    假的占位条目会让 `auth list` 显示"已登录"而实际不可用——这比直接拒绝更糟。
    """
    kind = (raw or "api-key").strip().lower().replace("_", "-")
    if kind == "oauth":
        raise CliError(
            "OAuth 凭证未实现：需要浏览器回调 + PKCE + token 刷新循环，本机无网络且本轮"
            "不引入 OAuth 依赖。api_key 类 provider 请用 "
            "`orbit auth add <provider> --api-key ...`。"
        )
    if kind not in {"api-key", ""}:
        raise CliError(f"未知凭证类型：{raw}（可用：api-key / api_key / oauth）")


def run_auth_add(context: Context, args) -> int:
    provider_name = (getattr(args, "provider", None) or "").strip()
    if not provider_name:
        raise CliError("用法：`orbit auth add <provider> [--api-key ...] [--label ...]`")
    _require_api_key_type(getattr(args, "cred_type", None))
    profile = _profile(provider_name)
    if profile.auth == "none":
        raise CliError(f"{profile.id} 不需要凭证（本地端点 / 用户自填端点）。")
    if profile.auth == "aws_sdk":
        raise CliError(
            f"{profile.id} 用 AWS 凭据链（boto3），不走 API key；本机未装 boto3，"
            "且它的线协议 bedrock_converse 也未实现。"
        )
    secret = (getattr(args, "api_key", None) or "").strip()
    if not secret:
        secret = _prompt_secret(f"{profile.id} 的 API key（输入不回显）：").strip()
    if not secret:
        raise CliError("密钥为空，已取消。")
    pool = _pool(context)
    credential = pool.add(profile.id, secret, label=(getattr(args, "label", None) or ""))
    context.out(f"已加入凭证池：{profile.id} #{credential.index}（{credential.label}）")
    context.out(f"  指纹：{_mask(credential.fingerprint)}（只存末 4 位，不落配置文件）")
    context.note(
        "这是凭证池里的第 "
        f"{len(pool.entries(profile.id))} 把 key。运行时当前读的是 keyring 的 "
        f"`{_primary_account()}` 主凭证；要用这把做主凭证请 `orbit login --provider "
        f"{profile.id}`。"
    )
    context.emit(
        {
            "ok": True,
            "provider": profile.id,
            "credential": credential.to_dict(),
        }
    )
    return 0


def run_auth_list(context: Context, args) -> int:
    pool = _pool(context)
    wanted = (getattr(args, "provider", None) or "").strip()
    providers = [wanted] if wanted else pool.known_providers()
    rows: list[tuple[str, ...]] = []
    payload: list[dict[str, Any]] = []
    for provider in providers:
        for credential in pool.entries(provider):
            rows.append(
                (
                    provider,
                    str(credential.index),
                    credential.label,
                    str(credential.priority),
                    credential.last_status or "可用",
                    str(credential.request_count),
                    _mask(credential.fingerprint),
                )
            )
            payload.append({"provider": provider, **credential.to_dict()})
    context.out("凭证池（密钥在系统 keyring，这里只显示指纹末 4 位）")
    context.out()
    if rows:
        context.table(
            ("provider", "序号", "标签", "优先级", "状态", "请求数", "指纹"), rows
        )
    else:
        context.out("（池里没有凭证）")
        context.out("追加：`orbit auth add <provider> --api-key ...`")
    context.emit({"ok": True, "credentials": payload})
    return 0


def run_auth_remove(context: Context, args) -> int:
    provider_name = (getattr(args, "provider", None) or "").strip()
    target = (getattr(args, "target", None) or "").strip()
    if not provider_name or not target:
        raise CliError("用法：`orbit auth remove <provider> <序号|标签>`")
    profile = _profile(provider_name)
    pool = _pool(context)
    removed = pool.remove(profile.id, target)
    if removed is None:
        raise CliError(
            f"{profile.id} 的凭证池里没有 `{target}`（`orbit auth list {profile.id}` 查看）"
        )
    context.out(f"已删除 {profile.id} #{removed.index}（{removed.label}）")
    context.emit({"ok": True, "removed": removed.to_dict()})
    return 0


def run_auth_reset(context: Context, args) -> int:
    provider_name = (getattr(args, "provider", None) or "").strip()
    if not provider_name:
        raise CliError("用法：`orbit auth reset <provider>`")
    profile = _profile(provider_name)
    pool = _pool(context)
    cleared = pool.reset(profile.id)
    total = len(pool.entries(profile.id))
    context.out(f"已清除 {profile.id} 的耗尽状态：{cleared}/{total} 条被标记过")
    context.emit({"ok": True, "provider": profile.id, "cleared": cleared, "total": total})
    return 0


def run_auth_status(context: Context, args) -> int:
    provider_name = (getattr(args, "provider", None) or "").strip()
    if not provider_name:
        raise CliError("用法：`orbit auth status <provider>`")
    profile = _profile(provider_name)
    pool = _pool(context)
    entries = pool.entries(profile.id)
    usable = [item for item in entries if not item.last_status]
    primary = fb.KeyringStore(
        f"orbit:{Path(context.settings.data_dir).resolve()}"
    ).get(_primary_account())
    rows: list[tuple[str, str, str]] = [
        ("provider", profile.id, profile.name),
        ("线协议", profile.transport, "已实现" if profile.supported else "未实现"),
        ("鉴权方式", profile.auth, profile.key_env or "（无环境变量）"),
        (
            "主凭证",
            "已设置" if primary else "未设置",
            f"keyring account = {_primary_account()}",
        ),
        ("池化凭证", f"{len(entries)} 条", f"可用 {len(usable)} / 耗尽 {len(entries) - len(usable)}"),
    ]
    for item in entries:
        rows.append(
            (
                f"  #{item.index}",
                item.last_status or "可用",
                f"{item.label} · 优先级 {item.priority} · 请求 {item.request_count} · "
                f"{_mask(item.fingerprint)}",
            )
        )
    context.out(f"{profile.id} 的鉴权状态")
    context.out()
    context.table(("项", "值", "详情"), rows)
    context.emit(
        {
            "ok": True,
            "provider": profile.id,
            "transport": profile.transport,
            "auth": profile.auth,
            "primary_present": bool(primary),
            "pool": [item.to_dict() for item in entries],
        }
    )
    return 0


def run_auth_logout(context: Context, args) -> int:
    """登出一个 provider：清主凭证 + 清该 provider 的整个池。"""
    provider_name = (getattr(args, "provider", None) or "").strip()
    if not provider_name:
        raise CliError("用法：`orbit auth logout <provider>`")
    profile = _profile(provider_name)
    removed_primary = _delete_primary_key(context)
    cleared = _pool(context).clear(profile.id)
    if not removed_primary and not cleared:
        context.out(f"{profile.id} 没有存过凭证，无需登出。")
        context.emit({"ok": True, "provider": profile.id, "removed": 0})
        return 0
    context.out(
        f"已登出 {profile.id}：主凭证{'已清除' if removed_primary else '本来就没有'}，"
        f"池化凭证清除 {cleared} 条"
    )
    context.emit(
        {"ok": True, "provider": profile.id, "primary_removed": removed_primary, "pool_cleared": cleared}
    )
    return 0


def run_auth_spotify(context: Context, args) -> int:
    raise CliError(
        "Spotify 鉴权（OAuth PKCE）未实现：需要浏览器回调监听 + PKCE 校验 + token 刷新，"
        "本机无网络且本轮不引入 OAuth 依赖。命令面已登记，避免看起来「没这个功能」。"
    )


# ── login / logout ──────────────────────────────────────────────────────────


def run_login(context: Context, args) -> int:
    """api_key 类 provider 的登录 = 把密钥写进系统 keyring。"""
    requested = (getattr(args, "provider", None) or "").strip()
    profile = _profile(requested) if requested else _current(context)
    context.note(
        "提示：Hermes 已把 `login` 标为废弃（改用 `auth` / `model` / `setup`）；"
        "Orbit 保留它作为「写密钥」的捷径。"
    )
    if profile.auth == "api_key":
        secret = _prompt_secret(f"{profile.id} 的 API key（输入不回显）：").strip()
        if not secret:
            raise CliError("密钥为空，已取消。")
        _store_primary_key(context, secret)
        context.out(f"已登录 {profile.id}：密钥写入系统 keyring（account = {_primary_account()}）")
        context.out(f"  指纹：{_mask(fb.fingerprint(secret))}")
        context.note("密钥不落任何配置文件。要用多把 key 轮转，见 `orbit auth add`。")
        context.emit(
            {
                "ok": True,
                "provider": profile.id,
                "auth": profile.auth,
                "fingerprint": fb.fingerprint(secret),
            }
        )
        return 0
    if profile.auth == "none":
        raise CliError(f"{profile.id} 不需要登录（{profile.notes or '本地 / 自填端点'}）。")
    if profile.auth == "aws_sdk":
        raise CliError(
            f"{profile.id} 用 AWS 凭据链（IAM / boto3），没有「登录」这一步；"
            "且本机未装 boto3、bedrock_converse 也未实现。"
        )
    raise CliError(
        f"{profile.id} 的鉴权方式是 {profile.auth}（OAuth）：未实现 —— 需要浏览器回调 + "
        "PKCE + token 刷新，本机无网络且本轮不引入 OAuth 依赖。"
    )


def run_logout(context: Context, args) -> int:
    """清除某个 provider 的凭证。

    Hermes 的 `--provider` 只有 4 个 OAuth 候选（nous / openai-codex / xai-oauth /
    spotify），因为它的 logout 只服务于 OAuth。Orbit 的 OAuth 未实现，所以这里接受
    **任意 provider id** 并按 api_key 语义清 keyring —— 这是有意的差异，写在这里而不是
    让命令变成"只能报未实现"。
    """
    requested = (getattr(args, "provider", None) or "").strip()
    profile = _profile(requested) if requested else _current(context)
    removed_primary = _delete_primary_key(context)
    cleared = _pool(context).clear(profile.id)
    if not removed_primary and not cleared:
        context.out(f"{profile.id} 没有存过凭证。")
        context.emit({"ok": True, "provider": profile.id, "removed": 0})
        return 0
    context.out(
        f"已登出 {profile.id}：主凭证{'已清除' if removed_primary else '本来就没有'}，"
        f"池化凭证清除 {cleared} 条"
    )
    context.emit(
        {"ok": True, "provider": profile.id, "primary_removed": removed_primary, "pool_cleared": cleared}
    )
    return 0


# ── moa ─────────────────────────────────────────────────────────────────────


def _load_presets(context: Context) -> dict[str, Any]:
    raw = _runtime_settings(context).get(MOA_PRESETS_KEY)
    return raw if isinstance(raw, dict) else {}


def _preset_rows(presets: dict[str, Any]) -> list[tuple[str, ...]]:
    rows: list[tuple[str, ...]] = []
    for name, preset in presets.items():
        if not isinstance(preset, dict):
            continue
        references = preset.get("reference_models")
        if isinstance(references, list):
            for item in references:
                if isinstance(item, dict):
                    rows.append(
                        (
                            str(name),
                            "reference",
                            str(item.get("provider", "")),
                            str(item.get("model", "")) or "（该家默认）",
                            str(preset.get("max_tokens", 4096)),
                            "启用" if preset.get("enabled", True) else "停用",
                        )
                    )
        aggregator = preset.get("aggregator")
        if isinstance(aggregator, dict):
            rows.append(
                (
                    str(name),
                    "aggregator",
                    str(aggregator.get("provider", "")),
                    str(aggregator.get("model", "")) or "（该家默认）",
                    str(preset.get("max_tokens", 4096)),
                    "启用" if preset.get("enabled", True) else "停用",
                )
            )
    return rows


def run_moa_list(context: Context, args) -> int:
    presets = _load_presets(context)
    default = _runtime_settings(context).get(MOA_DEFAULT_KEY) or "default"
    context.out("MoA 预设（Mixture of Agents：多个 reference 模型并行作答，aggregator 汇总）")
    context.out()
    rows = _preset_rows(presets)
    if rows:
        context.table(("预设", "角色", "provider", "模型", "max_tokens", "状态"), rows)
    else:
        context.out("（还没有任何预设）")
        context.out("新建：`orbit moa configure default --reference deepseek/deepseek-v4-flash "
                    "--aggregator openai/gpt-5.4`")
    context.out()
    context.out(f"默认预设：{default}")
    context.out("注意：运行时聚合未实现。配置会真实写进 settings.json，但 `orbit chat`")
    context.out("      目前不会用它做多模型聚合（需要改 app/graph/*，本轮禁改）。")
    context.emit(
        {
            "ok": True,
            "default_preset": default,
            "presets": presets,
            "runtime_implemented": False,
        }
    )
    return 0


def _parse_target_argument(text: str, label: str) -> dict[str, str]:
    provider_name, model = _split_selection(text)
    if not provider_name:
        raise CliError(f"{label} 需要 `provider/model` 形式，收到：{text!r}")
    profile = _profile(provider_name)
    if not profile.supported:
        reason = pv.UNSUPPORTED_TRANSPORTS.get(profile.transport, "该线协议未实现")
        raise CliError(f"{profile.id} 不能用于 MoA：{reason}")
    return {"provider": profile.id, "model": model or profile.default_model}


def run_moa_configure(context: Context, args) -> int:
    name = (getattr(args, "name", None) or "").strip() or "default"
    references = list(getattr(args, "reference", None) or [])
    aggregator = (getattr(args, "aggregator", None) or "").strip()
    max_tokens = getattr(args, "max_tokens", None)
    disable = bool(getattr(args, "disable", False))

    if not references and not aggregator and max_tokens is None and not disable:
        if not sys.stdin.isatty():
            raise CliError(
                "用法：`orbit moa configure <预设名> --reference provider/model "
                "[--reference ...] --aggregator provider/model [--max-tokens N] [--disable]`"
            )
        # 交互式：Hermes 的 `moa configure` 就是逐个槽位选模型，这里做同样的两问。
        try:
            raw_refs = input("reference 模型（逗号分隔，如 deepseek/deepseek-v4-flash）：")
            aggregator = input("aggregator 模型（如 openrouter/anthropic/claude-sonnet-4.6）：")
        except EOFError:
            raise CliError("没有读到输入，已取消。") from None
        references = [item.strip() for item in raw_refs.split(",") if item.strip()]

    presets = _load_presets(context)
    current = presets.get(name)
    preset: dict[str, Any] = dict(current) if isinstance(current, dict) else {}
    if references:
        preset["reference_models"] = [
            _parse_target_argument(item, "--reference") for item in references
        ]
    if aggregator:
        preset["aggregator"] = _parse_target_argument(aggregator, "--aggregator")
    if max_tokens is not None:
        try:
            preset["max_tokens"] = int(max_tokens)
        except (TypeError, ValueError) as exc:
            raise CliError(f"--max-tokens 需要整数，收到：{max_tokens!r}") from exc
    if disable:
        preset["enabled"] = False
    preset.setdefault("enabled", True)
    preset.setdefault("max_tokens", 4096)
    if not preset.get("reference_models") or not preset.get("aggregator"):
        raise CliError(
            "预设至少需要一个 reference 模型和一个 aggregator 模型"
            "（Hermes 的 default 预设是 2 reference + 1 aggregator）。"
        )
    presets[name] = preset
    path = _write_runtime(context, MOA_PRESETS_KEY, presets)
    if not _runtime_settings(context).get(MOA_DEFAULT_KEY):
        _write_runtime(context, MOA_DEFAULT_KEY, name)
    context.out(f"已保存 MoA 预设 `{name}`")
    for item in preset["reference_models"]:
        context.out(f"  reference：{item['provider']}/{item['model']}")
    context.out(f"  aggregator：{preset['aggregator']['provider']}/{preset['aggregator']['model']}")
    context.out(f"  max_tokens：{preset['max_tokens']} · 状态：{'启用' if preset['enabled'] else '停用'}")
    context.out(f"  写入：{path}")
    context.warn("配置已存，运行时聚合未实现：`/moa` 之类的多模型汇总需要改 app/graph/*，本轮未做。")
    context.emit({"ok": True, "preset": name, "config": preset, "path": str(path)})
    return 0


def run_moa_delete(context: Context, args) -> int:
    name = (getattr(args, "name", None) or "").strip()
    if not name:
        raise CliError("用法：`orbit moa delete <预设名>`")
    presets = _load_presets(context)
    if name not in presets:
        raise CliError(f"没有名为 `{name}` 的预设（现有：{', '.join(presets) or '无'}）")
    presets.pop(name)
    path = _write_runtime(context, MOA_PRESETS_KEY, presets)
    context.out(f"已删除 MoA 预设 `{name}`")
    context.out(f"  写入：{path}")
    if not presets:
        _pop_runtime(context, MOA_DEFAULT_KEY)
    elif _runtime_settings(context).get(MOA_DEFAULT_KEY) == name:
        _write_runtime(context, MOA_DEFAULT_KEY, next(iter(presets)))
        context.note(f"默认预设改为 `{next(iter(presets))}`")
    context.emit({"ok": True, "deleted": name, "remaining": sorted(presets)})
    return 0


# ── 注册 ────────────────────────────────────────────────────────────────────
#
# 本模块必须在 `hermes_parity` **之前**被导入：声明表里的 `model` / `fallback` /
# `auth` / `login` / `logout` / `moa` 占位会被 `register_specs` 自动跳过（它跳过
# 已登记的名字）。这条规则写在 `commands/__init__.py` 的 docstring 里——本模块的
# 导入位置就是那份"迁移规则"的第一个实例，不要把它挪到 `hermes_parity` 之后，
# 否则 `REGISTRY.add` 会因重名直接抛错。

#: Hermes 的 OAuth 相关选项。Orbit 的 OAuth 未实现，但选项面必须照抄——少一个选项，
#: 用户从 Hermes 带过来的脚本就会 argparse 报错（"未知参数"），而不是拿到一句
#: "OAuth 未实现"。**接受但不生效**，help 文本里已写明。
_OAUTH_OPTIONS: list[Option] = [
    Option(("--portal-url",), "Nous Portal 基址（OAuth 未实现，当前无效果）", metavar="URL"),
    Option(("--inference-url",), "推理 API 基址（OAuth 未实现，当前无效果）", metavar="URL"),
    Option(("--client-id",), "OAuth client id（OAuth 未实现，当前无效果）", metavar="ID"),
    Option(("--scope",), "OAuth scope（OAuth 未实现，当前无效果）", metavar="SCOPE"),
    Option(("--no-browser",), "不自动打开浏览器（OAuth 未实现，当前无效果）", action="store_true"),
    Option(("--timeout",), "网络超时秒数（OAuth 未实现，当前无效果）", metavar="SECONDS"),
    Option(("--ca-bundle",), "自定义 CA 证书 PEM（OAuth 未实现，当前无效果）", metavar="PEM"),
    Option(("--insecure",), "关闭 TLS 校验（仅测试；OAuth 未实现，当前无效果）", action="store_true"),
]


def _auth_subcommands() -> list[Command]:
    return [
        Command(
            name="add",
            help="添加池化凭证（密钥进系统 keyring）",
            handler=run_auth_add,
            group="core",
            state="done",
            options=[
                Option(("provider",), "provider id（如 deepseek / anthropic / openrouter）", nargs="?"),
                Option(("--type",), "凭证类型", dest="cred_type", choices=("oauth", "api-key", "api_key")),
                Option(("--label",), "可选显示标签", metavar="LABEL"),
                Option(("--api-key",), "密钥值（不给则安全提示输入）", metavar="KEY"),
                *_OAUTH_OPTIONS,
            ],
        ),
        Command(
            name="list",
            help="列出池化凭证（只显示指纹末 4 位）",
            handler=run_auth_list,
            group="core",
            state="done",
            options=[Option(("provider",), "可选 provider 过滤", nargs="?")],
        ),
        Command(
            name="remove",
            help="按序号 / 标签删除凭证",
            handler=run_auth_remove,
            group="core",
            state="done",
            options=[
                Option(("provider",), "provider id", nargs="?"),
                Option(("target",), "凭证序号或精确标签", nargs="?"),
            ],
        ),
        Command(
            name="reset",
            help="清除该 provider 全部凭证的耗尽状态（429 熔断后的恢复手段）",
            handler=run_auth_reset,
            group="core",
            state="done",
            options=[Option(("provider",), "provider id", nargs="?")],
        ),
        Command(
            name="status",
            help="查看 provider 的鉴权状态（主凭证 + 池）",
            handler=run_auth_status,
            group="core",
            state="done",
            options=[Option(("provider",), "provider id", nargs="?")],
        ),
        Command(
            name="logout",
            help="登出 provider 并清除存储的鉴权状态",
            handler=run_auth_logout,
            group="core",
            state="done",
            options=[Option(("provider",), "provider id", nargs="?")],
        ),
        Command(
            name="spotify",
            help="用 PKCE 完成 Spotify 鉴权（未实现，见报错原因）",
            handler=run_auth_spotify,
            group="core",
            parity="P2",
            state="partial",
            options=[
                Option(("action",), "login / status / logout", nargs="?"),
                Option(("--client-id",), "Spotify app client_id", metavar="ID"),
                Option(("--redirect-uri",), "允许的 localhost 回调地址", metavar="URI"),
                Option(("--scope",), "覆盖请求的 scope", metavar="SCOPE"),
                Option(("--no-browser",), "不自动打开浏览器", action="store_true"),
                Option(("--timeout",), "回调 / 换 token 超时秒数", metavar="SECONDS"),
            ],
        ),
    ]


REGISTRY.add(
    Command(
        name="model",
        help="选择默认模型与供应商（含 `--list` 列出全部 provider 与模型能力）",
        handler=run_model,
        group="core",
        parity="P0",
        state="done",
        options=[
            Option(("selection",), "provider 或 provider/模型，如 deepseek/deepseek-chat", nargs="?"),
            Option(("--list",), "列出全部 provider 与模型能力（Orbit 扩展）", action="store_true"),
            Option(("--base-url",), "覆盖端点（custom / 自建网关必填；Orbit 扩展）", metavar="URL"),
            Option(
                ("--env",),
                "同时写入 backend/.env，让后端启动时也用同一家（Orbit 扩展）",
                action="store_true",
            ),
            Option(
                ("--refresh",),
                "清空模型选择器磁盘缓存（不发起网络请求）",
                action="store_true",
            ),
            *_OAUTH_OPTIONS,
        ],
    ),
    Command(
        name="fallback",
        help="管理主模型失败时的回退链（触发条件：429 / 529 / 503 / 连接失败）",
        handler=run_fallback,
        group="core",
        parity="P0",
        state="done",
        subcommands=[
            Command(
                name="list",
                help="查看当前回退链",
                handler=run_fallback_list,
                group="core",
                state="done",
                aliases=("ls",),
            ),
            Command(
                name="add",
                help="追加一个回退目标（provider 或 provider/模型）",
                handler=run_fallback_add,
                group="core",
                state="done",
                options=[
                    Option(("provider",), "provider 或 provider/模型", nargs="?"),
                    Option(("--model",), "模型名（不给则用该家默认）", metavar="MODEL"),
                    Option(("--base-url",), "自定义 OpenAI 兼容端点", metavar="URL"),
                    Option(("--key-env",), "该端点密钥的环境变量名", metavar="ENV"),
                ],
            ),
            Command(
                name="remove",
                help="从回退链删除一项（给序号）",
                handler=run_fallback_remove,
                group="core",
                state="done",
                aliases=("rm",),
                options=[Option(("index",), "要删除的序号（`fallback list` 里的）", nargs="?")],
            ),
            Command(
                name="clear",
                help="清空全部回退项",
                handler=run_fallback_clear,
                group="core",
                state="done",
            ),
        ],
    ),
    Command(
        name="auth",
        help="管理池化的供应商凭证（密钥只进系统 keyring）",
        group="core",
        parity="P0",
        state="done",
        subcommands=_auth_subcommands(),
    ),
    Command(
        name="login",
        help="登录 api_key 类供应商（把密钥写进系统 keyring）",
        handler=run_login,
        group="core",
        parity="P1",
        state="done",
        options=[Option(("--provider",), "provider id（默认取当前 provider）", metavar="PROVIDER"), *_OAUTH_OPTIONS],
    ),
    Command(
        name="logout",
        help="清除供应商的鉴权状态（主凭证 + 该 provider 的凭证池）",
        handler=run_logout,
        group="core",
        parity="P1",
        state="done",
        options=[
            Option(("--provider",), "provider id（默认取当前 provider）", metavar="PROVIDER"),
        ],
    ),
    Command(
        name="moa",
        help="配置 Mixture of Agents 模型槽位（只做配置读写，运行时聚合未实现）",
        group="core",
        parity="P1",
        state="partial",
        subcommands=[
            Command(
                name="list",
                help="查看当前 MoA 模型槽位",
                handler=run_moa_list,
                group="core",
                state="done",
                aliases=("ls",),
            ),
            Command(
                name="configure",
                help="创建或更新一个 MoA 预设",
                handler=run_moa_configure,
                group="core",
                state="done",
                aliases=("config",),
                options=[
                    Option(("name",), "预设名（默认 default）", nargs="?"),
                    Option(
                        ("--reference",),
                        "reference 模型，可重复（provider/模型）",
                        action="append",
                        metavar="PROVIDER/MODEL",
                    ),
                    Option(("--aggregator",), "aggregator 模型（provider/模型）", metavar="PROVIDER/MODEL"),
                    Option(("--max-tokens",), "每个槽位输出上限", metavar="N"),
                    Option(("--disable",), "停用该预设", action="store_true"),
                ],
            ),
            Command(
                name="delete",
                help="删除一个 MoA 预设",
                handler=run_moa_delete,
                group="core",
                state="done",
                aliases=("rm",),
                options=[Option(("name",), "预设名", nargs="?")],
            ),
        ],
    ),
)
