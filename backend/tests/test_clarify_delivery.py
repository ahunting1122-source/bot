"""`clarify` 的投递真的走 `send_clarify`（MATRIX 5.13 的验收判据）。

## 之前缺的是什么

`render_choices()`（≤5 个出按钮、>5 个降级编号文本）与 `send_clarify()`
（有 `send_buttons` 走按钮、遇 `NotSupported` 退回文本）**都写好了、都测过**，
但唯一调用方是 `peer_extra.py` 的手动演示入口——`clarify` 工具本身从没经过它们。
**所以「≤5 个出按钮」这条规则在真实对话里从没生效过。**

本轮加了 `app/clarify_delivery.py` 作为进程级投递通道，`clarify_payload()` 投递时读它。

## 判据
1. **没配通道**：载荷里没有 `delivery`，行为与之前完全一致（安全默认）；
2. **配了有按钮的平台**：≤5 个选项**真的出按钮**（`used == "buttons"`）；
3. **配了没按钮的平台**：降级为编号文本，**正文不能丢**（`used == "text"` 且文本里有全部选项）。
"""

from __future__ import annotations

import pytest

from app import clarify_delivery, platforms
from app.tools import clarify_payload


class _ButtonAdapter(platforms.ConsoleAdapter):
    """一个有按钮能力的假适配器（控制台本身没有按钮）。

    `send_buttons` 必须返回 **`SendResult`**（`send_clarify` 读它的 `.ok` /
    `.message_id` / `.error`），返回字符串会被当成没有这些属性而报错。
    """

    platform = "fake_buttons"

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[str, tuple[str, ...]]] = []

    def send_buttons(self, channel, text, buttons, **_kwargs):
        self.sent.append((text, tuple(buttons)))
        return platforms.SendResult(
            ok=True, platform="fake_buttons", channel=channel, message_id="msg-buttons"
        )


@pytest.fixture(autouse=True)
def _clean():
    clarify_delivery.reset()
    yield
    clarify_delivery.reset()


def _args(count: int, *, allow_free_text: bool = True) -> dict:
    return {
        "question": "选哪个方案",
        "options": [{"label": f"选项{i}"} for i in range(1, count + 1)],
        "allow_free_text": allow_free_text,
    }


def test_without_a_channel_behaviour_is_unchanged():
    """没配通道 → 载荷里没有 `delivery`，与接线前完全一致。"""
    payload = clarify_payload(_args(2))
    assert payload["status"] == "needs_user"
    assert "delivery" not in payload
    # 编号文本仍然在 message 里——任何前端都能渲染
    assert "1) 选项1" in payload["message"]
    assert "2) 选项2" in payload["message"]


def test_five_or_fewer_options_really_produce_buttons():
    """**≤5 个选项必须真的出按钮**——这是 5.13 的核心承诺。"""
    adapter = _ButtonAdapter()
    clarify_delivery.configure(adapter, "chan-1")

    payload = clarify_payload(_args(3))
    delivery = payload["delivery"]

    assert delivery["delivered"] is True
    assert delivery["used"] == "buttons", f"3 个选项应该出按钮，实际 {delivery}"
    assert delivery["rendered"].degraded is False
    assert len(adapter.sent) == 1, "按钮消息没真的发出去"
    text, buttons = adapter.sent[0]
    # 按钮是 `(label, callback_id)` 对，回调 id 形如 `cl:<clarify_id>:<n>`——
    # 这个格式是平台侧回传时认选项的依据，值得钉住。
    labels = [label for label, _ in buttons]
    assert labels == ["选项1", "选项2", "选项3"]
    for index, (_, callback) in enumerate(buttons, start=1):
        assert callback.startswith("cl:"), callback
        assert callback.endswith(f":{index}"), callback
    # **正文不能丢**：按钮平台也要同时带同一份编号文本
    assert "需要你决定" in text


def test_more_than_five_options_degrade_to_numbered_text():
    """>5 个选项必须降级为编号文本（按钮放不下），且**正文里要有全部选项**。"""
    adapter = _ButtonAdapter()
    clarify_delivery.configure(adapter, "chan-2")

    payload = clarify_payload(_args(6))
    delivery = payload["delivery"]

    assert delivery["delivered"] is True
    assert delivery["used"] == "text", f"6 个选项应降级为文本，实际 {delivery}"
    assert delivery["rendered"].degraded is True
    for index in range(1, 7):
        assert f"{index}) 选项{index}" in payload["message"]


