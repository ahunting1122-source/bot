"""`orbit skills publish` + `orbit profile <distribution 10 条>` 的命令面（矩阵 7.13 / 9.16）。

## 为什么这两块挤在同一个模块里

它们共用**同一个接缝**：都要给 `hermes_parity` 声明表里**已经存在、但状态是 `todo`**
的路径填上实现（`register_handler`）。本波次只允许新建这一个 CLI 模块，所以两族命令
放在一起，接线机制只写一遍。业务逻辑一律不在本文件里：

- `orbit skills publish` → `app/skill_hub.py::SkillHub.publish`（打包 + lint 门禁）；
- `orbit profile …`（distribution）→ `app/profile_dist.py::ProfileDist`（13 条操作）。

理由与 `memory_extra2.py` 一样：这些能力的入口不止 CLI（HTTP、工具层、将来的 TUI 都会
用），把逻辑写在 handler 里等于把它们锁死在命令行上。

## 接了哪些路径

**`skills publish`（矩阵 7.13 的最后一条）**：表里声明的是 Hermes 的形状
（`--to {github,clawhub}` / `--repo` / `skill_path`）。本实现把选项换成
`--to <本地路径>` / `--apply` / `--repo`（保留但只用于"如实报未实现"），因为
**远程发布需要网络 + 注册表账号**，本机都没有。

**`profile` 的 10 条 distribution 路径**：`list` / `use` / `create` / `delete` /
`describe` / `alias` / `rename` / `install` / `update` / `info`。

**没有接的 3 条（`show` / `export` / `import`）**：它们已经被 `memory_extra2.py`
接管成**用户画像**（`data/profile/USER.md`）的语义。本波次不抢它们——同一条路径两种
语义（"看画像"vs"看发行包"）会让 `orbit profile show` 变成掷骰子。distribution 的
`show` / `export` / `import` 三条操作在 `ProfileDist` 里是完整实现的（`show()` /
`export()` / `import_profile()`），命令行侧由这几条等价路径覆盖：

- 看发行包详情：`orbit profile info <名字>`（比 `show` 多一层文件指纹）；
- 打包成目录：`orbit profile export` 是画像导出，发行包用 `ProfileDist.export()`
  （HTTP / 测试可达）——这一条**是缺口**，本模块不假装它存在；
- 装一份导出的目录：`orbit profile install <本地目录>`（与 `import` 是同一套复制逻辑，
  且 `install` 还会核对 `manifest.json` 之外的来源形状）。

## 两种导入顺序都要成立

照 `skill_extra2.py` / `mcp.py` 的约定：本模块排在 `hermes_parity` **之前**时
`register_handler` 生效、节点更新被跳过（那时表还没建），所以再包一层
`_table.register_specs`，等声明表组装完立刻重放一遍；排在**之后**时导入即重放。
`_mount_everything()` 幂等，两种顺序下都可以被重复调用。
"""

from __future__ import annotations

from typing import Any

import app.cli.commands._table as _table
from app.cli.commands._table import register_handler
from app.cli.context import CliError, Context, truncate
from app.cli.spec import REGISTRY, Option
from app.profile_dist import ProfileDist, ProfileDistError
from app.skill_hub import SkillHub, SkillHubError, is_remote_target

#: "未实现"的退出码（与 `_table._pending` / `mcp.UNIMPLEMENTED_CODE` 一致）。
UNIMPLEMENTED_CODE = 3


# ── 小工具 ──────────────────────────────────────────────────────────────────


def _dist(context: Context) -> ProfileDist:
    """profile distribution 管理器（落在 `context.data_dir/profiles/`）。"""
    return ProfileDist(context.data_dir)


def _text(args: Any, *names: str) -> str:
    """按顺序取第一个非空参数值。

    为什么需要：同一条路径在两种导入顺序下的 `dest` 可能不同（表里的形状 vs 本模块
    换上的形状），两种都认才不会"某个顺序下取到空值"。与 `profile_cli._first_arg`
    同一套做法。
    """
    for name in names:
        value = getattr(args, name, None)
        if value:
            return str(value).strip()
    return ""


def _flag(args: Any, *names: str) -> bool:
    """任一开关为真即为真（同样为了兼容两种 dest 形状）。"""
    return any(bool(getattr(args, name, False)) for name in names)


