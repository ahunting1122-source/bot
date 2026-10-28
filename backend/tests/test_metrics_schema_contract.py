"""`shared_metrics` schema 成为**真契约**（MATRIX 15.4）。

## 之前缺的是什么

`app/metrics_schema.py` 的 v1/v2 双版本、`schema_version` 字段、单向迁移与校验器
**都是真的**（有测试）。但**唯一 importer 是 `orbit metrics schema` 这条报告命令**
——`app/metrics.py`（指标聚合）与 `app/observability_sink.py`（exporter 落点）
**都不用 schema**。

于是**校验器校验的是用户喂进来的任意 JSON，而不是本产品产出的东西**。
**一个只有校验器、没有产出方的契约，等于没有契约。**

## 判据

1. `as_schema_payload()` 的产出**真的通过** `metrics_schema.validate()`；
2. **生产路径**（`orbit monitoring metrics --json`）真的带 `schema_version`；
3. **产出方自校验**：字段一旦漂移，`as_schema_payload()` **立刻抛**——
   不是等下游发现；
4. 源码层：`run_metrics` 用 `as_schema_payload` 而不是 `as_dict`。

## 边界（不要混淆）

**形状对 ≠ 数值对。** 这里验的是契约（字段名/类型/`available` 与 `value` 的一致性），
数值正确性由 `collect()` 的测试管。
"""

from __future__ import annotations

import pathlib

import pytest


@pytest.fixture()
def collected(tmp_path):
    from app import metrics
    from app.config import Settings
    from app.main import migrate
    from app.storage import Store

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    return metrics, metrics.collect(store, settings), settings


def test_the_produced_payload_passes_the_schema(collected):
    """**核心判据**：本产品产出的载荷必须通过自己的 schema 校验。"""
    from app import metrics_schema

    metrics, values, _settings = collected
    payload = metrics.as_schema_payload(values)

    assert payload["schema_version"] == metrics_schema.CURRENT_VERSION
    # 不抛 = 通过（`validate` 失败会抛 `SchemaError`）
    assert metrics_schema.validate(payload) in metrics_schema.SUPPORTED_VERSIONS
    assert payload["metrics"], "载荷里一个指标都没有——那这条测试证明不了什么"


def test_every_available_metric_has_a_numeric_value(collected):
    """`available` 为真就必须有数字 `value`；为假就必须有说明。

    这两条是 schema 的硬要求，也是**最容易在漂移时被破坏**的两条。
    """
    metrics, values, _settings = collected
    payload = metrics.as_schema_payload(values)

    for name, item in payload["metrics"].items():
        if item["available"]:
            assert isinstance(item.get("value"), (int, float)), f"{name} 可用却没有数字 value"
            assert not isinstance(item.get("value"), bool), f"{name} 的 value 是 bool"
        else:
            assert item.get("reason"), f"{name} 不可用却没写原因——空着等于没说"


def test_the_producer_self_validates(collected, monkeypatch):
    """**产出方自校验**：字段一旦漂移，产出时**立刻抛**。

    这条是 15.4 的要害：校验器只有接在**产出方**身上才是契约；
    只接在「用户喂 JSON」那条路上，它就只是个摆设。
    """
    from app import metrics_schema

    metrics, values, _settings = collected

    # 把校验器换成「必炸」，证明 `as_schema_payload` 真的在产出时调了它
    def _boom(_payload):
        raise metrics_schema.SchemaError("契约漂移")

    monkeypatch.setattr(metrics_schema, "validate", _boom)
    with pytest.raises(metrics_schema.SchemaError):
        metrics.as_schema_payload(values)


def test_the_cli_json_payload_carries_the_schema_version(tmp_path):
    """**生产路径**：`orbit monitoring metrics --json` 真的带 `schema_version`。"""
    import io
    import json
    from contextlib import redirect_stderr, redirect_stdout

    from app.cli.main import main as cli_main
    from app.config import Settings
    from app.main import migrate

    settings = Settings(data_dir=tmp_path / "d2", workspace=tmp_path / "f2")
    settings.prepare()
    migrate(settings)

    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = cli_main(["--json", "--data-dir", str(settings.data_dir), "monitoring", "metrics"])
    except SystemExit as exc:
        code = int(exc.code or 0)
    assert code == 0, f"命令失败：{err.getvalue()[:300]}"

    payload = json.loads(out.getvalue())
    assert payload.get("schema_version") == 2, (
        f"`--json` 输出没带 schema_version——契约又只存在于报告命令里了：{payload.keys()}"
    )
    assert payload.get("metrics"), "载荷里没有指标"


def test_run_metrics_uses_the_schema_producer():
    """**防回归**：`run_metrics` 必须用 `as_schema_payload`，不能用回 `as_dict`。

    15.4 的虚报形状是「校验器写好了、产出方不用它」——
    只验 `as_schema_payload` 能用是不够的，必须钉住生产路径调用它。
    """
    from app.cli.commands import ui as ui_module

    source = pathlib.Path(ui_module.__file__).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    assert "as_schema_payload(" in code, (
        "`run_metrics` 不再走 schema 产出了——`metrics_schema` 又变成只有报告命令用的摆设"
    )
