"""`security` / `secrets` / `egress` 三条命令面的实现。

## 接线方式（`_table.register_handler`）

这三族的命令**已经在声明表里**（`hermes_parity.py`，波次 W13），所以本模块不新建
`Command`，而是把实现"填"到表里已有的路径上：

    register_handler("security audit", run_audit)

导入顺序上本模块必须排在 `hermes_parity` 之前（`commands/__init__.py` 里的排序），
但 `import app.cli.commands.security` 会先跑包初始化——那时表可能**已经组装完毕**，
`HANDLERS` 里的登记不会回填到已构建的 `Command` 上（`sessions.py` 的 `_bind` 记过
这个坑）。所以这里同样做一次回填，并把表里**缺的两处**补上：

- 声明表的 `secrets` 下只有 `bitwarden` / `onepassword` 两棵子树，没有
  `status` / `set` / `get` / `remove` / `sync`；而 `orbit config set` 的报错
  （`core.py`）已经在教用户跑 `orbit secrets set <键>`。命令面缺口由
  :func:`mount_secrets_subcommands` 在导入时补上（幂等）。
- `security audit` 声明了 `--fail-on` / `--skip-*`，但没有 `--venv PATH`；
  由 :func:`mount_audit_options` 补上。

两处都是"往已构建的注册表上挂东西"，所以**只在表已存在时生效**；表还没组装时静默
跳过，交给 `register_specs` 去填——两种顺序都不炸。

## 关于退出码（与 Hermes 的刻意差异）

Hermes 的 `security audit` 默认 `--fail-on=critical`，低于该级别退出码是 0。
Orbit 更严：**任何命中都退出 1**。理由：供应链命中不是"告警级别问题"，静默的 0
退出码会被 CI 直接吞掉，而审计的全部价值就在"拦住流水线"。`--fail-on` 仍然被接受
（命令面一致），但只影响报告里的 `fail_on` 字段——这一点在输出里**明说**，不留
"这个 flag 好像没用"的疑问。

## 关于 `egress`

**不假装能用。** iron-proxy 是一个真实的 TLS 中间人代理进程：要下载二进制、生成 CA、
铸令牌、写 `proxy.yaml`、跑守护进程。Orbit 没有这一层，`egress` 一族如实报
"未配置 / 未实现"，并打印配置**形状**（让人知道接进来需要什么），而不是假装
"已启用"或返回 0 的空壳。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from app import security_audit
from app.cli.commands import _table
from app.cli.commands._table import register_handler
from app.cli.context import CliError, Context
from app.cli.spec import REGISTRY, Command, Option
from app.secrets import SecretResolver
from app.secrets import service_name as keyring_service

#: 默认的 `--fail-on`。与 Hermes 的命令面一致，但见模块 docstring 的"退出码"一节。
DEFAULT_FAIL_ON = "critical"

#: 出口代理的配置文件**本应**叫什么（Hermes 叫 `proxy.yaml`）。
#: Orbit 里这个文件永远不存在——写在这里是为了让 `egress config` 能给出确定的答案。
PROXY_YAML = "proxy.yaml"

#: 配置形状的说明（`egress status` / `egress setup` 共用）。
#:
#: 抄的是 Hermes `egress setup` 的语义：沙箱里只放**代理令牌**，真实供应商密钥留在
#: 代理侧；请求出去时由代理按令牌换成真凭据。形状写出来，是为了让"要接这一层"
#: 的人知道缺什么，而不是留一句"未实现"。
EGRESS_SHAPE: tuple[tuple[str, str], ...] = (
    ("tunnel_port", "9090（默认；--tunnel-port 可覆盖）"),
    ("credential_source", "env 或 bitwarden（--from-bitwarden / --no-bitwarden）"),
    ("providers[].name", "供应商 id，如 openai / anthropic"),
    ("providers[].upstream", "真实上游地址，如 https://api.openai.com"),
    ("providers[].token", "代理令牌（沙箱里只放这个，--rotate-tokens 重新铸造）"),
    ("providers[].credential", "真实密钥的引用（env 名或 keyring 引用），**留在代理侧**"),
    ("ca", "iron-proxy 自签 CA，供沙箱信任 TLS 中间人"),
)


# ── security audit ──────────────────────────────────────────────────────────


def run_audit(context: Context, args: Any) -> int:
    """跑三项供应链审计：venv 通告、插件危险模式、MCP 明文密钥。

    退出码：**任何命中 → 1**（见模块 docstring）。缓存缺失不算命中，但报告里会
    明确写"未审计"，不会输出"未发现漏洞"。
    """
    data_dir = context.data_dir
    fail_on = str(getattr(args, "fail_on", None) or DEFAULT_FAIL_ON)
    reports: list[security_audit.AuditReport] = []

    if not getattr(args, "skip_venv", False):
        raw_venv = getattr(args, "venv", None)
        site_packages = Path(raw_venv) if raw_venv else security_audit.default_site_packages()
        # 注意：这里**不**走 `app/osv.py`。两者扫的不是一回事——
        # `audit_venv` 扫「这个 venv 里装了什么」，`osv.audit_local` 扫
        # 「项目声明了什么」。`--venv PATH` 的语义是前者，换过去会忽略用户指定的 venv。
        reports.append(security_audit.audit_venv(site_packages, data_dir=data_dir))
    if not getattr(args, "skip_plugins", False):
        reports.append(security_audit.audit_plugins(data_dir / "plugins"))
    if not getattr(args, "skip_mcp", False):
        reports.append(security_audit.audit_mcp(data_dir / "mcp.json"))

    findings = [finding for report in reports for finding in report.findings]

    context.out("供应链审计")
    context.out()
    labels = {"venv": "已安装分发包（OSV 通告）", "plugins": "插件目录", "mcp": "MCP 配置"}
    for report in reports:
        context.out(f"■ {labels.get(report.kind, report.kind)}：{report.headline()}")
        context.out(f"  扫描：{report.scanned} 项")
        if report.cache_path:
            where = report.cache_updated_at or "（无更新时间为）"
            context.out(f"  本地通告缓存：{report.cache_path}（更新于 {where}）")
        if report.note:
            # 缓存缺失的提示必须原样打出来：它是"这不是安全结论"的唯一依据。
            context.out(f"  [注意] {report.note}")
        if report.findings:
            context.table(
                ("严重度", "对象", "说明"),
                [(item.severity, item.target, item.detail) for item in report.findings],
            )
        context.out()

    if findings:
        context.out(f"合计命中 {len(findings)} 条（最严重：{security_audit.worst(findings)}）")
        context.out("退出码 1：Orbit 在任何命中时都返回非零，不按 --fail-on 放宽。")
    else:
        audited = [report for report in reports if report.audited]
        missing = [report for report in reports if report.cache_missing]
        if missing:
            context.out("没有命中——但**有审计项因为缺少本地通告数据而没有真正执行**，见上方 [注意]。")
        elif audited:
            context.out("没有命中。")
        else:
            context.out("没有任何审计项真正执行（目标不存在或未实现），见上方说明。")

    payload = {
        "ok": not findings,
        "command": "security audit",
        "fail_on": fail_on,
        "fail_on_note": "Orbit 在任何命中时退出 1；--fail-on 只记录，不放宽退出码",
        "findings": len(findings),
        "worst": security_audit.worst(findings),
        "reports": [report.as_dict() for report in reports],
    }
    context.emit(payload)
    if findings and context.json:
        # `main` 只在退出码为 0 时 flush `--json` 载荷；本命令命中时退出码是 1，
        # 所以这里自己打一次——否则 `security audit --json` 在有命中时是**静默**的，
        # 而"有命中"恰恰是最需要机器可读输出的那一次。
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 1 if findings else 0


# ── secrets ─────────────────────────────────────────────────────────────────


def _resolver(context: Context) -> SecretResolver:
    """装配默认密钥源。**单点可替换**：测试 monkeypatch 这个函数即可，
    不需要碰真实的 Windows 凭据管理器。"""
    return SecretResolver.default(context.data_dir)


def run_secrets_status(context: Context, args: Any) -> int:
    """列出各密钥源的可用性与键名清单。**绝不打印任何值。**"""
    resolver = _resolver(context)
    rows = resolver.status()
    # 逐个 resolve 一遍只为填充 `shadowed`（同名冲突信息），取到的值立即丢弃。
    for key in resolver.all_keys():
        resolver.resolve(key)

    context.out("密钥源（值一律不打印；只有键名与可用性）")
    context.out()
    context.table(
        ("源", "可用", "键名"),
        [
            (
                str(row["source"]),
                "是" if row["available"] else "否",
                "、".join(row["keys"]) if row["keys"] else "（列不出 / 未配置）",
            )
            for row in rows
        ],
    )
    context.out()
    for row in rows:
        context.out(f"· {row['source']}：{row['describe']}")
    if resolver.shadowed:
        context.out()
        for key, losers in sorted(resolver.shadowed.items()):
            context.out(
                f"[注意] {key}：{resolver.origin.get(key, '?')} 里的值生效，"
                f"{'、'.join(losers)} 里的同名键被遮蔽（改了也不生效）"
            )
    context.out()
    context.out(f"keyring service = {keyring_service(context.data_dir)}")

    context.emit(
        {
            "ok": True,
            "command": "secrets status",
            "keyring_service": keyring_service(context.data_dir),
            "sources": rows,
            "shadowed": resolver.shadowed,
            "keys": resolver.all_keys(),
        }
    )
    return 0


def _read_value(args: Any) -> str:
    """从 **stdin** 读密钥值。

    密钥**不走命令行参数**：`orbit secrets set KEY sk-xxx` 会把明文留在 shell 历史、
    `ps` 输出和 CI 日志里。`core.py` 的 `config set` 已经因为同一个理由拒绝过密钥。
    """
    if not getattr(args, "stdin", False) and sys.stdin.isatty():
        raise CliError(
            "没有从 stdin 读到值。用法：\n"
            "  echo -n <值> | orbit secrets set <键>\n"
            "  orbit secrets set <键> --stdin < 文件\n"
            "（值不走命令行参数——那会进 shell 历史与进程列表。）"
        )
    try:
        return sys.stdin.read().strip()
    except OSError as exc:  # 例如 pytest 的 stdin 占位对象
        raise CliError(f"无法从 stdin 读取：{exc}") from exc


def run_secrets_set(context: Context, args: Any) -> int:
    """把 stdin 里的值写进**最高优先级的可用可写源**（默认 keyring）。"""
    key = str(getattr(args, "key", "") or "").strip()
    if not key:
        raise CliError(
            "用法：orbit secrets set <键>（值从 stdin 读）\n"
            "例：echo -n \"$KEY\" | orbit secrets set model_api_key"
        )
    value = _read_value(args)
    if not value:
        raise CliError("stdin 是空的，没有写入任何东西。")
    resolver = _resolver(context)
    target = resolver.set(key, value)
    tail = value[-4:] if len(value) >= 4 else ""
    context.out(f"已写入 {target}：{key}（值不回显，末 4 位 …{tail}）")
    if target != "keyring":
        context.warn(
            f"写的是 {target} 而不是系统 keyring——明文风险由你承担。"
            f"先修好 keyring（`orbit doctor`）再重写一次。"
        )
    context.emit({"ok": True, "command": "secrets set", "key": key, "source": target})
    return 0


def run_secrets_get(context: Context, args: Any) -> int:
    """取一个键。**默认脱敏**，`--reveal` 才打印明文。

    默认脱敏的理由：终端输出会进 scrollback、`tee` 的文件、CI 日志。要拿明文给脚本用
    的场合必须显式 `--reveal`——让人为"我要明文"这个动作签一次字。
    """
    key = str(getattr(args, "key", "") or "").strip()
    if not key:
        raise CliError("用法：orbit secrets get <键> [--reveal]")
    resolver = _resolver(context)
    value = resolver.resolve(key)
    if value is None:
        raise CliError(
            f"没有找到密钥：{key}（`orbit secrets status` 看各源可用性；"
            f"`orbit secrets set {key}` 写入系统 keyring）"
        )
    origin = resolver.origin.get(key, "?")
    reveal = bool(getattr(args, "reveal", False))
    if reveal:
        context.out(value)
    else:
        tail = value[-4:] if len(value) >= 4 else ""
        context.out(f"{key} = …{tail}（来源：{origin}；要明文加 --reveal）")
    payload: dict[str, Any] = {
        "ok": True,
        "command": "secrets get",
        "key": key,
        "source": origin,
        "revealed": reveal,
    }
    if reveal:
        payload["value"] = value
    context.emit(payload)
    return 0


def run_secrets_remove(context: Context, args: Any) -> int:
    """从所有可写源里删掉一个键。"""
    key = str(getattr(args, "key", "") or "").strip()
    if not key:
        raise CliError("用法：orbit secrets remove <键>")
    removed = _resolver(context).remove(key)
    if removed:
        context.out(f"已从 {'、'.join(removed)} 移除：{key}")
    else:
        context.out(f"没有可移除的：{key}（各源里都没有，或源不可写）")
    context.emit({"ok": True, "command": "secrets remove", "key": key, "removed": removed})
    return 0


def run_secrets_sync(context: Context, args: Any) -> int:
    """报告"每个源里有什么"。外部源的**取值路径未实现**，所以不会假装同步成功。"""
    resolver = _resolver(context)
    keys = resolver.keys()
    context.out("密钥源清点（同步是「把外部源的值拉进来」，本轮只做本地源）")
    context.out()
    context.table(
        ("源", "键名"),
        [
            (name, "、".join(names) if names else "（空 / 列不出）")
            for name, names in keys.items()
        ],
    )
    context.out()
    context.out(
        "外部源（bitwarden / onepassword）的取值路径未实现——解锁需要联网与交互式会话，"
        "本轮不做，所以**没有发生任何同步**。"
    )
    context.out("本地源之间不需要同步：keyring 已经是最高优先级，env 只读。")
    applied = bool(getattr(args, "apply", False))
    if applied:
        context.warn("--apply 没有可执行的动作：外部源未实现，本轮不会写入任何东西。")
    context.emit(
        {
            "ok": True,
            "command": "secrets sync",
            "sources": keys,
            "applied": False,
            "note": "外部源取值路径未实现（需要联网解锁），本轮没有发生同步",
        }
    )
    return 0


# ── egress ──────────────────────────────────────────────────────────────────


def _print_egress_shape(context: Context) -> None:
    context.out("配置形状（Hermes 的 proxy.yaml；Orbit 尚未落地）：")
    for key, meaning in EGRESS_SHAPE:
        context.out(f"  {key:24} {meaning}")


def run_egress_status(context: Context, args: Any) -> int:
    """如实报"未配置"。Orbit 没有代理进程，所以这里没有"运行中/已停止"可报。"""
    path = context.data_dir / PROXY_YAML
    context.out("出口凭证注入（iron-proxy 风格）：未配置 · 未实现")
    context.out()
    context.out("Orbit 里没有代理进程：没有 iron-proxy 二进制、没有 CA、没有令牌铸造，")
    context.out(f"也没有 {path.name}。模型与工具的出站请求由 httpx 直连，密钥在进程内使用，")
    context.out("**不经过任何令牌代换代理**。")
    context.out()
    context.out(f"配置文件（应存在但不存在）：{path}")
    context.out()
    _print_egress_shape(context)
    if getattr(args, "show_tokens", False):
        context.out()
        context.out("--show-tokens：没有令牌可显示（代理未实现，一枚令牌都没铸过）。")
    context.emit(
        {
            "ok": True,
            "command": "egress status",
            "configured": False,
            "implemented": False,
            "config_path": str(path),
            "config_exists": path.exists(),
            "tokens": [],
            "shape": [{"key": key, "meaning": meaning} for key, meaning in EGRESS_SHAPE],
        }
    )
    return 0


def run_egress_config(context: Context, args: Any) -> int:
    """打印**本应**生成的 `proxy.yaml` 路径，并说清它不存在。"""
    path = context.data_dir / PROXY_YAML
    context.out(str(path))
    context.out()
    context.out(
        "这个文件不存在：`egress setup` 未实现（需要下载 iron-proxy、生成 CA、"
        "铸造令牌、写配置）。Orbit 不生成一个空壳 proxy.yaml 来假装「已配置」。"
    )
    _print_egress_shape(context)
    context.emit(
        {
            "ok": True,
            "command": "egress config",
            "config_path": str(path),
            "config_exists": path.exists(),
            "implemented": False,
        }
    )
    return 0


def run_egress_setup(context: Context, args: Any) -> int:
    """向导未实现。**抛错而不是返回 0**：返回 0 会被脚本读成"配好了"。"""
    raise CliError(
        "`orbit egress setup` 未实现：Orbit 没有 iron-proxy 代理进程。\n"
        "需要的东西：iron-proxy 二进制、自签 CA、按供应商铸造的代理令牌、proxy.yaml、"
        "一个受管守护进程。本轮只提供 status / config 两条如实报告命令。\n"
        "配置形状（proxy.yaml）："
        + "；".join(f"{key} = {meaning}" for key, meaning in EGRESS_SHAPE),
        code=3,
    )


# ── 接线 ────────────────────────────────────────────────────────────────────


def _bind(path: str, handler: Any) -> None:
    """把实现登记到声明表的某条路径上（两种导入顺序都要成立）。

    与 `sessions.py::_bind` 同一套做法：`register_handler` 负责"表还没组装"的情况，
    回填负责"表已经组装完"的情况。
    """
    register_handler(path, handler)
    head, _, leaf = path.partition(" ")
    parent = REGISTRY.get(head)
    if parent is None:
        return
    target = parent.child(leaf) if leaf else parent
    if target is not None and not target.subcommands:
        target.handler = handler
        target.state = "done"


#: 声明表里**缺的** `secrets` 子命令：`(名字, 中文说明, 选项)`。
#:
#: 为什么由本模块补：`core.py` 的 `config set` 已经在报错里教用户跑
#: `orbit secrets set <键>`，声明表却只登记了 `secrets bitwarden/onepassword`。
#: 命令面缺口留着会让那句报错指向一条不存在的命令。
SECRETS_SUBCOMMANDS: tuple[tuple[str, str, tuple[Option, ...]], ...] = (
    ("status", "列出各密钥源的可用性与键名（绝不打印值）", ()),
    (
        "set",
        "把 stdin 里的值写进最高优先级的可用源（默认系统 keyring）",
        (
            Option(("--stdin",), "明确表示从标准输入读值（默认行为也是 stdin）", action="store_true"),
            Option(("key",), "键名，如 model_api_key", nargs="?"),
        ),
    ),
    (
        "get",
        "取一个键（默认脱敏，--reveal 才打印明文）",
        (
            Option(("--reveal",), "打印明文（会留在 scrollback / 日志里）", action="store_true"),
            Option(("key",), "键名", nargs="?"),
        ),
    ),
    ("remove", "从所有可写源里删除一个键", (Option(("key",), "键名", nargs="?"),)),
    (
        "sync",
        "清点各源里的键（外部源取值未实现，本轮不同步）",
        (Option(("--apply",), "尝试写入（外部源未实现，本轮无动作）", action="store_true"),),
    ),
)


def mount_secrets_subcommands() -> list[str]:
    """把 `SECRETS_SUBCOMMANDS` 挂到已存在的 `secrets` 命令上。**幂等**。

    返回本次真正挂上去的名字；表还没组装（`secrets` 不存在）时返回空列表——
    那种情况下 `register_handler` 的登记会在 `register_specs` 组装时生效，
    但**前提是声明表里有对应条目**，所以接线方仍需在表里补 5 条 `Spec`，
    或者把本模块排在 `hermes_parity` 之后导入。
    """
    parent = REGISTRY.get("secrets")
    if parent is None:
        return []
    mounted: list[str] = []
    for name, help_text, options in SECRETS_SUBCOMMANDS:
        if parent.child(name) is not None:
            continue
        parent.subcommands.append(
            Command(
                name=name,
                help=help_text,
                handler=_SECRETS_HANDLERS.get(name),
                group="core",
                state="done",
                options=list(options),
            )
        )
        mounted.append(name)
    return mounted


def mount_audit_options() -> bool:
    """给 `security audit` 补 `--venv PATH`（声明表里只有 `--fail-on` / `--skip-*`）。"""
    parent = REGISTRY.get("security")
    audit = parent.child("audit") if parent is not None else None
    if audit is None:
        return False
    if any("--venv" in option.flags for option in audit.options):
        return False
    audit.options.append(
        Option(
            ("--venv",),
            "扫描哪个 site-packages 目录（默认当前解释器的）",
            metavar="PATH",
        )
    )
    return True


#: 组装钩子是否已装过（幂等标记）。用模块级布尔而不是给函数挂属性：
#: 后者会被 ruff 的 B010 判为"setattr 常量属性名"，也确实没必要。
_ASSEMBLY_HOOK_INSTALLED = False


def _install_assembly_hook() -> bool:
    """在"表组装"这个唯一接缝上补挂缺失的命令面。

    为什么需要这一步：`commands/__init__.py` 把本模块排在 `hermes_parity` **之前**，
    所以导入本模块时表还没组装（`REGISTRY.get("secrets")` 是 None），
    :func:`mount_secrets_subcommands` 无从下手。而表组装（`register_specs`）是导入
    期间本模块能拿到的唯一"之后"时机，所以这里包一层：跑完原函数后再补挂。

    只在**表还没组装**时安装，且只包一次（幂等）。导入顺序反过来（本模块排在表之后）
    时不需要它，:func:`mount_secrets_subcommands` 会直接生效。
    """
    global _ASSEMBLY_HOOK_INSTALLED
    if _ASSEMBLY_HOOK_INSTALLED:
        return False
    original = _table.register_specs

    def register_specs(specs: Any) -> list[str]:
        skipped = original(specs)
        mount_secrets_subcommands()
        mount_audit_options()
        return skipped

    _table.register_specs = register_specs
    _ASSEMBLY_HOOK_INSTALLED = True
    return True


#: `secrets` 子命令名 → handler。放在这里而不是内联，是因为
#: :func:`mount_secrets_subcommands` 需要按名字查表。
_SECRETS_HANDLERS: dict[str, Any] = {
    "status": run_secrets_status,
    "set": run_secrets_set,
    "get": run_secrets_get,
    "remove": run_secrets_remove,
    "sync": run_secrets_sync,
}


def _bind_secrets() -> None:
    for name, handler in _SECRETS_HANDLERS.items():
        register_handler(f"secrets {name}", handler)


_bind("security audit", run_audit)
_bind("egress status", run_egress_status)
_bind("egress config", run_egress_config)
_bind("egress setup", run_egress_setup)
_bind_secrets()
# 表还没组装（本模块排在 `hermes_parity` 之前）→ 装组装钩子；已组装 → 立即挂上。
if REGISTRY.get("secrets") is None:
    _install_assembly_hook()
mount_secrets_subcommands()
mount_audit_options()
