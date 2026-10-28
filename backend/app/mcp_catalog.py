"""Hermes ``optional-mcps`` 目录的 Orbit 版（矩阵 8.10）。

## 这个目录是什么

Hermes 在 ``_internal/optional-mcps/<name>/manifest.yaml`` 里放了 **65 份**服务模板
（Nous 审核过的"一键安装"清单：官方托管的远程 MCP，加一份 git 安装的本地桥）。
``hermes mcp catalog`` 列出来、``hermes mcp picker`` 让人挑、``hermes mcp install
<name>`` 把选中的那条写进配置。本模块把这批模板抄成一份**纯数据**，并给出
``list_catalog`` / ``show`` / ``render`` / ``install`` 四个入口。

## 抄写纪律：只抄，不猜

每条的 ``transport`` / ``url`` / ``command`` / ``args`` / ``auth`` / ``docs`` 都逐字来自
对应 manifest。抄不到的**留空并标 ``unknown=True``**，绝不填一个"看起来合理"的值——
一个编出来的 ``npx -y @xxx/yyy`` 会让用户装上一个不存在的包，而报错发生在几分钟之后、
离真正的错误源（这份表）很远。

本轮只有 **1 条**是 ``unknown=True``：``n8n``。它的 ``command`` 是
``${INSTALL_DIR}/.venv/bin/python``，那个占位符由 Hermes 的 ``install: {type: git}``
步骤（clone 仓库 + 建 venv + pip install）在安装时替换。Orbit **不 clone 仓库**（本机无
网络，也不该在"装 MCP"这个动作里偷偷拉代码），所以没有可用的启动命令可抄。

``label`` 是唯一一个**派生**字段：manifest 里没有 label，本模块按 ``name`` 机械生成
（``twilio-docs`` → ``Twilio Docs``）。派生规则写在 :func:`label_of`，不是逐条手写。

## ``install`` 只写配置，不装包

本机无网络，``npx`` / ``uvx`` / ``pip`` 全都跑不了；即使有网络，"装 MCP"这件事在 Orbit
里也只是**往 ``data/mcp.json`` 里写一条配置**——依赖由第一次连接时的运行环境提供。
所以 :func:`install` 的返回值里固定带 ``needs_network=True`` 与一句如实的
``message``：配置已写入，首次连接需要网络与依赖。假装"安装完成"是最坏的一种：用户会以为
工具已经能用，而真正缺的东西（包、登录、网络）一个字都没说。

写盘复用 ``app/mcp.py`` 的 :func:`app.mcp.write_servers`（``mcp add`` 用的同一套），
不另写一份——两份写盘逻辑迟早会在字段白名单上分叉。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 目录里允许出现的 transport。``http`` = 远程 Streamable HTTP（Orbit 走 url），
#: ``stdio`` = 本地子进程（Orbit 走 command + args）。
TRANSPORTS = ("http", "stdio")
#: 目录里允许出现的鉴权形态（抄自 manifest 的 ``auth.type``）。
AUTH_TYPES = ("none", "oauth", "api_key")
#: ``mcp install`` 接受的 ``official/<name>`` 前缀（Hermes 的写法）。
OFFICIAL_PREFIX = "official/"


class CatalogError(RuntimeError):
    """目录相关的可预期失败（查不到条目、条目抄不全、服务已存在）。

    用独立异常而不是 ``ValueError``：CLI 要把它翻成"打印这句话 + 退出码"，
    而 ``ValueError`` 在别处有别的含义（参数解析）。
    """


@dataclass(frozen=True)
class CatalogEntry:
    """一条目录模板。字段与 manifest 一一对应，不多不少。"""

    name: str
    label: str
    description: str
    transport: str
    #: 远程端点（``transport == "http"`` 时有值）。
    url: str = ""
    #: 本地启动命令（``transport == "stdio"`` 时有值）。
    command: str = ""
    args: tuple[str, ...] = ()
    #: 需要用户提供的环境变量**名字**（值永远不抄——manifest 里也没有值）。
    env: tuple[str, ...] = ()
    #: ``none`` / ``oauth`` / ``api_key``。
    auth: str = "none"
    #: 厂商文档地址（manifest 的 ``source``）。
    docs: str = ""
    #: True = 这条的启动/连接方式**抄不到可用值**（见模块 docstring）。
    unknown: bool = False
    #: ``unknown=True`` 时的原因，原样给用户看。
    note: str = ""

    @property
    def target(self) -> str:
        """连接目标的一行摘要（表格里用）。"""
        if self.url:
            return self.url
        parts = [self.command, *self.args]
        return " ".join(part for part in parts if part)

    def as_dict(self) -> dict:
        """公开视图：JSON 安全（tuple → list），且不含任何"值"层面的秘密（本来也没有）。"""
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "transport": self.transport,
            "url": self.url,
            "command": self.command,
            "args": list(self.args),
            "env": list(self.env),
            "auth": self.auth,
            "docs": self.docs,
            "target": self.target,
            "unknown": self.unknown,
            "note": self.note,
        }


@dataclass(frozen=True)
class McpServerConfig:
    """一条可以写进 ``data/mcp.json`` 的服务配置。

    刻意与 ``CatalogEntry`` 分开：模板是"厂商声明的东西"，配置是"这台机器上的这条记录"
    （多了 enabled / auto_approve / timeout，少了 docs / label）。混成一个类会让
    ``render(overrides=...)`` 的边界变得说不清——哪些字段是抄来的、哪些是用户改的。
    """

    name: str
    transport: str = "http"
    url: str = ""
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    auth: str = ""
    enabled: bool = True
    auto_approve: bool = False
    index_resources: bool = False
    timeout: float | None = None

    def as_record(self) -> dict:
        """转成 ``save_servers`` / ``write_servers`` 接受的记录。

        只放**有值**的字段：``save_servers`` 对 ``args`` / ``env`` / ``headers`` 是"有就
        规范化、没有就不写"，塞一堆空列表进去只会在 ``mcp.json`` 里制造噪声。
        """
        record: dict[str, Any] = {
            "name": self.name,
            "enabled": self.enabled,
            "auto_approve": self.auto_approve,
            "index_resources": self.index_resources,
        }
        if self.url:
            record["url"] = self.url
        if self.command:
            record["command"] = self.command
            if self.args:
                record["args"] = list(self.args)
        if self.env:
            record["env"] = dict(self.env)
        if self.headers:
            record["headers"] = dict(self.headers)
        if self.auth:
            record["auth"] = self.auth
        if self.timeout is not None:
            record["timeout"] = self.timeout
        return record


def label_of(name: str) -> str:
    """由 ``name`` 机械派生显示名（manifest 里没有 label 字段）。

    规则只有一条：按 ``-`` / ``_`` 切词、每词首字母大写。**不维护特例表**——一旦开始
    手工美化（``hugging_face`` → ``Hugging Face``、``aws-knowledge`` → ``AWS Knowledge``），
    这张表就变成第二份需要维护的"事实"，而它并不承载任何信息。
    """
    words = name.replace("_", "-").split("-")
    return " ".join(word.capitalize() for word in words if word)


def _entry(name: str) -> CatalogEntry | None:
    """按名查模板，容忍 ``official/<name>`` 前缀（Hermes 的写法）。"""
    key = str(name or "").strip()
    if key.lower().startswith(OFFICIAL_PREFIX):
        key = key[len(OFFICIAL_PREFIX) :]
    for entry in CATALOG:
        if entry.name == key:
            return entry
    return None


def catalog_names() -> tuple[str, ...]:
    """全部模板名（顺序即展示顺序，按 ``name`` 排好）。"""
    return tuple(entry.name for entry in CATALOG)


def list_catalog() -> list[dict]:
    """全部模板的公开视图。"""
    return [entry.as_dict() for entry in CATALOG]


def show(name: str) -> CatalogEntry | None:
    """按名取一条模板；查不到返回 ``None``（"没有"与"出错"要分开）。"""
    return _entry(name)


def _override(overrides: dict | None, key: str, default: Any) -> Any:
    """读一个覆盖值：``None`` 视为"没给"，不能把用户的默认值顶掉。"""
    if not isinstance(overrides, dict):
        return default
    value = overrides.get(key)
    return default if value is None else value


def render(name: str, *, overrides: dict | None = None) -> McpServerConfig:
    """把一条模板渲染成可写进 ``mcp.json`` 的配置。

    ``overrides`` 支持：``name``（改本地服务名）、``enabled`` / ``auto_approve`` /
    ``index_resources`` / ``timeout``、``url``（远程端点覆盖）、``args``、``env``（dict，
    按变量名给值）、``headers``、``auth``。

    ``unknown=True`` 的模板**直接抛** :class:`CatalogError`：渲染出一条跑不起来的配置比
    报错更糟——用户会以为装好了。
    """
    entry = _entry(name)
    if entry is None:
        raise CatalogError(
            f"目录里没有 {name!r}。用 `orbit mcp catalog` 看可用的 {len(CATALOG)} 条。"
        )
    if entry.unknown:
        raise CatalogError(
            f"目录条目 {entry.name} 抄不到可用的连接方式：{entry.note or '（未说明）'}"
        )
    local_name = str(_override(overrides, "name", entry.name)).strip() or entry.name
    env: dict[str, str] = {}
    given_env = _override(overrides, "env", None)
    for key in entry.env:
        value = ""
        if isinstance(given_env, dict) and key in given_env:
            value = str(given_env[key])
        # 没给值就留空串：写进配置的是"这里需要你填"，而不是一个猜出来的默认值。
        env[key] = value
    raw_args = _override(overrides, "args", entry.args)
    args = tuple(str(item) for item in raw_args or ())
    headers = _override(overrides, "headers", None) or {}
    timeout = _override(overrides, "timeout", None)
    return McpServerConfig(
        name=local_name,
        transport=entry.transport,
        url=str(_override(overrides, "url", entry.url)),
        command=entry.command,
        args=args,
        env=env,
        headers={str(key): str(value) for key, value in dict(headers).items()},
        auth=str(_override(overrides, "auth", entry.auth if entry.auth != "none" else "")),
        enabled=bool(_override(overrides, "enabled", True)),
        auto_approve=bool(_override(overrides, "auto_approve", False)),
        index_resources=bool(_override(overrides, "index_resources", False)),
        timeout=None if timeout is None else float(timeout),
    )


def install(settings, name: str, *, overrides: dict | None = None) -> dict:
    """把一条模板写进 ``data/mcp.json``（**只写配置，不装包**）。

    返回载荷里的 ``needs_network`` 恒为 ``True``：目录里每一条要么是远程端点（首次连接
    要出网 + 多半要 OAuth 登录），要么是本地子进程（首次连接要那个可执行文件已经装好）。
    这句话必须跟着结果一起返回，而不是只写在文档里——CLI 会把它原样打给用户。
    """
    # 延迟 import：`orbit mcp catalog` 只是列个清单，不该为了它拖起 app.mcp 的整条
    # 依赖链（mcp SDK / httpx2 / app.tools）。
    from app.mcp import MCPManager, write_servers

    entry = _entry(name)
    config = render(name, overrides=overrides)
    manager = MCPManager(settings)
    servers = manager.servers()
    if any(str(item.get("name")) == config.name for item in servers):
        raise CatalogError(
            f"{config.name!r} 已在 data/mcp.json 里。先 `orbit mcp remove {config.name}` 再装。"
        )
    record = config.as_record()
    write_servers(manager, [*servers, record])
    return {
        "ok": True,
        "name": config.name,
        "catalog": entry.name if entry else config.name,
        "transport": config.transport,
        "target": config.url or " ".join([config.command, *config.args]).strip(),
        "auth": config.auth,
        # 如实说明：写进配置 ≠ 装好了。
        "needs_network": True,
        "message": (
            f"已把 {config.name} 的配置写入 data/mcp.json。"
            "首次连接时需要网络与依赖（远程服务还要完成 OAuth 登录），"
            "本命令不会下载任何包、也不会发网络请求。"
        ),
        "record": record,
    }


def summary() -> dict:
    """目录的整体统计（`mcp catalog --json` 的头部用）。"""
    return {
        "count": len(CATALOG),
        "http": sum(1 for entry in CATALOG if entry.transport == "http"),
        "stdio": sum(1 for entry in CATALOG if entry.transport == "stdio"),
        "oauth": sum(1 for entry in CATALOG if entry.auth == "oauth"),
        "unknown": sum(1 for entry in CATALOG if entry.unknown),
    }


__all__ = [
    "AUTH_TYPES",
    "CATALOG",
    "CatalogEntry",
    "CatalogError",
    "McpServerConfig",
    "OFFICIAL_PREFIX",
    "TRANSPORTS",
    "catalog_names",
    "install",
    "label_of",
    "list_catalog",
    "render",
    "show",
    "summary",
]

# ══════════════════════════════════════════════════════════════════════════════
# 目录数据：逐条抄自 Hermes 0.21.0 的 optional-mcps/<name>/manifest.yaml
#
# 抄写规则见模块 docstring。**改这里之前先读 manifest**——这份表的价值全在"和上游
# 一致"，凭印象改一个 url 就会把用户导到一个不存在的端点。
# ══════════════════════════════════════════════════════════════════════════════

CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        name="airtable",
        label="Airtable",
        description="Bases, tables, and records from your Airtable workspace.",
        transport="http",
        url="https://mcp.airtable.com/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://support.airtable.com/articles/9897799762-using-the-airtable-mcp-server",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="algolia",
        label="Algolia",
        description="Algolia search: indices, analytics, and settings (read-only).",
        transport="http",
        url="https://mcp.algolia.com/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://www.algolia.com/doc/guides/model-context-protocol/productivity-mcp",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="alltrails",
        label="Alltrails",
        description="AllTrails: find hikes and trails with reviews and ratings.",
        transport="http",
        url="https://www.alltrails.com/mcp",
        command="",
        args=(),
        env=(),
        auth="none",
        docs="https://www.alltrails.com/mcp",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="amplitude",
        label="Amplitude",
        description="Amplitude analytics: charts, dashboards, experiments, flags.",
        transport="http",
        url="https://mcp.amplitude.com/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://amplitude.com/docs/amplitude-ai/amplitude-mcp",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="asana",
        label="Asana",
        description="Tasks, projects, and goals from your Asana workspace.",
        transport="http",
        url="https://mcp.asana.com/sse",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://developers.asana.com/docs/using-asanas-mcp-server",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="atlassian",
        label="Atlassian",
        description="Jira issues and Confluence pages via Atlassian's hosted remote MCP.",
        transport="http",
        url="https://mcp.atlassian.com/v1/mcp/authv2",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://support.atlassian.com/rovo/docs/getting-started-with-the-atlassian-remote-mcp-server/",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="attio",
        label="Attio",
        description="CRM records, lists, and notes in Attio.",
        transport="http",
        url="https://mcp.attio.com/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://attio.com/help/apps/other-apps/using-the-attio-mcp-server",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="aws-knowledge",
        label="Aws Knowledge",
        description="Authoritative AWS docs, API references, and best practices.",
        transport="http",
        url="https://knowledge-mcp.global.api.aws",
        command="",
        args=(),
        env=(),
        auth="none",
        docs="https://awslabs.github.io/mcp/servers/aws-knowledge-mcp-server/",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="betterstack",
        label="Betterstack",
        description="Better Stack: logs, uptime monitors, incidents, and status pages.",
        transport="http",
        url="https://mcp.betterstack.com",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://betterstack.com/docs/getting-started/integrations/mcp/",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="buildkite",
        label="Buildkite",
        description="CI/CD pipelines, builds, and test results from Buildkite.",
        transport="http",
        url="https://mcp.buildkite.com/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://buildkite.com/docs/apis/mcp-server",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="calendly",
        label="Calendly",
        description="Scheduling links, events, and invitees from Calendly.",
        transport="http",
        url="https://mcp.calendly.com",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://developer.calendly.com/calendly-mcp-server",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="canva",
        label="Canva",
        description="Create, search, and manage Canva designs.",
        transport="http",
        url="https://mcp.canva.com/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://www.canva.dev/docs/mcp/",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="circleci",
        label="Circleci",
        description="CircleCI: diagnose build failures, read logs, rerun workflows.",
        transport="http",
        url="https://mcp.circleci.com/v1/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://circleci.com/docs/guides/toolkit/circleci-mcp-overview/",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="clickup",
        label="Clickup",
        description="Tasks, docs, and workspaces in ClickUp.",
        transport="http",
        url="https://mcp.clickup.com/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://developer.clickup.com/docs/connect-an-ai-assistant-to-clickups-mcp-server",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="close",
        label="Close",
        description="Sales CRM: leads, opportunities, calls, and emails.",
        transport="http",
        url="https://mcp.close.com/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://help.close.com/docs/mcp-server",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="cloudflare",
        label="Cloudflare",
        description="Full Cloudflare API access via the official remote MCP.",
        transport="http",
        url="https://mcp.cloudflare.com/mcp?codemode=false",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://developers.cloudflare.com/agents/model-context-protocol/cloudflare/servers-for-cloudflare/",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="cloudinary",
        label="Cloudinary",
        description="Upload, search, and transform media assets in Cloudinary.",
        transport="http",
        url="https://asset-management.mcp.cloudinary.com/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://cloudinary.com/documentation/cloudinary_llm_mcp",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="comfy-cloud",
        label="Comfy Cloud",
        description="Generate images, video, audio, and 3D on Comfy Cloud.",
        transport="http",
        url="https://cloud.comfy.org/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://docs.comfy.org/agent-tools/cloud",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="context7",
        label="Context7",
        description="Up-to-date, version-specific library docs and code examples.",
        transport="http",
        url="https://mcp.context7.com/mcp",
        command="",
        args=(),
        env=(),
        auth="none",
        docs="https://context7.com/docs/resources/all-clients",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="craft",
        label="Craft",
        description="Craft: structured docs, tasks, and personal knowledge base.",
        transport="http",
        url="https://mcp.craft.do/my/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://support.craft.do/hc/en-us/articles/29455875123101",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="datadog",
        label="Datadog",
        description="Logs, monitors, dashboards, and incidents from Datadog.",
        transport="http",
        url="https://mcp.datadoghq.com/api/unstable/mcp-server/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://docs.datadoghq.com/bits_ai/mcp_server/",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="deepwiki",
        label="Deepwiki",
        description="Ask questions about any public GitHub repo (Devin's DeepWiki).",
        transport="http",
        url="https://mcp.deepwiki.com/mcp",
        command="",
        args=(),
        env=(),
        auth="none",
        docs="https://docs.devin.ai/work-with-devin/deepwiki-mcp",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="dropbox",
        label="Dropbox",
        description="Search, read, and manage files in Dropbox.",
        transport="http",
        url="https://mcp.dropbox.com/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://help.dropbox.com/integrations/connect-dropbox-mcp-server",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="figma",
        label="Figma",
        description="Official Figma remote MCP — design context, Code Connect, and write-to-canvas via https://mcp.figma.com/mcp (OAuth).",
        transport="http",
        url="https://mcp.figma.com/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://developers.figma.com/docs/figma-mcp-server/remote-server-installation/",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="fireflies",
        label="Fireflies",
        description="Meeting transcripts, summaries, and action items.",
        transport="http",
        url="https://api.fireflies.ai/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://docs.fireflies.ai/getting-started/mcp-configuration",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="gamma",
        label="Gamma",
        description="Gamma: generate and edit AI presentations, docs, and sites.",
        transport="http",
        url="https://mcp.gamma.app/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://developers.gamma.app/docs/gamma-mcp-server",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="gitlab",
        label="Gitlab",
        description="GitLab: issues, merge requests, pipelines, and repo context.",
        transport="http",
        url="https://gitlab.com/api/v4/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://docs.gitlab.com/user/model_context_protocol/mcp_server/",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="globalping",
        label="Globalping",
        description="Ping, traceroute, DNS, and HTTP tests from global probes.",
        transport="http",
        url="https://mcp.globalping.dev/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://github.com/jsdelivr/globalping-mcp-server",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="grafana",
        label="Grafana",
        description="Query metrics, logs, dashboards, alerts, and incidents from Grafana Cloud.",
        transport="http",
        url="https://mcp.grafana.com/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://grafana.com/docs/grafana-cloud/ai-tools/mcp-servers/cloud-mcp/",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="hugging_face",
        label="Hugging Face",
        description="Models, datasets, Spaces, and papers from the Hugging Face Hub.",
        transport="http",
        url="https://huggingface.co/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://huggingface.co/docs/hub/agents-mcp",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="indeed",
        label="Indeed",
        description="Search jobs and listings on Indeed.",
        transport="http",
        url="https://mcp.indeed.com/claude/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://docs.indeed.com/indeed-mcp",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="intercom",
        label="Intercom",
        description="Conversations, tickets, and customer data from Intercom.",
        transport="http",
        url="https://mcp.intercom.com/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://developers.intercom.com/docs/guides/mcp",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="kiwi",
        label="Kiwi",
        description="Kiwi.com flight search: itineraries with direct booking links.",
        transport="http",
        url="https://mcp.kiwi.com",
        command="",
        args=(),
        env=(),
        auth="none",
        docs="https://www.kiwi.com/stories/kiwi-mcp-connector/",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="klaviyo",
        label="Klaviyo",
        description="Klaviyo marketing: campaigns, flows, segments, and reporting.",
        transport="http",
        url="https://mcp.klaviyo.com/mcp?core-tools-only=true&disable-tools-with-user-generated-content=true",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://developers.klaviyo.com/en/docs/klaviyo_mcp_server",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="linear",
        label="Linear",
        description="Find, create, and update Linear issues, projects, and comments.",
        transport="http",
        url="https://mcp.linear.app/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://linear.app/docs/mcp",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="microsoft-learn",
        label="Microsoft Learn",
        description="Official Microsoft, Azure, and .NET docs and code samples.",
        transport="http",
        url="https://learn.microsoft.com/api/mcp",
        command="",
        args=(),
        env=(),
        auth="none",
        docs="https://learn.microsoft.com/en-us/training/support/mcp-get-started",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="miro",
        label="Miro",
        description="Read and edit Miro boards, diagrams, and frames.",
        transport="http",
        url="https://mcp.miro.com/",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://developers.miro.com/docs/connecting-to-miro-mcp",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="mixpanel",
        label="Mixpanel",
        description="Mixpanel analytics: events, funnels, retention, dashboards.",
        transport="http",
        url="https://mcp.mixpanel.com/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://docs.mixpanel.com/docs/mcp",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="monday",
        label="Monday",
        description="Boards, items, docs, and workflows in monday.com.",
        transport="http",
        url="https://mcp.monday.com/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://developer.monday.com/apps/docs/mondaycom-mcp-integration",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="motherduck",
        label="Motherduck",
        description="MotherDuck: query DuckDB cloud warehouses with SQL.",
        transport="http",
        url="https://api.motherduck.com/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://motherduck.com/docs/key-tasks/ai-and-motherduck/mcp-setup/",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="n8n",
        label="N8n",
        description="Manage and inspect n8n workflows from Hermes (stdio bridge, no public port).",
        transport="stdio",
        url="",
        command="${INSTALL_DIR}/.venv/bin/python",
        args=('${INSTALL_DIR}/server.py',),
        env=('N8N_BASE_URL', 'N8N_API_KEY'),
        auth="api_key",
        docs="https://github.com/CyberSamuraiX/hermes-n8n-mcp",
        unknown=True,
        note="command/args 里是安装期占位符（${INSTALL_DIR}），需要 Hermes 的 git 安装步骤替换；Orbit 不 clone 仓库，因此抄不到可用的启动命令。",
    ),
    CatalogEntry(
        name="neon",
        label="Neon",
        description="Neon serverless Postgres: projects, branches, and SQL.",
        transport="http",
        url="https://mcp.neon.tech/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://neon.com/docs/ai/neon-mcp-server",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="netlify",
        label="Netlify",
        description="Sites, deploys, and env vars via Netlify's hosted MCP.",
        transport="http",
        url="https://netlify-mcp.netlify.app/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://docs.netlify.com/build/build-with-ai/agent-setup-guides/agent-setup-overview/",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="notion",
        label="Notion",
        description="Pages and databases from your Notion workspace.",
        transport="http",
        url="https://mcp.notion.com/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://developers.notion.com/docs/mcp",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="paypal",
        label="Paypal",
        description="Payments, invoices, and subscriptions via PayPal's hosted MCP.",
        transport="http",
        url="https://mcp.paypal.com/sse",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://developer.paypal.com/tools/mcp-server/",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="plaid",
        label="Plaid",
        description="Plaid dashboard: integrations, Items, and usage debugging.",
        transport="http",
        url="https://api.dashboard.plaid.com/mcp/",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://plaid.com/docs/resources/mcp/",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="postman",
        label="Postman",
        description="Postman workspaces, collections, environments, and APIs.",
        transport="http",
        url="https://mcp.postman.com/minimal",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://learning.postman.com/docs/reference/postman-api/postman-mcp-server/postman-mcp-remote-server/",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="prisma-postgres",
        label="Prisma Postgres",
        description="Create and manage Prisma Postgres databases.",
        transport="http",
        url="https://mcp.prisma.io/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://www.prisma.io/docs/postgres/integrations/mcp-server",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="railway",
        label="Railway",
        description="Railway: projects, services, deployments, and environments.",
        transport="http",
        url="https://mcp.railway.com",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://docs.railway.com/guides/mcp-server",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="robinhood",
        label="Robinhood",
        description="Robinhood agentic trading: portfolio, balances, and orders.",
        transport="http",
        url="https://agent.robinhood.com/mcp/trading",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://robinhood.com/us/en/support/articles/agentic-trading-overview/",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="semgrep",
        label="Semgrep",
        description="Scan code for security vulnerabilities with Semgrep.",
        transport="http",
        url="https://mcp.semgrep.ai/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://semgrep.dev/docs/mcp",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="sentry",
        label="Sentry",
        description="Issues, stack traces, and error context from Sentry.",
        transport="http",
        url="https://mcp.sentry.dev/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://docs.sentry.io/product/sentry-mcp/",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="square",
        label="Square",
        description="Catalog, orders, and payments via Square's hosted MCP.",
        transport="http",
        url="https://mcp.squareup.com/sse",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://developer.squareup.com/docs/mcp",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="strava",
        label="Strava",
        description="Strava: activities, fitness trends, training load (read-only).",
        transport="http",
        url="https://mcp.strava.com/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://support.strava.com/en-us/articles/15401531-strava-mcp-connector",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="stripe",
        label="Stripe",
        description="Payments, customers, and invoices via Stripe's hosted MCP.",
        transport="http",
        url="https://mcp.stripe.com",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://docs.stripe.com/mcp",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="supabase",
        label="Supabase",
        description="Database, auth, and storage from your Supabase projects.",
        transport="http",
        url="https://mcp.supabase.com/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://supabase.com/docs/guides/ai-tools/mcp",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="todoist",
        label="Todoist",
        description="Manage Todoist tasks and projects.",
        transport="http",
        url="https://ai.todoist.net/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://www.todoist.com/help/articles/todoist-mcp-server",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="trivago",
        label="Trivago",
        description="trivago hotel search: compare prices by city and dates.",
        transport="http",
        url="https://mcp.trivago.com/mcp",
        command="",
        args=(),
        env=(),
        auth="none",
        docs="https://mcp.trivago.com/mcp",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="twelve-data",
        label="Twelve Data",
        description="Stocks, forex, and crypto market data from Twelve Data.",
        transport="http",
        url="https://mcp.twelvedata.com/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://twelvedata.com/docs",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="twilio-docs",
        label="Twilio Docs",
        description="Twilio developer docs search (public beta, read-only).",
        transport="http",
        url="https://mcp.twilio.com/docs",
        command="",
        args=(),
        env=(),
        auth="none",
        docs="https://www.twilio.com/docs/ai/mcp",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="unreal-engine",
        label="Unreal Engine",
        description="Drive the Unreal Engine 5.8 editor over its local MCP server.",
        transport="http",
        url="http://127.0.0.1:8000/mcp",
        command="",
        args=(),
        env=(),
        auth="none",
        docs="https://dev.epicgames.com/documentation/unreal-engine/unreal-mcp-in-unreal-editor",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="vercel",
        label="Vercel",
        description="Deployments, logs, and projects via Vercel's hosted MCP.",
        transport="http",
        url="https://mcp.vercel.com",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://vercel.com/docs/mcp",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="webflow",
        label="Webflow",
        description="Sites, CMS collections, and pages via Webflow's hosted MCP.",
        transport="http",
        url="https://mcp.webflow.com/mcp",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://developers.webflow.com/mcp/reference/getting-started",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="wolfram",
        label="Wolfram",
        description="Wolfram|Alpha computation, math, and curated knowledge.",
        transport="http",
        url="https://agenttools.wolfram.com/mcp",
        command="",
        args=(),
        env=(),
        auth="none",
        docs="https://www.wolfram.com/agent-tools/",
        unknown=False,
        note="",
    ),
    CatalogEntry(
        name="wordpress-com",
        label="Wordpress Com",
        description="WordPress.com: posts, pages, drafts, stats, and comments.",
        transport="http",
        url="https://public-api.wordpress.com/wpcom/v2/mcp/v1",
        command="",
        args=(),
        env=(),
        auth="oauth",
        docs="https://developer.wordpress.com/docs/mcp/",
        unknown=False,
        note="",
    ),
)
