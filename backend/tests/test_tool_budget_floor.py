"""把「工具 schema 的 token 地板」钉成测试（MATRIX 5.11）。

## 为什么要这条测试

监视子agent 第 11 轮发现：**同一个"地板"数字在代码库里有三个值**——
MATRIX 里写 4,935、子agent 实测 5,448、`tools.py` 的 docstring 写"约 4,300"，
而且**没有任何测试钉住它**。

三个数并存的原因很朴素：它们都是在不同时间点手抄的。手抄的数字必然漂移，
而漂移的后果是——**"4,000 预算做不到"这个结论会被人当成估算而不是事实**。

所以这里不写死某个数字，而是**每次跑都重新算，并断言三件互相矛盾不了的事**：

1. 全量工具的 token 数 > 4,000（预算确实装不下）；
2. 压完描述之后**仍然** > 4,000（所以 `over_budget` 是对的，不是懒得压）；
3. 参数 schema 本身（把描述全删）就已经占掉大部分预算——**这是"地板"的定义**。

以后工具变多/变少，数字会变，但这三条关系不会变。**关系才是契约，数字不是。**
"""

from __future__ import annotations

import json

import pytest

from app import tools

BUDGET = 4_000


@pytest.fixture(scope="module")
def registry(tmp_path_factory):
    from app.config import Settings
    from app.storage import Store

    root = tmp_path_factory.mktemp("budget")
    settings = Settings(data_dir=root / "data", workspace=root / "files")
    settings.prepare()
    return tools.ToolRegistry(settings, Store(settings.data_dir / "app.sqlite"))


def _tokens(payload: object) -> int:
    return tools.estimate_tokens(json.loads(json.dumps(payload, default=str)))


def test_full_specs_exceed_the_budget(registry):
    """全量工具装不进 4,000 token——这是 `over_budget` 存在的前提。"""
    full = _tokens(registry.specs(platform="cli", budget=None))
    assert full > BUDGET, f"全量只有 {full} token，预算装得下，`over_budget` 该消失了"


def test_trimming_descriptions_cannot_reach_the_budget(registry):
    """压完描述**仍然**超预算——所以 `over_budget=True` 是事实，不是没努力。

    这条是最关键的一条：如果哪天它变绿（压完能进 4,000），那 `over_budget` 就该
    变成 False，而 MATRIX 5.11 的结论也要跟着改。**测试变绿是一个需要行动的信号**，
    不是"太好了终于过了"。
    """
    trimmed, over = tools._trim_specs(
        json.loads(json.dumps(registry.specs(platform="cli", budget=None), default=str)),
        BUDGET,
    )
    assert over is True, "压完描述就能进 4,000 了——`over_budget` 该改成 False"
    assert _tokens(trimmed) > BUDGET


def test_parameter_schema_alone_is_the_floor(registry):
    """把描述全删，剩下的就是**地板**——参数 schema 本身。

    地板存在的理由：schema 是调用契约。删了它，模型不知道要传什么参数，
    那个工具就永远调不动——**留一个调不动的工具比不暴露它更糟**。
    """
    specs = json.loads(json.dumps(registry.specs(platform="cli", budget=None), default=str))
    stripped = []
    for spec in specs:
        clone = json.loads(json.dumps(spec))
        fn = clone.get("function") or clone
        fn["description"] = ""
        params = fn.get("parameters") or {}
        props = params.get("properties") or {}
        for prop in props.values():
            if isinstance(prop, dict):
                prop["description"] = ""
        stripped.append(clone)
    floor = _tokens(stripped)
    full = _tokens(specs)
    assert 0 < floor <= full, f"地板 {floor} 不该大于全量 {full}"
    # 地板占全量的比例要"显著"——否则说明大部分 token 花在描述上，地板就不算地板
    assert floor > BUDGET * 0.5, f"地板只有 {floor} token，预算的瓶颈不是 schema"


def test_over_budget_flag_is_reported_honestly(registry):
    """`specs()` 的默认预算下必须如实报 `over_budget`。"""
    _, over = tools._trim_specs(
        json.loads(json.dumps(registry.specs(platform="cli", budget=None), default=str)),
        BUDGET,
    )
    assert over is True


def test_docstring_does_not_claim_a_different_floor(registry):
    """`tools.py` 的 docstring 里不该再写一个和实测对不上的地板数字。

    这条是"三个数并存"那个坑的守卫：docstring 里任何「约 N token」的说法，
    只要和实测地板差得太远就报错。要么删掉数字，要么让它跟着实测走。
    """
    import re

    source = tools.__file__ or ""
    text = open(source, encoding="utf-8").read()
    stripped = [
        json.loads(json.dumps(spec, default=str))
        for spec in registry.specs(platform="cli", budget=None)
    ]
    for spec in stripped:
        fn = spec.get("function") or spec
        fn["description"] = ""
    floor = _tokens(stripped)
    for match in re.finditer(r"约\s*([\d,]+)\s*token", text):
        claimed = int(match.group(1).replace(",", ""))
        assert abs(claimed - floor) <= floor * 0.35, (
            f"docstring 说「约 {claimed} token」，实测地板是 {floor}——差得太远。"
            "删掉这个数字，或者让它跟着实测走。"
        )
