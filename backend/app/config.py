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
    model: str = ""
    model_base_url: str = "https://api.openai.com/v1"
    model_api_key: SecretStr = SecretStr("")
    github_token: SecretStr = SecretStr("")
    github_repo: str = ""
    notion_url: str = ""
    browser_url: str = ""
    granted_actions: str = ""
    max_actions: int = 20
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
