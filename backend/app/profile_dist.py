"""profile distribution（多套可切换的配置发行包）——矩阵 9.16。

## 先纠正一个概念：Hermes 的 `profile` **不是**用户画像

这是本轮最容易抄错的一处。Hermes 的 `hermes profile …` 是 **profile distribution
管理器**：一整套可安装、可切换、可导出的配置（`settings.json` 片段 + `USER.md` +
技能清单），`hermes profile install <git-url>` 从远端装一套进来，`hermes profile use
<name>` 切粘性默认。

Orbit 的 `app/profile.py` 是**用户画像**（`data/profile/USER.md`，1,600 字符预算，
整段注入系统提示）——那是矩阵 9.2 的活，已经做完了。两个东西同名不同物：

| | 用户画像（9.2，已做） | profile distribution（9.16，本模块） |
|---|---|---|
| 回答什么 | "这个人是谁、怎么工作" | "这台机器现在按哪套配置跑" |
| 载体 | `<data>/profile/USER.md` 一份 | `<data>/profiles/<name>/` 一整套 |
| 命令 | `profile show/add/set/trim/…` | `profile list/use/install/export/…` |
| 切换 | 不存在"切换" | `use` 切粘性默认（**危险操作**） |

所以本模块**不碰** `app/profile.py` 的任何一行；两者唯一的接触点是：一套发行包里
带一份 `USER.md` 副本（装进来之后，它**不会**自动变成当前画像——那要等 `use` 生效）。

## 落盘布局

```
<data>/profiles/
├── active                     ← 粘性默认 profile 名（一行文本）
├── aliases/                   ← `alias` 生成的包装脚本（orbit-<name>.cmd / .sh）
└── <name>/
    ├── profile.json           ← 记录：name/version/description/source/created_at/dist
    ├── settings.json          ← 配置片段（只保留 Settings 认识的键）
    ├── USER.md                ← 用户画像副本（可选）
    └── skills.json            ← 技能清单 {"skills": [...]}（可选）
```

`profile.json` 里**不存** `active` 这个事实的真值——`active` 的真值是 `profiles/active`
那一行。读的时候按标记现算，写两个地方必然会在某次崩溃后分叉，而"哪个是当前生效的"
分叉是最难排查的一类问题（用户以为切了、实际没切）。

## 三条安全约束（为什么这么定）

1. **`install` 只接受本地目录**。远程（`git@…` / `https://…` / `owner/repo`）如实抛
   未实现：装一套 profile 意味着把别人的 `settings.json` 与 `USER.md` 应用到你的机器上，
   这需要 clone 仓库 + 逐文件审查，本机无网络也没法验证。写个永远超时的壳只会让人
   以为装上了。
2. **`install` 绝不覆盖正在生效的那套**。生效中的 profile 被就地覆盖 = 用户当前这次
   会话的配置在背后被换掉，而 `settings.json` 里的 `approval_mode` / `file_scope`
   正是安全边界。要换，先 `use` 到别的，再装。
3. **`use` 必须报代价**。切换不是改一个字符串：配置项、画像、技能清单会同时变。
   只回一句"已切换"，用户就不知道自己的审批模式从 `manual` 变成了 `off`。所以
   `use()` 返回一份 `changes`（settings 逐键 from→to、画像条目增删、技能增删），
   CLI 原样打出来。

## `update` 为什么能"保留用户数据"

发行包里的文件是**别人给的**，但用户在同一个目录里也会写自己的东西（改过的 `USER.md`、
`notes/`、自己的 `settings.json`）。`update` 的做法是：

- `profile.json` 的 `dist.hashes` 记下**装进来那一刻**每个发行文件的指纹；
- 重拉时，指纹**没变**的文件才覆盖（= 用户没动过）；
- 指纹**变了**的文件（用户改过）保留，并列进 `preserved`；
- 发行清单之外的文件**一律不动**（删别人的东西不属于"更新"）。

`--force-config` 是唯一的例外开关：它只放宽 `settings.json` 这一项（配置写错导致
装不上的时候，用户需要一条"以发行包为准"的路），其他用户数据仍然保留。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import profile

#: profile 目录名（`<data_dir>/profiles/`）。
PROFILES_DIRNAME = "profiles"
#: 粘性默认标记文件名（内容是 profile 名 + 换行）。
ACTIVE_FILENAME = "active"
#: 每个 profile 的记录文件名。
RECORD_FILENAME = "profile.json"
#: 配置片段文件名（与 `data/settings.json` 同名，但只放片段、只保留认识的键）。
SETTINGS_FILENAME = "settings.json"
#: 画像副本文件名（与 `app/profile.py` 的 `FILE_NAME` 一致——装进来之后人一眼能认出）。
USER_FILENAME = profile.FILE_NAME
#: 技能清单文件名（**清单**，不是技能正文：发行包不该顺手装代码）。
SKILLS_FILENAME = "skills.json"
#: 包装脚本目录名。
ALIAS_DIRNAME = "aliases"
#: `export` 的默认落点目录名。
EXPORT_DIRNAME = "profile-exports"
#: 发行包格式版本。改布局时升它，好让 `import` 能说"这份是旧格式"。
FORMAT = 1

#: 允许出现在 `settings.json` 片段里的键（= `app.config.Settings` 的字段名）。
#:
#: 为什么要白名单：发行包来自别人。`data_dir` / `workspace` / `file_scope` 这类键
#: 直接决定"能读写哪些目录"，让一个外来包随手改掉它们等于把沙箱边界交出去。
#: 白名单之外**不是静默忽略**——`install` 会把被忽略的键列出来，用户能看见"这个包
#: 想改什么、我没让它改"。
DIST_SETTINGS_KEYS: tuple[str, ...] = (
    "approval_mode",
    "command_timeout",
    "embedding_model",
    "file_scope",
    "fts_min_coverage",
    "granted_actions",
    "hardline_in_machine",
    "headless",
    "log_level",
    "max_actions",
    "max_seconds",
    "model",
    "model_base_url",
    "model_provider",
    "query_routing",
    "report_interval",
    "rerank_enabled",
    "rerank_model",
    "rerank_pool",
    "skill_archive_days",
    "skill_archive_min_success_rate",
    "skill_stale_failures",
    "vector_enabled",
)

#: 会**改变安全边界**的键：`use` 切换时单独高亮它们。
#:
#: 为什么要单独列：`model` 换了只是答得不一样，`approval_mode: manual → off` 是
#: "以后所有动作都不再问你了"。两种变化混在一张表里，用户会漏掉后者。
SAFETY_KEYS: frozenset[str] = frozenset(
    {"approval_mode", "file_scope", "hardline_in_machine", "granted_actions", "headless"}
)

#: 远程来源的写法。命中即如实报未实现（理由见模块 docstring 第 1 条）。
REMOTE_PREFIXES: tuple[str, ...] = ("git@", "ssh:", "git+", "hg+")

#: 13 条子命令 → 本模块的方法名。`import` 是 Python 关键字，所以方法叫 `import_profile`。
#:
#: 写成数据是为了让"13 条一条不少"成为一行断言（`tests/test_profile_dist.py`），
#: 而不是靠数 13 个函数名。
OPERATIONS: tuple[tuple[str, str], ...] = (
    ("list", "list"),
    ("use", "use"),
    ("create", "create"),
    ("delete", "delete"),
    ("describe", "describe"),
    ("show", "show"),
    ("alias", "alias"),
    ("rename", "rename"),
    ("export", "export"),
    ("import", "import_profile"),
    ("install", "install"),
    ("update", "update"),
    ("info", "info"),
)


#: 类型别名。`ProfileDist` 里有一个方法叫 `list`（命令名就叫 list），于是**类作用域**内的
#: `list[str]` 会被解析成那个方法而不是内置类型（mypy: "Function ... is not valid as a
#: type"）。类内的列表注解一律走这两个别名，别在类里直接写 `list[...]`。
TextList = list[str]
RowList = list[dict[str, Any]]


class ProfileDistError(RuntimeError):
    """发行包相关的可预期失败（重名、不存在、来源是远程、删生效中的那套…）。

    用独立异常而不是 `ValueError`：CLI 要把它翻成"打印这句话 + 退出码"，
    而 `ValueError` 在别处有别的含义（参数解析）。
    """


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    """读 JSON 对象。坏文件当空 dict——一个坏记录不该让整个 profile 目录不可用。

    用 ``utf-8-sig`` 而不是 ``utf-8``：Windows 上的 PowerShell `Set-Content -Encoding
    utf8`、记事本"UTF-8"都会写一个 BOM，而 `json.loads` 见到 BOM 直接报
    ``Unexpected UTF-8 BOM``。发行包来自别人的机器，这类文件是常态——静默当成空配置
    会让"装上了但什么都没改"，比报错更难查。
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """原子写 JSON（临时文件 + `os.replace`）。半截的 `profile.json` 比没有更糟：
    它会让 `list` 里出现一个"名字有、内容空"的幽灵 profile。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def file_hash(path: Path) -> str:
    """单文件内容指纹（sha256）。用于 `dist.hashes` 与 `info` 的完整性展示。"""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _as_version(raw: Any) -> int:
    """把记录里的 version 折成整数。读不出来就记 1——版本号是**别人写的**字段，
    一个 `"v2"` 不该让整套 profile 装不上。"""
    try:
        return int(str(raw).strip().lstrip("vV"))
    except (TypeError, ValueError):
        return 1


def _safe_name(raw: str) -> str:
    """校验 profile 名：只允许字母数字与 `-_.`，且不能是 `.`/`..`。

    为什么要死卡：profile 名会直接拼成目录路径（`<data>/profiles/<name>`）。一个
    `../..` 就能让 `delete` 删掉整个数据目录——这是本模块唯一一个"外来输入直接进
    路径"的地方，必须在这里拦死，而不是指望调用方先校验。
    """
    name = str(raw or "").strip()
    if not name:
        raise ProfileDistError("需要一个 profile 名")
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise ProfileDistError(f"非法的 profile 名：{raw!r}（不能含路径分隔符）")
    if any(ch in name for ch in '<>:"|?*') or name.endswith("."):
        raise ProfileDistError(f"非法的 profile 名：{raw!r}（含 Windows 不允许的字符）")
    if len(name) > 64:
        raise ProfileDistError(f"profile 名太长（≤64 字符）：{raw!r}")
    return name


def looks_remote(source: str) -> bool:
    """来源是否是**带协议头**的远程（`https://…` / `git@…` / `ssh:…`）。

    只看形状，不做出网探测——本机没有网络，探测本身就会超时。
    """
    text = str(source or "").strip()
    if not text:
        return False
    lowered = text.lower()
    return "://" in text or lowered.startswith(REMOTE_PREFIXES)


def looks_like_remote_ref(source: str) -> bool:
    """是否是 Hermes 的 `owner/repo` 注册表写法（`nous/hermes-profile`）。

    这一条**必须与"本地相对路径"分开判**：`profiles/work` 与 `nous/hermes-profile`
    长得一模一样。所以调用方要先看磁盘上有没有这个目录，只有"不存在 + 形状像
    owner/repo"才当成远程引用。单看形状就下结论，会把用户自己的相对路径判成远程。
    """
    text = str(source or "").strip()
    if not text or "\\" in text or Path(text).is_absolute():
        return False
    parts = text.lower().split("/")
    return len(parts) == 2 and all(parts) and "." not in parts[0]


@dataclass
class ProfileRecord:
    """一套 profile 的记录（`profile.json` 的形状 + 现算的 `active`）。

    字段就是任务书里的那 7 个：``name`` / ``version`` / ``description`` / ``source`` /
    ``path`` / ``active`` / ``created_at``。其余（``dist``）是**发行来源的记账**，
    没有它 `update` 就无法判断"哪个文件是别人给的、哪个是用户自己写的"。
    """

    name: str
    version: int = 1
    description: str = ""
    source: str = ""
    path: str = ""
    active: bool = False
    created_at: str = ""
    updated_at: str = ""
    #: 发行来源的记账：`settings_keys` / `skills` / `hashes`（文件名 → 装进来时的指纹）。
    dist: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "source": self.source,
            "path": self.path,
            "active": self.active,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "dist": dict(self.dist),
        }


class ProfileDist:
    """profile distribution 管理器。一个实例 = 一份 `data_dir` 的 profiles 视图。

    ``now`` 可注入（测试里要断言 `created_at`），默认取系统 UTC 时间。
    """

    def __init__(self, data_dir: Path | str, *, now: Any = None) -> None:
        self.data_dir = Path(data_dir)
        self._now = now

    def now(self) -> datetime:
        return self._now() if self._now is not None else datetime.now(UTC)

    # —— 路径 ——

    @property
    def root(self) -> Path:
        return self.data_dir / PROFILES_DIRNAME

    @property
    def active_path(self) -> Path:
        return self.root / ACTIVE_FILENAME

    @property
    def alias_root(self) -> Path:
        return self.root / ALIAS_DIRNAME

    def dir_of(self, name: str) -> Path:
        return self.root / _safe_name(name)

    def record_path(self, name: str) -> Path:
        return self.dir_of(name) / RECORD_FILENAME

    # —— 读：active ——

    def active_name(self) -> str:
        """当前生效的 profile 名（没有则空串）。

        标记指向一个**已经不存在**的 profile 时也返回空串并留一条 warning 字段
        （见 `status()`）：`active` 说 A、而 A 被删了，这种状态必须能被看见，
        否则下一次 `use` 的"代价对比"会拿一个空 profile 当基准。
        """
        try:
            text = self.active_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return ""
        return text if (self.root / text).is_dir() else ""

    def _stale_active(self) -> str:
        """`active` 标记指向的目录不存在时，返回那个名字（供 `status()` 报出来）。"""
        try:
            text = self.active_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return ""
        if text and not (self.root / text).is_dir():
            return text
        return ""

    def _set_active(self, name: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.active_path.with_name(self.active_path.name + ".tmp")
        tmp.write_text(name + "\n", encoding="utf-8")
        os.replace(tmp, self.active_path)

    # —— 读：记录 ——

    def names(self) -> TextList:
        """全部 profile 名（按名字排序；没有 `profile.json` 的目录也算——它可能是
        用户手建的，报出来比默默忽略更有用）。"""
        if not self.root.is_dir():
            return []
        return sorted(
            entry.name
            for entry in self.root.iterdir()
            if entry.is_dir() and entry.name != ALIAS_DIRNAME
        )

    def exists(self, name: str) -> bool:
        return self.dir_of(name).is_dir()

    def require(self, name: str) -> str:
        """取一个存在的 profile 名，不存在就给可行动的报错。"""
        clean = _safe_name(name)
        if not self.dir_of(clean).is_dir():
            known = ", ".join(self.names()) or "（空）"
            raise ProfileDistError(
                f"没有名为 {clean!r} 的 profile。现有：{known}"
                "（`orbit profile install <本地目录>` 装一套进来）"
            )
        return clean

    def record(self, name: str) -> ProfileRecord:
        """读一套 profile 的记录。缺 `profile.json` 时按目录名现造一条（不写盘）。"""
        clean = _safe_name(name)
        directory = self.dir_of(clean)
        payload = _read_json(directory / RECORD_FILENAME)
        active = self.active_name() == clean
        try:
            version = int(str(payload.get("version") or 1))
        except (TypeError, ValueError):
            version = 1
        dist = payload.get("dist")
        return ProfileRecord(
            name=str(payload.get("name") or clean),
            version=version,
            description=str(payload.get("description") or ""),
            source=str(payload.get("source") or ""),
            path=str(directory),
            active=active,
            created_at=str(payload.get("created_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
            dist=dict(dist) if isinstance(dist, dict) else {},
        )

    def _save_record(self, record: ProfileRecord) -> None:
        payload = {
            "name": record.name,
            "version": record.version,
            "description": record.description,
            "source": record.source,
            "created_at": record.created_at,
            "updated_at": record.updated_at or record.created_at,
            "format": FORMAT,
            "dist": record.dist,
        }
        _write_json(self.record_path(record.name), payload)

    # —— 读：发行文件 ——

    def settings_of(self, name: str) -> dict[str, Any]:
        """读一套 profile 的配置片段（只保留白名单键）。"""
        payload = _read_json(self.dir_of(name) / SETTINGS_FILENAME)
        return {key: value for key, value in payload.items() if key in DIST_SETTINGS_KEYS}

    def skills_of(self, name: str) -> TextList:
        """读技能清单。"""
        payload = _read_json(self.dir_of(name) / SKILLS_FILENAME)
        skills = payload.get("skills")
        if not isinstance(skills, list):
            return []
        return [str(item) for item in skills if str(item).strip()]

    def user_entries_of(self, name: str) -> TextList:
        """读画像副本的条目（复用 `app/profile.split_entries`，只读它）。

        复用而不是另写一个解析器：画像条目的分隔规则只能有一处定义，否则
        "发行包里有 3 条、切过去变成 4 条"这种差异会出现在 `use` 的代价报告里。

        读文件用 ``utf-8-sig``：带 BOM 的 `USER.md` 会让标题行变成一条"以 \\ufeff#
        开头"的条目（BOM 不是 `#`），于是 `use` 的代价报告里凭空多出一条画像。
        """
        path = self.dir_of(name) / USER_FILENAME
        try:
            return profile.split_entries(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError):
            return []

    # —— 1. list ——

    def list(self) -> RowList:
        """列出全部 profile（`active` 现算，不读记录里的陈旧值）。"""
        return [self.record(name).as_dict() for name in self.names()]

    def status(self) -> dict[str, Any]:
        """整体状态（CLI 的头部 + `--json` 载荷）。

        ``stale_active`` 是"标记指向一个已不存在的 profile"——那种状态必须报出来。
        """
        return {
            "active": self.active_name(),
            "stale_active": self._stale_active(),
            "root": str(self.root),
            "count": len(self.names()),
            "profiles": self.list(),
        }

    # —— 2. use ——

    def use(self, name: str) -> dict[str, Any]:
        """切换粘性默认 profile，并**列出切了之后哪些东西会变**。

        为什么必须报代价：切换不是改一个字符串。配置项（含审批模式、文件范围）、
        画像条目、技能清单会同时变。只回一句"已切换"，用户就不知道自己的
        `approval_mode` 从 `manual` 变成了 `off`——那是安全边界，不是风格偏好。

        返回 ``changes``：

        - ``settings``：逐键 `{key, from, to, safety}`（`safety=True` 的是安全边界项）
        - ``user_profile``：画像条目 `{added, removed}`
        - ``skills``：技能清单 `{added, removed}`
        - ``warnings``：生效时机、回退方式、以及"目标这套什么都没有"之类的事实

        **不复制任何文件**：`use` 只写 `<data>/profiles/active` 这一行标记。真正按
        标记装配是运行时的事（本轮 `app/config.py` 不归本模块），所以返回值里
        明确写 ``applies_at="下次装配"``——假装"立刻生效"是最容易让人困惑的一种。
        """
        clean = self.require(name)
        previous = self.active_name()
        if previous == clean:
            return {
                "ok": True,
                "name": clean,
                "previous": previous,
                "changed": False,
                "applies_at": "已经生效",
                "changes": {"settings": [], "user_profile": {"added": [], "removed": []},
                            "skills": {"added": [], "removed": []}},
                "warnings": [f"{clean} 已经是当前生效的 profile，没有改动。"],
            }
        changes = self.diff(previous, clean) if previous else self.diff(None, clean)
        self._set_active(clean)
        warnings = [
            "标记已写入，但**不会改当前进程的配置**：运行时在装配时读这个标记，"
            "所以代价从下次启动/下一次装配开始。",
        ]
        if previous:
            warnings.append(f"要回退：orbit profile use {previous}")
        else:
            warnings.append("之前没有生效中的 profile（`active` 标记原本为空）。")
        if not (self.dir_of(clean) / SETTINGS_FILENAME).is_file():
            warnings.append(f"{clean} 里没有 settings.json：切换后不会改变任何配置项。")
        safety = [row["key"] for row in changes["settings"] if row.get("safety")]
        if safety:
            warnings.append(
                f"注意：这次切换会改动安全边界相关的配置（{', '.join(safety)}）——"
                "确认目标 profile 是你自己装的那套"
            )
        return {
            "ok": True,
            "name": clean,
            "previous": previous,
            "changed": True,
            "applies_at": "下次装配",
            "changes": changes,
            "warnings": warnings,
        }

    def diff(self, before: str | None, after: str) -> dict[str, Any]:
        """两套 profile 的差异（`use` 的代价报告，也可单独用来预览切换）。"""
        old_settings = self.settings_of(before) if before else {}
        new_settings = self.settings_of(after)
        settings: list[dict[str, Any]] = []
        for key in sorted(set(old_settings) | set(new_settings)):
            old_value = old_settings.get(key)
            new_value = new_settings.get(key)
            if old_value == new_value:
                continue
            settings.append(
                {
                    "key": key,
                    "from": old_value,
                    "to": new_value,
                    "safety": key in SAFETY_KEYS,
                }
            )
        old_entries = self.user_entries_of(before) if before else []
        new_entries = self.user_entries_of(after)
        old_skills = self.skills_of(before) if before else []
        new_skills = self.skills_of(after)
        return {
            "settings": settings,
            "user_profile": {
                "added": [item for item in new_entries if item not in old_entries],
                "removed": [item for item in old_entries if item not in new_entries],
            },
            "skills": {
                "added": [item for item in new_skills if item not in old_skills],
                "removed": [item for item in old_skills if item not in new_skills],
            },
        }

    # —— 3. create ——

    def create(
        self,
        name: str,
        *,
        clone_from: str | None = None,
        clone_all: bool = False,
        description: str = "",
        skills: bool = True,
        alias: bool = True,
    ) -> dict[str, Any]:
        """新建一套 profile（可选从现有 profile 克隆）。

        ``clone_from`` 只复制**发行文件**（settings/USER/skills 清单）；``clone_all``
        连用户自己的文件一起复制。默认不克隆——"新建"的语义是从空开始，把它做成
        "默认复制当前那套"会让用户以为自己在改一套干净的配置，其实在改副本。
        """
        clean = _safe_name(name)
        directory = self.dir_of(clean)
        if directory.exists():
            raise ProfileDistError(f"profile 已存在：{clean}（要改它请用 `orbit profile describe`）")
        moment = self.now()
        dist: dict[str, Any] = {"settings_keys": [], "skills": [], "hashes": {}}
        source = ""
        if clone_from:
            origin = self.require(clone_from)
            source = str(self.dir_of(origin))
            settings = self.settings_of(origin)
            dist["settings_keys"] = sorted(settings)
            dist["skills"] = self.skills_of(origin) if skills else []
            directory.mkdir(parents=True, exist_ok=True)
            if settings:
                _write_json(directory / SETTINGS_FILENAME, settings)
            if skills:
                _write_json(directory / SKILLS_FILENAME, {"skills": dist["skills"]})
            user_md = self.dir_of(origin) / USER_FILENAME
            if user_md.is_file():
                shutil.copy2(user_md, directory / USER_FILENAME)
            if clone_all:
                known = {SETTINGS_FILENAME, SKILLS_FILENAME, USER_FILENAME, RECORD_FILENAME}
                for entry in sorted(self.dir_of(origin).iterdir()):
                    if entry.name in known:
                        continue
                    target = directory / entry.name
                    if entry.is_dir():
                        shutil.copytree(entry, target, dirs_exist_ok=True)
                    else:
                        shutil.copy2(entry, target)
        else:
            directory.mkdir(parents=True, exist_ok=True)
            _write_json(directory / SETTINGS_FILENAME, {})
            _write_json(directory / SKILLS_FILENAME, {"skills": []})
        for filename in (SETTINGS_FILENAME, SKILLS_FILENAME, USER_FILENAME):
            path = directory / filename
            if path.is_file():
                dist["hashes"][filename] = file_hash(path)
        record = ProfileRecord(
            name=clean,
            version=1,
            description=str(description or "").strip(),
            source=source,
            path=str(directory),
            active=self.active_name() == clean,
            created_at=_iso(moment),
            updated_at=_iso(moment),
            dist=dist,
        )
        self._save_record(record)
        result: dict[str, Any] = {
            "ok": True,
            "name": clean,
            "cloned_from": clone_from or "",
            "path": str(directory),
            "aliases": [],
        }
        if alias:
            result["aliases"] = self.alias(clean)["paths"]
        return result

    # —— 4. delete ——

    def delete(self, name: str, *, yes: bool = False) -> dict[str, Any]:
        """删掉一套 profile（目录整份删除，不可恢复）。

        两条硬约束：

        1. **必须显式 `yes`**——删目录不可逆，`settings.json` 与画像副本一起没了；
        2. **不能删正在生效的那套**。生效中的 profile 被删掉，`active` 标记就指向一个
           不存在的目录，下一次 `use` 的代价对比会拿空 profile 当基准，而用户看到的
           是"我的配置凭空回到默认"。
        """
        clean = self.require(name)
        if self.active_name() == clean:
            raise ProfileDistError(
                f"{clean} 正在生效，不能删。先 `orbit profile use <另一个>` 切走再删"
                "（否则 active 标记会指向一个不存在的目录）"
            )
        if not yes:
            raise ProfileDistError(f"这会删掉 {clean} 整份配置（不可恢复）。确认请加 --yes")
        directory = self.dir_of(clean)
        shutil.rmtree(directory, ignore_errors=True)
        removed_aliases = self._drop_alias_files(clean)
        return {
            "ok": True,
            "name": clean,
            "removed": not directory.exists(),
            "path": str(directory),
            "aliases_removed": removed_aliases,
        }

    # —— 5. describe ——

    def describe(
        self,
        name: str | None = None,
        *,
        text: str | None = None,
        auto: bool = False,
        overwrite: bool = False,
        all: bool = False,
    ) -> dict[str, Any]:
        """读或设 description。

        四种用法（对应 Hermes 的 `--text/--auto/--overwrite/--all`）：

        - 只给名字：读这一条的描述；
        - 不给名字（或 ``all=True``）：列出全部 profile 的描述（**空白也要列出来**，
          否则"哪些还没写描述"看不出来）；
        - ``text=``：设置描述。已有描述且没给 ``overwrite`` 就拒绝——描述是给人看的
          一句话，"默默覆盖"会让用户以为自己的说明还在；
        - ``auto=True``：从发行内容机械生成一句（配置项数 / 技能数 / 画像条目数）。
          它**不调模型**：本机没有可用的摘要模型，编一句"看起来像摘要"的话等于假装
          理解了这个 profile。
        """
        if text is None and not auto:
            if all or not name:
                rows = [
                    {"name": item["name"], "description": item["description"], "active": item["active"]}
                    for item in self.list()
                ]
                return {"ok": True, "profiles": rows, "count": len(rows)}
            clean = self.require(name)
            record = self.record(clean)
            return {
                "ok": True,
                "name": clean,
                "description": record.description,
                "active": record.active,
            }
        clean = self.require(str(name or ""))
        record = self.record(clean)
        candidate = str(text).strip() if text is not None else self._auto_description(clean)
        if record.description and not overwrite and candidate != record.description:
            raise ProfileDistError(
                f"{clean} 已有描述：{record.description!r}。要改请加 --overwrite"
            )
        previous = record.description
        record.description = candidate
        record.updated_at = _iso(self.now())
        self._save_record(record)
        return {
            "ok": True,
            "name": clean,
            "description": candidate,
            "previous": previous,
            "auto": bool(auto),
        }

    def _auto_description(self, name: str) -> str:
        """从发行内容生成一句描述（不调模型，纯计数）。"""
        settings = self.settings_of(name)
        skills = self.skills_of(name)
        entries = self.user_entries_of(name)
        parts = [f"{len(settings)} 项配置"]
        if skills:
            parts.append(f"{len(skills)} 个技能")
        if entries:
            parts.append(f"{len(entries)} 条画像")
        head = settings.get("model_provider") or settings.get("model") or "默认模型"
        return f"{head} · " + " / ".join(parts)

    # —— 6. show ——

    def show(self, name: str) -> dict[str, Any]:
        """一套 profile 的详情：记录 + 发行内容摘要（不打印文件正文）。

        与 `info` 的分工：`show` 回答"这套是什么"（给人看），`info` 回答"这份发行包
        里到底有哪些文件、指纹是什么"（核对完整性用）。
        """
        clean = self.require(name)
        record = self.record(clean)
        settings = self.settings_of(clean)
        skills = self.skills_of(clean)
        entries = self.user_entries_of(clean)
        return {
            **record.as_dict(),
            "settings": settings,
            "settings_count": len(settings),
            "skills": skills,
            "skills_count": len(skills),
            "user_entries": entries,
            "user_entries_count": len(entries),
            "has_settings": (self.dir_of(clean) / SETTINGS_FILENAME).is_file(),
            "has_user_profile": (self.dir_of(clean) / USER_FILENAME).is_file(),
        }

    # —— 7. alias ——

    def alias(
        self,
        name: str,
        *,
        remove: bool = False,
        alias_name: str | None = None,
    ) -> dict[str, Any]:
        """生成（或删除）一套 profile 的包装脚本。

        ## 脚本做什么

        把 `settings.json` 片段翻译成 `ORBIT_<KEY>` 环境变量，再调 `orbit`。这是
        **真机制**而不是装饰：`app/config.py` 的 `Settings` 是 pydantic-settings，
        `env_prefix="ORBIT_"`，所以 `ORBIT_MODEL_PROVIDER=kimi` 确实会覆盖
        `data/settings.json` 里的值——一次调用一套配置，**不动粘性默认**。

        ## 它不做什么（必须说清楚）

        包装脚本**只覆盖 settings**。画像（USER.md）与技能清单不在环境变量能表达的
        范围内，它们要等 `use` 写 `active` 标记、运行时装配时读。所以 `alias` 的
        返回里带 ``covers`` / ``not_covers`` 两个字段，CLI 会原样打出来——把"半套
        生效"说成"整套生效"是这条命令最容易撒的谎。
        """
        clean = self.require(name)
        stem = _safe_name(alias_name) if alias_name else clean
        cmd_path, sh_path = self._alias_paths(stem)
        if remove:
            removed: list[str] = []
            for path in (cmd_path, sh_path):
                if path.is_file():
                    path.unlink()
                    removed.append(str(path))
            return {"ok": True, "name": clean, "alias": stem, "paths": removed, "removed": True}
        settings = self.settings_of(clean)
        lines_cmd = ["@echo off", f"rem orbit profile alias: {clean}"]
        lines_sh = ["#!/bin/sh", f"# orbit profile alias: {clean}"]
        for key, value in sorted(settings.items()):
            rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            lines_cmd.append(f'set ORBIT_{key.upper()}={rendered}')
            lines_sh.append(f'export ORBIT_{key.upper()}="{rendered}"')
        data_dir = str(self.data_dir)
        lines_cmd.append(f'"{_orbit_command()}" --data-dir "{data_dir}" %*')
        lines_sh.append(f'exec {_orbit_command()} --data-dir "{data_dir}" "$@"')
        self.alias_root.mkdir(parents=True, exist_ok=True)
        cmd_path.write_text("\r\n".join(lines_cmd) + "\r\n", encoding="utf-8")
        sh_path.write_text("\n".join(lines_sh) + "\n", encoding="utf-8")
        return {
            "ok": True,
            "name": clean,
            "alias": stem,
            "paths": [str(cmd_path), str(sh_path)],
            "removed": False,
            "covers": ["settings.json 里的每一项（翻译成 ORBIT_<KEY> 环境变量）"],
            "not_covers": [
                "用户画像（USER.md）：要用它请 `orbit profile use " + clean + "`",
                "技能清单（skills.json）：同上",
            ],
            "env": {f"ORBIT_{key.upper()}": value for key, value in settings.items()},
        }

    # —— 8. rename ——

    def _alias_paths(self, stem: str) -> tuple[Path, Path]:
        """一套别名对应的两个脚本路径（`.cmd` 给 cmd/PowerShell，`.sh` 给 git-bash）。"""
        return (
            self.alias_root / f"orbit-{stem}.cmd",
            self.alias_root / f"orbit-{stem}.sh",
        )

    def _drop_alias_files(self, stem: str) -> TextList:
        """直接删别名文件（**不校验 profile 是否存在**）。

        `rename` 需要它：目录改名之后旧名字已经不是合法 profile 了，走 `alias(remove=True)`
        会在 `require()` 那里被拒——而旧名字的包装脚本正需要被删掉。
        """
        removed: list[str] = []
        for path in self._alias_paths(stem):
            if path.is_file():
                path.unlink()
                removed.append(str(path))
        return removed

    def rename(self, old_name: str, new_name: str) -> dict[str, Any]:
        """重命名一套 profile（目录 + 记录 + 生效标记 + 包装脚本一起改）。

        为什么四处都要动：少改一处就会出现"目录叫 B、记录叫 A、active 指向 A、脚本
        叫 A"的四份事实。`active` 指向的那套被改名时，标记必须跟着改——否则重命名
        就等于把用户当前的配置悄悄停用了。
        """
        old = self.require(old_name)
        new = _safe_name(new_name)
        if self.dir_of(new).exists():
            raise ProfileDistError(f"目标名字已被占用：{new}")
        was_active = self.active_name() == old
        source_dir = self.dir_of(old)
        target_dir = self.dir_of(new)
        source_dir.rename(target_dir)
        record = self.record(new)
        record.name = new
        record.path = str(target_dir)
        record.updated_at = _iso(self.now())
        self._save_record(record)
        self._drop_alias_files(old)
        if was_active:
            self._set_active(new)
        aliases = self.alias(new)["paths"]
        return {
            "ok": True,
            "old": old,
            "new": new,
            "path": str(target_dir),
            "active": was_active,
            "aliases": aliases,
        }

    # —— 9. export ——

    def export(self, name: str, *, output: str | Path | None = None) -> dict[str, Any]:
        """把一套 profile 打包成一个**目录**（可被 `import` / `install` 直接装回去）。

        只导出**发行文件**（settings / USER / skills 清单 / profile.json），不导出用户
        在这个目录里另存的东西：导出物的用途是"给别人装"，把用户的私人笔记一起打包
        出去是数据泄漏。默认落点 `<data>/profile-exports/<name>/`。
        """
        clean = self.require(name)
        directory = Path(output).expanduser() if output else self.data_dir / EXPORT_DIRNAME / clean
        if directory.exists() and any(directory.iterdir()):
            raise ProfileDistError(f"导出目录非空：{directory}（换一个路径，或先清掉它）")
        directory.mkdir(parents=True, exist_ok=True)
        source = self.dir_of(clean)
        copied: list[str] = []
        for filename in (SETTINGS_FILENAME, USER_FILENAME, SKILLS_FILENAME):
            path = source / filename
            if path.is_file():
                shutil.copy2(path, directory / filename)
                copied.append(filename)
        record = self.record(clean)
        payload = record.as_dict()
        payload.pop("active", None)
        _write_json(directory / RECORD_FILENAME, payload)
        manifest = {
            "format": FORMAT,
            "name": clean,
            "version": record.version,
            "description": record.description,
            "exported_at": _iso(self.now()),
            "files": {filename: file_hash(directory / filename) for filename in copied},
        }
        _write_json(directory / "manifest.json", manifest)
        return {
            "ok": True,
            "name": clean,
            "path": str(directory),
            "files": [*copied, RECORD_FILENAME, "manifest.json"],
            "manifest": manifest,
        }

    # —— 10. import ——

    def import_profile(
        self,
        archive: str | Path,
        *,
        name: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """从一份**导出的目录**导入一套 profile。

        实现上就是 `install`（同一套复制逻辑 + 同一套安全约束），差别只有一处：
        导入会先核对 `manifest.json` 里的文件指纹，指纹不符就**拒绝**——导出物在
        传递过程中被改过（少了一个文件、多了一行配置），装进来的就不是导出时那份了。
        没有 `manifest.json` 的目录也能导入（当作普通本地目录），但返回值里会如实
        标 ``verified=False``。
        """
        directory = Path(str(archive)).expanduser()
        if not directory.is_dir() and (looks_remote(str(archive)) or looks_like_remote_ref(str(archive))):
            raise ProfileDistError(
                f"远程导入未实现：{archive!r}（需要网络 + clone 仓库）。"
                "把 profile 目录拷到本地，再用 `orbit profile import <本地目录>`"
            )
        if not directory.is_dir():
            raise ProfileDistError(f"找不到目录：{directory}")
        manifest = _read_json(directory / "manifest.json")
        files = manifest.get("files")
        verified = False
        if isinstance(files, dict) and files:
            mismatched = [
                filename
                for filename, expected in files.items()
                if file_hash(directory / filename) != str(expected)
            ]
            if mismatched:
                raise ProfileDistError(
                    f"导入被拒绝：{', '.join(sorted(mismatched))} 的指纹与 manifest.json 不符。"
                    "这份导出物在传递过程中被改过（或用 `--force` 之外的路径装它）"
                )
            verified = True
        result = self.install(directory, name=name, force=force, yes=True)
        return {**result, "verified": verified, "manifest": manifest}

    # —— 11. install ——

    def install(
        self,
        source: str | Path,
        *,
        name: str | None = None,
        alias: bool = False,
        force: bool = False,
        yes: bool = False,
    ) -> dict[str, Any]:
        """从**本地目录**装一套配置（settings 片段 + USER.md + skills 清单）。

        ## 为什么只接受本地目录

        装一套 profile = 把别人的 `settings.json` 应用到你的机器上（`approval_mode` /
        `file_scope` 就在里面）。远程安装要做的是：clone 仓库 → 逐文件审查 → 装。
        本机没有网络，第一步就做不到；即使有网络，"审查"这一步也没有可依赖的判据。
        所以远程来源如实抛未实现，并给出本机能做的替代（先拷到本地再装）。

        ## 为什么不覆盖生效中的那套

        `name` 与当前生效的 profile 撞名时**直接拒绝**（`force` 也不行）：生效中的那套
        被就地替换，等于用户当前这次会话的安全边界在背后被换掉，而屏幕上什么提示都没有。
        要换：先 `use` 到别的，再装（或装成另一个名字）。

        ## `force` 是什么

        只放宽"目标已存在但**不是生效中的**"这一种情况（覆盖一套闲置的 profile）。
        它**不放宽**远程来源、也不放宽生效中那套——这两个不是"再确认一次"能解决的，
        是设计上不该发生的事。
        """
        text = str(source or "").strip()
        if not text:
            raise ProfileDistError("需要一个来源目录")
        directory = Path(text).expanduser()
        # 顺序很重要：**先看磁盘**。`profiles/work` 与 `nous/hermes-profile` 形状相同，
        # 只有"本地确实没有这个目录"时才把它当远程引用。
        if not directory.is_dir() and (looks_remote(text) or looks_like_remote_ref(text)):
            raise ProfileDistError(
                f"远程安装未实现：{text!r}。装一套 profile 需要 clone 仓库 + 逐文件审查，"
                "本机无网络，做不到。本地做法：把 profile 目录拷到本地，"
                "再 `orbit profile install <本地目录>`"
            )
        if not directory.is_dir():
            raise ProfileDistError(f"找不到目录：{directory}")
        origin_record = _read_json(directory / RECORD_FILENAME)
        clean = _safe_name(name or str(origin_record.get("name") or "") or directory.name)
        active = self.active_name()
        if active and active == clean:
            raise ProfileDistError(
                f"{clean} 正在生效，不能就地覆盖（生效中的配置在背后被换掉是最危险的一种）。"
                f"先 `orbit profile use <另一个>` 切走，或 `orbit profile install {directory} --name <别的名字>`"
            )
        target = self.dir_of(clean)
        if target.exists() and not force:
            raise ProfileDistError(f"profile 已存在：{clean}（要覆盖请加 --force）")
        if not yes:
            raise ProfileDistError(
                f"这会往 {target} 写入一套配置（含 settings.json 片段与 USER.md）。确认请加 --yes"
            )
        settings = _read_json(directory / SETTINGS_FILENAME)
        kept = {key: value for key, value in settings.items() if key in DIST_SETTINGS_KEYS}
        ignored = sorted(key for key in settings if key not in DIST_SETTINGS_KEYS)
        skills = self._read_source_skills(directory)
        moment = self.now()
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        _write_json(target / SETTINGS_FILENAME, kept)
        _write_json(target / SKILLS_FILENAME, {"skills": skills})
        user_source = directory / USER_FILENAME
        has_user = user_source.is_file()
        if has_user:
            shutil.copy2(user_source, target / USER_FILENAME)
        hashes = {
            filename: file_hash(target / filename)
            for filename in (SETTINGS_FILENAME, SKILLS_FILENAME, USER_FILENAME)
            if (target / filename).is_file()
        }
        record = ProfileRecord(
            name=clean,
            version=_as_version(origin_record.get("version")),
            description=str(origin_record.get("description") or ""),
            source=str(directory),
            path=str(target),
            active=False,
            created_at=_iso(moment),
            updated_at=_iso(moment),
            dist={
                "settings_keys": sorted(kept),
                "skills": skills,
                "hashes": hashes,
                "ignored_settings": ignored,
            },
        )
        self._save_record(record)
        result: dict[str, Any] = {
            "ok": True,
            "name": clean,
            "path": str(target),
            "source": str(directory),
            "settings": kept,
            "ignored_settings": ignored,
            "skills": skills,
            "has_user_profile": has_user,
            "active": False,
            "aliases": [],
            "notes": [
                f"装好了但没有生效：用 `orbit profile use {clean}` 切换（切换会改配置与画像）。",
            ],
        }
        if ignored:
            result["notes"].append(
                f"被忽略的配置项：{', '.join(ignored)}"
                "（不在允许改动的白名单里——它们会改数据目录/工作区这类边界）"
            )
        if alias:
            result["aliases"] = self.alias(clean)["paths"]
        return result

    def _read_source_skills(self, directory: Path) -> TextList:
        """读来源里的技能清单。

        两种形态都认：`skills.json`（Orbit 导出的形态）与 `skills/` 目录（Hermes 的
        形态，一个子目录一个技能）。只记**名字**，不复制技能正文——发行包不该顺手
        往你的技能库里塞代码，那要走 `skills install` 的隔离区扫描。
        """
        payload = _read_json(directory / SKILLS_FILENAME)
        listed = payload.get("skills")
        if isinstance(listed, list):
            return sorted({str(item) for item in listed if str(item).strip()})
        folder = directory / "skills"
        if not folder.is_dir():
            return []
        names: set[str] = set()
        for entry in sorted(folder.iterdir()):
            if entry.is_dir() and (entry / "SKILL.md").is_file():
                names.add(entry.name)
        return sorted(names)

    # —— 12. update ——

    def update(
        self, name: str, *, force_config: bool = False, yes: bool = False
    ) -> dict[str, Any]:
        """从 `source` 重新拉一遍发行文件，**保留用户数据**。

        规则（模块 docstring 里有完整理由）：

        - 来源目录还在才谈得上更新，否则如实报"没有可重拉的来源"；
        - 指纹与装进来时一致（用户没动过）的发行文件 → 覆盖；
        - 指纹变了（用户改过）的 → 保留，列进 `preserved`；
        - `--force-config` 只额外覆盖 `settings.json`，而且**如果那份 settings.json
          被用户改过，必须再给 `yes`**：那等于扔掉用户手写的配置，不该顺手发生；
        - 发行清单之外的文件一律不动。

        不更新 `created_at`（那是"这套 profile 什么时候建的"），只更新 `updated_at`。
        """
        clean = self.require(name)
        record = self.record(clean)
        if not record.source:
            raise ProfileDistError(
                f"{clean} 没有来源（`create` 出来的 profile 不从任何地方拉）。"
                "要更新它，请直接改目录里的文件"
            )
        directory = Path(record.source)
        if not directory.is_dir():
            raise ProfileDistError(
                f"{clean} 的来源已不可用：{directory}。更新需要那个目录还在"
                "（本机无网络，远端来源本来就不支持）"
            )
        hashes = record.dist.get("hashes")
        known = dict(hashes) if isinstance(hashes, dict) else {}
        settings_path = self.dir_of(clean) / SETTINGS_FILENAME
        baseline = str(known.get(SETTINGS_FILENAME) or "")
        settings_modified = bool(
            baseline and settings_path.is_file() and file_hash(settings_path) != baseline
        )
        if force_config and settings_modified and not yes:
            raise ProfileDistError(
                f"{clean} 的 settings.json 被你改过，--force-config 会覆盖它（不可恢复）。"
                "确认请同时加 --yes"
            )
        settings = _read_json(directory / SETTINGS_FILENAME)
        kept = {key: value for key, value in settings.items() if key in DIST_SETTINGS_KEYS}
        ignored = sorted(key for key in settings if key not in DIST_SETTINGS_KEYS)
        skills = self._read_source_skills(directory)
        target = self.dir_of(clean)
        refreshed: list[str] = []
        preserved: list[str] = []

        def _apply(filename: str, writer) -> None:
            """按指纹决定覆盖还是保留。没有旧指纹的文件视为"用户的东西"，保留。"""
            path = target / filename
            previous = str(known.get(filename) or "")
            if path.is_file() and previous and file_hash(path) != previous:
                preserved.append(filename)
                return
            writer(path)
            refreshed.append(filename)

        def _write_settings(path: Path) -> None:
            _write_json(path, kept)

        def _write_skills(path: Path) -> None:
            _write_json(path, {"skills": skills})

        if force_config:
            _write_settings(target / SETTINGS_FILENAME)
            refreshed.append(SETTINGS_FILENAME)
        else:
            _apply(SETTINGS_FILENAME, _write_settings)
        _apply(SKILLS_FILENAME, _write_skills)
        user_source = directory / USER_FILENAME
        if user_source.is_file():
            _apply(USER_FILENAME, lambda path: shutil.copy2(user_source, path))
        # 记账只更新**覆盖过**的文件。被保留的文件必须继续挂着"发行基线"那个旧指纹：
        # 若把它改成用户当前内容的指纹，下一次 update 就会认为"用户没改过"，
        # 于是把用户的改动静默覆盖掉——"保留用户数据"只保一次等于没保。
        new_hashes = {
            filename: str(known[filename])
            for filename in known
            if (target / filename).is_file() and filename not in refreshed
        }
        for filename in refreshed:
            new_hashes[filename] = file_hash(target / filename)
        record.dist = {
            "settings_keys": sorted(kept),
            "skills": skills,
            "hashes": new_hashes,
            "ignored_settings": ignored,
        }
        # 版本号只跟来源走（来源没写就保留原来的），否则一次 update 会把 v3 抹回 1。
        source_version = _read_json(directory / RECORD_FILENAME).get("version")
        if source_version not in (None, ""):
            record.version = _as_version(source_version)
        record.updated_at = _iso(self.now())
        self._save_record(record)
        notes = []
        if preserved:
            notes.append(
                f"这些文件被你改过，已保留：{', '.join(preserved)}"
                "（要强制以来源为准：`--force-config` 只对 settings.json 生效）"
            )
        if ignored:
            notes.append(f"来源里有被忽略的配置项：{', '.join(ignored)}")
        return {
            "ok": True,
            "name": clean,
            "source": str(directory),
            "refreshed": refreshed,
            "preserved": preserved,
            "version": record.version,
            "settings": kept,
            "skills": skills,
            "notes": notes,
        }

    # —— 13. info ——

    def info(self, name: str) -> dict[str, Any]:
        """发行包清单：文件 + 指纹 + 与记录是否一致。

        ``drift`` 列出"现在磁盘上的指纹 ≠ 记录里的指纹"的文件。这是 `update` 判断
        "用户改过没有"的同一个依据，把它单独暴露出来，用户就能在更新之前先知道
        哪些文件会被保留。
        """
        clean = self.require(name)
        record = self.record(clean)
        known = record.dist.get("hashes")
        known = dict(known) if isinstance(known, dict) else {}
        directory = self.dir_of(clean)
        files: list[dict[str, Any]] = []
        for path in sorted(item for item in directory.rglob("*") if item.is_file()):
            relative = path.relative_to(directory).as_posix()
            current = file_hash(path)
            recorded = str(known.get(relative) or "")
            files.append(
                {
                    "path": relative,
                    "size": path.stat().st_size,
                    "hash": current,
                    "recorded": recorded,
                    "drift": bool(recorded) and current != recorded,
                    "managed": relative in known,
                }
            )
        return {
            **record.as_dict(),
            "format": FORMAT,
            "files": files,
            "drift": [item["path"] for item in files if item["drift"]],
            "managed": sorted(known),
            "settings_keys": list(record.dist.get("settings_keys") or []),
            "ignored_settings": list(record.dist.get("ignored_settings") or []),
            "skills": self.skills_of(clean),
            "user_entries_count": len(self.user_entries_of(clean)),
        }


def _orbit_command() -> str:
    """包装脚本里调用的 orbit 入口（安装后的 console script 名）。

    为什么不写成 ``python -m app.cli``：`app/cli/main.py` 只有 `main()` 函数，不是可
    执行模块；写一个跑不起来的入口，脚本会比"没生成脚本"更难排查。所以直接用
    `orbit`，PATH 里没有时是标准的"command not found"，用户一眼能懂。
    """
    return "orbit"


__all__ = [
    "ACTIVE_FILENAME",
    "ALIAS_DIRNAME",
    "DIST_SETTINGS_KEYS",
    "EXPORT_DIRNAME",
    "FORMAT",
    "OPERATIONS",
    "PROFILES_DIRNAME",
    "ProfileDist",
    "ProfileDistError",
    "ProfileRecord",
    "RECORD_FILENAME",
    "SAFETY_KEYS",
    "SETTINGS_FILENAME",
    "SKILLS_FILENAME",
    "USER_FILENAME",
    "file_hash",
    "looks_like_remote_ref",
    "looks_remote",
]
