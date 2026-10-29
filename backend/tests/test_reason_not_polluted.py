"""守卫：未实现工具的 `reason` 不能被跨 domain 复用（第 44 轮那个 bug 的防线）。

## 这个 bug 是什么

第 44 轮我把 `reason="同上。"` 批量换成 `kanban_attach_url` 的说明文字 ——
**而那个占位串在很多 domain 的条目上都被用过**（desktop / collab / home），
于是 **20 条**的 `reason` 变成了「要联网：`kanban_attach_url` 需要服务端下载 URL…」。

**后果比"文字错"严重**：那些 `reason` 是**分类依据** ——
所以「本机能做 0」那个结论有 20 条是基于错的数据得出的。

**判据：批量文本替换必须限定在「目标行」内，不能按值全局替换 ——
`reason="同上。"` 这种占位串天生跨 domain 复用，按值替换必然误伤。**
"""

from __future__ import annotations

from collections import defaultdict

from app.tool_catalog import HERMES_TOOLS, orbit_coverage


def test_no_reason_is_shared_across_domains():
    """**跨 domain 共享同一条 reason 就是污染信号。**

    不同 domain 的工具缺的东西几乎不可能完全一样（desktop 缺 UI 运行时、
    home 缺 HA 实例、collab 缺平台凭证）。**文字一模一样只可能是复制粘贴。**

    允许**同 domain 内**共享（例如 `ha_get_state` 与 `ha_call_service` 都缺 HA 实例）。
    """
    coverage = orbit_coverage()
    missing = [t for t in HERMES_TOOLS if t.name in set(coverage.missing)]

    by_reason: dict[str, set[str]] = defaultdict(set)
    for tool in missing:
        if tool.reason.strip():
            by_reason[tool.reason.strip()].add(tool.domain)

    cross = {
        reason[:60]: sorted(domains)
        for reason, domains in by_reason.items()
        if len(domains) > 1
    }
    assert not cross, (
        "这些 reason 被跨 domain 复用（很可能是批量替换误伤）：\n"
        + "\n".join(f"  {domains} ← {text}…" for text, domains in cross.items())
    )


def test_every_missing_tool_still_has_a_reason():
    """缺能力时不能悄悄消失——**每条未实现的都必须写清为什么**。"""
    coverage = orbit_coverage()
    missing = [t for t in HERMES_TOOLS if t.name in set(coverage.missing)]
    assert missing, "一条都不缺了——这条断言该改成别的"
    for tool in missing:
        assert tool.reason.strip(), f"{tool.name} 未实现却没写理由"


def test_no_reason_mentions_an_unrelated_tool():
    """**reason 里不该出现另一个工具的名字**——那正是污染的形状。"""
    coverage = orbit_coverage()
    missing = [t for t in HERMES_TOOLS if t.name in set(coverage.missing)]
    offenders = [
        tool.name
        for tool in missing
        if any(
            other.name in tool.reason
            for other in missing
            if other.name != tool.name
        )
    ]
    assert not offenders, (
        f"这些条目的 reason 提到了别的工具名（复制粘贴的痕迹）：{offenders}"
    )
