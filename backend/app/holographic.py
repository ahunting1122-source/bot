"""全息图记忆（HRR：Holographic Reduced Representations），MATRIX 9.11。

对标 Hermes 的 `plugins/memory/holographic/`（`holographic.py` 11 KB 的 HRR 代数 +
`store.py` 的 `hrr_vector BLOB` + `memory_banks` 打包向量）。Orbit 这里只做**代数与
打分**这一层：不落库、不建 provider（`memory_providers.py` 的 8 个 provider 名单不动，
理由见那个模块的 docstring——名单按本机可用生态选，不照抄）。

## 为什么要有这一层

`app/memory_graph.py` 的三条启发式（同前缀 / 正文提到 / 同时间段）全是**字符串**关系，
答不了"两条记忆的内容像不像"。全息向量补的正是这一块：把一段文字压成一个定长复数向量，
再用循环卷积把"谁-什么关系"绑在一起，于是
`bind(实体, 角色) ⊛ 内容` 这类结构可以整体存、整体取。

## 数值口径（照抄 research 07 §4.1，不自己拍）

- 维度 `DIM = 1024`（Hermes 的 `hrr_dim` 默认值）。
- 打分三路权重 **fts 0.4 / jaccard 0.3 / hrr 0.3**。出处：
  `handoff/hermes-parity/research/07-memory-layer.md` §4.1
  「打分权重：**fts 0.4 / jaccard 0.3 / hrr 0.3**（无 numpy 时自动重分配为 0.6 / 0.4 / 0）」。
  本机装了 numpy，所以用 0.4/0.3/0.3，不启用那条降级权重。
- trust ±0.05 / −0.10 属于**信任分**，不在本模块（这里没有跨会话的写入反馈闭环）。

## 这是 HRR 的**近似**实现——具体近似在哪

真 HRR（Plate 1995）的原子是随机实向量（分量 ~ N(0, 1/N)），`bind` 是循环卷积、
`unbind` 是相关。它有一个统计性质：**单对 bind→unbind 的还原余弦只有 ≈ 1/√2**
（实测本机 0.744）——信号能量与串扰能量相当，还原是"统计上对"而不是"精确对"。
好处是对噪声鲁棒，坏处是本机拿它做单对还原的验收（要求相似度 > 0.8）根本过不了。

所以 Orbit 把原子定义在**频域**：`atom(text) = ifft(e^{iφ})`，φ 由 `hashlib` 派生。
这样每个原子的频谱是单位模长的随机相位，**时域自相关严格是 delta**，于是
`unbind(bind(a, b), b) = a`（实测余弦 1.0）。`bind` / `unbind` 仍然是用 FFT 做的
循环卷积与相关，一个字都没偷换；换掉的只是"原子长什么样"。

代价必须写清楚：**精确可逆 ⇔ 对噪声敏感**。真 HRR 的还原是统计近似，一条记录被
轻微扰动后仍能"大概取出"；这里的还原是代数精确，相位一动就精确地错。本机没有
Hermes 的参照实现可比对，所以验证只做到一个层面：**同一份数据下 bind→unbind 能
近似（这里是精确）还原**，以及"共享词越多、hrr 分越高"这个单调性。更细的规范化
细节（能量归一、bank 的分桶、temporal decay）都没有实现，也不假装实现。

## 为什么 FTS 这一路是代理分

Hermes 的 0.4 那一路是 SQLite FTS5 的 `bm25()`。Orbit 的 `memories` 表**没有 FTS 虚表**
（加 FTS5 表要迁移，`app/storage.py` 与 alembic 是禁改区），所以这里用"查询词覆盖率"
作代理：同一路的职责（词面命中）不变，权重不变，只是没有 IDF 与长度归一。
中文按 2 字 shingle 切——与 `memory_graph` / `retrieval` 的结论一致：
unicode61 对中文命中 0，必须走子串/trigram 类切法。
"""

from __future__ import annotations

import hashlib
import re

import numpy as np

from app.storage import similarity as _char_similarity

#: HRR 维度。Hermes 的 `hrr_dim` 默认值（research 07 §4.1）。
DIM = 1024

#: 三路打分权重：fts 0.4 / jaccard 0.3 / hrr 0.3。
#: 出处：`handoff/hermes-parity/research/07-memory-layer.md` §4.1。
#: 三者之和必须是 1.0——`combine()` 会断言这一点，否则"权重"只是三个魔数。
W_FTS = 0.4
W_JACCARD = 0.3
W_HRR = 0.3

#: 派生相位用的域分隔前缀。换掉它就等于换一套原子（旧向量全部失效），
#: 所以带上版本号：将来若改编码方式，这里升 v2，而不是悄悄让旧数据"看起来像"。
SEED_PREFIX = b"orbit-holographic-v1"

