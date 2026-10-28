"""`--reasoning` 真的传到模型（MATRIX 4.4 的验收判据）。

## 之前缺的是什么

`app/reasoning.py` 的 `to_request` / `extract` 与 `agent.py` 的 `reasoning_effort`
参数链**都在**（`get_model(reasoning_effort=...)` → `_client_builder` →
`build_chat_model(reasoning_effort=...)`），但 `graph/nodes.py` 的调用点
**全部不传**——于是 `reasoning_effort` 恒为 None。

`orbit chat --reasoning high` 只改了 `settings.reasoning_level`，**那个值没人读**。
参数链完整、单测也过，**但从没被激活过**。

## 判据
1. `Settings` 真的声明了 `reasoning_level`（否则 pydantic 的 `model_copy` 会把它
   作为裸属性塞进去——能用但不是模型字段，序列化会丢）；
2. `Nodes._reasoning_effort()` 能把档位读出来，空串转 None；
3. **源码层：`get_model()` 不允许有不带 `reasoning_effort` 的调用点**——
   这条是防回归的关键：将来再加一个调用点忘了传，测试会红。
"""

from __future__ import annotations

import pathlib
import re


class _FakeRunner:
    """只需要 `settings`（`Nodes` 的 `settings` 属性委托给它）。"""

    def __init__(self, settings) -> None:
        self.settings = settings


def _nodes(settings):
    from app.graph.nodes import Nodes

    return Nodes(_FakeRunner(settings))


def test_settings_declares_reasoning_level(tmp_path):
    """`reasoning_level` 必须是**真的模型字段**，不是 `model_copy` 塞的裸属性。"""
    from app.config import Settings

    assert "reasoning_level" in Settings.model_fields, (
        "`Settings` 没声明 `reasoning_level`——`--reasoning` 会变成一个"
        "不是字段的裸属性，序列化时丢掉"
    )
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    assert settings.reasoning_level == ""


def test_reasoning_effort_reads_the_level(tmp_path):
    """档位读得出来；空串转 None（表示「不传」，不是「传空字符串」）。"""
    from app.config import Settings

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    assert _nodes(settings)._reasoning_effort() is None

    settings.reasoning_level = "high"
    assert _nodes(settings)._reasoning_effort() == "high"

    settings.reasoning_level = "  "
    assert _nodes(settings)._reasoning_effort() is None, "空白应视为不传"


def test_no_get_model_call_site_forgets_the_argument():
    """**防回归**：`get_model()` 不允许有不带 `reasoning_effort` 的调用点。

    4.4 的虚报形状就是「链在、但没人传值」。只验链是验不出这种病的——
    必须逐个调用点检查。将来加第 3 个调用点忘了传，这条会红。
    """
    from app.graph import nodes as nodes_module

    source = pathlib.Path(nodes_module.__file__).read_text(encoding="utf-8")
    # **先剔掉注释行**：解释性注释里也会出现 `get_model()` 这种字面量，
    # 第一版没剔，结果把一句「之前所有调用点都不传」的注释当成了真调用点。
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    # 找出所有 `get_model(...)` 调用（排除 `getattr(..., "get_model", None)` 这类字符串）
    calls = re.findall(r"(?<![\"'])get_model\(([^)]*)\)", code)
    assert calls, "没找到任何 get_model 调用点——测试失效了，先确认代码结构"

    bare = [args for args in calls if "reasoning_effort" not in args]
    assert not bare, (
        f"这些 get_model 调用没传 reasoning_effort：{bare}——"
        "`--reasoning` 在那些路径上不会生效"
    )


def test_cli_flag_writes_the_field_that_nodes_reads():
    """`--reasoning` 写的键名必须与 `Nodes` 读的键名一致。

    **两处写字符串常量是最容易漂的地方**：一个改成 `reasoning`、另一个还是
    `reasoning_level`，两边各自都对，合起来永远不生效。
    """
    from app.cli.commands import chat as chat_module
    from app.graph import nodes as nodes_module

    chat_source = pathlib.Path(chat_module.__file__).read_text(encoding="utf-8")
    assert '"reasoning_level"' in chat_source, (
        "`chat.py` 不再写 `reasoning_level` 了——`Nodes._reasoning_effort()` 会读不到"
    )
    nodes_source = pathlib.Path(nodes_module.__file__).read_text(encoding="utf-8")
    assert '"reasoning_level"' in nodes_source, (
        "`nodes.py` 不再读 `reasoning_level` 了——`--reasoning` 会失效"
    )