def _dist_error(exc: Exception) -> CliError:
    """`ProfileDistError` → `CliError`（打印这句话 + 退出码）。

    远程来源单独给退出码 3：脚本要能区分"这条没实现"和"这条跑失败了"。
    """
    message = str(exc)
    code = UNIMPLEMENTED_CODE if "未实现" in message else 1
    return CliError(message, code=code)


# ── skills publish ──────────────────────────────────────────────────────────


def run_skills_publish(context: Context, args: Any) -> int:
    """`orbit skills publish <技能> [--to <本地目录>] [--apply]`。

    **默认 dry_run**：先打印"会发布什么、内容 hash 是多少、写到哪"，`--apply` 才落盘。
    发布是**对外可见**的动作，默认就该先看后做。

    门禁在 `SkillHub.publish` 里：`SKILL.md` 必须先过 `skill_lint` 零 ERROR。这里只负责
    把 `SkillHubError` 翻成退出码，并如实报出"远程发布未实现"。
    """
    name = _text(args, "skill_path", "name", "path")
    if not name:
        raise CliError("需要一个技能：`orbit skills publish <目录或技能名>`")
    to = _text(args, "to")
    repo = _text(args, "repo")
    if repo:
        raise CliError(
            f"`--repo {repo}` 未实现：它只用于 GitHub 发布（往别人的仓库/社区索引里写一条），"
            "需要网络 + 账号 token。本机能做的是打包到本地目录："
            "`orbit skills publish <技能> --to <本地目录> --apply`",
            code=UNIMPLEMENTED_CODE,
        )
    if is_remote_target(to):
        raise CliError(
            f"远程发布未实现：{to!r}。发布到社区索引需要网络 + 注册表账号 + 社区索引协议，"
            "本机都没有。本地做法：`orbit skills publish <技能> --to <本地目录> --apply`，"
            "产物是一份 SKILL.md + manifest.json（可直接被 `orbit skills install` 装回来）",
            code=UNIMPLEMENTED_CODE,
        )
    hub = SkillHub(context.data_dir)
    dry_run = not _flag(args, "apply")
    try:
        report = hub.publish(name, to=to or None, dry_run=dry_run)
    except SkillHubError as exc:
        raise CliError(str(exc)) from exc
    manifest = report["manifest"]
    context.out(f"{'将发布' if dry_run else '已发布'} {report['name']} → {report['target']}")
    context.table(
        ("项", "值"),
        (
            ("版本", str(manifest["version"])),
            ("内容 hash", report["content_hash"][:16] + "…"),
            ("文件", str(manifest["files"])),
            ("依赖", ", ".join(manifest["dependencies"]) or "（未声明）"),
            ("作者", manifest["author"] or "（未填）"),
            ("来源", report["source"]),
        ),
    )
    lint = report["lint"]
    context.out(f"lint：{lint['errors']} ERROR / {lint['warnings']} warning")
    for finding in lint["findings"]:
        context.note("  " + str(finding.get("code")) + " " + str(finding.get("message")))
    if report["overwrites"]:
        context.warn(
            "目标已存在同名包："
            + ("内容有变化，--apply 会整份替换（旧的多余文件会被删掉）"
               if report["changed"] else "内容与将要发布的完全一致")
        )
    if dry_run:
        context.out("[dry-run] 未写盘。确认请加 --apply")
    else:
        context.out(f"已写出 {report['files']} 个文件（含 manifest.json）")
        context.note(
            "装回来：`orbit skills install "
            + report["name"]
            + "`（走隔离区扫描后才会生效）"
        )
    context.emit({"ok": True, **report})
    return 0


# ── profile list / use ──────────────────────────────────────────────────────


