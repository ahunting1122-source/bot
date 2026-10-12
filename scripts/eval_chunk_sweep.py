"""Chunk size 参数扫描：不同切块配置下 recall@5 / MRR@5 的对比。

补的是 RAG 面经第 12 题留下的洞——"切块 400 字 / 上限 500"一直是设计文档里的
定值，没有对照实验。本脚本复用 scripts/eval_rag.py 的 DDL、拆词与打分函数，
只把 `app/indexing.py` 顶部的切块常量换成不同档位，重跑同一套评测。

两组语料：

1. `synthetic`：eval_rag 自带的 3 篇合成短文档（保证与既有评测可比）；
2. `repo_docs`：本仓库真实的 6 篇中文设计文档（约 6 万字），长文才切得出多个块，
   切块参数在这里才可分辨——**结论以这组为准**。

用法（在 backend/ 下）：

    uv run python ../scripts/eval_chunk_sweep.py
    uv run python ../scripts/eval_chunk_sweep.py --json ../data/chunk_sweep.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(HERE))

import eval_rag  # noqa: E402

import app.indexing as indexing  # noqa: E402

#: 扫描档位：现状（300–500）作基准；600–800 仍在 bge 512 token 窗口附近；
#: 1000–1200 明显越过窗口（用于验证"超窗会掉分"这个论断）；
#: 外加零重叠一档，验证 overlap 是否真的换来了召回。
CONFIGS: list[dict[str, Any]] = [
    {"name": "300-500 overlap80 (现状)", "target": 400, "min": 300, "max": 500, "overlap": 80},
    {"name": "600-800 overlap80", "target": 700, "min": 600, "max": 800, "overlap": 80},
    {"name": "1000-1200 overlap120", "target": 1100, "min": 1000, "max": 1200, "overlap": 120},
    {"name": "300-500 overlap0", "target": 400, "min": 300, "max": 500, "overlap": 0},
]

#: 真实语料：仓库自己的中文设计文档（长文，能切出多块）。
REPO_DOCS = [
    "ARCHITECTURE.md",
    "AGENT_DESIGN.md",
    "HARNESS_DESIGN.md",
    "AGENT_ORCHESTRATION.md",
    "MVP_PLAN.md",
    "ROADMAP.md",
]

#: 查询与期望片段：片段全部经过 grep 核验确实存在于上述文档中。
REPO_CASES: list[tuple[str, list[str]]] = [
    ("为什么两个字的中文词查不到", ["只匹配 ≥3 字符"]),
    ("回答没有引用来源算完成吗", ["无来源不算完成"]),
    ("MCP 的 resources 怎么进索引", ["异步抓取入索引队列"]),
    ("向量存储用哪个库", ["sqlite-vec"]),
    ("切块的目标长度是多少", ["300–500 中文字"]),
    ("三层预算是哪三层", ["动作 / token / 时间"]),
    ("索引接受什么来源", ["web_page"]),
    ("工具状态不确定时能重发吗", ["禁止重发"]),
    ("记忆和技能有什么区别", ["事实与偏好"]),
    ("密钥存在哪里", ["唯一放真值"]),
    ("重启后不确定的结果怎么处理", ["先回读核对"]),
    ("检索类任务必须给出什么", ["引用回填"]),
]


def repo_corpus() -> dict[str, str]:
    return {name: (ROOT / name).read_text(encoding="utf-8") for name in REPO_DOCS}


def rank_trigram(conn, query):
    return eval_rag.fts_search(conn, query, "chunks_fts_trigram", eval_rag.LIST_DEPTH)


def rank_chars(conn, query):
    return eval_rag.fts_search(conn, query, "chunks_fts_chars", eval_rag.LIST_DEPTH)


RANKERS = (
    ("trigram", rank_trigram),
    ("chars", rank_chars),
    ("rrf_fusion", eval_rag.fused),
)


def apply(config: dict[str, Any]) -> None:
    indexing.TARGET_CHARS = config["target"]
    indexing.MIN_CHARS = config["min"]
    indexing.MAX_CHARS = config["max"]
    indexing.OVERLAP_CHARS = config["overlap"]


def run(config: dict[str, Any], corpus: dict[str, str], cases: list[tuple[str, list[str]]]):
    apply(config)
    eval_rag.CORPUS = corpus  # build_db 读的是模块级语料，这里换掉即可复用同一套 DDL
    with tempfile.TemporaryDirectory() as tmp:
        conn, total = eval_rag.build_db(pathlib.Path(tmp) / "sweep.sqlite")
        try:
            rows: dict[str, Any] = {"chunks": total}
            for label, ranker in RANKERS:
                recall, mrr = eval_rag.score(cases, ranker, conn)
                rows[label] = {"recall_at_5": round(recall, 3), "mrr_at_5": round(mrr, 3)}
        finally:
            conn.close()
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=pathlib.Path, default=None, help="另存 JSON 报告")
    args = parser.parse_args()

    corpora = {
        "synthetic": (dict(eval_rag.CORPUS), eval_rag.CASES),
        "repo_docs": (repo_corpus(), REPO_CASES),
    }

    report: list[dict[str, Any]] = []
    header = (
        f"{'语料':<11}{'配置':<28}{'块数':>5}{'trigram':>10}{'chars':>10}{'融合':>10}{'融合MRR':>10}"
    )
    for corpus_name, (corpus, cases) in corpora.items():
        print(f"\n== {corpus_name} ==  文档 {len(corpus)} 篇 / 查询 {len(cases)} 条")
        print(header)
        print("-" * len(header))
        for config in CONFIGS:
            rows = run(config, corpus, cases)
            report.append({"corpus": corpus_name, "config": config["name"], **rows})
            print(
                f"{corpus_name:<11}{config['name']:<28}{rows['chunks']:>5}"
                f"{rows['trigram']['recall_at_5']:>10.3f}"
                f"{rows['chars']['recall_at_5']:>10.3f}"
                f"{rows['rrf_fusion']['recall_at_5']:>10.3f}"
                f"{rows['rrf_fusion']['mrr_at_5']:>10.3f}"
            )
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\njson report -> {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
