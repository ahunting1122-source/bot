import json
from pathlib import Path
from urllib.parse import urlparse

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


def http_url(value: str, label: str = "网址") -> str:
    """Validate configured URLs without echoing credentials or query strings in errors."""
    value = value.strip()
    if not value:
        return value
    try:
        parsed = urlparse(value)
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not any(char.isspace() for char in value)
            and "\\" not in value
        )
        # Accessing port validates its syntax and range.
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(f"{label}须为有效的 HTTP(S) 地址，且不能包含用户名或密码")
    return value


# Site-scoped approvals may only cover in-page actions. Anything that writes outward,
# deletes, or spends stays confirmed one by one, no matter what the user remembered.
GRANTABLE_TOOLS = frozenset(
    {"browser_click", "browser_fill", "browser_press", "browser_scroll"}
)

#: 文件访问级别的取值。对话页可切换，持久化在 `data/settings.json`。
#:   它是**整个权限面的总开关**，同时约束文件工具与命令执行：
#:   - `workspace`：**仅工作文件夹**（默认）。文件工具只能落在 `settings.workspace` 之内；
#:     `run_command` 的工作目录也固定在此目录内，且"不可逆命令"（格式化 / 关机 /
#:     递归删根目录等）会被拒绝——这是**默认档的兜底**，不是完整沙箱。
#:   - `machine`：**完全访问权限**。文件工具可读写整机任意路径，`run_command` 不限工作
#:     目录、不限命令，**不做任何拦截**（用户要的就是"完全控制这台电脑"）。
#: 判据有意做成"白名单 + 坏值即降级"：配置损坏永远退回更窄的那一档，不会反向放开。
FILE_SCOPES = ("workspace", "machine")
FILE_SCOPE_LABELS = {"workspace": "仅工作文件夹", "machine": "完全访问权限"}


def normalize_file_scope(value: object) -> str:
    """把任意来源的值收敛到合法取值；不认识的一律退回 `workspace`。"""
    return value if isinstance(value, str) and value in FILE_SCOPES else "workspace"