def run_profile_list(context: Context, args: Any) -> int:
    """`orbit profile list`：列出全部 profile（`active` 现算）。"""
    dist = _dist(context)
    status = dist.status()
    if not status["profiles"]:
        context.out("还没有任何 profile。")
        context.note("装一套：orbit profile install <本地目录> --yes")
        context.note("或新建：orbit profile create <名字>")
        context.emit({"ok": True, **status})
        return 0
    context.table(
        ("名称", "生效", "版本", "配置项", "技能", "来源", "描述"),
        [
            (
                str(row["name"]),
                "是" if row["active"] else "",
                str(row["version"]),
                str(len(row.get("dist", {}).get("settings_keys") or [])),
                str(len(row.get("dist", {}).get("skills") or [])),
                truncate(str(row["source"]) or "-", 40),
                truncate(str(row["description"]) or "-", 30),
            )
            for row in status["profiles"]
        ],
    )
    if status["stale_active"]:
        context.warn(
            f"active 标记指向一个已不存在的 profile：{status['stale_active']}（"
            "`orbit profile use <名字>` 重新指定）"
        )
    context.out("")
    context.out(f"生效中：{status['active'] or '（无）'}　共 {status['count']} 套")
    context.emit({"ok": True, **status})
    return 0


def run_profile_use(context: Context, args: Any) -> int:
    """`orbit profile use <名字>`：切粘性默认，并**列出切了之后哪些东西会变**。

    切 profile 是危险操作：配置项（含审批模式、文件范围）、画像条目、技能清单会同时
    变。只回一句"已切换"，用户就不知道自己的 `approval_mode` 从 `manual` 变成了
    `off`——那是安全边界，不是风格偏好。所以这里把 `changes` 整份打出来。
    """
    name = _text(args, "profile_name", "name")
    dist = _dist(context)
    try:
        result = dist.use(name)
    except ProfileDistError as exc:
        raise _dist_error(exc) from exc
    if not result["changed"]:
        context.out(f"{result['name']} 已经是当前生效的 profile，没有改动。")
        context.emit({"ok": True, **result})
        return 0
    context.out(f"已切换：{result['previous'] or '（无）'} → {result['name']}")
    changes = result["changes"]
    rows = changes["settings"]
    if rows:
        context.out("")
        context.out("配置项变化（安全边界项标 ✱）：")
        context.table(
            ("配置", "原值", "新值"),
            [
                (
                    ("✱ " if row["safety"] else "") + str(row["key"]),
                    "-" if row["from"] is None else truncate(str(row["from"]), 30),
                    "-" if row["to"] is None else truncate(str(row["to"]), 30),
                )
                for row in rows
            ],
        )
    else:
        context.out("")
        context.out("配置项没有变化。")
    for label, key in (("画像条目", "user_profile"), ("技能清单", "skills")):
        part = changes[key]
        if part["added"] or part["removed"]:
            context.out("")
            context.out(
                f"{label}：+{len(part['added'])} / -{len(part['removed'])}"
            )
            for item in part["added"]:
                context.note(f"  + {truncate(str(item), 60)}")
            for item in part["removed"]:
                context.note(f"  - {truncate(str(item), 60)}")
    context.out("")
    for warning in result["warnings"]:
        context.warn(warning)
    context.emit({"ok": True, **result})
    return 0


# ── profile create / delete / rename ────────────────────────────────────────


def run_profile_create(context: Context, args: Any) -> int:
    """`orbit profile create <名字> [--clone-from …] [--description …]`。

    `--clone` = 从**当前生效**的 profile 克隆发行文件；`--clone-all` 连用户自己的文件
    一起克隆；`--no-skills` 不复制技能清单；`--no-alias` 不生成包装脚本（默认生成，
    与 Hermes 一致）。
    """
    name = _text(args, "profile_name", "name")
    if not name:
        raise CliError("需要一个名字：`orbit profile create <名字>`")
    dist = _dist(context)
    clone_from = _text(args, "clone_from")
    if _flag(args, "clone", "clone_all") and not clone_from:
        clone_from = dist.active_name() or ""
        if not clone_from:
            context.note("当前没有生效中的 profile，`--clone` 无从克隆，按空配置新建。")
    try:
        result = dist.create(
            name,
            clone_from=clone_from or None,
            clone_all=_flag(args, "clone_all"),
            description=_text(args, "description"),
            skills=not _flag(args, "no_skills"),
            alias=not _flag(args, "no_alias"),
        )
    except ProfileDistError as exc:
        raise _dist_error(exc) from exc
    context.out(f"已创建 profile：{result['name']} → {result['path']}")
    if result["cloned_from"]:
        context.out(f"从 {result['cloned_from']} 克隆（只复制发行文件）")
    for path in result["aliases"]:
        context.note(f"包装脚本：{path}")
    context.note(f"启用它：orbit profile use {result['name']}（切换会改配置与画像）")
    context.emit({"ok": True, **result})
    return 0


