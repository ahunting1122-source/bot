"""按需加载真的接在分发门上（MATRIX 5.2 的验收判据）。

## 之前缺的是什么

`app/tool_discovery.py` 的 AST 懒发现能扫出 58 个工具、与真实工具表对得上，
测试也齐。**但 `ensure_loaded()` 零外部调用方**——`tools_extra3.py` 只用了
`discover` / `describe` / `index_path` 三个**报告**函数。
**所以「按需 import」这件事从没发生过**：工具全在启动时一次性 import 完。

## 判据（含一条必须「不做」的）

1. 注册表里被拿掉的工具，模型再报这个名字时**能按索引 import 回来**；
2. `validate()` 这道门之后**真的能通过**（不是只有内部方法能用）；
3. **不能假装加载**：`app.tools` 之外的模块（`app.browser` / `app.web`）的工具
   需要装配层传 `executor`，**本层拿不到**——那种情况必须如实报「未知工具」，
   **编一个假执行器只会换来「工具调了但什么也没发生」，比报错难查得多**；
4. `lazy_load = False` 时退回旧行为。
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def registry(tmp_path):
    from app.config import Settings
    from app.storage import Store
    from app.tools import ToolRegistry

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    store = Store(settings.data_dir / "app.sqlite")
    reg = ToolRegistry(settings, store)
    yield reg
    reg.lazy_load = True


def test_a_missing_local_tool_is_loaded_on_first_use(registry):
    """**核心判据**：拿掉的 `app.tools` 工具，再报名字能按索引 import 回来。"""
    name = "local_list_files"
    assert name in registry.tools, "前置条件不成立：这个工具本该在默认注册表里"

    registry.tools.pop(name)
    assert name not in registry.tools

    tool = registry._lazy_tool(name)
    assert tool is not None, "懒加载没把它 import 回来——`ensure_loaded` 又没接上"
    assert name in registry.tools, "import 了但没进注册表"


def test_validate_passes_after_lazy_load(registry):
    """**分发门**上真的能用——不是只有内部方法能调。

    这条比上一条重要：模型报的工具名走的正是 `validate()`，
    只有它通了才算「按需加载在真实路径上生效」。
    """
    name = "local_list_files"
    registry.tools.pop(name)

    # `validate` 通过 = 工具存在且参数形状合法；拿不到就会抛「未知工具」
    result = registry.validate(name, {"path": "."})
    assert isinstance(result, dict)
    assert name in registry.tools, "validate 之后工具应该已在注册表里"


def test_a_tool_from_another_module_is_not_faked(registry):
    """**必须「不做」的一条**：其它模块的工具不能被假装加载。

    `browser_back` 定义在 `app.browser`，它的执行器只有装配层知道。
    本层如果编一个假执行器把它塞进注册表，模型调用后会得到
    「工具调了但什么也没发生」——**比报错难查得多**。
    """
    name = "browser_back"
    registry.tools.pop(name, None)

    with pytest.raises(ValueError, match="未知工具"):
        registry.validate(name, {})
    assert name not in registry.tools, "不该把一个没有执行器的工具塞进注册表"


def test_lazy_load_can_be_switched_off(registry):
    """`lazy_load = False` 退回「全部启动时注册」的旧行为。"""
    registry.lazy_load = False
    name = "local_list_files"
    registry.tools.pop(name)

    assert registry._lazy_tool(name) is None
    assert name not in registry.tools


def test_the_mechanism_is_reachable_from_production_code():
    """防回归：`validate()` 里必须真的调 `_lazy_tool`。

    只验「`_lazy_tool` 能用」是不够的——5.2 的虚报形状正是
    「机制写好了、没人调」。这条钉住调用点存在。
    """
    import pathlib

    from app import tools as tools_module

    source = pathlib.Path(tools_module.__file__).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    assert "self._lazy_tool(name)" in code, (
        "`validate()` 不再调 `_lazy_tool` 了——按需加载又变成死机制"
    )


def test_an_index_pointing_outside_its_root_is_rejected(tmp_path):
    """**安全边界**：索引文件里指向 `root` 之外的条目必须被丢掉。

    索引里写的是**模块路径**，而 `ensure_loaded()` 会 `importlib.import_module(...)`
    ——**谁能写索引，谁就能让进程 import 任意模块**。
    当前索引只由 `discover()` 扫 `app_root()` 生成，但 `load_index(path)` 这个函数
    已经存在、`data_dir` 又可写，所以这条边界要现在就有。

    **为什么不是「为未来假设的问题写代码」**：`load_index` 是**已存在的接口**，
    不是需要发明场景才存在的风险。判据是「这行代码已经存在」而不是
    「这个场景可能发生」。
    """
    import json

    from app.tool_discovery import load_index

    root = tmp_path / "app"
    root.mkdir()
    (root / "ok.py").write_text("TOOL = 1\n", encoding="utf-8")

    index_file = tmp_path / "index.json"
    index_file.write_text(
        json.dumps(
            {
                "root": str(root),
                "tools": {
                    "inside": {"module": "app.ok", "path": str(root / "ok.py"), "lineno": 1},
                    # 越界：指向系统目录里的一个真实文件
                    "outside": {
                        "module": "os",
                        "path": str(tmp_path / ".." / "outside.py"),
                        "lineno": 1,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    index = load_index(index_file)
    assert index is not None, "索引整体不该因为一个坏条目就作废"
    names = {entry.name for entry in index.entries.values()} if index.entries else set()
    assert "outside" not in names, (
        "越界条目没被丢掉——一个能被外部改写的索引就是代码执行面"
    )
