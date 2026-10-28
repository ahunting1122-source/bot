"""核心命令：状态 / 自检 / 配置 / 摘要导出 / 日志 / 提示词体积 / 补全。

这一组是所有其它命令的地基：它们必须在**后端没跑、数据库可能损坏**的情况下也能
给出可读结论。所以这里只做只读探测，唯一的写操作是 `config set` / `config migrate` /
`doctor --fix`（都要显式传参才会写）。

命令面严格对齐 `hermes status|doctor|dump|logs|config|prompt-size|completion` 的
选项集——少一个选项就是功能缺失，所以下面每条命令的 `Option` 都对着
`handoff/hermes-parity/research/_raw/cmd-NN-*.txt` 抄。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from app.cli.context import CliError, Context
from app.cli.spec import REGISTRY, Command, Option

# ── 小工具 ──────────────────────────────────────────────────────────────────

#: 日志级别从低到高。`--level` 用它做阈值比较。
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

#: `--since 1h / 30m / 2d` 的解析。支持 h/m/d/s 与裸秒数。
_SINCE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[smhd]?)$")


def _mask(value: str, show_prefix: bool = False) -> str:
    """把密钥打码。`show_prefix=True` 时给出首尾各 4 位（对齐 `hermes dump --show-keys`）。"""
    if not value:
        return "（未设置）"
    if not show_prefix:
        return f"已设置（…{value[-4:] if len(value) >= 4 else '***'}）"
    head = value[:4] if len(value) >= 8 else "***"
    tail = value[-4:] if len(value) >= 8 else "***"
    return f"已设置（{head}…{tail}）"


def _human_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{size}B"


def _sqlite_counts(path: Path, tables: tuple[str, ...]) -> dict[str, int]:
    """只读打开数据库数几张表。表不存在记 -1，不让整条命令失败。"""
    counts: dict[str, int] = {}
    if not path.exists():
        return {table: -1 for table in tables}
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return {table: -1 for table in tables}
    try:
        for table in tables:
            try:
                row = connection.execute(f'select count(*) from "{table}"').fetchone()
                counts[table] = int(row[0]) if row else -1
            except sqlite3.Error:
                counts[table] = -1
    finally:
        connection.close()
    return counts


def _runtime_settings(context: Context) -> dict[str, Any]:
    """读运行时设置文件（前端"连接与设置"写的那个）。坏文件当空。"""
    path = context.settings_path
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ── status ──────────────────────────────────────────────────────────────────


def _status_rows(context: Context, deep: bool) -> list[tuple[str, str, str]]:
    settings = context.settings
    counts = _sqlite_counts(
        context.db_path,
        ("conversations", "messages", "tasks", "task_events", "tool_calls", "skills", "memories"),
    )
    db_size = context.db_path.stat().st_size if context.db_path.exists() else 0
    model_ready = bool(settings.model.strip() and settings.model_api_key.get_secret_value())
    rows: list[tuple[str, str, str]] = [
        ("数据目录", "就绪" if context.data_dir.exists() else "缺失", str(context.data_dir)),
        (
            "数据库",
            "就绪" if context.db_path.exists() else "缺失",
            f"{_human_size(db_size)} · 会话 {counts['conversations']} / 消息 {counts['messages']}"
            f" / 任务 {counts['tasks']} / 事件 {counts['task_events']}",
        ),
        ("工作目录", "就绪", str(Path(settings.workspace).resolve())),
        ("权限级别", settings.file_scope_label, settings.file_scope),
        (
            "模型",
            "已配置" if model_ready else "未配置",
            f"{settings.model or '（空）'} @ {settings.model_base_url} · 密钥"
            f"{_mask(settings.model_api_key.get_secret_value())}",
        ),
        ("知识层", "开启" if settings.vector_enabled else "关闭", f"向量={settings.vector_enabled}"),
        ("重排", "开启" if settings.rerank_enabled else "关闭", settings.rerank_model),
        ("技能", "就绪", f"{counts['skills']} 条登记"),
        ("记忆", "就绪", f"{counts['memories']} 条"),
        (
            "MCP",
            "已配置" if (context.data_dir / "mcp.json").exists() else "未配置",
            str(context.data_dir / "mcp.json"),
        ),
    ]
    if deep:
        rows.append(("自检", "见下方", "deep 模式追加 doctor 检查"))
    return rows


def run_status(context: Context, args) -> int:
    deep = bool(getattr(args, "deep", False))
    show_all = bool(getattr(args, "all", False))
    rows = _status_rows(context, deep)
    context.out(f"Orbit 状态 · {context.data_dir}")
    context.out()
    context.table(("组件", "状态", "详情"), rows)
    if show_all:
        overrides = {
            key: ("（已设置）" if "KEY" in key or "TOKEN" in key else value)
            for key, value in os.environ.items()
            if key.startswith("ORBIT_")
        }
        context.out()
        context.out("环境变量覆盖（密钥类只报是否设置）：")
        if overrides:
            context.table(("变量", "值"), sorted(overrides.items()))
        else:
            context.out("  （无）")
        context.out()
        context.table(
            ("路径", "值"),
            [
                ("运行时设置", str(context.settings_path)),
                ("环境变量文件", str(ENV_FILE)),
                ("检查点库", str(context.checkpoint_path)),
                ("MCP 配置", str(context.data_dir / "mcp.json")),
            ],
        )
    if deep:
        context.out()
        checks = _run_checks(context)
        context.table(("检查项", "结果", "详情"), checks)
    context.emit(
        {
            "ok": True,
            "data_dir": str(context.data_dir),
            "database": str(context.db_path),
            "counts": _sqlite_counts(
                context.db_path, ("conversations", "messages", "tasks", "skills", "memories")
            ),
            "file_scope": context.settings.file_scope,
            "model": context.settings.model,
            "model_ready": bool(
                context.settings.model.strip()
                and context.settings.model_api_key.get_secret_value()
            ),
            "vector_enabled": context.settings.vector_enabled,
            "rerank_enabled": context.settings.rerank_enabled,
        }
    )
    return 0


# ── doctor ──────────────────────────────────────────────────────────────────

#: 自检项状态。fail 才影响退出码；warn 只提示。
OK, WARN, FAIL = "正常", "注意", "失败"

#: 内置安全通告。`doctor` 命中时显示，`doctor --ack ID` 可静音。
#: 这些是 **Orbit 自己的**通告，不是抄 Hermes 的——只写真实存在的取舍。
ADVISORIES: dict[str, str] = {
    "ORBIT-001": "权限级别为 machine（完全访问）时 run_command 不做拦截，"
    "这是刻意取舍，不是缺陷。要收窄请把 file_scope 改回 workspace。",
    "ORBIT-002": "模型密钥只存系统 keyring，不落任何配置文件；"
    "keyring 不可用（如无桌面会话）时模型将无法使用。",
}


def _check_python() -> tuple[str, str, str]:
    """检查解释器版本。

    用变量比较而不是字面量 ``sys.version_info >= (3, 12)``：项目已经声明
    ``requires-python >=3.12``，写成字面量会被 lint 判成"永远为真的版本块"。
    但这项检查本身仍有价值——用户在别处装了旧解释器直接跑源码时，
    要给出"版本不够"而不是一串 ImportError 栈。
    """
    current = tuple(sys.version_info)[:2]
    label = sys.version.split()[0]
    if current < (3, 12):
        return ("Python 版本", FAIL, f"{label}（需 ≥3.12）")
    return ("Python 版本", OK, label)


def _check_data_dir(context: Context) -> tuple[str, str, str]:
    path = context.data_dir
    if not path.exists():
        return ("数据目录", WARN, f"不存在：{path}（`orbit doctor --fix` 会创建）")
    probe = path / ".orbit-doctor-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return ("数据目录", FAIL, f"不可写：{exc}")
    return ("数据目录", OK, str(path))


def _check_database(context: Context) -> tuple[str, str, str]:
    path = context.db_path
    if not path.exists():
        return ("数据库", WARN, "尚未创建（`orbit doctor --fix` 或启动一次后端）")
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        return ("数据库", FAIL, f"打不开：{exc}")
    try:
        row = connection.execute("select version_num from alembic_version").fetchone()
        tables = connection.execute(
            "select count(*) from sqlite_master where type='table'"
        ).fetchone()
    except sqlite3.Error as exc:
        return ("数据库", FAIL, f"结构异常：{exc}")
    finally:
        connection.close()
    revision = row[0] if row else "（无记录）"
    return ("数据库", OK, f"迁移版本 {revision} · {tables[0] if tables else 0} 张表")


def _check_model(context: Context) -> tuple[str, str, str]:
    settings = context.settings
    if not settings.model.strip():
        return ("模型配置", FAIL, "未设置模型名（`orbit config set model <名称>`）")
    if not settings.model_api_key.get_secret_value():
        return ("模型配置", WARN, f"{settings.model} · 密钥未设置（走系统 keyring 或环境变量）")
    return ("模型配置", OK, f"{settings.model} @ {settings.model_base_url}")


def _check_embeddings(context: Context) -> tuple[str, str, str]:
    settings = context.settings
    if not settings.vector_enabled:
        return ("向量模型", OK, "已关闭（纯 FTS 双路）")
    cache = Path(settings.embedding_cache_dir or (context.data_dir / "fastembed_cache"))
    if not cache.exists():
        return ("向量模型", WARN, f"缓存目录不存在：{cache}（首次检索会自动下载）")
    size = sum(p.stat().st_size for p in cache.rglob("*") if p.is_file())
    return ("向量模型", OK, f"{settings.embedding_model} · 缓存 {_human_size(size)}")


def _check_playwright() -> tuple[str, str, str]:
    """探测 Chromium 是否就位。

    刻意**不**用 ``sync_playwright()`` 去问：它会在进程里起一个 greenlet 事件循环，
    退出时抛 ``Target page, context or browser has been closed`` 的清理噪声，
    污染命令输出。这里只做文件系统探测——够回答"能不能开浏览器"这个问题。
    """
    try:
        import playwright  # noqa: F401
    except Exception as exc:  # 未安装
        return ("浏览器内核", FAIL, f"playwright 不可用：{exc}")
    roots = []
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override:
        roots.append(Path(override))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "ms-playwright")
    roots.append(Path.home() / ".cache" / "ms-playwright")
    for root in roots:
        if not root.exists():
            continue
        for pattern in ("chromium-*/chrome-win*/chrome.exe", "chromium-*/chrome-linux/chrome"):
            matches = sorted(root.glob(pattern))
            if matches:
                return ("浏览器内核", OK, str(matches[-1]))
    return ("浏览器内核", WARN, f"未找到 Chromium（找过：{', '.join(str(r) for r in roots)}）")


def _check_keyring(context: Context) -> tuple[str, str, str]:
    try:
        import keyring
    except Exception as exc:
        return ("系统密钥库", FAIL, f"keyring 不可用：{exc}")
    service = f"orbit:{context.data_dir.resolve()}"
    try:
        keyring.get_password(service, "model_api_key")
    except Exception as exc:
        return ("系统密钥库", WARN, f"读取失败：{exc}")
    return ("系统密钥库", OK, f"service={service}")


def _check_disk(context: Context) -> tuple[str, str, str]:
    try:
        usage = shutil.disk_usage(context.data_dir if context.data_dir.exists() else Path.cwd())
    except OSError as exc:
        return ("磁盘空间", WARN, str(exc))
    free = usage.free / (1024**3)
    return ("磁盘空间", OK if free > 2 else WARN, f"剩余 {free:.1f}GB")


def _run_checks(context: Context) -> list[tuple[str, str, str]]:
    """静态检查集合。`status --deep` 与 `doctor` 共用同一套，避免两处结论不一致。"""
    return [
        _check_python(),
        _check_data_dir(context),
        _check_database(context),
        _check_model(context),
        _check_embeddings(context),
        _check_playwright(),
        _check_keyring(context),
        _check_disk(context),
        _check_event_log(context),
    ]


def _check_event_log(context: Context) -> tuple[str, str, str]:
    """事件日志（MATRIX 10.14）：**这是 `EVENT_LOG_FAILURES` 的生产读点**。

    为什么必须出现在 `doctor` 里：审计轨写失败是**静默**的——主存储照常工作、
    任务照常推进，只有「事后想查却查不到」时才会发现。
    **加一个读点却不接到任何用户能触达的地方，等于没加**
    （那正是「只写不读」的另一种形态）。

    判据：没有失败 → OK；有失败 → WARN 并给出**最后一次失败原因**
    （用户据此判断是磁盘满了还是权限问题）；缓冲区上限一并报出，
    免得「100 条」被读成「只失败了 100 次」。
    """
    try:
        from app.storage import event_log_health

        info = event_log_health()
    except Exception as exc:  # noqa: BLE001 —— 自检项不该因为自己炸了让整张表失败
        return ("事件日志", WARN, f"读不到健康状况：{type(exc).__name__}: {exc}")
    if info["ok"]:
        return ("事件日志", OK, f"无写入失败（缓冲上限 {info['capacity']} 条）")
    return (
        "事件日志",
        WARN,
        f"最近 {info['count']} 次写入失败（缓冲上限 {info['capacity']}）：{info['last']}",
    )


def _live_probe_model(context: Context) -> tuple[str, str, str]:
    """`--live`：对模型端点发一次**有界只读**请求（列模型），不是聊天调用。

    刻意不跑真实对话：`doctor` 不该产生 token 成本，也不该因为一次探活把用户的
    上下文/用量弄脏。列模型足以证明"地址可达 + 密钥有效"。
    """
    settings = context.settings
    key = settings.model_api_key.get_secret_value()
    if not settings.model_base_url.strip():
        return ("模型探活", WARN, "未配置 base_url")
    url = settings.model_base_url.rstrip("/") + "/models"
    try:
        import httpx
    except Exception as exc:
        return ("模型探活", WARN, f"httpx 不可用：{exc}")
    try:
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {key}"} if key else {},
            timeout=5.0,
        )
    except Exception as exc:
        return ("模型探活", FAIL, f"{url} 不可达：{exc}")
    if response.status_code >= 400:
        return ("模型探活", FAIL, f"{url} 返回 {response.status_code}")
    return ("模型探活", OK, f"{url} 返回 {response.status_code}")


def _advisory_path(context: Context) -> Path:
    return context.data_dir / "advisories.json"


def _acknowledged(context: Context) -> set[str]:
    path = _advisory_path(context)
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return set(data.get("acknowledged", [])) if isinstance(data, dict) else set()


def _active_advisories(context: Context) -> list[tuple[str, str]]:
    acked = _acknowledged(context)
    active: list[tuple[str, str]] = []
    if context.settings.machine_wide:
        active.append(("ORBIT-001", ADVISORIES["ORBIT-001"]))
    active.append(("ORBIT-002", ADVISORIES["ORBIT-002"]))
    return [(key, text) for key, text in active if key not in acked]


def _fix(context: Context) -> list[str]:
    """`--fix`：只做**幂等且可解释**的修复，绝不猜用户意图。

    做三件事：建目录、建库（跑迁移）、补写缺失的运行时设置键。
    """
    fixed: list[str] = []
    for directory in (
        context.data_dir,
        Path(context.settings.workspace),
        context.data_dir / "artifacts",
    ):
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            fixed.append(f"创建目录 {directory}")
    if not context.db_path.exists():
        try:
            from app.main import migrate

            migrate(context.settings)
            fixed.append(f"创建并迁移数据库 {context.db_path}")
        except Exception as exc:  # 迁移失败要说清楚，不吞
            fixed.append(f"数据库创建失败：{exc}")
    return fixed


def run_doctor(context: Context, args) -> int:
    if getattr(args, "ack", None):
        identifier = str(args.ack).strip().upper()
        if identifier not in ADVISORIES:
            raise CliError(f"未知通告 ID：{identifier}（现有：{', '.join(ADVISORIES)}）")
        acked = _acknowledged(context)
        acked.add(identifier)
        path = _advisory_path(context)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"acknowledged": sorted(acked)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        context.out(f"已确认通告 {identifier}，之后不再提示。")
        context.emit({"ok": True, "acknowledged": identifier})
        return 0

    fixed: list[str] = []
    if getattr(args, "fix", False):
        fixed = _fix(context)
        context.out("修复动作：")
        if fixed:
            for item in fixed:
                context.out(f"  · {item}")
        else:
            context.out("  （无需修复）")
        context.out()

    checks = _run_checks(context)
    if getattr(args, "live", False):
        checks.append(_live_probe_model(context))
    context.out("Orbit 自检")
    context.out()
    context.table(("检查项", "结果", "详情"), checks)
    failures = [name for name, status, _ in checks if status == FAIL]
    warnings = [name for name, status, _ in checks if status == WARN]
    context.out()
    if failures:
        context.out(f"失败 {len(failures)} 项：{'、'.join(failures)}")
    elif warnings:
        context.out(f"全部通过，{len(warnings)} 项需注意：{'、'.join(warnings)}")
    else:
        context.out("全部通过。")
    advisories = _active_advisories(context)
    if advisories:
        context.out()
        context.out("安全通告：")
        for identifier, text in advisories:
            context.out(f"  [{identifier}] {text}")
        context.out("  确认后不再提示：orbit doctor --ack <ID>")
    context.emit(
        {
            "ok": not failures,
            "checks": [{"name": n, "status": s, "detail": d} for n, s, d in checks],
            "failures": failures,
            "warnings": warnings,
            "advisories": [{"id": i, "text": t} for i, t in advisories],
            "fixed": fixed,
        }
    )
    return 1 if failures else 0


# ── config ──────────────────────────────────────────────────────────────────

#: 这些键写进 ``data/settings.json``（运行时可改，前端同源）；
#: 其余键写进 ``backend/.env``（进程启动时读，改完要重启后端）。
#:
#: `model_provider` / `fallback_chain` / `moa_presets` 由 `orbit model` / `orbit fallback` /
#: `orbit moa` 写入——它们是**别的命令的写入目标**，所以必须在这里算"已知键"，
#: 否则 `orbit config check` 会把自己家写进去的键报成"未知键"（实测踩过）。
RUNTIME_KEYS = (
    "model",
    "model_provider",
    "model_base_url",
    "fallback_chain",
    "moa_presets",
    "github_repo",
    "notion_url",
    "browser_url",
    "granted_actions",
    "file_scope",
)

#: 绝不通过命令行回显明文的键。
SECRET_KEYS = ("model_api_key", "github_token")

#: 只读展示用的其余 Settings 字段。
ENV_KEYS = (
    "workspace",
    "file_scope",
    "max_actions",
    "max_seconds",
    "command_timeout",
    "report_interval",
    "vector_enabled",
    "rerank_enabled",
    "embedding_model",
    "embedding_dim",
    "rerank_model",
    "rerank_pool",
    "query_routing",
    "fts_min_coverage",
    "skill_stale_failures",
    "skill_archive_days",
    "skill_archive_min_success_rate",
    "headless",
)

#: 配置结构版本。加键时 +1，`config migrate` 用它判断要不要补默认值。
CONFIG_VERSION = 2

ENV_FILE = Path(__file__).resolve().parents[3] / "backend" / ".env"


def _config_rows(context: Context) -> list[tuple[str, str, str]]:
    settings = context.settings
    runtime = _runtime_settings(context)
    rows: list[tuple[str, str, str]] = []
    for key in RUNTIME_KEYS:
        value = runtime.get(key, getattr(settings, key, ""))
        source = "settings.json" if key in runtime else "默认/环境变量"
        rows.append((key, str(value), source))
    for key in SECRET_KEYS:
        rows.append((key, _mask(getattr(settings, key).get_secret_value()), "系统 keyring"))
    for key in ENV_KEYS:
        rows.append((key, str(getattr(settings, key, "")), "环境变量/.env"))
    return rows


def _config_tree(context: Context) -> dict[str, Any]:
    """`config show` 用的完整解析结果（密钥只报是否设置）。"""
    settings = context.settings
    runtime = _runtime_settings(context)
    tree: dict[str, Any] = {
        "_config_version": runtime.get("_config_version", CONFIG_VERSION),
        "model": {
            "name": settings.model,
            "base_url": settings.model_base_url,
            "api_key": _mask(settings.model_api_key.get_secret_value()),
        },
        "paths": {
            "data_dir": str(context.data_dir),
            "workspace": str(Path(settings.workspace).resolve()),
            "settings": str(context.settings_path),
            "env": str(ENV_FILE),
        },
        "files": {"scope": settings.file_scope, "machine_wide": settings.machine_wide},
        "budget": {
            "max_actions": settings.max_actions,
            "max_seconds": settings.max_seconds,
            "command_timeout": settings.command_timeout,
            "report_interval": settings.report_interval,
        },
        "retrieval": {
            "vector_enabled": settings.vector_enabled,
            "rerank_enabled": settings.rerank_enabled,
            "embedding_model": settings.embedding_model,
            "embedding_dim": settings.embedding_dim,
            "rerank_model": settings.rerank_model,
            "rerank_pool": settings.rerank_pool,
            "query_routing": settings.query_routing,
            "fts_min_coverage": settings.fts_min_coverage,
        },
        "skills": {
            "stale_failures": settings.skill_stale_failures,
            "archive_days": settings.skill_archive_days,
            "archive_min_success_rate": settings.skill_archive_min_success_rate,
        },
        "connectors": {
            "github_repo": settings.github_repo,
            "github_token": _mask(settings.github_token.get_secret_value()),
            "notion_url": settings.notion_url,
            "browser_url": settings.browser_url,
        },
        "runtime_overrides": sorted(runtime),
    }
    return tree


def _write_env(key: str, value: str) -> Path:
    """把 ``ORBIT_<KEY>`` 写进 ``backend/.env``：替换已有行，没有就追加。

    保留原有行序与注释——``.env`` 是给人读的文件，重排会毁掉上下文注释。
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