#: 查询/正文切词：ASCII 词（含数字下划线）+ 中日韩字符。CJK 不走 `\w+`，
#: 因为 `\w` 对中文是整段匹配——"喜欢咖啡"会被当成一个词，覆盖率就永远是 0 或 1。
_TOKEN = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff]")

#: CJK shingle 长度。取 2 与 `storage.similarity` 的 bigram 口径一致。
_SHINGLE = 2


# ── 编码 ─────────────────────────────────────────────────────────────────────


def _phases(seed: bytes, dim: int) -> np.ndarray:
    """从种子派生 `dim` 个 [0, 2π) 上的相位。

    用 SHA-256 的**计数器块**展开（`sha256(seed + block_index)`），而不是
    `random`：`random` 依赖全局状态，同进程里别处调一次 `random.seed()` 就会让
    "同文本同向量"失效；而向量要落库、要跨进程比对，可复现是硬要求。
    """
    out = np.empty(dim, dtype=np.float64)
    filled = 0
    block = 0
    while filled < dim:
        digest = hashlib.sha256(seed + block.to_bytes(8, "big")).digest()
        # 32 字节 = 16 个 uint16。用 uint16 而不是 float32：整数映射到相位是精确的，
        # 浮点在不同 numpy 版本上末位可能不同，那会让"可复现"变成"大概可复现"。
        values = np.frombuffer(digest, dtype=">u2").astype(np.float64)
        take = min(len(values), dim - filled)
        out[filled : filled + take] = values[:take]
        filled += take
        block += 1
    return out / 65535.0 * 2.0 * np.pi


def encode_atom(text: str, dim: int = DIM) -> np.ndarray:
    """把**一个词**编码成原子向量：`ifft(单位模长随机相位)`。

    为什么在频域上取单位模长：这样时域自相关严格是 delta（CAZAC 类序列），
    `unbind(bind(a, b), b)` 才能精确还原成 `a`。这是本模块与真 HRR 唯一的分歧点，
    代价写在模块 docstring 里。
    """
    if dim < 2:
        raise ValueError(f"HRR 维度至少为 2，收到 {dim}")
    seed = hashlib.sha256(SEED_PREFIX + text.encode("utf-8")).digest()
    spectrum = np.exp(1j * _phases(seed, dim))
    # `ifft` 自带 1/N 归一；乘 sqrt(N) 让原子范数回到 1 附近，bind 之后量级不漂。
    return np.fft.ifft(spectrum) * np.sqrt(dim)


def terms(text: str) -> list[str]:
    """切词：ASCII 词原样（小写），CJK 连续片段切成 2 字 shingle。"""
    raw = [token.lower() for token in _TOKEN.findall(text or "")]
    out: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        chunk = "".join(buffer)
        buffer.clear()
        if len(chunk) <= _SHINGLE:
            out.append(chunk)
            return
        out.extend(chunk[index : index + _SHINGLE] for index in range(len(chunk) - 1))

    for token in raw:
        if token.isascii():
            flush()
            out.append(token)
        else:
            buffer.append(token)
    flush()
    return out


def encode(text: str, dim: int = DIM) -> np.ndarray:
    """把一段文字编码成单位长度的全息向量。

    **确定性**：同一文本、同一 dim，永远得到同一个向量（哈希派生，不碰 `random`）。

    文本是**原子叠加**而不是"整串一起哈希"：叠加之后两段共享词的文字才有相似度
    （"张三 喜欢 咖啡" vs "张三 讨厌 咖啡" 的 hrr 分 ≈ 0.67），整串哈希只会给出
    0 或 1。代价是词序信息丢失——HRR 本来就不编码顺序，要顺序得用 `bind` 显式绑位置。
    """
    tokens = terms(text)
    if not tokens:
        # 空文本（或全是标点）：返回零向量。零向量的相似度定义成 0，见 `similarity`。
        return np.zeros(dim, dtype=np.complex128)
    vector = superpose([encode_atom(token, dim) for token in tokens])
    return vector


def superpose(vectors) -> np.ndarray:
    """叠加（superposition）：逐分量相加。

    不做"除以条数"的归一化——叠加的语义是"这些原子同时在"，
    归一化会让"3 条记忆的 bundle"与"1 条记忆"在相似度上不可比。
    需要单位长度时由调用方（`encode`）显式归一。
    """
    items = list(vectors)
    if not items:
        raise ValueError("superpose 至少需要一个向量")
    total = np.zeros_like(np.asarray(items[0], dtype=np.complex128))
    for vector in items:
        total = total + np.asarray(vector, dtype=np.complex128)
    norm = float(np.linalg.norm(total))
    return total / norm if norm else total