def test_adapter_without_buttons_degrades_but_keeps_the_text():
    """没有按钮能力的平台：降级为文本，**消息一定发得出去**。"""
    clarify_delivery.configure(platforms.ConsoleAdapter(), "cli")

    payload = clarify_payload(_args(2))
    delivery = payload["delivery"]

    assert delivery["delivered"] is True
    assert delivery["used"] == "text"
    assert "没有按钮" in str(delivery["rendered"].reason)
    assert "1) 选项1" in payload["message"]


def test_degraded_text_actually_carries_the_original_question():
    """**「发出去了」和「发出的是原问题」是两个断言。**

    监视子agent 的原话：降级成文本时如果内容丢了，测试仍然会绿——因为
    `delivered is True` 只证明"调到了平台"，不证明"用户看到了问题"。
    所以这条**抓住真正发出去的那段文本**，逐项核对：问题原文、每个选项、
    以及"可以自由作答"的说明都在里面。
    """
    sent: list[str] = []

    class _RecordingText(platforms.ConsoleAdapter):
        """没有按钮能力的平台，但把真正发出去的文本记下来。"""

        platform = "recording"

        def send_text(self, channel, text, **_kwargs):
            sent.append(text)
            return platforms.SendResult(
                ok=True, platform="recording", channel=channel, message_id="m1"
            )

    clarify_delivery.configure(_RecordingText(), "chan-r")
    # 返回值这里故意不看——**这条只认"真正发出去的文本"**。
    # 载荷对不对由上面几条断言管；两者混在一起会让人以为验了投递。
    clarify_payload(_args(3))

    assert sent, "降级路径没有真的调用 send_text"
    text = sent[-1]
    # ① 问题原文在里面（不是"一条提示"，是用户要回答的那个问题）
    assert "选哪个方案" in text, f"发出去的文本丢了问题原文：{text!r}"
    # ② 每个选项都在，且带序号（用户要能按序号回）
    for index in range(1, 4):
        assert f"{index}) 选项{index}" in text, f"发出去的文本缺选项 {index}：{text!r}"
    # ③ 自由作答的说明也在（否则用户以为只能点按钮）
    assert "回复序号" in text, f"发出去的文本缺作答说明：{text!r}"


def test_degraded_text_marks_a_forced_choice():
    """`allow_free_text=False` 时，发出去的文本必须明说「只能选」。"""
    sent: list[str] = []

    class _RecordingText(platforms.ConsoleAdapter):
        platform = "recording2"

        def send_text(self, channel, text, **_kwargs):
            sent.append(text)
            return platforms.SendResult(
                ok=True, platform="recording2", channel=channel, message_id="m2"
            )

    clarify_delivery.configure(_RecordingText(), "chan-r2")
    clarify_payload(_args(2, allow_free_text=False))

    text = sent[-1]
    assert "只能" in text and "1-2" in text, (
        f"强制选择时文本没写清只能选几项：{text!r}"
    )


def test_delivery_failure_does_not_break_the_question():
    """投递炸了也不能让提问失败——用户连问题都看不到比"没发按钮"糟得多。"""

    class _Broken(platforms.ConsoleAdapter):
        platform = "broken"

        def send_buttons(self, *_a, **_k):
            raise RuntimeError("平台挂了")

        def send_text(self, *_a, **_k):
            raise RuntimeError("平台挂了")

    clarify_delivery.configure(_Broken(), "chan-3")
    payload = clarify_payload(_args(2))

    # 提问本身照常返回
    assert payload["status"] == "needs_user"
    assert "1) 选项1" in payload["message"]
    # 投递结果如实记下失败
    assert payload["delivery"]["delivered"] is False
    assert "RuntimeError" in payload["delivery"]["error"]


def test_describe_reports_the_channel():
    """`describe()` 要能自曝通道状态（诊断用）。"""
    assert clarify_delivery.describe()["configured"] is False
    clarify_delivery.configure(platforms.ConsoleAdapter(), "cli")
    info = clarify_delivery.describe()
    assert info["configured"] is True
    assert info["channel"] == "cli"
