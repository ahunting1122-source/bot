"""`auxiliary.summary` 槽位真的被摘要器用上（MATRIX 4.9）。

## 之前缺的是什么

`app/aux_slots.py` 定义了 **18 个** `auxiliary.*` 槽位，`resolve_slot()` 也能正确解析
（写 `settings.json` 的 `auxiliary.summary = {provider: openai, model: gpt-4o-mini}`
→ `inherited=False, source=slot`）。**但全仓没有任何生产代码调用它。**

于是用户配了「摘要用便宜模型」，**摘要仍然跑主模型，而且没有任何提示**。
18 个槽位里 `summary` 最该接：摘要是后台高频调用，用贵模型纯属浪费。

## 三条判据

1. **没配 → 逐字退回原有行为**（新机制必须先是「能关」）；
2. **配了且拿得到密钥 → 真的用槽位的模型与超时**；
3. **配了但拿不到密钥 → 警告并退回主模型**，不造一个必然 401 的客户端。
   **「配了却不用」比「不配」更让人困惑，所以必须说出来。**
"""

from __future__ import annotations

import json


def _service(tmp_path, auxiliary: dict | None):
    from app.config import Settings
    from app.storage import Store
    from app.summaries import SummaryService

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files", model="deepseek-chat")
    settings.prepare()
    if auxiliary is not None:
        (settings.data_dir / "settings.json").write_text(
            json.dumps({"auxiliary": auxiliary}), encoding="utf-8"
        )
    store = Store(settings.data_dir / "app.sqlite")
    return SummaryService(settings, store), settings


def test_without_configuration_the_main_model_is_used(tmp_path):
    """**判据 ①**：没配槽位 → `_slot_client()` 返回 `None`，走主模型（既有行为不变）。"""
    service, _settings = _service(tmp_path, None)
    assert service._slot_client() is None


def test_a_configured_slot_is_really_used(tmp_path, monkeypatch):
    """**判据 ②**：配了槽位且能拿到密钥 → 真的用槽位的模型与超时。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    service, _settings = _service(
        tmp_path,
        {"summary": {"provider": "openai", "model": "gpt-4o-mini", "timeout": 15}},
    )

    client = service._slot_client()

    assert client is not None, "配了槽位却没用上——18 个槽位还是死的"
    assert client.model_name == "gpt-4o-mini", f"用的不是槽位模型：{client.model_name}"
    assert client.request_timeout == 15, "超时没听槽位的（原来写死 60s）"


def test_a_configured_slot_without_a_key_falls_back_and_says_so(tmp_path, monkeypatch, caplog):
    """**判据 ③**：配了但没密钥 → 退回主模型，**并且必须说出来**。

    「配了却不用」比「不配」更让人困惑：用户以为自己省了钱，其实没有。
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    service, _settings = _service(
        tmp_path, {"summary": {"provider": "openai", "model": "gpt-4o-mini"}}
    )

    with caplog.at_level("WARNING"):
        client = service._slot_client()

    assert client is None, "没密钥却造了客户端——那必然 401"
    messages = [record.getMessage() for record in caplog.records]
    assert any("auxiliary.summary" in message for message in messages), (
        f"没有警告日志——「配了却不用」用户永远不会知道：{messages}"
    )


def test_the_summary_service_really_consults_the_slot():
    """**防回归**：`_model()` 必须调 `_slot_client()`。

    4.9 的虚报形状是「机制写好了、`grep` 零命中」——
    只验 `resolve_slot` 能用是不够的，必须钉住生产路径调了它。
    """
    import pathlib

    from app import summaries as summaries_module

    source = pathlib.Path(summaries_module.__file__).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    assert "self._slot_client()" in code, (
        "`SummaryService._model()` 不再查槽位了——`auxiliary.*` 又变回零消费者"
    )
    assert "aux_slots.resolve_slot(" in code, "没有调 `resolve_slot`，槽位解析又是死接口"


def test_a_foreign_main_key_is_never_used_for_the_slot(tmp_path, monkeypatch, caplog):
    """**真 bug 的回归测试**：主模型有钥匙 + 槽位换了一家 → **不能拿主钥匙去撞 401**。

    ## 这是一个实现与承诺相反的 bug

    第一版直接调 providers.resolve_api_key(profile, settings)，而它第一步是

    `python
    key = _secret_value(getattr(settings, "model_api_key", None))
    if key: return key          # ← 与 profile 无关，非空就返回
    `

    于是「主模型 deepseek、槽位配 openai」时拿到的是 **deepseek 的钥匙**，
    if not key 那道闸门**根本不触发**：不告警、照造客户端，然后必然 401。
    **docstring 里承诺的「不造一个必然 401 的客户端」，实现恰好相反。**

    ## 为什么这条测试之前不存在

    _service 建 Settings(..., model="deepseek-chat") **不设 model_api_key**，
    主 key 恒空 → 走环境变量 → 绿。**危险场景从未被构造。**

    而它恰恰是槽位功能的主要用法：**用户配槽位就是为了换一家便宜的。**
    """
    from pydantic import SecretStr

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    service, settings = _service(
        tmp_path, {"summary": {"provider": "openai", "model": "gpt-4o-mini"}}
    )
    # **主模型有钥匙**（deepseek 的），槽位要的是 openai 的
    settings.model_api_key = SecretStr("sk-deepseek-key")

    with caplog.at_level("WARNING"):
        client = service._slot_client()

    assert client is None, (
        "拿主模型的钥匙造了 openai 客户端——请求必然 401，而用户只会看到一个看不懂的错误"
    )
    messages = [record.getMessage() for record in caplog.records]
    assert any("auxiliary.summary" in message for message in messages), (
        f"没告警——用户不知道槽位没生效：{messages}"
    )