def run_profile_delete(context: Context, args: Any) -> int:
    """`orbit profile delete <名字> --yes`：删掉一套（不可恢复；生效中的那套不能删）。"""
    name = _text(args, "profile_name", "name")
    dist = _dist(context)
    try:
        result = dist.delete(name, yes=_flag(args, "yes"))
    except ProfileDistError as exc:
        raise _dist_error(exc) from exc
    context.out(f"已删除 profile：{result['name']}（{result['path']}）")
    for path in result["aliases_removed"]:
        context.note(f"同时删除包装脚本：{path}")
    context.emit({"ok": True, **result})
    return 0


def run_profile_rename(context: Context, args: Any) -> int:
    """`orbit profile rename <旧名> <新名>`：目录 / 记录 / 生效标记 / 脚本一起改。"""
    old = _text(args, "old_name", "old")
    new = _text(args, "new_name", "new")
    dist = _dist(context)
    try:
        result = dist.rename(old, new)
    except ProfileDistError as exc:
        raise _dist_error(exc) from exc
    context.out(f"已重命名：{result['old']} → {result['new']}")
    if result["active"]:
        context.note("这套是生效中的 profile，`active` 标记已跟着改名。")
    for path in result["aliases"]:
        context.note(f"包装脚本已重新生成：{path}")
    context.emit({"ok": True, **result})
    return 0


# ── profile describe / show 之外的信息命令 ──────────────────────────────────


def run_profile_describe(context: Context, args: Any) -> int:
    """`orbit profile describe [名字] [--text …] [--auto] [--overwrite] [--all]`。

    不给 `--text` / `--auto` 就是**读**：给了名字读一条，不给名字（或 `--all`）列全部。
    `--auto` 是机械生成（配置项数 / 技能数 / 画像条目数），不调模型——本机没有可用的
    摘要模型，编一句"看起来像摘要"的话等于假装理解了这个 profile。
    """
    name = _text(args, "profile_name", "name")
    dist = _dist(context)
    text = getattr(args, "text", None)
    try:
        result = dist.describe(
            name or None,
            text=None if text is None else str(text),
            auto=_flag(args, "auto"),
            overwrite=_flag(args, "overwrite"),
            all=_flag(args, "all"),
        )
    except ProfileDistError as exc:
        raise _dist_error(exc) from exc
    if "profiles" in result:
        context.table(
            ("名称", "生效", "描述"),
            [
                (
                    str(row["name"]),
                    "是" if row["active"] else "",
                    truncate(str(row["description"]) or "（未写）", 60),
                )
                for row in result["profiles"]
            ],
        )
        context.emit({"ok": True, **result})
        return 0
    if "description" in result and "previous" not in result:
        context.out(f"{result['name']}：{result['description'] or '（未写）'}")
    else:
        context.out(f"已设置 {result['name']} 的描述：{result['description']}")
        if result["previous"]:
            context.note(f"原描述：{result['previous']}")
    context.emit({"ok": True, **result})
    return 0


def run_profile_info(context: Context, args: Any) -> int:
    """`orbit profile info <名字>`：发行包清单（文件 + 指纹 + 是否被改过）。"""
    name = _text(args, "profile_name", "name")
    dist = _dist(context)
    try:
        result = dist.info(name)
    except ProfileDistError as exc:
        raise _dist_error(exc) from exc
    context.out(
        f"{result['name']}（版本 {result['version']}"
        f"{'，生效中' if result['active'] else ''}）"
    )
    context.out(f"来源：{result['source'] or '（无）'}")
    context.out(f"建立：{result['created_at'] or '-'}　最近更新：{result['updated_at'] or '-'}")
    context.out("")
    context.table(
        ("文件", "字节", "发行包管理", "被改过", "指纹"),
        [
            (
                str(row["path"]),
                str(row["size"]),
                "是" if row["managed"] else "",
                "是" if row["drift"] else "",
                str(row["hash"])[:12],
            )
            for row in result["files"]
        ],
    )
    if result["drift"]:
        context.warn(
            f"这些文件与装进来时的指纹不一致（`update` 会保留它们）：{', '.join(result['drift'])}"
        )
    if result["ignored_settings"]:
        context.note(f"安装时被忽略的配置项：{', '.join(result['ignored_settings'])}")
    context.emit({"ok": True, **result})
    return 0


