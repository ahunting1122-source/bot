"""`Settings` 的「裸属性」守卫（监视子agent 第 22 轮 P0 ③）。

## 病是什么

`Settings` 是 pydantic 模型，而 `settings.model_copy(update={...})` **不校验也不报错**：
未声明的键会被作为**裸属性**塞进 `__dict__`。

于是「设置项不存在」与「设置项存在且被正确读取」在**静态阅读下完全同形**：

```python
context.settings = context.settings.model_copy(update={"reasoning_level": "high"})
...
level = getattr(self.settings, "reasoning_level", "")   # 永远读到 ""，因为字段不存在
```

`getattr(..., default)` 让「缺」看起来像「有」——**这正是 2.2 与 4.4 各骗过我一次的形状**。

## 判据要三条，不能只查「读得到」

| 判据 | 挡住什么 |
|---|---|
| `getattr(settings, name)` 读得到 | 连属性都没有（最粗） |
| `name in Settings.model_fields` | **裸属性**（`model_copy` 塞的） |
| `name in settings.model_dump()` | 不会被序列化（落盘 / 跨进程会丢） |

**只查第一条会被裸属性骗过**——而「裸属性」恰恰是最常见的那一种。

## 本轮实测结论

扫全仓 `settings.model_copy(update={...})` 与 `settings.X = ...`，查出 **4 个裸属性**：
`pass_session_id` / `quiet` / `skills_preload` / `verbose`。**四个全是「只写不读」**——
真正的路径分别是 `state["pass_session_id"]`、框架的 `Context.quiet`、
`state["skills"]`，而 `verbose` **在 chat 里根本没实现**。
已全部删掉死写入（`verbose` 改成如实说明「没实现」，照本文件 `--dev` 的既有做法）。
"""

from __future__ import annotations

import pathlib
import re

APP = pathlib.Path(__file__).resolve().parents[1] / "app"


def assert_real_settings_field(settings, name: str) -> None:
    """断言 `name` 是**真的** Settings 字段（三条判据缺一不可）。

    可复用：任何「这个配置项有没有真的接上」的判断都该走这里，而不是
    `hasattr` / `getattr(..., default)`——那两个对裸属性都是 True。
    """
    from app.config import Settings

    assert name in Settings.model_fields, (
        f"`{name}` 不在 `Settings.model_fields` 里——它是 `model_copy(update=...)` "
        "塞进去的裸属性，不是真的字段"
    )
    assert hasattr(settings, name), f"`{name}` 是字段但实例上没有"
    assert name in settings.model_dump(), (
        f"`{name}` 是字段但不在 `model_dump()` 里——序列化（落盘/跨进程）会丢"
    )


def _model_copy_literals(source: str) -> list[str]:
    """抠出 `settings.model_copy(update={...})` 字面量里的键名。"""
    keys: list[str] = []
    for match in re.finditer(
        r"(?:context\.)?settings\.model_copy\(\s*update\s*=\s*\{([^}]*)\}", source
    ):
        keys.extend(re.findall(r"\"([a-z_]+)\"\s*:", match.group(1)))
    return keys


def test_no_settings_model_copy_sets_an_undeclared_key():
    """**不许**用 `model_copy` 往 `Settings` 上塞未声明的键。

    这条红了 = 又出现一个「看起来能配、实际没人读」的裸属性。
    """
    from app.config import Settings

    fields = set(Settings.model_fields)
    offenders: list[str] = []
    for path in APP.rglob("*.py"):
        source = path.read_text(encoding="utf-8", errors="ignore")
        for key in _model_copy_literals(source):
            if key not in fields:
                offenders.append(f"{path.relative_to(APP.parent)}: {key}")
    assert not offenders, (
        "这些键不是 Settings 字段，却被 model_copy 塞成了裸属性：\n  "
        + "\n  ".join(sorted(set(offenders)))
        + "\n（要么在 `Settings` 里声明它，要么删掉这次写入——死写入比不写更糟）"
    )


def _declared(settings, name: str) -> bool:
    """`name` 是不是**声明过的**（字段或类上的 property）。

    **为什么不能只查 `model_fields`**：`machine_wide` 是 `Settings` 上的 `@property`，
    合法且有人用。第一版只查 `model_fields`，把它误报成了 bug。
    正确的判据是「**在类上定义过**」——字段和 property 都算，**实例上的裸属性不算**。
    """
    from app.config import Settings

    return name in Settings.model_fields or hasattr(type(settings), name)


def _keys_written_to_settings() -> set[str]:
    """全仓**真正**写进 `settings` 的键名（用 AST，不用正则）。

    **为什么必须用 AST**：第一版用正则，把两类字符串当成了写入：
    ① docstring 里的示例 `settings.turn_budgets = {...}`（`turn_budgets.py:98`）；
    ② 事件 payload 里的 `"turn_budgets": turns.to_dict()`。
    **源码扫描类测试最大的假阳性源就是注释与字符串**——这已经是同一个坑第二次。
    AST 只认 `Attribute` 目标与 `model_copy(update={...})` 字面量，字符串一律不碰。
    """
    import ast

    written: set[str] = set()

    def _is_settings(node: ast.AST) -> bool:
        """`settings` / `self.settings` / `context.settings`。"""
        if isinstance(node, ast.Name):
            return node.id == "settings"
        if isinstance(node, ast.Attribute):
            return node.attr == "settings"
        return False

    for path in APP.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            # settings.X = ...
            targets: list[ast.AST] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and _is_settings(target.value):
                    written.add(target.attr)
            # settings.model_copy(update={"x": ...})
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "model_copy"
                and _is_settings(node.func.value)
            ):
                for keyword in node.keywords:
                    if keyword.arg == "update" and isinstance(keyword.value, ast.Dict):
                        for key in keyword.value.keys:
                            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                                written.add(key.value)
    return written


