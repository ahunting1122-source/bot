"""Skill 层：流程知识的加载与落盘。

一期范围：
- 读：从 `data/skills/<name>/SKILL.md` 解析 frontmatter + 正文，供 `skill_load` 工具
  与 prepare 注入简述使用。
- 镜像同步：`sync_to_store` 把文件同步进 `skills` 表（幂等 upsert，文件是真值）。
- 落盘：`save_skill` 把模型提议的新流程写成 SKILL.md 文件（frontmatter 标 status）。
- 治理（四道闸：查重/矛盾转备选/择优/淘汰）留待二期，本模块不做。

frontmatter 字段：
- name: str       技能唯一名（= 目录名）
- scope: str      global / project / site
- version: int    正文版本
- triggers: list  触发条件（自然语言，供注入简述与相关性判断）
- status: str     unreviewed（新沉淀待确认）/ active（在用）；二期扩展 stale/archived

文件是正文真值，人可直接编辑、改完即生效——所以加载器每次都读磁盘，不缓存。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

#: 简述注入系统提示的技能上限；同簇只注入一个（排序在注入方做）。
CATALOG_LIMIT = 20

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_yamlish(raw: str) -> dict[str, Any]:
    """解析 SKILL.md 头部那段简化 YAML（key: value / key:\n  - item），只取已知键。

    不引入 yaml 依赖：技能文件是内部约定格式，字段少、层级浅，手写解析足够；
    解析失败不抛异常——坏文件跳过，绝不拖垮任务。
    """
    meta: dict[str, Any] = {}
    current_list: str | None = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        if line.startswith((" ", "\t")):
            if current_list and line.strip().startswith("-"):
                item = line.strip()[1:].strip()
                if item:
                    meta.setdefault(current_list, []).append(item)
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value:
            meta[key] = value
            current_list = None
        else:
            current_list = key
    return meta


def _as_int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return 0


def parse_skill(text: str, name: str) -> dict[str, Any]:
    """把 SKILL.md 全文拆成（元数据, 正文）。文件缺失或格式坏时返回空技能。"""
    match = _FRONTMATTER.match(text)
    meta: dict[str, Any] = {}
    body = text
    if match:
        meta = _parse_yamlish(match.group(1))
        body = text[match.end() :]
    return {
        "name": str(meta.get("name") or name),
        "scope": str(meta.get("scope") or "global"),
        "version": _as_int(meta.get("version")),
        "triggers": [str(t) for t in meta.get("triggers") or []],
        # 一句话简述。索引注入时按 60 字符预算截断（见 `graph/nodes.render_skills`）：
        # 索引是**路由信号**不是正文，把整段描述塞进提示词会把预算吃光。
        "description": str(meta.get("description") or ""),
        # frontmatter 里显式声明的 status（active/unreviewed 等）；缺省为空串，
        # 由 sync_to_store 决定是否据此覆盖表的治理态（见其 docstring）。
        "status": str(meta.get("status") or ""),
        "body": body.strip(),
    }


def load_skill(root: Path, name: str) -> dict[str, Any]:
    """读单个技能。name 允许带 .md 后缀；不存在则返回 name 原样、body 为空。"""
    clean = name.removesuffix(".md")
    path = root / clean / "SKILL.md"
    if not path.is_file():
        return {"name": clean, "scope": "global", "version": 0, "triggers": [], "body": ""}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {"name": clean, "scope": "global", "version": 0, "triggers": [], "body": ""}
    return parse_skill(text, clean)


_SAFE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")


def _validate_name(name: str) -> str:
    """技能名只能是不含路径分隔符的短标识，防止模型写出目录穿越。"""
    clean = (name or "").strip().removesuffix(".md")
    if not _SAFE_NAME.match(clean):
        raise ValueError(f"非法技能名：{name!r}（只允许字母数字下划线连字符，长度≤80）")
    return clean


def save_skill(
    root: Path,
    name: str,
    scope: str,
    triggers: list[str],
    body: str,
    *,
    status: str = "unreviewed",
    version: int = 1,
) -> dict[str, Any]:
    """把技能写成一个 SKILL.md 文件（文件是真值）。

    只做最基础的安全校验（名字/scope）与 frontmatter 结构正确；查重/矛盾转备选由
    调用方（skill_save 工具）在落盘前用 find_conflict 决策，本函数不掺治理逻辑。
    `version` 供更新路径传入（同 name 覆盖时 +1）。
    返回落盘后的技能元数据（与 load_skill 同构）。
    """
    clean = _validate_name(name)
    scope = (scope or "global").strip()
    if scope not in ("global", "project", "site"):
        scope = "global"
    body = (body or "").strip()
    if not body:
        raise ValueError("技能正文不能为空")
    if not root.is_dir():
        root.mkdir(parents=True, exist_ok=True)
    dir_path = root / clean
    dir_path.mkdir(parents=True, exist_ok=True)
    trig_lines = "\n".join(f"  - {t}" for t in triggers) if triggers else "  - "
    content = (
        "---\n"
        f"name: {clean}\n"
        f"scope: {scope}\n"
        f"version: {version}\n"
        f"status: {status}\n"
        "triggers:\n"
        f"{trig_lines}\n"
        "---\n\n"
        f"{body}\n"
    )
    (dir_path / "SKILL.md").write_text(content, encoding="utf-8")
    return {
        "name": clean,
        "scope": scope,
        "version": version,
        "triggers": [str(t) for t in triggers],
        "body": body,
    }


def catalog(root: Path, limit: int = CATALOG_LIMIT) -> list[dict[str, Any]]:
    """列出全部技能（目录 + 简述）。坏文件跳过；顺序稳定（按目录名）。"""
    skills: list[dict[str, Any]] = []
    if not root.is_dir():
        return skills
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        skill = load_skill(root, entry.name)
        if not skill["body"]:
            continue
        skills.append(
            {
                "name": skill["name"],
                "scope": skill["scope"],
                "version": skill["version"],
                "triggers": skill["triggers"],
                # 带上 description：注入索引时要用它（60 字符预算），
                # 而 `catalog` 是注入侧唯一的数据来源。
                "description": skill.get("description", ""),
            }
        )
        if len(skills) >= limit:
            break
    return skills


def skill_summary(skill: dict[str, Any]) -> str:
    """一句话简述，供注入系统提示用。"""
    name = skill.get("name") or ""
    triggers = (skill.get("triggers") or [])[:2]
    if triggers:
        return f"{name}：{'；'.join(triggers)}"
    return name


def _trigger_overlap(a: str, b: str) -> bool:
    """两个触发词是否重叠：≥2 字连续公共子串（与 nodes._trigger_matches 同口径）。"""
    a = (a or "").strip()
    b = (b or "").strip()
    if len(a) < 2 or len(b) < 2:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    for i in range(len(shorter) - 1):
        if shorter[i : i + 2] in longer:
            return True
    return False


def find_conflict(
    existing: list[dict[str, Any]], new_scope: str, new_triggers: list[str]
) -> list[dict[str, Any]]:
    """写入查重：找出与新技能重叠的现有技能。

    重叠判定（务实简化，因技能无独立 host 字段）：
    - 同名：由调用方单独处理（更新而非新建）
    - 同 scope 且任一触发词有 ≥2 字公共子串：判为「可能撞车」→ 矛盾转备选

    返回重叠技能列表（不含自身 name 匹配，那个走更新路径）。
    """
    new_scope = (new_scope or "global").strip()
    new_triggers = [str(t).strip() for t in (new_triggers or []) if str(t).strip()]
    conflicts: list[dict[str, Any]] = []
    for skill in existing:
        if (skill.get("scope") or "global") != new_scope:
            continue
        old_triggers = skill.get("triggers") or []
        if any(
            _trigger_overlap(nt, ot)
            for nt in new_triggers
            for ot in old_triggers
        ):
            conflicts.append(skill)
    return conflicts


def render_alternative_mark(skill_name: str) -> str:
    """生成「备选」触发词标记，注入到被标注技能的前端 triggers 里。"""
    return f"备选：{skill_name}"


def _refuse_capture(body: str) -> str:
    """写入侧内容守门：判断模型想沉淀的技能正文是否该被拒绝。

    Hermes 的 ``_DO_NOT_CAPTURE_BLOCK`` 核心是「不要捕获」——Orbit 此前完全没有这道闸，
    模型会把「这次试了三次都失败」沉淀成一条像样的技能，污染后续所有相似任务。

    纯启发式（关键词 + 结构），不调模型——与 M2 记忆写入门槛同一纪律：harness 拦截，
    不靠提示词。返回拒绝原因（非空 = 拒绝），空串 = 放行。
    """
    text = (body or "").strip()
    if not text:
        return "技能正文为空"

    lowered = text.lower()

    # ① 环境相关故障：缺二进制、未装包、命令找不到、凭据未配置
    #    → 用户可自行修复，不是持久规则。
    for marker in (
        "command not found",
        "no such file",
        "module not found",
        "not found:",
        "未安装",
        "没有安装",
        "缺少依赖",
        "依赖缺失",
        "找不到命令",
        "命令不存在",
        "请先安装",
        "需要安装",
        "凭据未配置",
        "未配置密钥",
        "未设置 api",
        "api key 未",
        "token 未",
    ):
        if marker in lowered:
            return "看起来是环境/依赖/凭据问题，用户可自行修复，不是可复用的流程知识"

    # ② 对工具/功能的负面断言：会固化成拒绝，问题修好后仍被引用数月
    for marker in (
        "工具不能用",
        "工具坏了",
        "不能用",
        "无法使用",
        "不可用",
        "失效了",
        "not working",
        "is broken",
        "doesn't work",
        "does not work",
    ):
        if marker in lowered:
            return "包含对工具/功能的负面断言，这类内容会在问题修好后仍被引用，不该沉淀"

    # ③ 会话内已解决的瞬时错误：该记的是重试模式，不是原始失败
    for marker in (
        "重试后成功",
        "重试成功",
        "再次尝试成功",
        "第二次就成功",
        "重启后正常",
        "retry succeeded",
        "after retry",
    ):
        if marker in lowered:
            return "描述的是会话内已解决的瞬时错误，应沉淀重试模式而非原始失败"

    # ④ 一次性任务叙述：不构成一类可复用工作
    for marker in ("今天的", "这次的", "刚才的", "刚刚的", "这个任务"):
        if marker in lowered:
            return "像是一次性任务的叙述，不构成可复用的工作流"

    # ⑤ 未解决的失败：不得把未验证的失败序列写成可靠工作流
    for marker in ("尚未解决", "还没解决", "没能解决", "未解决", "暂时没辙", "搞不定"):
        if marker in lowered:
            return "通篇是没有结论的失败描述，不能沉淀成可靠工作流"

    return ""



async def sync_to_store(store, root: Path) -> int:
    """把磁盘上的技能文件同步进 `skills` 表（幂等 upsert），返回同步条数。

    文件是真值、表是索引：本函数只做「文件 → 索引」的单向镜像，不做治理决策。

    status 覆盖规则（防治理态被误复活）：
    - 新行 → 采纳文件声明的 status（缺省 active）
    - 表里 unreviewed + 文件声明 active → 升级为 active（人工确认）
    - 表里 stale/archived（治理态）→ 仅当文件 version 高于表 version（文件被用户改过）
      才采纳文件 status，否则保留治理态
    - 文件删了 → 表 status = archived（保留统计，不删行）

    关键：stale/archived 一旦由 `record_outcome` 标上，不会被「文件里恰好写着
    status: active」这行静默复活——因为手写技能的 frontmatter 常态就是 active，
    若无条件采纳，治理刚降权就会被下一次 sync 复活。复活必须来自用户真实改文件
    （version 递增）。
    """
    from sqlalchemy import select

    from app.storage import Skill

    if root is None or not root.is_dir():
        return 0
    on_disk = {s["name"] for s in catalog(root, limit=10_000)}
    existing = {
        row.name: row
        for row in await store.rows(select(Skill))
    }
    synced = 0
    for name in sorted(on_disk):
        skill = load_skill(root, name)
        row = existing.get(name)
        is_new = row is None
        if row is None:
            row = Skill(name=name)
        old_version = int(row.version or 0)  # 赋值前先取旧值，供治理态复活判断
        row.scope = skill["scope"]
        row.triggers = skill["triggers"]
        row.version = skill["version"]
        row.body = skill["body"]
        row.path = str(root / name / "SKILL.md")
        declared = skill.get("status") or ""
        file_version = skill.get("version") or 0
        if is_new:
            # 新行：采纳文件声明（缺省 active）
            row.status = declared if declared in ("active", "unreviewed") else "active"
        elif row.status == "unreviewed" and declared == "active":
            # 人工确认：unreviewed → active
            row.status = "active"
        elif row.status in ("stale", "archived"):
            # 治理态：只有文件 version 递增（用户真实改过）才采纳声明，否则保留治理态
            if declared in ("active", "unreviewed") and file_version > old_version:
                row.status = declared
        else:
            # active 常态：文件声明 unreviewed 才改（几乎不会发生），否则保持 active
            if declared == "unreviewed":
                row.status = "unreviewed"
        await store.save(row)
        synced += 1
    # 文件已删但表里还有 → 标 archived（保留统计，不删）
    for name, row in existing.items():
        if name not in on_disk and row.status != "archived":
            row.status = "archived"
            await store.save(row)
    return synced