# ── 绑定 / 解绑 ──────────────────────────────────────────────────────────────


def bind(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """循环卷积 `a ⊛ b`（用 FFT 实现，numpy 自带，不引别的库）。

    语义：把两个向量"绑"成一个新的、与两者都不像的向量。经典用法是
    `bind(实体, 角色)` —— 绑完就再也搜不到实体本身，只能靠 `unbind` 取回。
    """
    first = np.asarray(a, dtype=np.complex128)
    second = np.asarray(b, dtype=np.complex128)
    if first.shape != second.shape:
        raise ValueError(f"bind 的两个向量长度必须相同：{first.shape} vs {second.shape}")
    return np.fft.ifft(np.fft.fft(first) * np.fft.fft(second))


def unbind(bound: np.ndarray, key: np.ndarray) -> np.ndarray:
    """循环相关 `bound ⊛* key`：`bind` 的逆运算。

    `unbind(bind(a, b), b) ≈ a`。近似还是精确取决于原子的频谱——见模块 docstring。
    """
    first = np.asarray(bound, dtype=np.complex128)
    second = np.asarray(key, dtype=np.complex128)
    if first.shape != second.shape:
        raise ValueError(f"unbind 的两个向量长度必须相同：{first.shape} vs {second.shape}")
    return np.fft.ifft(np.fft.fft(first) * np.conj(np.fft.fft(second)))


def similarity(left: np.ndarray, right: np.ndarray) -> float:
    """两个复数向量的余弦相似度，取实部（虚部是相位残差，不是"更多信息"）。

    零向量对任何东西都是 0：空文本与空文本"相似"在检索里没有意义，
    给 1.0 会让所有空记忆霸占榜首。
    """
    first = np.asarray(left, dtype=np.complex128)
    second = np.asarray(right, dtype=np.complex128)
    denom = float(np.linalg.norm(first) * np.linalg.norm(second))
    if not denom:
        return 0.0
    return float(np.real(np.vdot(first, second)) / denom)


# ── 三路打分 ─────────────────────────────────────────────────────────────────


def fts_score(query: str, text: str) -> float:
    """FTS 那一路的**代理分**：查询词在正文里的覆盖率 ∈ [0, 1]。

    不是真 bm25（`memories` 表没有 FTS5 虚表，加表是禁改区）。职责相同：
    回答"字面上命中了多少"。没有 IDF、没有长度归一，所以短正文不会因为"短"
    而被抬高——这在本模块里是好事，因为记忆正文本来就只有几十个字。
    """
    wanted = terms(query)
    if not wanted:
        return 0.0
    haystack = (text or "").lower()
    hits = sum(1 for token in set(wanted) if token in haystack)
    return hits / len(set(wanted))


def jaccard_score(query: str, text: str) -> float:
    """字符 bigram Jaccard。复用 `storage.similarity`，不另写一套。

    为什么复用而不是自己算：`storage.synonym_key` 用同一口径判"两个 key 是不是同义"，
    两处口径一旦分叉，会出现"同义判定说像、检索说不像"的鬼故事。
    """
    return float(_char_similarity(query or "", text or ""))


def hrr_score(query: str, text: str, *, dim: int = DIM) -> float:
    """全息相似度 ∈ [0, 1]：`max(0, cos(encode(query), encode(text)))`。

    负相关截到 0：`score` 要按权重加权求和，负分量会让"三路都很差"的条目拿到
    比"三路都为零"更低的分数，从而被排到后面——但"负相似"在词袋模型里没有语义，
    只是随机相位碰巧反相。截到 0 是把它当噪声而不是当信号。
    """
    return max(0.0, similarity(encode(query, dim), encode(text, dim)))


def combine(fts: float, jaccard: float, hrr: float) -> float:
    """按 research 的权重合成总分。三路都先夹到 [0, 1]，避免上游给超界值把总分带飞。"""
    def clamp(value: float) -> float:
        return 0.0 if value < 0.0 else (1.0 if value > 1.0 else float(value))

    return W_FTS * clamp(fts) + W_JACCARD * clamp(jaccard) + W_HRR * clamp(hrr)


def score(query: str, memory: str, *, dim: int = DIM) -> float:
    """组合三种信号给一条记忆打分：**fts 0.4 / jaccard 0.3 / hrr 0.3**。

    三条信号的分工（照 Hermes 的检索管线）：
    - fts：字面命中，最硬的信号，权重最高；
    - jaccard：字符级重合，补"分词切歪了但字面很像"的情况；
    - hrr：叠加向量的余弦，补"词不同但原子重合"的情况。
    """
    return combine(
        fts_score(query, memory),
        jaccard_score(query, memory),
        hrr_score(query, memory, dim=dim),
    )


def explain(query: str, memory: str, *, dim: int = DIM) -> dict:
    """把三路分数与合成结果一起报出来（CLI `--json` 与排障用）。

    只报总分不报分量，用户就没法回答"为什么这条排第一"——而权重是可以在
    research 里被质疑、被调整的东西，看不见分量的权重等于黑箱。
    """
    fts = fts_score(query, memory)
    jaccard = jaccard_score(query, memory)
    hrr = hrr_score(query, memory, dim=dim)
    return {
        "fts": round(fts, 4),
        "jaccard": round(jaccard, 4),
        "hrr": round(hrr, 4),
        "weights": {"fts": W_FTS, "jaccard": W_JACCARD, "hrr": W_HRR},
        "total": round(combine(fts, jaccard, hrr), 4),
    }


# ── 记忆图 ───────────────────────────────────────────────────────────────────


class MemoryGraph:
    """全息图记忆：key → value 的本地图，带 HRR 向量与 BFS 邻居。

    `add` 维护两样东西：正文（打分要原文）与向量（hrr 那一路）。
    向量在这里**缓存**而不是每次重算——`encode` 是 SHA-256 + FFT，1024 维一次约
    0.3 ms，几百条记忆乘以每条查询都要算一遍就变成可感知的延迟了。
    """

    def __init__(self, entries: dict[str, str] | None = None, *, dim: int = DIM) -> None:
        if dim < 2:
            raise ValueError(f"HRR 维度至少为 2，收到 {dim}")
        self.dim = int(dim)
        self._values: dict[str, str] = {}
        self._vectors: dict[str, np.ndarray] = {}
        for key, value in (entries or {}).items():
            self.add(key, value)

    # ── 写 ──
    def add(self, key: str, value: str) -> None:
        """加一条（同 key 覆盖）。空 key 直接拒绝——它在图里没有可寻址的 id。"""
        name = (key or "").strip()
        if not name:
            raise ValueError("记忆 key 不能为空")
        self._values[name] = "" if value is None else str(value)
        self._vectors[name] = encode(f"{name} {self._values[name]}", self.dim)

    def remove(self, key: str) -> bool:
        name = (key or "").strip()
        if name not in self._values:
            return False
        self._values.pop(name, None)
        self._vectors.pop(name, None)
        return True

    # ── 读 ──
    def keys(self) -> list[str]:
        return list(self._values)

    def entries(self) -> dict[str, str]:
        return dict(self._values)

    def vector(self, key: str) -> np.ndarray:
        return self._vectors[(key or "").strip()]

    def bank(self) -> np.ndarray:
        """整个库打包成一个向量（Hermes 的 `memory_banks`）。

        全空库返回零向量，而不是抛异常：`orbit memory holographic` 在没有记忆的
        数据目录上应该打印"空库"，而不是一个栈。
        """
        if not self._vectors:
            return np.zeros(self.dim, dtype=np.complex128)
        return superpose(list(self._vectors.values()))

    def bank_norm(self) -> float:
        """库向量的模长。`bank()` 是向量，`abs()` 在 numpy 上给的是逐元素模——
        这里要的是标量，所以单独开一个方法，免得每个调用点各写一遍 `np.linalg.norm`。
        """
        return float(np.linalg.norm(self.bank()))

    def query(self, text: str, limit: int = 10) -> list[dict]:
        """按 `score` 排序取前 `limit` 条。返回每条的三路分量，便于解释排名。"""
        needle = (text or "").strip()
        if not needle or not self._values:
            return []
        rows: list[dict] = []
        for key, value in self._values.items():
            detail = explain(needle, f"{key} {value}", dim=self.dim)
            rows.append({"key": key, "value": value, **detail})
        rows.sort(key=lambda row: (-row["total"], row["key"]))
        return rows[: max(0, int(limit))]

    def neighbors(self, key: str, depth: int = 1) -> list[dict]:
        """BFS 邻居。图结构复用 `memory_graph` 的三条启发式，不另造一套边。

        为什么不在这里按向量距离连边：向量距离是"内容像"，图谱的边是"关系"，
        两者混在一张图上会让"邻居"既可能同前缀又可能同时间段还可能只是措辞接近，
        用户没法解释自己看到的东西。要按相似度找，用 `query`。
        """
        from app.memory_graph import neighbors as graph_neighbors

        return graph_neighbors(key, depth, source=self._values)


#: `W_FTS + W_JACCARD + W_HRR` 必须正好是 1.0。放在模块末尾做一次导入期断言，
#: 而不是在 `combine` 里每次算——权重写错是**常量写错**，应该一开始就炸。
assert abs(W_FTS + W_JACCARD + W_HRR - 1.0) < 1e-9, "三路权重之和必须为 1.0"
