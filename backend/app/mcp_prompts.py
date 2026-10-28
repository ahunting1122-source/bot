"""MCP prompts → Orbit 技能（对标 Hermes 把 prompts 当技能用）。

## 这一层解决什么问题

MCP server 除了 tools，还可以暴露 **prompts**：带参数的提示模板（`list_prompts` 列出来，
`get_prompt(name, arguments)` 取渲染后的正文）。Hermes 把它们当**技能**用——因为对模型来说
"一段可复用的做法说明"和"一个技能"是同一个东西，分开维护只会让两边漂移。

于是本模块的职责只有一件：把 `prompts` 映射成 Orbit 的技能形状（`name` / `description` /
`body`），并提供一个**能在同步路径里安全读取**的入口。

## 命名

``mcp__<server>__<prompt>``，与 `app/mcp.py` 的 `tool_name()` 同构（同样过 `sanitize`，
同样截到 64 字符）。同构不是美观问题：模型看到的前缀必须只有一种解释，否则
"这个 `mcp__` 名字是工具还是技能"要靠猜，而 `skill_load` 与工具分发是两条不同的路径。

## 同步 / 异步边界：**缓存 + 显式刷新**，绝不在同步路径里跑事件循环

技能目录的读取是**同步**的（`app/skills.py` 每次装配都读磁盘，`skill_load` 工具直接读
文件），而 MCP 客户端是**异步**的。两边的桥只有一座：本模块的缓存。

为什么不能在同步路径里"顺手"把异步调用跑掉（`asyncio.run()` / `loop.run_until_complete()`）：

1. **调用线程上通常已经有循环在跑**。后端（FastAPI）的主线程、CLI 的 `Context` 专用循环
   线程（见 `app/cli/context.py` 的长注释）都是这样。此时 `asyncio.run()` 直接抛
   ``RuntimeError: asyncio.run() cannot be called from a running event loop``；
   `run_until_complete()` 抛 ``Cannot run the event loop while another loop is running``。
2. **就算硬起一个新循环也是错的**。MCP 的 stdio/HTTP 传输把读写任务绑在**创建它的那个
   循环**上；在另一个循环里 await 同一个 client，表现是随机挂死而不是报错——排查成本极高。
3. **同步路径在装配热路径上**。每轮装配都去网络拉一遍 prompt 正文 = 首 token 延迟跟着
   服务商抖动走，而 prompt 内容本身几乎不变。

所以策略是：**异步侧 `refresh()` 写缓存，同步侧 `skills()` 只读缓存**。刷新时机是显式的
（连接成功后、`orbit mcp prompts --refresh`、服务重连），而不是读的时候。缓存的失效也
显式（`invalidate(server)`）——服务掉线时它的 prompt 必须跟着消失，否则模型会看到一个
永远渲染不出正文的技能。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from app.mcp import dumped, sanitize

log = logging.getLogger("orbit.mcp.prompts")

#: prompt 正文上限。与工具结果同量级（16000）：prompt 是要拼进上下文的，不能无界。
MAX_PROMPT_BODY = 16000
#: 取单个 prompt 正文的超时。失败只让该 prompt 缺正文，不拖垮整批。
PROMPT_TIMEOUT = 5.0
#: `list_prompts` 翻页上限（防 forever-cursor）。
MAX_PAGES = 20


#: 技能名上限。与 `app/skill_index.py` 的 `MAX_NAME_LENGTH` 对齐：技能名是要进系统提示的
#: 索引键，超长会让"提示里的名字、目录里的名字、`skill_load` 用的名字"三者对不上。
MAX_SKILL_NAME = 64


def prompt_skill_name(server: str, prompt: str) -> str:
    """模型面向的技能名：``mcp__<server>__<prompt>``（ASCII、稳定、不与本地技能撞名）。

    截到 :data:`MAX_SKILL_NAME`，与 `app/mcp.py` 的 `tool_name` 同口径。截断可能撞名，
    但那要求两个名字的前 64 字符完全相同；真撞了由调用方按"后者跳过"处理，比留一个
    超长名字导致 `skill_load` 永远找不到要可查得多。
    """
    return f"mcp__{sanitize(server)}__{sanitize(prompt)}"[:MAX_SKILL_NAME]


def _shape(value) -> dict:
    """读一个 SDK 对象**或普通 dict**。

    `app.mcp.dumped` 只认带 ``model_dump`` 的 pydantic 模型（dict 会得到空字典），而
    本模块的输入既可能来自真实 MCP 客户端，也可能来自测试/离线快照里的 dict。统一在
    这一处抹平，免得每个调用点都写一遍 ``isinstance(x, dict)``。
    """
    if isinstance(value, dict):
        return value
    return dumped(value)


def parse_prompt_skill_name(name: str) -> tuple[str, str] | None:
    """``mcp__a__b`` → ``("a", "b")``；不是 MCP 技能名则返回 None。

    只切**前两个**双下划线之后的部分：服务名与 prompt 名各自被 `sanitize` 过，
    但 prompt 名里可能原本就有下划线，从右边切会切错。
    """
    text = (name or "").strip()
    if not text.startswith("mcp__"):
        return None
    rest = text[len("mcp__") :]
    if "__" not in rest:
        return None
    server, _, prompt = rest.partition("__")
    if not server or not prompt:
        return None
    return server, prompt


def prompt_text(result) -> str:
    """把 ``get_prompt`` 的结果摊平成一段正文。

    一条 PromptMessage 的 content 通常是 ``TextContent``（``{"type":"text","text":…}``），
    但也可能直接是字符串，或图片/音频块。逐条抽 text 后换行拼接——图片块没有文字可抽，
    就不贡献正文（不塞一个 ``[image]`` 占位：技能正文里出现这种东西只会误导模型）。

    规则与 `app/mcp._render_prompt` 一致（那边给"连接时预取正文"用）。这里独立实现而不是
    复用私有函数，是为了本模块可以脱离 `MCPManager` 单测。
    """
    blocks: list[str] = []
    for item in _shape(result).get("messages") or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, dict):
            text = content.get("text")
            if text is not None:
                blocks.append(str(text))
        elif isinstance(content, str):
            blocks.append(content)
        elif content is not None:
            blocks.append(str(content))
    return "\n".join(blocks).strip()[:MAX_PROMPT_BODY]


def prompt_arguments(prompt) -> tuple[dict[str, Any], ...]:
    """prompt 的参数声明 → ``({"name","description","required"}, …)``。

    留着它是因为"带参数的 prompt"和"不带参数的 prompt"在技能目录里必须能区分：
    前者模型需要先问清参数才能用，后者直接渲染即可。
    """
    rows: list[dict[str, Any]] = []
    for item in _shape(prompt).get("arguments") or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("name") or "")
        if not label:
            continue
        rows.append(
            {
                "name": label,
                "description": str(item.get("description") or ""),
                "required": bool(item.get("required", False)),
            }
        )
    return tuple(rows)


def prompt_description(prompt, label: str) -> str:
    """技能的 ``description``：优先服务给的 description，其次 title，最后退回 prompt 名。

    **不编**：三处都没有就写 prompt 名本身，而不是拿正文首行去凑——正文是模板，
    首行常常是 ``{{topic}}`` 这种占位符，当描述反而误导路由。
    """
    shape = _shape(prompt)
    for key in ("description", "title"):
        value = str(shape.get(key) or "").strip()
        if value:
            return value[:500]
    return label


@dataclass(frozen=True)
class PromptSkill:
    """一个 MCP prompt 的技能化形态。

    ``body`` 可能为空：``get_prompt`` 失败（超时、服务报错、参数缺失）时它仍是"仅描述"
    的占位条目，由消费方决定跳过。留占位而不是丢弃，是为了让"这个服务声明了 prompt
    但取不到正文"这件事在目录里可见——静默丢弃等于把故障藏起来。
    """

    name: str
    description: str
    body: str
    server: str
    prompt: str
    arguments: tuple[dict[str, Any], ...] = ()

    def as_skill(self) -> dict[str, Any]:
        """Orbit 技能形状（三个键，与 `app/skills.py` 的读侧一致）。"""
        return {"name": self.name, "description": self.description, "body": self.body}

    def as_row(self) -> dict[str, Any]:
        """CLI 表格用的宽形状（多给 server / 参数，便于人读）。"""
        return {
            "name": self.name,
            "description": self.description,
            "server": self.server,
            "prompt": self.prompt,
            "arguments": [dict(item) for item in self.arguments],
            "chars": len(self.body),
        }


def skills_from_listing(server: str, prompts) -> list[PromptSkill]:
    """``list_prompts`` 的结果 → 技能列表（**不取正文**，正文由 :func:`render_prompt` 补）。

    纯函数：只读传入的对象，不发请求、不碰缓存。测试与"只想知道有哪些 prompt"的
    CLI 路径都靠它。
    """
    skills: list[PromptSkill] = []
    for prompt in prompts or []:
        label = str(_shape(prompt).get("name") or "").strip()
        if not label:
            continue
        skills.append(
            PromptSkill(
                name=prompt_skill_name(server, label),
                description=prompt_description(prompt, label),
                body="",
                server=server,
                prompt=label,
                arguments=prompt_arguments(prompt),
            )
        )
    return skills


async def render_prompt(
    client,
    server: str,
    name: str,
    arguments: dict[str, str] | None = None,
    *,
    limit: float = PROMPT_TIMEOUT,
) -> str:
    """调 ``get_prompt`` 并把结果拼成正文。

    ``name`` 既接受裸 prompt 名，也接受 ``mcp__<server>__<prompt>`` 形式的技能名——
    模型手里拿到的通常是后者（它在技能清单里看到的就是这个名字）。

    失败**抛**而不是返回空串：调用方（`skill_load` / CLI）需要区分"正文就是空的"和
    "取正文失败了"，后者要给出原因。超时用 ``asyncio.timeout`` 而不是 ``wait_for``，
    与 `app/mcp.py` 保持一致。
    """
    label = name
    parsed = parse_prompt_skill_name(name)
    if parsed is not None:
        label = parsed[1]
    async with asyncio.timeout(limit):
        result = await client.get_prompt(label, arguments or None)
    return prompt_text(result)


class PromptRegistry:
    """prompts 的缓存与桥接点。一个实例 = 一份"当前连着的服务提供了哪些 prompt"的视图。

    缓存按服务分桶（``_skills[server]``），因为服务的生命周期是独立的：一个服务掉线
    只该让它自己的 prompt 消失（`invalidate(server)`），不该把别人的一起清掉。
    """

    def __init__(self, *, timeout: float = PROMPT_TIMEOUT) -> None:
        self.timeout = timeout
        self._skills: dict[str, list[PromptSkill]] = {}
        self._clients: dict[str, Any] = {}
        #: 上次成功刷新的时刻（``asyncio.get_event_loop().time()`` 口径，仅供 `is_stale`）。
        self._refreshed: dict[str, float] = {}

    # —— 异步侧：刷新 / 渲染 ——

    async def refresh(self, server: str, client) -> list[PromptSkill]:
        """从一个服务拉全部 prompt 并**预取正文**，结果写进缓存。

        预取正文（每个 prompt 一次 `get_prompt`）是刻意的：技能目录是同步读的，
        正文必须在异步侧就备好；留到读的时候再拉，就又回到"同步路径跑异步"的死路上。

        单条取正文失败只让该条缺正文（记日志），不影响同批其它 prompt，也不让
        `refresh` 抛——一个坏 prompt 不该让整个服务的技能消失。
        """
        skills = skills_from_listing(server, await self._list_prompts(client))
        filled: list[PromptSkill] = []
        for skill in skills:
            body = ""
            try:
                body = await render_prompt(
                    client, server, skill.prompt, None, limit=self.timeout
                )
            except Exception as exc:  # noqa: BLE001 - 取正文失败只记日志
                log.debug("MCP %s 取 prompt %s 正文失败：%s", server, skill.prompt, exc)
            filled.append(
                PromptSkill(
                    name=skill.name,
                    description=skill.description,
                    body=body,
                    server=skill.server,
                    prompt=skill.prompt,
                    arguments=skill.arguments,
                )
            )
        self._skills[server] = filled
        self._clients[server] = client
        self._refreshed[server] = asyncio.get_event_loop().time()
        return filled

    async def _list_prompts(self, client) -> list[Any]:
        """翻页拉全部 prompt（带上限）。能力未声明或调用失败 → 空列表，不抛。"""
        prompts: list[Any] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            try:
                result = await client.list_prompts(cursor=cursor)
            except Exception as exc:  # noqa: BLE001 - 不支持 / 报错都按"没有 prompt"
                log.debug("列出 prompts 失败：%s", exc)
                return prompts
            rows = getattr(result, "prompts", None)
            if rows is None:
                rows = _shape(result).get("prompts")
            prompts.extend(list(rows or []))
            next_cursor = _shape(result).get("next_cursor")
            if not next_cursor:
                break
            cursor = str(next_cursor)
        return prompts

    async def render_prompt(
        self,
        server: str,
        name: str,
        arguments: dict[str, str] | None = None,
    ) -> str:
        """用**已缓存的客户端**渲染一个 prompt。

        为什么用缓存里的客户端而不是参数传：调用方（`skill_load`）手里只有技能名，
        没有客户端；而 registry 本来就在 `refresh()` 时见过它。没有客户端就说明服务
        没连上（或已失效），这时报错比返回空正文诚实。

        prompt 名优先从**缓存记录**里取：技能名可能被截到 64 字符（见
        :func:`prompt_skill_name`），从截断后的名字反解出来的 prompt 名是错的。
        """
        client = self._clients.get(server)
        if client is None:
            raise LookupError(f"MCP 服务 {server} 未连接，无法渲染 prompt {name}")
        record = self.get(name)
        label = record.prompt if record is not None else name
        return await render_prompt(client, server, label, arguments, limit=self.timeout)

    # —— 同步侧：只读缓存 ——

    def records(self) -> list[PromptSkill]:
        """全部已缓存 prompt（顺序 = 服务刷新顺序）。**不发请求、不碰事件循环**。"""
        rows: list[PromptSkill] = []
        for skills in self._skills.values():
            rows.extend(skills)
        return rows

    def skills(self) -> list[dict[str, Any]]:
        """技能形状的列表（`name` / `description` / `body`）。同步路径的唯一入口。"""
        return [record.as_skill() for record in self.records()]

    def for_server(self, server: str) -> list[PromptSkill]:
        return list(self._skills.get(server, []))

    def get(self, name: str) -> PromptSkill | None:
        for record in self.records():
            if record.name == name:
                return record
        return None

    def body(self, name: str) -> str | None:
        """技能名 → 正文；不命中返回 None（"没有这个技能"与"正文为空"要能区分）。"""
        record = self.get(name)
        return None if record is None else record.body

    def servers(self) -> list[str]:
        return list(self._skills)

    def is_stale(self, server: str, ttl: float) -> bool:
        """该服务的缓存是否已超过 ``ttl`` 秒（``ttl <= 0`` 恒为 False = 只靠显式刷新）。"""
        if ttl <= 0:
            return False
        seen = self._refreshed.get(server)
        if seen is None:
            return True
        return (asyncio.get_event_loop().time() - seen) > ttl

    # —— 失效 ——

    def invalidate(self, server: str | None = None) -> None:
        """丢掉缓存。``server`` 为空则全清。

        服务掉线时必须调用：它的 prompt 正文已经渲染不出来了，继续留在技能目录里
        会让模型挑一个永远失败的工具。
        """
        if server is None:
            self._skills.clear()
            self._clients.clear()
            self._refreshed.clear()
            return
        self._skills.pop(server, None)
        self._clients.pop(server, None)
        self._refreshed.pop(server, None)


@dataclass
class RegistryStats:
    """一次刷新的汇总（CLI `mcp prompts` 的 `--json` 载荷）。"""

    servers: list[str] = field(default_factory=list)
    skills: int = 0
    with_body: int = 0
    without_body: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "servers": list(self.servers),
            "skills": self.skills,
            "with_body": self.with_body,
            "without_body": self.without_body,
        }


def summarize(registry: PromptRegistry) -> RegistryStats:
    """把缓存汇总成一句能报出来的数：有几个服务、几条技能、几条缺正文。"""
    records = registry.records()
    return RegistryStats(
        servers=registry.servers(),
        skills=len(records),
        with_body=sum(1 for record in records if record.body),
        without_body=sum(1 for record in records if not record.body),
    )