# ── profile install / update / alias ────────────────────────────────────────


def run_profile_install(context: Context, args: Any) -> int:
    """`orbit profile install <本地目录> [--name …] [--alias] [--force] [--yes]`。

    **只接受本地目录**：装一套 profile = 把别人的 `settings.json` 应用到你的机器上
    （`approval_mode` / `file_scope` 就在里面），远程安装要先 clone 再逐文件审查，
    本机没有网络，第一步就做不到。
    """
    source = _text(args, "source", "path")
    dist = _dist(context)
    try:
        result = dist.install(
            source,
            name=_text(args, "name") or None,
            alias=_flag(args, "alias"),
            force=_flag(args, "force"),
            yes=_flag(args, "yes"),
        )
    except ProfileDistError as exc:
        raise _dist_error(exc) from exc
    context.out(f"已安装 profile：{result['name']} → {result['path']}")
    context.out(f"来源：{result['source']}")
    context.table(
        ("项", "值"),
        (
            ("配置项", ", ".join(sorted(result["settings"])) or "（无）"),
            ("技能清单", ", ".join(result["skills"]) or "（无）"),
            ("画像副本", "有" if result["has_user_profile"] else "无"),
            ("生效", "否（要显式 use）"),
        ),
    )
    for note in result["notes"]:
        context.note(note)
    for path in result["aliases"]:
        context.note(f"包装脚本：{path}")
    context.emit({"ok": True, **result})
    return 0


def run_profile_update(context: Context, args: Any) -> int:
    """`orbit profile update <名字> [--force-config] [--yes]`：从来源重拉，**保留用户数据**。"""
    name = _text(args, "profile_name", "name")
    dist = _dist(context)
    try:
        result = dist.update(
            name, force_config=_flag(args, "force_config"), yes=_flag(args, "yes")
        )
    except ProfileDistError as exc:
        raise _dist_error(exc) from exc
    context.out(
        f"已更新 {result['name']}（版本 {result['version']}）："
        f"覆盖 {len(result['refreshed'])} 个文件，保留 {len(result['preserved'])} 个"
    )
    if result["refreshed"]:
        context.note("覆盖：" + ", ".join(result["refreshed"]))
    if result["preserved"]:
        context.warn("保留（你改过）：" + ", ".join(result["preserved"]))
    for note in result["notes"]:
        context.note(note)
    context.emit({"ok": True, **result})
    return 0


def run_profile_alias(context: Context, args: Any) -> int:
    """`orbit profile alias <名字> [--name 脚本名] [--remove]`：生成包装脚本。

    脚本把 `settings.json` 翻译成 `ORBIT_<KEY>` 环境变量再调 `orbit`（pydantic-settings
    的 `env_prefix="ORBIT_"` 让这条路真的生效），**不动粘性默认**。它只覆盖 settings：
    画像与技能清单要靠 `use` 写 `active` 标记，返回里 `not_covers` 会如实列出这一点。
    """
    name = _text(args, "profile_name", "name_profile", "target")
    # `--name NAME` 在表里的 dest 是 `name`，而位置参数叫 `profile_name`：前者是**脚本名**，
    # 后者是**profile 名**（Hermes 的 `profile alias <profile> --name <别名>`）。
    alias_name = _text(args, "alias_name", "name")
    if not name:
        raise CliError("需要一个 profile 名：`orbit profile alias <名字>`")
    dist = _dist(context)
    try:
        result = dist.alias(name, remove=_flag(args, "remove"), alias_name=alias_name or None)
    except ProfileDistError as exc:
        raise _dist_error(exc) from exc
    if result["removed"]:
        context.out(f"已删除 {result['alias']} 的包装脚本（{len(result['paths'])} 个）")
    else:
        context.out(f"已生成 {result['alias']} 的包装脚本：")
        for path in result["paths"]:
            context.out(f"  {path}")
        context.out("")
        context.out("脚本会设置的环境变量：")
        for key, value in sorted(result["env"].items()):
            context.out(f"  {key}={value}")
        context.note("")
        context.note("覆盖范围：" + "；".join(result["covers"]))
        context.warn("不覆盖：" + "；".join(result["not_covers"]))
    context.emit({"ok": True, **result})
    return 0


