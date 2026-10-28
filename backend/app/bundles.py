"""技能别名集合（bundles）：一个 ``/<bundle>`` 斜杠命令装载多个技能。

对标 Hermes `hermes bundles {create,list,show,delete,reload}`（帮助原文：
「Skill bundles let you load several skills under one **slash command**」）。
规格出处：`handoff/hermes-parity/research/04-skill-layer.md` §6.4 / §7.2。

## 两个「bundle」不要混淆

Hermes 里同名不同物：

| 概念 | 位置 | 含义 |
|---|---|---|
| `SkillBundle` dataclass | `skills_hub.py:147` | **下载得到的技能文件包**（传输单元） |
| CLI `hermes bundles` | `hermes_cli` | **技能别名集合**（一个斜杠命令装多个技能） |

本模块实现的是**后者**。前者在 `app/skill_hub.py` 里以「hub 目录里的一个技能包」
的形式存在，没有独立的 dataclass——因为本地目录形式的 hub 不需要「把一堆文件塞进
一个 dict 里传输」这件事。

## 为什么落一个 JSON 文件而不是一个目录

Hermes 的 bundle 是磁盘上的目录集合，所以 `reload` 是「重扫 bundles 目录」。
本实现落在 ``<data_dir>/skill-bundles.json``（一个文件），理由：

1. bundle 的全部内容是「名字 → 技能名列表」的映射，没有文件需要热加载；
2. 一个文件可以被 `git diff` 看懂，目录树不行；
3. **`reload` 的语义因此更清楚**：它重读的是 bundle 里引用的**技能文件**
   （用户手工改完 SKILL.md 之后要刷新索引），而不是 bundle 定义本身。

## 引用不存在的技能必须报错并列出

Hermes 的 `create` 允许先把 bundle 建起来（技能可以后装）。本实现**不允许**：
一个引用了 5 个技能、其中 2 个不存在的 bundle，在 `/bundle` 触发时会静默只装载 3 个
——用户看到的是「这个 bundle 好像没起作用」，而真正的原因（名字打错了）永远不会
被说出来。所以 `create` 一次把缺失的全部列出来，让人一次改完。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.skill_index import SkillIndex, SkillIndexError

#: bundle 定义文件（`<data_dir>/skill-bundles.json`）。
BUNDLES_FILENAME = "skill-bundles.json"
#: bundle 名会变成斜杠命令，所以按命令名约束（与技能名同规则）。
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
#: 名字长度上限（斜杠命令要能被人打出来，也避免一串不可读的长名）。
MAX_BUNDLE_NAME_LENGTH = 64
#: 单个 bundle 的技能数上限。超过它说明这不是「一个快捷入口」，是一份分类清单。
MAX_BUNDLE_SKILLS = 20


class BundlesError(RuntimeError):
    """bundle 的可预期失败（名字非法、已存在、引用不存在的技能…）。"""


def _iso(moment: datetime) -> str:
    return moment.isoformat()


@dataclass(frozen=True)
class Bundle:
    """一个技能别名集合。

    ``instruction`` 是 Hermes ``create --instruction`` 的落地（「额外指导，前置
    拼接到装载的技能内容之前」）。它是任务书 4 个字段之外的**第 5 个字段**：
    CLI 有这个选项，把它丢掉就等于「抄了选项名没抄语义」——所以宁可多一个字段，
    也不静默忽略用户的输入。
    """

    name: str
    description: str = ""
    skills: tuple[str, ...] = ()
    created_at: str = ""
    instruction: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "skills": list(self.skills),
            "created_at": self.created_at,
            "instruction": self.instruction,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Bundle:
        """从磁盘读回。坏字段退默认——一个字段坏了不该让整份 bundle 读不出来。"""
        raw_skills = payload.get("skills") or []
        skills: list[str] = []
        if isinstance(raw_skills, list):
            skills = [str(item).strip() for item in raw_skills if str(item).strip()]
        return cls(
            name=str(payload.get("name") or ""),
            description=str(payload.get("description") or ""),
            skills=tuple(skills),
            created_at=str(payload.get("created_at") or ""),
            instruction=str(payload.get("instruction") or ""),
        )


@dataclass
class ReloadReport:
    """一次 reload 的结果：重新读到了哪些技能、哪些不见了。"""

    reloaded: list[dict[str, Any]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"reloaded": list(self.reloaded), "missing": list(self.missing)}


class Bundles:
    """bundle 仓库。一个实例 = 一份 ``data_dir`` 的 bundle 视图。"""

    def __init__(
        self,
        data_dir: Path | str,
        *,
        index: SkillIndex | None = None,
        now: Any = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.index = index if index is not None else SkillIndex(self.data_dir)
        self._now = now

    def now(self) -> datetime:
        return self._now() if self._now is not None else datetime.now(UTC)

    @property
    def path(self) -> Path:
        return self.data_dir / BUNDLES_FILENAME

    # —— 持久化 ——

    def _read(self) -> dict[str, Bundle]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        raw = payload.get("bundles") if isinstance(payload, dict) else None
        if not isinstance(raw, dict):
            return {}
        bundles: dict[str, Bundle] = {}
        for key, value in raw.items():
            if isinstance(value, dict):
                bundle = Bundle.from_dict({**value, "name": value.get("name") or key})
                bundles[str(key)] = bundle
        return bundles

    def _write(self, bundles: dict[str, Bundle]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "bundles": {name: bundle.as_dict() for name, bundle in sorted(bundles.items())},
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    # —— 校验 ——

    def _validate_name(self, name: str) -> str:
        clean = (name or "").strip().lstrip("/")
        if not clean:
            raise BundlesError("bundle 名不能为空（它会变成 /斜杠命令）")
        if len(clean) > MAX_BUNDLE_NAME_LENGTH:
            raise BundlesError(f"bundle 名 {len(clean)} 字符，超过 {MAX_BUNDLE_NAME_LENGTH} 上限")
        if not _NAME_RE.fullmatch(clean):
            raise BundlesError(
                f"bundle 名 {clean!r} 不合规：只允许小写字母、数字、连字符、下划线"
                "（它会变成 /斜杠命令，不能带空格和斜杠）"
            )
        return clean

    def known_skills(self) -> set[str]:
        """当前索引里真实存在的技能名（bundle 校验的唯一依据）。"""
        return {record.name for record in self.index.index().records}

    def _validate_skills(self, skills: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        """去重并校验引用；有缺失就**一次性全列出来**再抛。"""
        cleaned: list[str] = []
        for item in skills:
            name = str(item or "").strip()
            if name and name not in cleaned:
                cleaned.append(name)
        if not cleaned:
            raise BundlesError("bundle 至少要引用一个技能（`--skill <名字>`，可重复）")
        if len(cleaned) > MAX_BUNDLE_SKILLS:
            raise BundlesError(
                f"一个 bundle 引用 {len(cleaned)} 个技能，超过 {MAX_BUNDLE_SKILLS} 上限"
                "（这不是快捷入口，是一份分类清单）"
            )
        known = self.known_skills()
        missing = [name for name in cleaned if name not in known]
        if missing:
            raise BundlesError(
                f"引用了不存在的技能：{'、'.join(missing)}"
                f"（本机现有 {len(known)} 个技能，`orbit skills list` 可查）"
            )
        return tuple(cleaned)

    # —— CRUD ——

    def create(
        self,
        name: str,
        skills: list[str] | tuple[str, ...],
        *,
        description: str = "",
        instruction: str = "",
        force: bool = False,
    ) -> Bundle:
        """新建（或 ``force`` 覆盖）一个 bundle。"""
        clean = self._validate_name(name)
        validated = self._validate_skills(skills)
        bundles = self._read()
        if clean in bundles and not force:
            raise BundlesError(f"bundle 已存在：{clean}（要覆盖请加 --force）")
        existing = bundles.get(clean)
        created_at = existing.created_at if existing and existing.created_at else _iso(self.now())
        bundle = Bundle(
            name=clean,
            description=description.strip(),
            skills=validated,
            created_at=created_at,
            instruction=instruction.strip(),
        )
        bundles[clean] = bundle
        self._write(bundles)
        return bundle

    def list(self) -> list[Bundle]:
        """全部 bundle（按名字排序）。"""
        stored = self._read()
        return [stored[name] for name in sorted(stored)]

    def show(self, name: str) -> Bundle:
        """取一个 bundle（不存在则抛 ``BundlesError``）。"""
        clean = (name or "").strip().lstrip("/")
        bundles = self._read()
        if clean not in bundles:
            known = "、".join(sorted(bundles)) or "（还没有任何 bundle）"
            raise BundlesError(f"没有这个 bundle：{clean or '（空）'}。现有：{known}")
        return bundles[clean]

    def delete(self, name: str) -> bool:
        """删除一个 bundle。返回是否真的删掉了一条。"""
        clean = (name or "").strip().lstrip("/")
        bundles = self._read()
        if clean not in bundles:
            return False
        bundles.pop(clean)
        self._write(bundles)
        return True

    # —— reload ——

    def reload(self, bundle: str | Bundle | None = None) -> ReloadReport:
        """把 bundle 里的技能**重新读一遍**（用于手工改完文件后刷新索引）。

        先 `index.invalidate()` 再逐个 `load()`：索引层有 30 秒 TTL 缓存，不失效就
        可能读到改之前的快照——「我明明改了却没生效」是这类缓存最典型的抱怨。
        引用已消失的技能不抛异常，而是进 ``missing``：reload 是**观察**动作，
        它必须能报告「有个技能没了」，而不是在第一个缺失处停下。
        """
        self.index.invalidate()
        if bundle is None:
            targets = self.list()
        elif isinstance(bundle, Bundle):
            targets = [bundle]
        else:
            targets = [self.show(str(bundle))]
        report = ReloadReport()
        for item in targets:
            for skill_name in item.skills:
                try:
                    loaded = self.index.load(skill_name)
                except SkillIndexError:
                    report.missing.append(f"{item.name}:{skill_name}")
                    continue
                report.reloaded.append(
                    {
                        "bundle": item.name,
                        "skill": skill_name,
                        "tier": loaded["tier"],
                        "path": loaded["path"],
                        "ok": loaded["ok"],
                        "error": loaded["error"],
                        "body_lines": len(str(loaded["body"]).splitlines()),
                    }
                )
        return report

    def load(self, name: str) -> dict[str, Any]:
        """装载一个 bundle：返回 ``instruction`` + 每个技能的全文（斜杠命令用）。

        缺技能时**报错并列出**，不静默少装（理由见模块 docstring）。
        """
        bundle = self.show(name)
        self.index.invalidate()
        loaded: list[dict[str, Any]] = []
        missing: list[str] = []
        for skill_name in bundle.skills:
            try:
                item = self.index.load(skill_name)
            except SkillIndexError:
                missing.append(skill_name)
                continue
            loaded.append(
                {"name": item["name"], "tier": item["tier"], "body": item["body"],
                 "description": item["description"], "path": item["path"]}
            )
        if missing:
            raise BundlesError(
                f"bundle {bundle.name} 引用的技能不存在：{'、'.join(missing)}"
                "（先补上或 `orbit bundles create --force` 改定义）"
            )
        return {
            "name": bundle.name,
            "description": bundle.description,
            "instruction": bundle.instruction,
            "skills": loaded,
        }


__all__ = [
    "BUNDLES_FILENAME",
    "MAX_BUNDLE_NAME_LENGTH",
    "MAX_BUNDLE_SKILLS",
    "Bundle",
    "Bundles",
    "BundlesError",
    "ReloadReport",
]
