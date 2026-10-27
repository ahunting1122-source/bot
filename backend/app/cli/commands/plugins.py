"""插件命令：列出、详情、启停、安装、移除、搜索、体检、能力面、打包、更新。

对齐 Hermes 的 `hermes plugins` 11 条子命令（调研 05 §4.8 的 `--help` 实测）：

| 子命令 | 本实现的状态 |
|---|---|
| `list` / `ls` | 完整（扫两个根 + 启停状态 + `--enabled/--user/--no-bundled/--plain`） |
| `show` / `info` | 完整（16 字段 + Orbit 扩展字段 + env 提示 + 扫描裁决） |
| `enable` / `disable` | 完整（落 `<data_dir>/plugins.json`，含 `allow_tool_override` 授权位） |
| `install` | **只支持本地目录**；远程源明确报"未实现 + 原因"（本机无网络） |
| `remove` | 完整（只删用户安装根，绝不碰随包插件） |
| `search` | **只在本地已装插件里搜**；社区索引需要网络，明确报未实现 |
| `doctor` | 完整（按真实运行期契约校验：entry 能不能 import、register 在不在、扫描裁决） |
| `capabilities` | 完整（19 种 `register_*` + 17 个 hook 事件，标出哪些有落点） |
| `pack` | `export` / `show` 可用；`install` 报未实现（要 git + 网络） |
| `update` | 报未实现（要远程源）；本地插件给出"重新扫描"的实际动作 |

**为什么不实现远程安装却仍登记这条命令**：命令面本身是对标物。少登记一条，`orbit
plugins --help` 就会比 Hermes 少一行，矩阵也会跟着丢条目；而"登记 + 诚实报错"既保住
命令面，又不谎称能用（报错里写清缺什么、缺在哪）。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml  # type: ignore[import-untyped]  # 本机无 types-PyYAML（无网络，不能装）

from app import plugins
from app.cli.context import CliError, Context
from app.cli.spec import REGISTRY, Command, Option

# ── 公共小工具 ──────────────────────────────────────────────────────────────


def _discovery(context: Context) -> plugins.DiscoveryResult:
    return plugins.discover(settings=context.settings)


def _state(context: Context) -> plugins.PluginState:
    return plugins.load_state(context.settings)


def _find(context: Context, name: str) -> plugins.PluginManifest:
    """按名字找已装插件（用户根与随包根都算）。"""
    result = _discovery(context)
    manifest = result.by_name(name)
    if manifest is None:
        known = "、".join(item.name for item in result.manifests) or "（一个都没有）"
        raise CliError(f"没有这个插件：{name}。已发现：{known}")
    return manifest


def _is_user_plugin(context: Context, manifest: plugins.PluginManifest) -> bool:
    """True = 装在 `<data_dir>/plugins/` 下（可删可改）；False = 随包插件。"""
    user_root = plugins.plugins_root(context.settings).resolve()
    return manifest.directory.resolve().is_relative_to(user_root)


def _is_remote(identifier: str) -> bool:
    """判断标识符是不是远程源（而不是本地目录）。

    远程的四种真实写法：git URL、`owner/repo` 简写、`.git` 结尾、带协议前缀。
    `owner/repo` 这条**必须排除已存在的本地相对路径**，否则 `orbit plugins install
    ./plugins/foo` 会被误判成远程。
    """
    text = identifier.strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered.startswith(("http://", "https://", "git@", "git://", "ssh://")):
        return True
    if lowered.endswith(".git"):
        return True
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", text) and not Path(text).exists():
        return True
    return False


def _declared_capabilities(manifest: plugins.PluginManifest) -> list[str]:
    """manifest 声明了哪些能力（由 provides_* / hooks / kind 推导）。

    **为什么从声明推导而不是读代码**：`plugins capabilities` 的价值在于"不 import
    插件就能回答它会碰哪些宿主表面"（Hermes 的定位：consent and audit layer）。
    """
    out: list[str] = []
    if manifest.provides_tools:
        out.append("register_tool")
    if manifest.hooks:
        out.append("register_hook")
    if manifest.provides_platform or manifest.kind == "platform":
        out.append("register_platform")
    if manifest.provides_provider or manifest.kind == "provider":
        out.append("register_model_provider")
    if manifest.provides_web_providers:
        out.append("register_web_provider")
    if manifest.provides_browser_providers:
        out.append("register_browser_provider")
    if manifest.kind == "dashboard":
        out.append("register_dashboard_page")
    return out


# ── list ────────────────────────────────────────────────────────────────────


def run_list(context: Context, args) -> int:
    """列出已发现插件（用户根 + 随包根）。"""
    result = _discovery(context)
    state = _state(context)
    rows: list[dict] = []
    for manifest in result.manifests:
        user = _is_user_plugin(context, manifest)
        if getattr(args, "user", False) and not user:
            continue
        if getattr(args, "no_bundled", False) and not user:
            continue
        enabled = state.is_enabled(manifest.name, manifest.enabled_by_default)
        if getattr(args, "enabled", False) and not enabled:
            continue
        rows.append(
            {
                "name": manifest.name,
                "version": manifest.version,
                "kind": manifest.kind,
                "origin": "user" if user else "bundled",
                "enabled": enabled,
                "description": manifest.description.splitlines()[0] if manifest.description else "",
            }
        )

    if getattr(args, "plain", False):
        for row in rows:
            context.out(f"{row['name']}\t{row['version']}\t{row['kind']}\t{row['origin']}")
    else:
        context.out(f"插件根：{'、'.join(str(item) for item in result.roots)}")
        context.table(
            ("插件", "版本", "kind", "来源", "状态", "说明"),
            [
                (
                    row["name"],
                    row["version"] or "-",
                    row["kind"],
                    row["origin"],
                    "启用" if row["enabled"] else "禁用",
                    row["description"],
                )
                for row in rows
            ],
        )
    for error in result.errors:
        context.warn(f"{error.path}：{error.message}")
    context.emit({"ok": True, "roots": [str(item) for item in result.roots], "plugins": rows})
    return 0


# ── show ────────────────────────────────────────────────────────────────────


def run_show(context: Context, args) -> int:
    """单插件详情：16 字段 + Orbit 扩展字段 + env 提示 + 扫描裁决。"""
    manifest = _find(context, str(getattr(args, "name", "") or ""))
    state = _state(context)
    scan = plugins.scan_plugin(
        manifest.directory, source=str(manifest.path), data_dir=context.data_dir
    )
    context.out(f"插件：{manifest.name}" + (f"（{manifest.label}）" if manifest.label else ""))
    context.out(f"版本：{manifest.version or '-'} · kind：{manifest.kind}（{plugins.KIND_TIMING[manifest.kind]}）")
    context.out(f"路径：{manifest.path}")
    context.out(f"来源：{'用户安装' if _is_user_plugin(context, manifest) else '随包'}")
    context.out(
        "状态："
        + ("启用" if state.is_enabled(manifest.name, manifest.enabled_by_default) else "禁用")
        + " · 内置工具覆盖授权："
        + ("是" if state.allow_tool_override(manifest.name) else "否")
    )
    if manifest.description:
        context.out()
        context.out(manifest.description.strip())
    context.out()
    context.table(
        ("字段", "值"),
        [
            ("author", manifest.author or "-"),
            ("license", manifest.license or "-"),
            ("homepage", manifest.homepage or "-"),
            ("entry", manifest.entry),
            ("platforms", "、".join(manifest.platforms) or "-"),
            ("min_orbit_version", manifest.min_orbit_version or "-"),
            ("enabled_by_default", "是" if manifest.enabled_by_default else "否"),
            ("provides_tools", "、".join(manifest.provides_tools) or "-"),
            ("provides_web_providers", "、".join(manifest.provides_web_providers) or "-"),
            ("provides_browser_providers", "、".join(manifest.provides_browser_providers) or "-"),
            ("pip_dependencies", "、".join(manifest.pip_dependencies) or "-"),
            ("hooks", "、".join(manifest.hooks) or "-"),
            ("unknown_fields", "、".join(manifest.unknown_fields) or "-"),
        ],
    )
    prompts = plugins.env_prompts(manifest)
    if prompts:
        context.out()
        context.out("环境变量：")
        context.table(
            ("变量", "必填", "密码框", "说明", "申请地址"),
            [
                (
                    item["name"],
                    "是" if item["required"] else "否",
                    "是" if item["password"] else "否",
                    item["description"],
                    item["url"] or "-",
                )
                for item in prompts
            ],
        )
    context.out()
    context.out(plugins.format_scan_report(scan))
    context.emit(
        {
            "ok": True,
            "plugin": manifest.as_dict(),
            "env_prompts": prompts,
            "state": {
                "enabled": state.is_enabled(manifest.name, manifest.enabled_by_default),
                "allow_tool_override": state.allow_tool_override(manifest.name),
            },
            "scan": scan.as_dict(),
        }
    )
    return 0


# ── enable / disable ────────────────────────────────────────────────────────


def run_enable(context: Context, args) -> int:
    """启用插件。`--allow-tool-override` 是**唯一**的内置工具覆盖授权位。"""
    name = str(getattr(args, "name", "") or "")
    manifest = _find(context, name)
    allow = getattr(args, "allow_tool_override", False)
    deny = getattr(args, "no_allow_tool_override", False)
    if allow and deny:
        raise CliError("--allow-tool-override 与 --no-allow-tool-override 不能同时给")
    override = True if allow else (False if deny else None)
    state = _state(context)
    state.enable(manifest.name, allow_tool_override=override)
    missing = manifest.missing_env()
    context.out(f"已启用：{manifest.name}")
    if override is not None:
        context.out(
            "内置工具覆盖授权：" + ("已授予（该插件可覆盖 shell_exec 等内置工具）" if override else "已拒绝")
        )
    if missing:
        # 缺必填凭证**不阻止启用**（Hermes 也是让插件自己在运行期报错），但必须说出来。
        context.warn(
            f"{manifest.name} 还缺必填环境变量：{'、'.join(missing)}——"
            f"现在启用它，运行到相关功能时会失败"
        )
    context.emit(
        {
            "ok": True,
            "plugin": manifest.name,
            "enabled": True,
            "allow_tool_override": state.allow_tool_override(manifest.name),
            "missing_env": list(missing),
        }
    )
    return 0


def run_disable(context: Context, args) -> int:
    name = str(getattr(args, "name", "") or "")
    manifest = _find(context, name)
    state = _state(context)
    state.disable(manifest.name)
    context.out(f"已禁用：{manifest.name}（目录保留，用 enable 可恢复）")
    context.emit({"ok": True, "plugin": manifest.name, "enabled": False})
    return 0


# ── install / remove ────────────────────────────────────────────────────────


def run_install(context: Context, args) -> int:
    """安装插件。**本轮只支持本地目录**。"""
    identifier = str(getattr(args, "identifier", "") or "").strip()
    if not identifier:
        raise CliError("用法：orbit plugins install <本地目录>（远程源未实现）")
    if _is_remote(identifier):
        raise CliError(
            f"远程安装未实现：{identifier}\n"
            "原因：本机无网络，且没有实现 git clone / 社区索引解析 / 40 位 SHA 钉版校验。"
            "本轮只支持 `orbit plugins install <本地目录>`。"
        )
    if getattr(args, "ref", None):
        raise CliError("--ref 只对远程安装有意义（本机无网络，远程安装未实现）")
    want_enable = getattr(args, "enable", False)
    want_disable = getattr(args, "no_enable", False)
    if want_enable and want_disable:
        raise CliError("--enable 与 --no-enable 不能同时给")
    enable = True if want_enable else (False if want_disable else None)
    source = Path(identifier).expanduser()
    if not source.is_dir():
        raise CliError(f"本地安装源不存在或不是目录：{source}")
    try:
        result = plugins.install_local(
            source, context.settings, force=bool(getattr(args, "force", False)), enable=enable
        )
    except plugins.PluginGuardError as exc:
        raise CliError(str(exc)) from exc
    except plugins.PluginError as exc:
        raise CliError(str(exc)) from exc
    context.out(f"已安装：{result.name} → {result.target}")
    context.out(f"扫描裁决：{result.verdict}（{result.findings} 项发现）· {result.reason}")
    context.out("状态：" + ("已启用" if result.enabled else "已装但禁用（用 enable 开启）"))
    context.emit({"ok": True, **result.as_dict()})
    return 0


def run_remove(context: Context, args) -> int:
    name = str(getattr(args, "name", "") or "")
    manifest = _find(context, name)
    if not _is_user_plugin(context, manifest):
        raise CliError(
            f"{manifest.name} 是随包插件（{manifest.directory}），remove 只删用户安装根。"
            "删随包插件等于改仓库代码，请改用 disable。"
        )
    try:
        target = plugins.remove_local(manifest.name, context.settings)
    except plugins.PluginError as exc:
        raise CliError(str(exc)) from exc
    context.out(f"已移除：{manifest.name}（{target}）")
    context.out(
        "注意：插件自己的数据在 "
        f"{plugins.plugin_data_dir(manifest.name, context.settings)}，未被删除"
    )
    context.emit({"ok": True, "plugin": manifest.name, "removed": str(target)})
    return 0


# ── search ──────────────────────────────────────────────────────────────────


def run_search(context: Context, args) -> int:
    """搜索插件。**只在本地已装插件里搜**——社区索引需要网络。"""
    if getattr(args, "refresh", False):
        raise CliError(
            "未实现：--refresh 需要联网重取社区插件索引；本机无网络，"
            "本轮 search 只在本地已装插件里匹配"
        )
    term = str(getattr(args, "term", "") or "").strip().lower()
    capability = str(getattr(args, "capability", "") or "").strip().lower()
    result = _discovery(context)
    hits: list[dict] = []
    for manifest in result.manifests:
        haystack = f"{manifest.name} {manifest.description} {manifest.label}".lower()
        if term and term not in haystack:
            continue
        declared = _declared_capabilities(manifest)
        if capability and not any(capability in item for item in declared):
            continue
        hits.append(
            {
                "name": manifest.name,
                "version": manifest.version,
                "kind": manifest.kind,
                "description": manifest.description.splitlines()[0] if manifest.description else "",
                "capabilities": declared,
            }
        )
    context.out("搜索范围：本地已装插件（社区索引需要网络，未实现）")
    if hits:
        context.table(
            ("插件", "版本", "kind", "声明能力", "说明"),
            [
                (
                    row["name"],
                    row["version"] or "-",
                    row["kind"],
                    "、".join(row["capabilities"]) or "-",
                    row["description"],
                )
                for row in hits
            ],
        )
    else:
        context.out("没有匹配的插件。")
    context.emit({"ok": True, "term": term, "capability": capability, "results": hits})
    return 0


# ── doctor ──────────────────────────────────────────────────────────────────


def _doctor_target(context: Context, raw: str | None) -> plugins.PluginManifest:
    """把 `doctor` 的 target 解析成 manifest：可以是目录、plugin.yaml 或已装插件名。"""
    text = str(raw or "").strip()
    if not text:
        text = "."
    candidate = Path(text).expanduser()
    if candidate.is_file():
        return plugins.load_manifest(candidate)
    if candidate.is_dir():
        manifest_path = candidate / "plugin.yaml"
        if not manifest_path.is_file():
            raise CliError(f"{candidate} 里没有 plugin.yaml")
        return plugins.load_manifest(manifest_path, root=candidate)
    return _find(context, text)


def run_doctor(context: Context, args) -> int:
    """按真实运行期契约校验插件。``--ci`` 时有 error 就返回非零。"""
    try:
        manifest = _doctor_target(context, getattr(args, "target", None))
    except plugins.PluginError as exc:
        raise CliError(str(exc)) from exc
    checks = plugins.doctor(manifest, data_dir=context.data_dir)
    context.out(f"体检：{manifest.name}（{manifest.path}）")
    context.table(
        ("级别", "检查项", "说明"),
        [(item.level, item.name, item.detail) for item in checks],
    )
    errors = [item for item in checks if item.level == "error"]
    warns = [item for item in checks if item.level == "warn"]
    context.out()
    context.out(f"结论：{len(errors)} 个错误 · {len(warns)} 个警告")
    context.emit(
        {
            "ok": not errors,
            "plugin": manifest.name,
            "checks": [item.as_dict() for item in checks],
            "errors": len(errors),
            "warnings": len(warns),
        }
    )
    if errors and getattr(args, "ci", False):
        return 1
    return 0


# ── capabilities ────────────────────────────────────────────────────────────


def run_capabilities(context: Context, args) -> int:
    """展示能力面：19 种 `register_*` 与 17 个 hook 事件，标出哪些有真实落点。"""
    from app.hooks import EVENT_EVIDENCE, EVENT_NOTES, HOOK_EVENTS, INFERRED_EVENTS

    name = str(getattr(args, "name", "") or "").strip()
    payload: dict = {
        "ok": True,
        "register_methods": [
            {
                "name": capability,
                "landed": capability in plugins.LANDED_CAPABILITIES,
                "detail": plugins.LANDED_CAPABILITIES.get(capability)
                or plugins.UNSUPPORTED_CAPABILITIES.get(capability, ""),
            }
            for capability in plugins.PLUGIN_CAPABILITIES
        ],
        "hook_events": [
            {
                "event": event,
                "evidence": EVENT_EVIDENCE[event],
                "note": EVENT_NOTES.get(event, ""),
            }
            for event in (*HOOK_EVENTS, *INFERRED_EVENTS)
        ],
    }

    if name:
        manifest = _find(context, name)
        state = _state(context)
        declared = _declared_capabilities(manifest)
        payload["plugin"] = {
            "name": manifest.name,
            "declared": declared,
            "granted": {
                "enabled": state.is_enabled(manifest.name, manifest.enabled_by_default),
                "allow_tool_override": state.allow_tool_override(manifest.name),
            },
            "hooks": list(manifest.hooks),
        }
        context.out(f"插件：{manifest.name}")
        context.table(
            ("声明能力", "有落点", "说明"),
            [
                (
                    item,
                    "是" if item in plugins.LANDED_CAPABILITIES else "否",
                    plugins.LANDED_CAPABILITIES.get(item)
                    or plugins.UNSUPPORTED_CAPABILITIES.get(
                        item, "（manifest 声明，但不在 Orbit 的 19 种能力面里）"
                    ),
                )
                for item in declared
            ],
        )
        context.out()
        context.out(
            "已授予："
            + ("启用" if state.is_enabled(manifest.name, manifest.enabled_by_default) else "禁用")
            + " · 内置工具覆盖："
            + ("是" if state.allow_tool_override(manifest.name) else "否")
        )
        context.emit(payload)
        return 0

    landed = len(plugins.LANDED_CAPABILITIES)
    context.out(
        f"能力面：{len(plugins.PLUGIN_CAPABILITIES)} 种 register_*（{landed} 种有落点，"
        f"{len(plugins.PLUGIN_CAPABILITIES) - landed} 种显式不支持）"
    )
    context.out(
        "定位：能力是**同意与审计层**，不是沙箱——插件与 agent 同进程同权限。"
    )
    context.out()
    context.table(
        ("register_*", "有落点", "落点 / 不支持的原因"),
        [
            (
                capability,
                "是" if capability in plugins.LANDED_CAPABILITIES else "否",
                plugins.LANDED_CAPABILITIES.get(capability)
                or plugins.UNSUPPORTED_CAPABILITIES.get(capability, ""),
            )
            for capability in plugins.PLUGIN_CAPABILITIES
        ],
    )
    context.out()
    context.out(
        f"hook 事件：{len(HOOK_EVENTS)} 个实证 + {len(INFERRED_EVENTS)} 个推断"
        f"（推断项不计入 17）"
    )
    context.table(
        ("事件", "证据", "语义"),
        [
            (event, "实证" if EVENT_EVIDENCE[event] == "manifest" else "推断", EVENT_NOTES.get(event, ""))
            for event in (*HOOK_EVENTS, *INFERRED_EVENTS)
        ],
    )
    context.emit(payload)
    return 0


# ── pack ────────────────────────────────────────────────────────────────────


def run_pack(context: Context, args) -> int:
    """插件集（`orbit-pack.yaml`）：`export` / `show` 可用，`install` 未实现。"""
    # argparse 的 dest 是 `_pack_command`（`main._add_command` 按 `_<父命令>_command`
    # 命名），不是 `pack_command`——写错就会静默落到最后一个分支（install 报未实现）。
    action = str(getattr(args, "_pack_command", "") or "")
    result = _discovery(context)
    state = _state(context)

    if action == "export":
        payload = {
            "version": 1,
            "plugins": [
                {
                    "name": manifest.name,
                    "version": manifest.version,
                    "kind": manifest.kind,
                    "origin": "user" if _is_user_plugin(context, manifest) else "bundled",
                    "enabled": state.is_enabled(manifest.name, manifest.enabled_by_default),
                    # 钉版位：远程安装未实现，这里只记声明版本（不是 commit SHA）。
                    "ref": "",
                }
                for manifest in result.manifests
            ],
        }
        text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        target = getattr(args, "name", None)
        if target:
            path = Path(str(target)).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            context.out(f"已写出 {path}")
        else:
            context.out(text.rstrip())
        context.emit({"ok": True, "pack": payload})
        return 0

    source = str(getattr(args, "source", "") or "").strip()
    if not source:
        raise CliError("用法：orbit plugins pack show <orbit-pack.yaml>")
    path = Path(source).expanduser()
    if not path.is_file():
        raise CliError(f"pack 文件不存在：{path}（https URL 需要网络，未实现）")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise CliError(f"读不了 pack 文件 {path}：{exc}") from exc
    entries = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise CliError(f"{path} 里没有 plugins 列表")
    installed = {manifest.name: manifest for manifest in result.manifests}
    rows = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        local = installed.get(name)
        rows.append(
            (
                name,
                str(entry.get("version") or "-"),
                str(entry.get("ref") or "-"),
                "已装" if local else "缺",
            )
        )
    if action == "show":
        context.table(("插件", "版本", "钉版 ref", "本机"), rows)
        context.emit({"ok": True, "pack": data, "rows": rows})
        return 0
    # install
    raise CliError(
        "pack install 未实现：它要 fan-out 成逐个 pinned 的远程安装（git clone + "
        "40 位 SHA 校验），本机无网络。可用 `pack show` 先看清单。"
    )


# ── update ──────────────────────────────────────────────────────────────────


def run_update(context: Context, args) -> int:
    """更新插件。远程 `git pull` 未实现；本地插件给"重新扫描"这个真实动作。"""
    name = str(getattr(args, "name", "") or "")
    manifest = _find(context, name)
    if not _is_user_plugin(context, manifest):
        raise CliError(
            f"{manifest.name} 是随包插件，更新它等于改仓库代码；随包插件随 Orbit 一起升级"
        )
    scan = plugins.scan_plugin(
        manifest.directory, source=str(manifest.path), data_dir=context.data_dir
    )
    context.out(f"{manifest.name} 是本地安装（无远程源可 pull）。")
    context.out("已就地重新扫描当前目录：")
    context.out(plugins.format_scan_report(scan))
    context.out()
    context.out(
        "未实现：远程 git pull 更新（本机无网络，也没有实现 commit SHA 钉版与 diff 审查）。"
        "要换版本请 `plugins remove` 后 `plugins install <新目录>`。"
    )
    context.emit({"ok": True, "plugin": manifest.name, "scan": scan.as_dict()})
    return 0


# ── 命令树 ──────────────────────────────────────────────────────────────────

_PLUGIN_COMMAND = Command(
    name="plugins",
    help="插件管理与校验",
    group="ext",
    hermes="plugins",
    parity="P0",
    # `partial` 是诚实的：list/show/enable/disable/remove/doctor/capabilities 完整，
    # 但 install 只支持本地目录、search 只在本地匹配、pack install 与 update 的远程
    # 拉取未实现（本机无网络）。标 done 会让对标矩阵撒谎。
    state="partial",
    description=(
        "安装、更新、移除、列出或校验原生插件。\n"
        "能力（capabilities）是同意与审计层，**不是沙箱**——插件与 agent 同进程同权限。\n"
        "本轮 install 只支持本地目录：远程源需要网络，明确报未实现。"
    ),
    needs_settings=False,
    subcommands=[
        Command(
            name="list",
            help="列出已发现插件",
            handler=run_list,
            group="ext",
            state="done",
            needs_settings=False,
            aliases=("ls",),
            options=[
                Option(("--enabled",), "只看已启用的", action="store_true"),
                Option(("--user",), "只看用户安装的", action="store_true"),
                Option(("--no-bundled",), "隐藏随包插件", action="store_true"),
                Option(("--plain",), "紧凑纯文本输出（不用表格）", action="store_true"),
            ],
        ),
        Command(
            name="show",
            help="查看单个插件详情",
            handler=run_show,
            group="ext",
            state="done",
            needs_settings=False,
            aliases=("info",),
            options=[Option(("name",), "插件名或 key", metavar="NAME")],
        ),
        Command(
            name="enable",
            help="启用插件",
            handler=run_enable,
            group="ext",
            state="done",
            needs_settings=True,
            options=[
                Option(
                    ("--allow-tool-override",),
                    "授予该插件覆盖内置工具（shell_exec、write_file 等）的权限",
                    action="store_true",
                ),
                Option(
                    ("--no-allow-tool-override",),
                    "启用但不授予内置工具覆盖（跳过确认提示）",
                    action="store_true",
                ),
                Option(("name",), "插件名", metavar="NAME"),
            ],
        ),
        Command(
            name="disable",
            help="禁用插件（不卸载）",
            handler=run_disable,
            group="ext",
            state="done",
            needs_settings=True,
            options=[Option(("name",), "插件名", metavar="NAME")],
        ),
        Command(
            name="install",
            help="从本地目录安装插件（远程源未实现）",
            handler=run_install,
            group="ext",
            state="partial",
            needs_settings=True,
            options=[
                Option(("--force", "-f"), "覆盖已有同名插件目录（**不覆盖 dangerous 裁决**）", action="store_true"),
                Option(("--ref",), "远程安装用的 40 位 commit SHA（本机无网络，给了会报错）", metavar="COMMIT_SHA"),
                Option(("--enable",), "安装后自动启用", action="store_true"),
                Option(("--no-enable",), "装成禁用，稍后用 enable 开启", action="store_true"),
                Option(("identifier",), "本地目录路径（Git URL / owner/repo 未实现）", metavar="IDENTIFIER"),
            ],
        ),
        Command(
            name="remove",
            help="移除用户安装的插件",
            handler=run_remove,
            group="ext",
            state="done",
            needs_settings=True,
            aliases=("rm", "uninstall"),
            options=[Option(("name",), "插件名（只删用户安装根）", metavar="NAME")],
        ),
        Command(
            name="search",
            help="搜索插件（本地已装；社区索引需要网络）",
            handler=run_search,
            group="ext",
            state="partial",
            needs_settings=False,
            options=[
                Option(("--capability",), "按声明能力过滤（如 tool/platform/hooks）", metavar="CAP"),
                Option(("--refresh",), "联网重取社区索引（未实现，给了会报错）", action="store_true"),
                Option(("term",), "搜索词（省略则列出全部本地插件）", nargs="?"),
            ],
        ),
        Command(
            name="doctor",
            help="按真实运行期契约校验插件",
            handler=run_doctor,
            group="ext",
            state="done",
            needs_settings=False,
            options=[
                Option(("--ci",), "有 error 时以非零码退出", action="store_true"),
                Option(("target",), "插件目录 / plugin.yaml / 已装插件名（默认当前目录）", nargs="?"),
            ],
        ),
        Command(
            name="capabilities",
            help="查看声明能力 vs 已授予能力（19 种 register_* + 17 个 hook 事件）",
            handler=run_capabilities,
            group="ext",
            state="done",
            needs_settings=False,
            options=[Option(("name",), "插件名（省略则列出能力面本身）", nargs="?")],
        ),
        Command(
            name="pack",
            help="插件集（orbit-pack.yaml）：export / show 可用，install 未实现",
            group="ext",
            state="partial",
            needs_settings=False,
            subcommands=[
                Command(
                    name="export",
                    help="把当前安装导出成 pack YAML",
                    handler=run_pack,
                    group="ext",
                    state="done",
                    needs_settings=False,
                    options=[Option(("--name",), "写入该文件（省略则输出到 stdout）", metavar="NAME")],
                ),
                Command(
                    name="show",
                    help="解析并展示一个 pack（干跑）",
                    handler=run_pack,
                    group="ext",
                    state="done",
                    needs_settings=False,
                    options=[Option(("source",), "pack 文件路径", metavar="SOURCE")],
                ),
                Command(
                    name="install",
                    help="安装一个 pack（需要网络，未实现）",
                    handler=run_pack,
                    group="ext",
                    state="partial",
                    needs_settings=False,
                    options=[
                        Option(("--force", "-f"), "覆盖已装插件", action="store_true"),
                        Option(("source",), "pack 文件路径或 https URL", metavar="SOURCE"),
                    ],
                ),
            ],
        ),
        Command(
            name="update",
            help="更新插件（远程 pull 未实现；本地插件就地重新扫描）",
            handler=run_update,
            group="ext",
            state="partial",
            needs_settings=False,
            options=[Option(("name",), "插件名", metavar="NAME")],
        ),
    ],
)


def _install_command() -> None:
    """把本模块的命令树装进注册表。

    **为什么要这个函数而不是一句 ``REGISTRY.add``**：声明表 ``hermes_parity`` 里已经
    有 `plugins` 这一条（W10 占位，``state="todo"``）。`commands/__init__.py` 的导入
    顺序约定是"真实现排在声明表之前"，但本轮交付约束是**不改** ``__init__.py``，
    所以本模块可能在占位之后才被导入（例如测试里 ``import app.cli.commands.plugins``）。
    那时 ``REGISTRY.add`` 会抛"命令重复登记：plugins"。

    因此这里做的是"**有占位就就地替换**"：名字不变、子命令与 handler 换成真实现。
    父级接线完成后（把本模块排到 ``hermes_parity`` 之前），走的是 ``REGISTRY.add``
    那条分支，两条路径都正确。
    """
    existing = REGISTRY.get("plugins")
    if existing is None:
        REGISTRY.add(_PLUGIN_COMMAND)
        return
    REGISTRY._commands["plugins"] = _PLUGIN_COMMAND  # noqa: SLF001 —— Registry 没给替换 API


_install_command()

__all__ = [
    "run_capabilities",
    "run_disable",
    "run_doctor",
    "run_enable",
    "run_install",
    "run_list",
    "run_pack",
    "run_remove",
    "run_search",
    "run_show",
    "run_update",
]