# ── 接线 ────────────────────────────────────────────────────────────────────

#: `skills publish` 的选项：表里抄的是 Hermes 的远程形状（`--to {github,clawhub}`），
#: 本实现是本地打包，形状必须跟着语义走——否则用户会以为 `--to` 只接受那两个字面量。
PUBLISH_OPTIONS: tuple[Option, ...] = (
    Option(("skill_path",), "要发布的技能：目录路径、已安装技能名或 hub identifier",
           metavar="SKILL_PATH"),
    Option(("--to",), "目标目录（**只接受本地路径**；github/clawhub 需要网络+社区索引，未实现）",
           metavar="PATH"),
    Option(("--repo",), "远程仓库（仅 GitHub 发布用；本机未实现，给了会如实报错）",
           metavar="REPO"),
    Option(("--apply",), "真的写盘（默认 dry-run：只报将发布什么、hash 是多少）",
           action="store_true"),
)

#: 路径 → handler。`profile` 那 10 条在表里已有声明，选项形状与本实现一致，直接接管。
BINDINGS: tuple[tuple[str, Any], ...] = (
    ("skills publish", run_skills_publish),
    ("profile list", run_profile_list),
    ("profile use", run_profile_use),
    ("profile create", run_profile_create),
    ("profile delete", run_profile_delete),
    ("profile describe", run_profile_describe),
    ("profile alias", run_profile_alias),
    ("profile rename", run_profile_rename),
    ("profile install", run_profile_install),
    ("profile update", run_profile_update),
    ("profile info", run_profile_info),
)

#: 本模块**没有**接的 profile 路径，显式列出让"未实现"可被 grep 到。
#: 理由：这三条已被 `memory_extra2.py` 接管成**用户画像**语义（见模块 docstring）。
USER_PROFILE_PATHS: tuple[str, ...] = ("profile show", "profile export", "profile import")


def _bind(path: str, handler: Any, options: tuple[Option, ...] | None = None) -> None:
    """登记某条路径的实现；表已组装时顺手填到 `Command` 上（与 `mcp.py` / `skill_extra2.py` 同）。

    ``options`` 只在**本模块要换掉表里那份形状**时给（目前只有 `skills publish`）。
    """
    key = " ".join(path.split())
    if key not in _table.HANDLERS:
        register_handler(key, handler)
    parts = key.split()
    node = REGISTRY.get(parts[0])
    for part in parts[1:]:
        if node is None:
            return
        node = node.child(part)
    if node is None or node.subcommands:
        return
    node.handler = handler
    node.state = "done"
    if options is not None:
        node.options = list(options)


def _mount_everything() -> None:
    """把本模块负责的路径一次性接上。两种导入顺序下都成立、都可重复调用。"""
    for path, handler in BINDINGS:
        _bind(path, handler, options=PUBLISH_OPTIONS if path == "skills publish" else None)


def _install_assembly_hook() -> bool:
    """在「表组装」这一个缝上补挂（照 `skill_extra2.py` / `mcp.py`）。"""
    original = _table.register_specs
    if getattr(original, "_orbit_skill_extra3_wrapped", False):
        return False

    def register_specs(specs: Any) -> list[str]:
        skipped = original(specs)
        _mount_everything()
        return skipped

    register_specs._orbit_skill_extra3_wrapped = True  # type: ignore[attr-defined]
    _table.register_specs = register_specs
    return True


_install_assembly_hook()
_mount_everything()


def mounted_paths() -> list[str]:
    """本模块负责的路径（测试用它逐条核对"一条不少"）。"""
    return [path for path, _ in BINDINGS]


__all__ = [
    "BINDINGS",
    "PUBLISH_OPTIONS",
    "UNIMPLEMENTED_CODE",
    "USER_PROFILE_PATHS",
    "mounted_paths",
    "run_profile_alias",
    "run_profile_create",
    "run_profile_delete",
    "run_profile_describe",
    "run_profile_info",
    "run_profile_install",
    "run_profile_list",
    "run_profile_rename",
    "run_profile_update",
    "run_profile_use",
    "run_skills_publish",
]