def test_no_setting_is_written_as_a_bare_attribute_and_read_back():
    """**真 bug 的形状**：写用裸属性、读用 `getattr`。

    `model_copy(update={"x": ...})` 对未声明的键不报错（塞进 `__dict__`），
    于是 `getattr(settings, "x", default)` **真的读得到**——**能跑，但 `x` 不是字段**：
    序列化会丢、`model_fields` 里查不到、静态阅读看不见。**靠巧合工作。**

    这条**只报这个形状**（写过的键 + 没声明 + 被 getattr 读），不是把所有
    `getattr(settings, ...)` 都判成错——像 `window.window_of` 那种「字段将来加了就
    自动生效」的**有文档的前向兼容**读法是合法的，**它没有对应的写入方**。
    """
    from app.config import Settings

    settings = Settings(data_dir=APP.parent / ".pytest_tmp", workspace=APP.parent / ".pytest_tmp")
    written = _keys_written_to_settings()
    undeclared_writes = {name for name in written if not _declared(settings, name)}

    offenders: list[str] = []
    for path in APP.rglob("*.py"):
        source = path.read_text(encoding="utf-8", errors="ignore")
        for number, line in enumerate(source.splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            for match in re.finditer(
                r"getattr\(\s*(?:self\.)?(?:context\.)?settings\s*,\s*\"([a-z_]+)\"\s*,", line
            ):
                name = match.group(1)
                if name in undeclared_writes:
                    offenders.append(
                        f"{path.relative_to(APP.parent)}:{number}: {name}"
                    )
    assert not offenders, (
        "这些设置项**写的时候是裸属性、读的时候靠 getattr**（能跑但不是字段）：\n  "
        + "\n  ".join(sorted(set(offenders)))
        + "\n修法：在 `Settings` 里声明它（与 `reasoning_level` / `safe_mode` 同款）"
    )


def test_known_forward_compatible_reads_are_not_flagged():
    """反证：`window.window_of` 那种**有文档的前向兼容**读法不该被误报。

    它的 docstring 明说「`Settings` 目前没有 `context_length` 字段，字段一旦加进
    `app/config.py` 就自动生效」——**没有写入方**，所以不属于上面那个 bug 形状。
    """
    written = _keys_written_to_settings()
    for name in ("context_length", "tirith_enabled", "turn_budgets"):
        assert name not in written, (
            f"`{name}` 现在有写入方了——如果它还是裸属性，那就是真 bug 了"
        )


def test_the_four_known_bare_attributes_are_gone():
    """本轮删掉的 4 个裸属性不许回来。

    `pass_session_id` / `quiet` / `skills_preload` / `verbose` —— 它们曾经只写不读。
    """
    from app.config import Settings

    for name in ("pass_session_id", "quiet", "skills_preload", "verbose"):
        assert name not in Settings.model_fields, (
            f"`{name}` 被加成了真字段，但没有任何地方读它——加字段前先确认有人读"
        )


def test_settings_json_with_a_readonly_property_name_does_not_crash(tmp_path):
    """**真 bug 的回归测试**：`settings.json` 里写一个只读属性名，CLI 不能崩。

    原来 `app/cli/context.py::hydrate()` 的守卫是 `hasattr(self.settings, key)`。
    **`hasattr` 对 property 也返回 True**，而 `Settings` 上有：
    - 自己的只读 property：`machine_wide` / `file_scope_label`
    - **pydantic 自带的**：`model_extra` / `model_fields_set`

    这 4 个 `hasattr` 全是 True，`setattr` 却抛
    `AttributeError: property '...' has no setter` —— **未捕获，CLI 启动直接崩**。
    用户只是往配置文件里写了一个合法但只读的属性名，拿到的错误却是
    「property 没有 setter」，与「你的配置写错了」毫无关系。

    修法：`hasattr(...)` → `key in type(settings).model_fields`。
    语义正好是「只有声明字段可写」，同时挡裸属性 + property + pydantic 内置属性。
    """
    import json

    from app.cli.context import Context
    from app.config import Settings

    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "machine_wide": True,
                "file_scope_label": "x",
                "model_extra": 1,
                "model_fields_set": ["a"],
                "no_such_field": 2,
                "model": "keep-me",
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(data_dir=tmp_path, workspace=tmp_path)
    context = Context(settings=settings, data_dir=tmp_path)

    # **不崩就是主要断言**（修之前这里会抛 AttributeError）
    context.hydrate()
    # 注意断言 `context.settings` 而不是传进去的那个对象：`Context.__init__` 在给了
    # `data_dir` 时会 `model_copy` 出一个新实例，两者不是同一个。
    assert context.settings.model == "keep-me", "合法的声明字段应该被写进去"


def test_the_model_copy_scanner_is_not_vacuous():
    """防空转：`_model_copy_literals` 至少要扫到几处。

    与 `assert calls` / `assert len(hermes) >= 70` 同一个习惯：
    **源码扫描类断言一旦扫不到东西，就会永远绿。**
    """
    total = 0
    for path in APP.rglob("*.py"):
        total += len(_model_copy_literals(path.read_text(encoding="utf-8", errors="ignore")))
    # 阈值是 1 而不是 3：本轮删掉 4 个死写入之后，字面量 model_copy(update={...}) 本来就
    # 只剩少数几处。这条守的是「扫描还活着」，不是「写入点有多少」。
    assert total >= 1, f"一处 model_copy(update={{...}}) 都没扫到（{total}）——正则失效了"