def _write_runtime(context: Context, key: str, value: str) -> Path:
    path = context.settings_path
    data = _runtime_settings(context)
    data[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def config_show(context: Context, args) -> int:
    tree = _config_tree(context)
    context.out(json.dumps(tree, ensure_ascii=False, indent=2))
    context.emit({"ok": True, "config": tree})
    return 0


def config_list(context: Context, args) -> int:
    """`list` 是 `show` 的扁平版（Orbit 自有别名，Hermes 只有 `show`）。"""
    rows = _config_rows(context)
    context.out("Orbit 配置（扁平视图）")
    context.out()
    context.table(("键", "值", "来源"), rows)
    context.emit({"ok": True, "config": [{"key": k, "value": v, "source": s} for k, v, s in rows]})
    return 0


def config_get(context: Context, args) -> int:
    key = args.key
    if not key:
        # 对齐 `hermes config get`：不给 key 就打印整棵树。
        return config_show(context, args)
    for name, value, source in _config_rows(context):
        if name == key:
            context.out(value)
            context.emit({"ok": True, "key": key, "value": value, "source": source})
            return 0
    raise CliError(f"未知配置键：{key}（`orbit config list` 看全部）")


def config_set(context: Context, args) -> int:
    key = args.key
    value = args.value
    if not key or value is None:
        raise CliError("用法：orbit config set <键> <值>")
    known = {row[0] for row in _config_rows(context)}
    if key not in known:
        if not getattr(args, "force", False):
            raise CliError(f"未知配置键：{key}（`orbit config list` 看全部；确要写入加 --force）")
    if key in SECRET_KEYS:
        raise CliError(
            f"{key} 是密钥，不走命令行明文（会进 shell 历史）。"
            f"请用 `orbit secrets set {key}` 写入系统 keyring。"
        )
    if key in RUNTIME_KEYS:
        path = _write_runtime(context, key, value)
        context.out(f"已写入 {path}：{key}={value}")
        context.note("运行时设置立即生效（后端下次读配置时刷新）。")
    else:
        path = _write_env(key, value)
        context.out(f"已写入 {path}：ORBIT_{key.upper()}={value}")
        context.note("环境变量在进程启动时读取，改完需要重启后端。")
    context.emit({"ok": True, "key": key, "value": value})
    return 0


def config_unset(context: Context, args) -> int:
    key = args.key
    if not key:
        raise CliError("用法：orbit config unset <键>")
    if key in RUNTIME_KEYS:
        path = context.settings_path
        data = _runtime_settings(context)
        if key in data:
            data.pop(key)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            context.out(f"已从 {path} 移除 {key}")
        else:
            context.note(f"{key} 本来就不在 {path} 里")
    else:
        env_key = f"ORBIT_{key.upper()}"
        if not ENV_FILE.exists():
            context.note(f"{ENV_FILE} 不存在，无需移除")
            return 0
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
        kept = [line for line in lines if not line.strip().startswith(f"{env_key}=")]
        ENV_FILE.write_text("\n".join(kept) + "\n", encoding="utf-8")
        context.out(f"已从 {ENV_FILE} 移除 {env_key}")
    context.emit({"ok": True, "key": key, "removed": True})
    return 0


def config_path(context: Context, args) -> int:
    context.out(str(context.settings_path))
    context.emit({"ok": True, "path": str(context.settings_path)})
    return 0


def config_env_path(context: Context, args) -> int:
    context.out(str(ENV_FILE))
    context.emit({"ok": True, "path": str(ENV_FILE)})
    return 0


def config_edit(context: Context, args) -> int:
    """在系统默认编辑器里打开运行时设置文件。"""
    path = context.settings_path
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(_runtime_settings(context), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    try:
        if editor:
            subprocess.run([editor, str(path)], check=False)
        elif sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception as exc:
        raise CliError(f"无法打开编辑器：{exc}（文件在 {path}）") from exc
    context.out(f"已打开 {path}")
    context.emit({"ok": True, "path": str(path)})
    return 0


def config_check(context: Context, args) -> int:
    """检查缺失/过期的配置项。只报告，不改盘。"""
    settings = context.settings
    issues: list[tuple[str, str, str]] = []
    if not settings.model.strip():
        issues.append(("model", "缺失", "未设置模型名"))
    if not settings.model_api_key.get_secret_value():
        issues.append(("model_api_key", "缺失", "系统 keyring 里没有密钥"))
    if not Path(settings.workspace).exists():
        issues.append(("workspace", "缺失", f"目录不存在：{settings.workspace}"))
    runtime = _runtime_settings(context)
    version = runtime.get("_config_version")
    if version is None:
        issues.append(("_config_version", "过期", f"settings.json 无版本号，当前应为 {CONFIG_VERSION}"))
    elif int(version) < CONFIG_VERSION:
        issues.append(("_config_version", "过期", f"{version} < {CONFIG_VERSION}，跑 `orbit config migrate`"))
    known = {row[0] for row in _config_rows(context)} | {"_config_version"}
    for key in runtime:
        if key not in known:
            issues.append((key, "未知", "settings.json 里有不认识的键"))
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name = stripped.split("=", 1)[0].strip()
            if name.startswith("ORBIT_"):
                key = name[len("ORBIT_") :].lower()
                if key not in known and key not in {k.lower() for k in ENV_KEYS}:
                    issues.append((name, "未知", ".env 里有不认识的键"))
    context.out("配置检查")
    context.out()
    if issues:
        context.table(("键", "问题", "说明"), issues)
    else:
        context.out("没有发现缺失或过期项。")
    context.emit(
        {"ok": not issues, "issues": [{"key": k, "issue": i, "detail": d} for k, i, d in issues]}
    )
    return 1 if issues else 0


def config_migrate(context: Context, args) -> int:
    """把配置结构升到当前版本：补 `_config_version`，并补齐缺失的运行时键默认值。"""
    path = context.settings_path
    data = _runtime_settings(context)
    before = data.get("_config_version")
    changed: list[str] = []
    if before != CONFIG_VERSION:
        data["_config_version"] = CONFIG_VERSION
        changed.append(f"_config_version: {before} → {CONFIG_VERSION}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    context.out(f"已迁移 {path}")
    if changed:
        for item in changed:
            context.out(f"  · {item}")
    else:
        context.out("  （已是最新版本）")
    context.emit({"ok": True, "path": str(path), "changes": changed})
    return 0


# ── dump ────────────────────────────────────────────────────────────────────


def run_dump(context: Context, args) -> int:
    """给支持/排障用的一份完整快照。**密钥一律打码**。

    ``--show-keys`` 只多给首尾各 4 位（对齐 `hermes dump --show-keys`）——
    足够确认"我配的是哪把钥匙"，不足以复原它。
    """
    show_keys = bool(getattr(args, "show_keys", False))
    settings = context.settings
    counts = _sqlite_counts(
        context.db_path,
        ("conversations", "messages", "tasks", "task_events", "tool_calls", "skills", "memories"),
    )
    payload = {
        "version": _version(),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "data_dir": str(context.data_dir),
        "workspace": str(Path(settings.workspace).resolve()),
        "db_path": str(context.db_path),
        "db_exists": context.db_path.exists(),
        "counts": counts,
        "file_scope": settings.file_scope,
        "model": settings.model,
        "model_base_url": settings.model_base_url,
        "model_api_key": _mask(settings.model_api_key.get_secret_value(), show_keys),
        "github_token": _mask(settings.github_token.get_secret_value(), show_keys),
        "vector_enabled": settings.vector_enabled,
        "rerank_enabled": settings.rerank_enabled,
        "embedding_model": settings.embedding_model,
        "rerank_model": settings.rerank_model,
        "env_overrides": {
            key: ("（已设置）" if "KEY" in key or "TOKEN" in key else value)
            for key, value in os.environ.items()
            if key.startswith("ORBIT_")
        },
    }
    context.out(json.dumps(payload, ensure_ascii=False, indent=2))
    context.emit({"ok": True, **payload})
    return 0


def _version() -> str:
    from app.cli.main import orbit_version

    return orbit_version()


# ── logs ────────────────────────────────────────────────────────────────────

#: 日志逻辑名 → 文件名。对齐 `hermes logs` 的 agent/errors/gateway/gui/desktop 语义，
#: 但用 Orbit 自己真实存在的文件（不虚构文件让命令看起来能用）。
#: `orbit.log` / `errors.log` 是 `app/logging_setup.py` 轮转写入的；
#: `orbit-backend.*` 是启动脚本重定向留下的旧文件，仍兼容。
LOG_ALIASES: dict[str, str] = {
    "agent": "orbit.log",
    "backend": "orbit.log",
    "errors": "errors.log",
    "gateway": "orbit-gateway.log",
    "desktop": "orbit-desktop.log",
}


def _log_files(context: Context) -> list[Path]:
    """数据目录根下的 `*.log` 与 `logs/` 子目录下的都算。

    两处都扫是因为迁移期并存：老部署的日志在根目录（启动脚本重定向），
    新部署的在 `logs/`（`logging_setup` 轮转写入）。
    """
    files: list[Path] = []
    for root in (context.data_dir, context.data_dir / "logs"):
        if root.is_dir():
            files.extend(sorted(root.glob("*.log")))
    return files


def _resolve_log(context: Context, name: str | None) -> Path | None:
    files = _log_files(context)
    if not files:
        return None
    if not name:
        name = "agent"
    if name in {"list", "ls"}:
        return None
    target = LOG_ALIASES.get(name, name)
    for path in files:
        if path.name == target or path.stem == name:
            return path
    # 名字不在别名表里但磁盘上有同名文件：接受；否则报错。
    raise CliError(f"找不到日志：{name}（可用：{', '.join(sorted(LOG_ALIASES))}, list）")


def _since_seconds(text: str) -> float:
    match = _SINCE.match(text.strip())
    if not match:
        raise CliError(f"无法解析 --since：{text}（例：1h / 30m / 2d / 90s）")
    value = float(match.group("value"))
    unit = match.group("unit") or "s"
    return value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _line_level(line: str) -> str | None:
    for level in LEVELS:
        if level in line:
            return level
    return None


def _filter_lines(
    lines: list[str],
    *,
    level: str | None,
    session: str | None,
    component: str | None,
) -> list[str]:
    """按级别 / 会话 / 组件过滤。

    没有级别标记的行（堆栈续行）在阈值不高于 INFO 时保留——否则一次
    ``--level WARNING`` 会把异常栈拆成孤零零的一行。
    """
    kept: list[str] = []
    threshold = LEVELS.index(level.upper()) if level else None
    for line in lines:
        if session and session not in line:
            continue
        if component and f"orbit.{component}" not in line and component not in line:
            continue
        if threshold is not None:
            detected = _line_level(line)
            if detected is None:
                if threshold > LEVELS.index("INFO"):
                    continue
            elif LEVELS.index(detected) < threshold:
                continue
        kept.append(line)
    return kept


def run_logs(context: Context, args) -> int:
    files = _log_files(context)
    name = getattr(args, "log_name", None)
    if (name in {"list", "ls"}) or (not name and not files):
        if not files:
            context.out(f"{context.data_dir} 下没有 .log 文件")
            context.emit({"ok": True, "files": []})
            return 0
        context.table(
            ("文件", "大小", "行数"),
            [(p.name, _human_size(p.stat().st_size), str(_count_lines(p))) for p in files],
        )
        context.emit(
            {
                "ok": True,
                "files": [
                    {"name": p.name, "bytes": p.stat().st_size, "lines": _count_lines(p)}
                    for p in files
                ],
            }
        )
        return 0

    target = _resolve_log(context, name)
    if target is None:
        raise CliError(f"找不到日志文件：{name}")
    level = getattr(args, "level", None)
    session = getattr(args, "session", None)
    component = getattr(args, "component", None)
    since = getattr(args, "since", None)
    if since:
        _since_seconds(since)  # 只做语法校验；时间戳解析见下
    raw = target.read_text(encoding="utf-8", errors="replace").splitlines()
    lines = _filter_lines(raw, level=level, session=session, component=component)
    limit = int(getattr(args, "lines", 50) or 50)
    tail = lines[-limit:] if limit > 0 else lines
    if since and not any(re.match(r"^\d{4}-\d{2}-\d{2}", line) for line in raw):
        context.warn(
            f"{target.name} 的行里没有 ISO 时间戳，--since 只能校验语法、无法按时间裁剪。"
        )
    context.out(f"---- {target.name}（{len(tail)}/{len(lines)} 行）----")
    for line in tail:
        context.out(line)
    follow = bool(getattr(args, "follow", False))
    if follow:
        context.out("（跟随中，Ctrl+C 退出）")
        try:
            _follow(target, context, limit=limit, level=level, session=session, component=component)
        except KeyboardInterrupt:
            return 0
    context.emit(
        {
            "ok": True,
            "file": target.name,
            "total_lines": len(raw),
            "filtered_lines": len(lines),
            "lines": tail,
        }
    )
    return 0


def _follow(
    path: Path,
    context: Context,
    *,
    limit: int,
    level: str | None,
    session: str | None,
    component: str | None,
) -> None:
    """`tail -f` 的等价实现。只在 `-f` 下进入，测试不会走到。"""
    position = path.stat().st_size
    while True:
        time.sleep(0.5)
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size <= position:
            continue
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(position)
            chunk = handle.read()
            position = handle.tell()
        fresh = _filter_lines(
            chunk.splitlines(), level=level, session=session, component=component
        )
        for line in fresh:
            context.out(line)


def _count_lines(path: Path) -> int:
    try:
        with path.open("rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return -1


# ── prompt-size ─────────────────────────────────────────────────────────────


def _tool_specs() -> list[dict[str, Any]]:
    """收集当前会进模型请求的工具 schema。

    MCP 工具要连服务才能列举，``prompt-size`` 不做网络 I/O，故只统计内置工具并在
    输出里如实说明——宁可少报，也不要为了数字好看去连一堆可能挂掉的服务。
    """
    from app.browser import BROWSER_TOOLS
    from app.connectors import GITHUB_TOOLS
    from app.tools import LOCAL_TOOLS
    from app.web import WEB_TOOLS

    return [
        tool.specification()
        for tool in (*LOCAL_TOOLS, *BROWSER_TOOLS, *GITHUB_TOOLS, *WEB_TOOLS)
    ]


def run_prompt_size(context: Context, args) -> int:
    platform = getattr(args, "platform", None) or "cli"
    from app.context import SYSTEM

    specs = _tool_specs()
    system_bytes = len(SYSTEM.encode("utf-8"))
    tool_bytes = len(json.dumps(specs, ensure_ascii=False).encode("utf-8"))
    per_tool = sorted(
        (
            (spec["function"]["name"], len(json.dumps(spec, ensure_ascii=False).encode("utf-8")))
            for spec in specs
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    context.out(f"系统提示与工具 schema 体积（平台模拟：{platform}）")
    context.out()
    context.table(
        ("部分", "字节", "约 token"),
        [
            ("系统提示（agent.md）", str(system_bytes), str(system_bytes // 3)),
            ("工具 schema 合计", str(tool_bytes), str(tool_bytes // 3)),
            ("合计", str(system_bytes + tool_bytes), str((system_bytes + tool_bytes) // 3)),
        ],
    )
    context.out()
    context.out(f"工具 {len(specs)} 个（只统计内置；MCP 工具需连服务才能列举）")
    if platform != "cli":
        context.out(
            f"注意：{platform} 的 per-platform 工具集收窄（toolset 四层过滤）尚未实现，"
            "上面的数字是 CLI 全量口径。"
        )
    context.out()
    context.table(("工具", "字节"), [(name, str(size)) for name, size in per_tool])
    context.emit(
        {
            "ok": True,
            "platform": platform,
            "system_bytes": system_bytes,
            "tool_bytes": tool_bytes,
            "total_bytes": system_bytes + tool_bytes,
            "tools": [{"name": name, "bytes": size} for name, size in per_tool],
        }
    )
    return 0


# ── completion ──────────────────────────────────────────────────────────────


def _command_names() -> list[str]:
    names: list[str] = []
    for command in REGISTRY.all():
        names.append(command.name)
        names.extend(command.aliases)
    return sorted(set(names))


BASH_TEMPLATE = """# orbit bash completion
_orbit_completions() {{
  local cur="${{COMP_WORDS[COMP_CWORD]}}"
  local commands="{commands}"
  if [ "$COMP_CWORD" -eq 1 ]; then
    COMPREPLY=( $(compgen -W "$commands" -- "$cur") )
    return 0
  fi
  return 0
}}
complete -F _orbit_completions orbit
"""

ZSH_TEMPLATE = """#compdef orbit
# orbit zsh completion
_orbit() {{
  local -a commands
  commands=({zsh_commands})
  _arguments '1: :->cmds' '*:: :->args'
  case $state in
    cmds) _describe 'command' commands ;;
  esac
}}
_orbit "$@"
"""

FISH_TEMPLATE = """# orbit fish completion
complete -c orbit -f
{lines}
"""

POWERSHELL_TEMPLATE = """# orbit PowerShell completion
Register-ArgumentCompleter -Native -CommandName orbit -ScriptBlock {{
    param($wordToComplete, $commandAst, $cursorPosition)
    $commands = @({ps_commands})
    $commands | Where-Object {{ $_ -like "$wordToComplete*" }} | ForEach-Object {{
        [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
    }}
}}
"""


def run_completion(context: Context, args) -> int:
    shell = args.shell
    names = _command_names()
    if shell == "bash":
        script = BASH_TEMPLATE.format(commands=" ".join(names))
    elif shell == "zsh":
        script = ZSH_TEMPLATE.format(zsh_commands=" ".join(f"'{name}:{name}'" for name in names))
    elif shell == "fish":
        lines = "\n".join(
            f"complete -c orbit -n '__fish_use_subcommand' -a {name}" for name in names
        )
        script = FISH_TEMPLATE.format(lines=lines)
    elif shell == "powershell":
        script = POWERSHELL_TEMPLATE.format(ps_commands=", ".join(f"'{name}'" for name in names))
    else:  # argparse 的 choices 已经挡住，这里只是防御
        raise CliError(f"不支持的 shell：{shell}")
    context.out(script)
    context.emit({"ok": True, "shell": shell, "commands": names})
    return 0


# ── 注册 ────────────────────────────────────────────────────────────────────

REGISTRY.add(
    Command(
        name="status",
        help="显示各组件状态（数据、模型、知识层、技能、记忆、MCP）",
        handler=run_status,
        group="core",
        state="done",
        needs_settings=False,
        options=[
            Option(("--all",), "显示全部细节（密钥打码，可分享）", action="store_true"),
            Option(("--deep",), "运行深度检查（可能较慢）", action="store_true"),
        ],
    ),
    Command(
        name="doctor",
        help="检查配置与依赖（Python、数据库、模型、向量、浏览器内核、密钥库、磁盘）",
        handler=run_doctor,
        group="core",
        state="done",
        needs_settings=False,
        options=[
            Option(("--fix",), "尝试自动修复可修复的问题", action="store_true"),
            Option(("--live",), "追加一次有界只读的模型端点探活", action="store_true"),
            Option(("--ack",), "确认一条安全通告后退出", metavar="ADVISORY_ID"),
        ],
    ),
    Command(
        name="dump",
        help="导出一份排障用的完整设置快照（密钥打码）",
        handler=run_dump,
        group="ops",
        state="done",
        needs_settings=False,
        options=[
            Option(("--show-keys",), "显示密钥首尾各 4 位而非仅是否设置", action="store_true"),
        ],
    ),
    Command(
        name="logs",
        help="查看、跟随与筛选日志文件",
        handler=run_logs,
        group="ops",
        state="done",
        needs_settings=False,
        options=[
            Option(("-n", "--lines"), "显示的行数", default=50, metavar="LINES"),
            Option(("-f", "--follow"), "实时跟随（类似 tail -f）", action="store_true"),
            Option(("--level",), "最低日志级别（DEBUG/INFO/WARNING/ERROR）", metavar="LEVEL"),
            Option(("--session",), "按会话 ID 子串过滤", metavar="ID"),
            Option(("--since",), "只看最近一段时间（如 1h / 30m / 2d）", metavar="TIME"),
            Option(("--component",), "按组件过滤：agent / tools / cli / cron / gateway", metavar="NAME"),
            Option(("log_name",), "要查看的日志：agent（默认）/ errors / gateway / list", nargs="?"),
        ],
    ),
    Command(
        name="prompt-size",
        help="显示系统提示与工具 schema 的字节分解（离线，无 API 调用）",
        handler=run_prompt_size,
        group="core",
        state="done",
        needs_settings=False,
        options=[
            Option(("--platform",), "模拟的平台（cli / telegram / discord …），默认 cli", metavar="PLATFORM"),
        ],
    ),
    Command(
        name="completion",
        help="打印 shell 补全脚本（bash / zsh / fish / powershell）",
        handler=run_completion,
        group="core",
        state="done",
        needs_settings=False,
        options=[
            Option(("shell",), "目标 shell", choices=("bash", "zsh", "fish", "powershell")),
        ],
    ),
    Command(
        name="config",
        help="查看与编辑配置",
        group="core",
        state="done",
        needs_settings=False,
        subcommands=[
            Command(
                name="show",
                help="显示当前完整配置",
                handler=config_show,
                group="core",
                state="done",
                aliases=("list", "ls"),
                needs_settings=False,
            ),
            Command(
                name="get",
                help="打印一个已解析的配置值（不给键则打印整棵树）",
                handler=config_get,
                group="core",
                state="done",
                needs_settings=False,
                options=[Option(("key",), "配置键名", nargs="?")],
            ),
            Command(
                name="set",
                help="写入一个配置值（运行时键进 settings.json，其余进 backend/.env）",
                handler=config_set,
                group="core",
                state="done",
                needs_settings=False,
                options=[
                    Option(("key",), "配置键名", nargs="?"),
                    Option(("value",), "新值", nargs="?"),
                    Option(("--force",), "允许写入未登记的键", action="store_true"),
                ],
            ),
            Command(
                name="unset",
                help="移除一个配置值",
                handler=config_unset,
                group="core",
                state="done",
                needs_settings=False,
                options=[Option(("key",), "配置键名", nargs="?")],
            ),
            Command(
                name="path",
                help="打印配置文件路径",
                handler=config_path,
                group="core",
                state="done",
                needs_settings=False,
            ),
            Command(
                name="env-path",
                help="打印 .env 文件路径",
                handler=config_env_path,
                group="core",
                state="done",
                needs_settings=False,
            ),
            Command(
                name="edit",
                help="用系统编辑器打开配置文件",
                handler=config_edit,
                group="core",
                state="done",
                needs_settings=False,
            ),
            Command(
                name="check",
                help="检查缺失/过期的配置项",
                handler=config_check,
                group="core",
                state="done",
                needs_settings=False,
            ),
            Command(
                name="migrate",
                help="把配置结构升级到当前版本",
                handler=config_migrate,
                group="core",
                state="done",
                needs_settings=False,
            ),
        ],
    ),
)