def load_grants(raw: str) -> dict[str, list[str]]:
    """Stored as JSON: {"host": ["browser_click", ...]}. Broken data is ignored, never fatal."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(host): [str(tool) for tool in tools]
        for host, tools in parsed.items()
        if isinstance(tools, list)
    }


def dump_grants(grants: dict[str, list[str]]) -> str:
    return json.dumps(
        {host: sorted(set(tools)) for host, tools in grants.items() if tools},
        ensure_ascii=False,
    )


def is_notion_host(host: str | None) -> bool:
    if not host:
        return False
    return any(
        host == domain or host.endswith("." + domain)
        for domain in ("notion.so", "notion.com", "notion.site")
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ORBIT_", env_file=ROOT / "backend" / ".env", extra="ignore"
    )
    data_dir: Path = ROOT / "data"
    workspace: Path = ROOT / "data" / "workspace"
    #: 权限级别（**对话页可切换**，持久化在 `data/settings.json`）。
    #: `workspace` = 仅工作文件夹（默认）；`machine` = 完全访问权限。
    #: 同时约束**文件类工具**（list/search/read/write）的可达范围与 **`run_command`
    #: 命令执行**（cwd 与拦截策略）；浏览器与网络类工具本来就不受目录限制，不在此开关之内。
    file_scope: str = "workspace"
    #: 命令审批模式（见 `app/approvals.py`）。
    #:   `off`（默认）—— 只拦硬地板，危险模式放行。这是 Orbit v1 的既有行为：
    #:                   信任模型是"事后审计 + 三层预算刹车"，不是事前打断。
    #:   `smart`       —— 危险模式只有进白名单才放行，否则拒绝并说明怎么加白。
    #:   `manual`      —— 危险模式一律拒绝（Orbit 没有交互式审批通道，语义是
    #:                   "拒绝并让人自己去做"，不是"弹窗问一句"）。
    approval_mode: str = "off"
    #: 「完全访问权限」档是否也守硬地板。**默认 False**：尊重既有的产品决策
    #: （`README` 与 `tests/test_file_scope.py` 都固定了"machine 档不设限"）。
    #: Hermes 在 YOLO 下仍保留硬地板；要对齐它就把这里打开。
    hardline_in_machine: bool = False
    #: 日志级别。日志落 `<data_dir>/logs/`：主日志 5 MB × 5 份轮转，
    #: `errors.log` 只收 WARNING 以上（见 `app/logging_setup.py`）。
    log_level: str = "INFO"
    #: `run_command` 的默认超时（秒）。到点强制终止进程——**这是运行保护，不是权限限制**，
    #: 它在两档都生效，避免一条挂住的命令把整轮任务钉死。
    command_timeout: int = 120
    model: str = ""
    #: 当前 provider id（`app/providers.py` 注册表里的键，如 `deepseek`）。**留空 = 由
    #: `model_base_url` 反查**。之所以需要这个显式字段：反查天生有歧义（同一端点可能
    #: 对应多个 profile，如 `https://api.moonshot.cn/v1` 既是 kimi-coding-cn 也是
    #: moonshot 开放平台），用户明确选过的 provider 不该被"猜"覆盖。由 `orbit model`
    #: 写入 `data/settings.json`，也可用 `ORBIT_MODEL_PROVIDER` 覆盖。
    model_provider: str = ""
    model_base_url: str = "https://api.openai.com/v1"
    model_api_key: SecretStr = SecretStr("")
    github_token: SecretStr = SecretStr("")
    github_repo: str = ""
    #: Home Assistant 实例地址（如 http://homeassistant.local:8123）。
    home_assistant_url: str = ""
    #: HA 的长期访问令牌。
    home_assistant_token: SecretStr = SecretStr("")
    #: Discord bot token（Discord 开发者门户 → Bot）。
    discord_bot_token: SecretStr = SecretStr("")
    notion_url: str = ""
    browser_url: str = ""
    granted_actions: str = ""
    max_actions: int = 40
    # 一个任务最多走几轮 think→execute（MATRIX 2.2）。
    #
    # **必须在这里声明**：`turn_budgets.resolve()` 用 `getattr(settings, "max_turns")`
    # 读它，而 pydantic 的 `Settings` 会把**未声明的 kwarg 静默丢弃**——
    # 之前没有这个字段，所以 `Settings(max_turns=2)` 不报错也不生效，
    # `resolve()` 永远回落到默认 90。**这正是 2.2 修了两遍仍然不生效的根因。**
    # `None` = 没配，交给 `turn_budgets` 的默认档（父 90 / 子 50）。
    max_turns: int | None = None
    # 推理档位（MATRIX 4.4）：`off` / `low` / `medium` / `high`，空串 = 不传。
    #
    # **必须声明**：`orbit chat --reasoning high` 之前写的是
    # `updates["reasoning_level"]` → `settings.model_copy(update=...)`。
    # pydantic v2 的 `model_copy` **不校验也不报错**，未声明的键会作为裸属性塞进
    # `__dict__`——所以它"能用"但**不是模型字段**：`Settings.model_fields` 里查不到，
    # 序列化时会丢，静态阅读时看不见。**与 `max_turns` 同一类病。**
    reasoning_level: str = ""
    # 「不注入项目上下文」（`orbit chat --safe-mode` / `--ignore-rules`）。
    #
    # **必须声明**：`chat.py` 用 `model_copy(update={"safe_mode": True})` 写它们，
    # 而 `app/context.py` 用 `getattr(settings, "safe_mode", False)` 读它们。
    # 两边都对，**但写的是裸属性**——pydantic 不校验也不报错，于是它"能用"：
    # `model_copy` 把键塞进 `__dict__`，`getattr` 就真读到了 True。
    # **这与 `reasoning_level` 是同一族**：能跑，但不是字段（序列化会丢、
    # `model_fields` 里查不到、静态阅读看不见）。**靠巧合工作的代码迟早会坏。**
    safe_mode: bool = False
    ignore_rules: bool = False
    # 单轮回复的时间上限（秒）。到点后**任务转后台继续**、先给用户一份中间结论，
    # 而不是直接把任务掐断（`agent.py` 的预算兜底仍会硬停，这里只是默认档位）。
    max_seconds: int = 360
    # 任务转入后台后，每隔多少秒发一条进度报告（`background_report` 事件）。
    report_interval: int = 60
    # 技能治理阈值（默认值定死，ORBIT_ 环境变量 / .env 可覆盖以保留调参能力）
    skill_stale_failures: int = 2  # 连续失败满此数标 stale 降权
    skill_archive_days: int = 90  # 距上次使用超过此天数归档
    skill_archive_min_success_rate: float = 0.5  # 成功率低于此值（且有失败）归档
    # 向量路（阶段 6）。**默认开启（2026-10-08 收口）**：检索第二档=向量路的意义就是
    # "用词不同语义相同"能召回，正是 A/B 评测存在的理由；模型已本地就位、524 块已回填、
    # 扩展加载失败/模型缺失/维度不符都会降级为空 + 一次 warning 不阻塞任务，所以默认开的
    # 风险已被兜住。**要退回纯 FTS 行为**（如未下载模型、或想省内存）只需 `ORBIT_VECTOR_ENABLED=0`。
    vector_enabled: bool = True
    # 默认模型：本机（16 逻辑核 / 16GB 内存 / 无 GPU）实测选型见
    # `handoff/vector-path-plan.md` 与 `scripts/eval_embedding_models.py`。
    # 候选实测（195 块真实语料 + A/B 双集 recall@5）：
    #   bge-small-zh-v1.5   512 维 0.09GB · A 0.438 / B 0.583 · 31ms/段
    #   jina-v2-base-zh     768 维 0.64GB · A 0.562 / B 0.500 · 164ms/段
    #   embeddinggemma-300m 768 维 1.24GB · A 0.562 / B 0.917（带 task 前缀）· 173ms/段
    #   Qwen3-Embedding-0.6B(-Q) 质量更高，但是 0.6B 解码器，纯 CPU 代价过大，
    #                       且本环境下载守卫会中断 1-2GB 拉取，故未采纳（配置项仍可切）
    # 代价要清楚：gemma 比 bge-small 慢 5.6 倍（0.86s → 4.84s / 28 段），
    # 已超过方案 D4 定的 "digest 延迟 > 1.5s 就上后台队列" 触发线，队列化是后续项。
    # 想换回快而弱的配置：embedding_model=BAAI/bge-small-zh-v1.5、embedding_dim=512、
    # 两个前缀留空即可（维度变化由启动时的 ensure_schema 自动重建，需重跑回填）。
    embedding_model: str = "google/embeddinggemma-300m"
    embedding_dim: int = 768
    # 模型缓存目录。**默认留空 → 落到 `<data_dir>/fastembed_cache`**。
    # 不要依赖 fastembed 的默认值：它用 `%TEMP%/fastembed_cache`，模型有 0.1–2.4GB，
    # 放在系统临时目录既有被清理的风险，又会吃系统盘空间（本机 C 盘只剩 15GB）。
    embedding_cache_dir: str = ""
    # 查询/文档两侧的指令前缀（**默认对 embeddinggemma 成立**）。
    # - embeddinggemma 两侧都要（这是它的文档要求）：实测 B 集向量单路 recall@5
    #   从不加前缀的 0.833 升到 0.917（+1 条查询，12 条样本里属噪声量级，但方向明确）
    # - bge v1.5：两侧都留空（v1.0 才需要前缀，实测加上反而掉分：
    #   188 块真实语料 A 集 0.312→0.438、B 集 0.417→0.583）
    # - Qwen3-Embedding：只在查询侧加 `Instruct: {任务}\nQuery:{q}`，文档侧不加
    # 两个字段都保留，换模型时按该模型文档填即可。
    vector_query_prefix: str = "task: search result | query: "
    vector_doc_prefix: str = "title: none | text: "
    # onnxruntime 的 CPU 内存 arena。本机实测：1.24GB 的 embeddinggemma-300m
    # 开着 arena 时进程工作集冲到 5.3GB，把 16GB 的机器压到只剩 0.4GB 可用而卡死；
    # 关掉 arena 能显著压低峰值。**内存宽裕时可打开换回一点吞吐。**
    embedding_cpu_mem_arena: bool = False
    # onnxruntime 的 intra-op 线程数；0 = 交给 onnxruntime 自定（默认 0）。
    embedding_threads: int = 0
    # ── 融合的第三档：cross-encoder 重排（`HARNESS_DESIGN.md §4.3` 的阶段 3）──────
    # 动机（见 `handoff/2026-10-08-hermes-deep-dive.md §6` 与 `handoff/vector-path-plan.md`）：
    # 实测证明"给向量路加权"是 wash，真正在丢分的是**融合本身**——两路 FTS 的 OR 词组
    # 查询几乎总能凑满候选，rank-1 噪声拿 1/61 就能与向量的正确命中同量级。Hermes 的
    # hybrid 把 rrf / linear(加权) / cross-encoder 并列为三档，能治"融合失真"的是**重排**。
    # 默认关：与接入前逐字一致，测试基线不受 1GB 模型下载影响。
    # **2026-10-08 收口：默认开启**。证据 = B 集 0.750 → **0.917（过门）**，且重排的作用是
    # "让融合不再伤害强路径"——这正是当前唯一的实测痛点。代价 +0.4s/查询（20 对 × 20ms）
    # 与 1.42GB 常驻，可接受；模型缺失时 `build_reranker` 退回 `NullReranker` 空转，不炸。
    # 要退回融合后直接取 top-K 只需 `ORBIT_RERANK_ENABLED=0`。
    rerank_enabled: bool = True
    # fastembed 0.8.1 的中文可选只有 bge-reranker-base(1.04GB) 与 jina-reranker-v2-base-multilingual(1.11GB)。
    rerank_model: str = "BAAI/bge-reranker-base"
    # 候选池大小：**重排只能重排池内的**，`recall@pool` 是重排效果的硬天花板。
    rerank_pool: int = 20
    # 查询类型路由：专名/标识符/路径/引号 → FTS 优先且门控更严；极短词 → 只走字符级；
    # 其余自然语言问句 → 三路均衡。默认开：它对纯中文散文查询是恒等变换（如实记录在评测里）。
    query_routing: bool = True
    # 候选质量门控：**只在"词面路由"（lexical）下生效**，按"查询词窗在命中块里的覆盖率"
    # 过滤 FTS 噪声候选。FTS 的 OR 词组只要命中**任意一个**词窗就返回整行，于是"只沾上一个
    # 3 字窗"的块也会挤进候选并拿到 rank，进 RRF 后变成噪声。
    # 为什么不做全局门控：自然语言查询的正确块常常**只覆盖查询的一部分**（例如查询问"机械
    # 键盘哪个轴体适合打字"，正确块只写"段落轴适合打字"），全局阈值会误杀。而标识符/路径类
    # 查询理应被逐字覆盖，门控在那里才安全。自然语言路由的阈值恒为 0（不门控）。
    fts_min_coverage: float = 0.5
    # Background tool use may opt into headless; explicit login always opens a visible window.
    headless: bool = False
    allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:6185",
        "http://localhost:6185",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    )

    def prepare(self) -> None:
        for directory in (self.data_dir, self.workspace, self.data_dir / "artifacts"):
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def machine_wide(self) -> bool:
        """True = 完全访问权限：文件工具不再把路径夹在授权目录内。

        静态配置里的坏值（含大小写错误、空串）都会被判成 False，即安全的那一档。
        """
        return self.file_scope == "machine"

    @property
    def file_scope_label(self) -> str:
        return FILE_SCOPE_LABELS[normalize_file_scope(self.file_scope)]
