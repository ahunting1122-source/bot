"""Click the complete Orbit interface against an isolated real backend.

Run after ``npm --prefix frontend run build``:
    backend/.venv/Scripts/python.exe scripts/ui_smoke.py

Only model responses, browser destinations, and GitHub transport are simulated.
FastAPI routes, persistence, settings validation, LangGraph, SSE and React are real.
No running user service, profile, configuration, credentials or history is touched.
"""

import argparse
import asyncio
import json
import re
import socket
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import uvicorn
from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from ui_fixture import MARKDOWN, UIFixture  # noqa: E402


class Checks:
    def __init__(self, browser, fixture, artifacts):
        self.browser, self.fixture, self.artifacts = browser, fixture, artifacts
        self.results = []
        self.errors = []

    async def page(self):
        page = await self.browser.new_page(viewport={"width": 1440, "height": 1000})
        page.set_default_timeout(7000)
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        await page.goto(self.fixture.base_url)
        await expect(page.get_by_label("给 Orbit 发消息")).to_be_visible()
        return page

    @staticmethod
    def where(error):
        """把异常定位到 ui_smoke.py 的具体行——裸 assert 的 str() 是空的，没法排查。"""
        line = ""
        trace = error.__traceback__
        while trace is not None:
            if trace.tb_frame.f_code.co_filename.endswith("ui_smoke.py"):
                line = f"ui_smoke.py:{trace.tb_lineno}"
            trace = trace.tb_next
        text = f"{type(error).__name__}: {error}".strip()
        return f"{text} @ {line}" if line else text

    async def run(self, name, callback):
        page = await self.page()
        try:
            details = await callback(page)
            self.results.append({"name": name, "status": "passed", "evidence": details or {}})
            print(f"PASS {name}", flush=True)
        except Exception as error:
            shot = self.artifacts / f"failure-{len(self.results) + 1}.png"
            await page.screenshot(path=str(shot), full_page=True)
            detail = self.where(error)
            self.results.append({"name": name, "status": "failed", "error": detail, "screenshot": str(shot)})
            print(f"FAIL {name}: {detail}", flush=True)
        finally:
            await page.context.set_offline(False)
            self.fixture.model.stream_release.set()
            self.fixture.model.stream_finish_release.set()
            self.fixture.summary_model.release.set()
            # A failed assertion must not leave a paused fake model blocking later cases.
            tasks = await (await page.request.get(self.fixture.base_url + "/api/tasks")).json()
            for task in tasks:
                if task["status"] in {"PENDING", "RUNNING"}:
                    await page.request.post(self.fixture.base_url + f"/api/tasks/{task['id']}/cancel")
            await page.close()

    async def nav(self, page, name):
        button = page.get_by_role("navigation", name="主导航").get_by_role("button", name=name, exact=True)
        toggle = page.get_by_role("button", name="打开导航")
        if await toggle.is_visible() and not await page.get_by_role("button", name="关闭导航").count():
            await toggle.click()
        await button.click()

    async def settings(self, page):
        await self.nav(page, "连接与设置")
        await expect(page.get_by_role("heading", name="连接与设置", exact=True)).to_be_visible()

    async def memories(self, page):
        await self.nav(page, "对话摘要")
        await expect(page.get_by_role("heading", name="对话摘要")).to_be_visible()
        await expect(page.get_by_role("button", name="刷新列表", exact=True)).to_be_enabled()

    async def vault(self, page):
        """密钥存储：明文开关、站点账号增删、登录状态提示。"""
        await self.nav(page, "密钥存储")
        await expect(page.get_by_role("heading", name="密钥存储", exact=True)).to_be_visible()
        for section in ("密钥", "站点账号", "登录状态"):
            await expect(page.get_by_role("heading", name=section, exact=True)).to_be_visible()

        value = "smoke-key-abc123"
        key_row = page.locator(".vault-row").filter(has_text="模型密钥")
        await key_row.get_by_label("模型密钥 新值").fill(value)
        await key_row.get_by_role("button", name="保存", exact=True).click()
        await expect(page.get_by_role("status")).to_contain_text("模型密钥 已保存")
        await expect(key_row.locator("code")).to_have_text(value)

        await page.get_by_role("button", name=re.compile("全部隐藏")).click()
        await expect(key_row.locator("code")).not_to_have_text(value)
        await page.get_by_role("button", name=re.compile("显示全部明文")).click()
        await expect(key_row.locator("code")).to_have_text(value)

        await page.get_by_label("站点主机名").fill("vault.example")
        await page.get_by_label("站点用户名").fill("alice")
        await page.get_by_label("站点密码").fill("s3cret")
        await page.get_by_role("button", name="添加", exact=True).click()
        await expect(page.get_by_role("status")).to_contain_text("已保存 vault.example 的账号")
        site_row = page.locator(".vault-row").filter(has_text="vault.example")
        await expect(site_row.locator("code")).to_contain_text("alice / s3cret")

        await site_row.get_by_role("button", name="删除", exact=True).click()
        await expect(page.get_by_role("status")).to_contain_text("已删除 vault.example 的账号")
        await expect(page.locator(".vault-row").filter(has_text="vault.example")).to_have_count(0)

        # 隔离环境里没有真实的可见浏览器，登录状态应给出可操作的提示而不是空白。
        await expect(page.get_by_role("heading", name="登录状态")).to_be_visible()
        await expect(page.get_by_text(re.compile("打开浏览器登录"))).to_be_visible()

    async def send(self, page, text, keyboard=False):
        await page.get_by_label("给 Orbit 发消息").fill(text)
        if keyboard:
            await page.get_by_label("给 Orbit 发消息").press("Control+Enter")
        else:
            await page.get_by_role("button", name="发送消息", exact=True).click()

    async def new_chat(self, page):
        await self.nav(page, "新对话")
        await expect(page.get_by_label("给 Orbit 发消息")).to_have_value("")

    async def wait_reply(self, page, text):
        await expect(page.locator("article.message.assistant").last).to_contain_text(text)
        await expect(page.get_by_role("button", name="停止回复", exact=True)).to_have_count(0)

    async def navigation(self, page):
        await expect(page.get_by_role("button", name="发送消息", exact=True)).to_be_disabled()
        await page.get_by_label("给 Orbit 发消息").fill("   ")
        await expect(page.get_by_role("button", name="发送消息", exact=True)).to_be_disabled()
        await page.get_by_role("button", name="读一份资料，整理成笔记").click()
        await expect(page.get_by_label("给 Orbit 发消息")).to_have_value(re.compile("工作目录"))
        for label in ("看看项目最近在讨论什么", "把整理好的内容同步到 Notion"):
            await page.get_by_role("button", name=label).click()
            await expect(page.get_by_role("heading", name="连接与设置", exact=True)).to_be_visible()
            await page.get_by_role("button", name="返回对话", exact=True).click()
        for label in ("管理连接", "连接 GitHub", "连接 Notion", "本地工作空间"):
            await page.get_by_role("button", name=re.compile(label)).click()
            await expect(page.get_by_role("heading", name="连接与设置", exact=True)).to_be_visible()
            await page.get_by_role("button", name="返回对话", exact=True).click()
        await page.locator(".model-button").click()
        await self.nav(page, "工作台")
        await self.memories(page)
        await page.get_by_role("button", name="返回对话", exact=True).click()
        await self.nav(page, "资料整理")
        await expect(page.get_by_role("region", name="对话内容")).to_contain_text("决定使用 Markdown")
        await self.nav(page, "资料整理")
        await expect(page.get_by_role("region", name="对话内容")).to_contain_text("决定使用 Markdown")
        await page.get_by_role("link", name=re.compile("orbit")).click()
        await expect(page.get_by_label("给 Orbit 发消息")).to_have_value("")

    async def settings_forms(self, page):
        await self.settings(page)
        model = page.locator("form.settings-section").filter(has=page.get_by_role("heading", name="模型服务"))
        browser = page.locator("form.settings-section").filter(has=page.get_by_role("heading", name="通用网页"))
        github = page.locator("form.settings-section").filter(has=page.get_by_role("heading", name="GitHub", exact=True))
        notion = page.locator("form.settings-section").filter(has=page.get_by_role("heading", name="Notion 页面"))
        await page.get_by_label("模型名称", exact=True).fill("")
        await page.get_by_role("button", name="保存模型设置", exact=True).click()
        assert not await page.get_by_label("模型名称", exact=True).evaluate("el => el.checkValidity()")
        await page.get_by_label("模型名称", exact=True).fill("ui-fixture")
        await page.get_by_label("API 地址", exact=True).fill("not-a-url")
        await page.get_by_role("button", name="保存模型设置", exact=True).click()
        assert not await page.get_by_label("API 地址", exact=True).evaluate("el => el.checkValidity()")
        await page.get_by_label("API 地址", exact=True).fill(self.fixture.base_url + "/fixture-model")
        await page.get_by_label(re.compile("API Key")).fill("ui-fixture-replacement")
        await expect(page.get_by_role("button", name="检查模型连接", exact=True)).to_be_disabled()
        await page.get_by_role("button", name="保存模型设置", exact=True).click()
        await expect(model.get_by_role("status")).to_contain_text("模型设置已保存")
        await expect(page.get_by_label(re.compile("API Key"))).to_have_value("")
        await page.get_by_role("button", name="检查模型连接", exact=True).click()
        await expect(model.get_by_role("status")).to_contain_text("连接检查通过")
        await page.get_by_label("模型名称", exact=True).fill("fixture-fail")
        await page.get_by_role("button", name="保存模型设置", exact=True).click()
        await expect(model.get_by_role("status")).to_contain_text("已保存")
        await page.get_by_role("button", name="检查模型连接", exact=True).click()
        await expect(model.get_by_role("alert")).to_contain_text("HTTP 401")
        await page.get_by_label("模型名称", exact=True).fill("ui-fixture")
        await expect(model.get_by_role("alert")).to_have_count(0)
        await page.get_by_role("button", name="保存模型设置", exact=True).click()
        await expect(model.get_by_role("status")).to_contain_text("已保存")
        await page.get_by_role("button", name="检查模型连接", exact=True).click()
        await expect(model.get_by_role("status")).to_contain_text("连接检查通过")
        assert len(self.fixture.model_checks) == 3
        await page.get_by_label("仓库", exact=True).fill("invalid repository")
        await page.get_by_role("button", name="保存 GitHub 设置", exact=True).click()
        await expect(github.get_by_role("alert")).to_be_visible()
        await page.get_by_label("仓库", exact=True).fill("fixture/docs")
        await page.get_by_label(re.compile("访问 Token")).fill("ui-fixture-github-token")
        await page.get_by_role("button", name="保存 GitHub 设置", exact=True).click()
        await expect(github.get_by_role("status")).to_contain_text("设置已保存")
        await expect(page.get_by_label(re.compile("访问 Token"))).to_have_value("")
        await expect(page.get_by_role("button", name="打开浏览器登录", exact=True)).to_be_disabled()
        await page.get_by_label("打开的网址", exact=True).fill("file:///fixture")
        await page.get_by_role("button", name="保存网页设置", exact=True).click()
        assert not await page.get_by_label("打开的网址", exact=True).evaluate("el => el.checkValidity()")
        await page.get_by_label("打开的网址", exact=True).fill("https://www.bilibili.com/")
        await page.get_by_role("button", name="保存网页设置", exact=True).click()
        await expect(browser.get_by_role("status")).to_contain_text("设置已保存")
        await page.get_by_role("button", name="打开浏览器登录", exact=True).click()
        await expect(browser.get_by_role("status")).to_contain_text("已打开可见浏览器")
        assert self.fixture.browser_calls[-1] == "https://www.bilibili.com/"
        await page.get_by_label("打开的网址", exact=True).fill("https://www.bilibili.com/fail")
        await page.get_by_role("button", name="打开浏览器登录", exact=True).click()
        await expect(browser.get_by_role("alert")).to_contain_text("暂时无法打开")
        await page.get_by_label("打开的网址", exact=True).fill("https://www.bilibili.com/video")
        await page.get_by_role("button", name="打开浏览器登录", exact=True).click()
        await expect(browser.get_by_role("status")).to_contain_text("已打开可见浏览器")
        assert self.fixture.browser_calls[-1] == "https://www.bilibili.com/video"
        await expect(page.get_by_role("button", name="打开 Notion 登录", exact=True)).to_be_disabled()
        await page.get_by_label("可编辑页面链接", exact=True).fill("https://www.bilibili.com/")
        await page.get_by_role("button", name="保存 Notion 设置", exact=True).click()
        await expect(notion.get_by_role("alert")).to_be_visible()
        await page.get_by_label("可编辑页面链接", exact=True).fill("https://www.notion.so/fixture-note")
        await page.get_by_role("button", name="保存 Notion 设置", exact=True).click()
        await expect(notion.get_by_role("status")).to_contain_text("设置已保存")
        await page.get_by_role("button", name="打开 Notion 登录", exact=True).click()
        await expect(notion.get_by_role("status")).to_contain_text("已打开可见浏览器")
        assert self.fixture.browser_calls[-1] == "https://www.notion.so/fixture-note"
        await page.reload()
        await self.settings(page)
        await expect(page.get_by_label("仓库", exact=True)).to_have_value("fixture/docs")
        await expect(page.get_by_label("打开的网址", exact=True)).to_have_value("https://www.bilibili.com/video")
        await expect(page.get_by_label("可编辑页面链接", exact=True)).to_have_value("https://www.notion.so/fixture-note")
        await page.get_by_role("button", name="返回对话", exact=True).click()
        for title, value in [("看看项目最近在讨论什么", "GitHub"), ("把整理好的内容同步到 Notion", "Notion")]:
            await page.get_by_role("button", name=title).click()
            await expect(page.get_by_label("给 Orbit 发消息")).to_have_value(re.compile(value))

    async def summaries(self, page):
        await self.memories(page)
        await expect(page.get_by_role("button", name="生成摘要：尚未开始", exact=True)).to_be_disabled()
        await page.get_by_label("搜索对话摘要").fill("不存在的摘要关键词")
        await expect(page.get_by_text("没有找到匹配的对话，可以换个关键词。")).to_be_visible()
        await page.get_by_role("button", name="清空摘要搜索").click()
        await page.get_by_label("搜索对话摘要").fill("每周")
        await expect(page.locator(".conversation-summary")).to_have_count(1)
        await page.get_by_role("button", name="清空摘要搜索").click()
        await page.get_by_role("button", name="刷新列表", exact=True).click()
        await expect(page.get_by_role("button", name="刷新列表", exact=True)).to_be_enabled()
        await page.get_by_role("button", name="打开对话：资料整理", exact=True).click()
        await expect(page.get_by_role("region", name="对话内容")).to_contain_text("决定使用 Markdown")
        await self.memories(page)
        await page.get_by_role("button", name="打开对话：资料整理", exact=True).click()
        await expect(page.get_by_role("region", name="对话内容")).to_contain_text("决定使用 Markdown")
        await self.memories(page)
        await page.get_by_role("button", name="打开对话：登录讨论", exact=True).focus()
        await page.keyboard.press("Enter")
        await expect(page.get_by_role("region", name="对话内容")).to_contain_text("需要人工确认网页登录")
        await self.memories(page)
        self.fixture.summary_model.release.clear()
        await page.get_by_role("button", name="重新生成摘要：资料整理", exact=True).click()
        await expect(page.get_by_role("button", name="重新生成摘要：资料整理", exact=True)).to_be_disabled()
        self.fixture.summary_model.release.set()
        await expect(page.get_by_role("button", name="打开对话：资料整理回顾", exact=True)).to_be_visible(timeout=10000)
        self.fixture.summary_model.fail_next = True
        await page.get_by_role("button", name="生成摘要：登录讨论", exact=True).click()
        card = page.locator(".conversation-summary").filter(has=page.get_by_role("button", name="打开对话：登录讨论", exact=True))
        await expect(card.get_by_text("生成失败", exact=True)).to_be_visible(timeout=10000)
        await card.get_by_role("button", name="生成摘要：登录讨论", exact=True).click()
        await expect(page.get_by_role("button", name="打开对话：登录讨论回顾", exact=True)).to_be_visible(timeout=10000)
        await page.get_by_role("button", name="打开对话：登录讨论回顾", exact=True).focus()
        await page.keyboard.press("Space")
        await expect(page.get_by_role("region", name="对话内容")).to_contain_text("需要人工确认网页登录")
        await self.memories(page)
        summary_path = f"**/api/conversations/{self.fixture.ids['ready']}/summary"

        async def fail_rebuild(route):
            await route.fulfill(status=503, json={"detail": "隔离摘要重生成请求失败"})

        await page.route(summary_path, fail_rebuild)
        await page.get_by_role("button", name="重新生成摘要：资料整理回顾", exact=True).click()
        await expect(page.get_by_role("alert")).to_contain_text("隔离摘要重生成请求失败")
        await page.unroute(summary_path, fail_rebuild)
        await page.get_by_role("button", name="重新生成摘要：资料整理回顾", exact=True).click()
        await expect(page.get_by_role("alert")).to_have_count(0)
        await expect(page.get_by_role("button", name="重新生成摘要：资料整理回顾", exact=True)).to_be_enabled(timeout=10000)
        await page.get_by_role("button", name="返回对话", exact=True).click()
        await expect(page.get_by_label("给 Orbit 发消息")).to_be_visible()

    async def summary_retry_button(self, page):
        state = {"fail": True}

        async def fail_once(route):
            if state["fail"]:
                state["fail"] = False
                await route.fulfill(status=503, json={"detail": "隔离网络故障"})
            else:
                await route.continue_()

        await page.route("**/api/conversations", fail_once)
        await self.nav(page, "对话摘要")
        await expect(page.get_by_role("alert")).to_contain_text("隔离网络故障")
        await page.get_by_role("button", name="重试", exact=True).click()
        await expect(page.get_by_role("alert")).to_have_count(0)
        await expect(page.locator(".conversation-summary").first).to_be_visible()

    async def chat_stream_markdown(self, page):
        self.fixture.model.stream_release.clear()
        await self.send(page, "UI_STREAM 请展示 Markdown 流式正文")
        live = page.locator(".live-reply")
        await expect(live).to_contain_text("流式 Markdown 验证")
        await expect(live.get_by_role("heading", name="流式 Markdown 验证")).to_be_visible()
        assert "MARKDOWN_DONE" not in await live.inner_text()
        await page.get_by_role("navigation", name="主导航").get_by_role("button", name=re.compile("^资料整理")).click()
        await expect(page.get_by_role("region", name="对话内容")).to_contain_text("决定使用 Markdown")
        await expect(page.locator(".live-reply")).to_have_count(0)
        await self.nav(page, "UI_STREAM 请展示 Markdown 流式正文")
        await expect(page.locator(".live-reply")).to_contain_text("流式 Markdown 验证")
        self.fixture.model.stream_release.set()
        await self.wait_reply(page, "MARKDOWN_DONE")
        final = page.locator("article.message.assistant").last
        await expect(final.locator("strong")).to_have_text("加粗内容")
        await expect(final.locator("table tbody tr")).to_have_count(1)
        await expect(final.locator("pre code")).to_have_text('print("markdown-ok")\n')
        await expect(final.locator("blockquote")).to_contain_text("这是一段引用")
        await expect(final.locator("ul li")).to_have_count(2)
        await expect(final.locator("ol li")).to_have_count(2)
        async with page.expect_popup() as popup_info:
            await final.get_by_role("link", name="本地参考链接").click()
        popup = await popup_info.value
        await popup.wait_for_url("**/fixture-link")
        await popup.close()
        await expect(page.locator("article.message.assistant")).to_have_count(1)
        tasks = await (await page.request.get(self.fixture.base_url + "/api/tasks")).json()
        reply = next(task for task in tasks if "UI_STREAM" in task["goal"])
        events = await (await page.request.get(self.fixture.base_url + f"/api/tasks/{reply['id']}/events")).text()
        assert events.count('"event_type": "message_delta"') > 2, "流式正文应当产生多次 message_delta"
        history = await (await page.request.get(self.fixture.base_url + f"/api/conversations/{reply['conversation_id']}/messages")).json()
        # 收尾落库会对正文做 strip（去掉首尾空白），因此比对的是 strip 后的 fixture 正文。
        assert history[-1]["content"] == MARKDOWN.strip(), (
            f"落库正文应等于 fixture 正文；实际 {history[-1]['content'][:80]!r}"
        )
        await self.send(page, "下一条消息", keyboard=True)
        await self.wait_reply(page, "CHAT_DONE")

    async def chat_zero_confirmation(self, page):
        """v1 事前零确认：memory_save 直接执行，不再出现「允许并继续 / 拒绝」。

        全流程没有任何事前确认按钮出现，即为通过；若 approval 回归，这一轮既到不了
        APPROVED_DONE，下面那条 count(0) 断言也会立刻失败。
        """
        await self.send(page, "UI_ACCEPT 请记住测试偏好")
        await self.wait_reply(page, "APPROVED_DONE")
        await expect(
            page.get_by_role("button", name="允许并继续", exact=True)
        ).to_have_count(0)
        await expect(page.get_by_role("button", name="拒绝", exact=True)).to_have_count(0)
        memories = await (await page.request.get(self.fixture.base_url + "/api/memories")).json()
        assert any(item["key"] == "UI_ACCEPT" for item in memories)
        # 零确认不等于不透明：过程说明仍然可见可展开。
        await page.get_by_text("查看过程说明", exact=True).click()
        await expect(page.locator("details.thinking-summary")).to_have_attribute("open", "")
        await expect(page.locator("details.thinking-summary")).to_contain_text("直接保存")

    async def chat_file_scope(self, page):
        """对话页的文件访问级别开关：切换 → 提示联动 → 落库 → 刷新后仍在 → 切回。"""
        button = page.locator(".scope-button")
        await expect(button).to_contain_text("仅工作文件夹")
        await button.click()
        menu = page.get_by_role("listbox", name="文件访问级别")
        await expect(menu).to_be_visible()
        await expect(
            menu.get_by_role("option", name=re.compile("仅工作文件夹"))
        ).to_have_attribute("aria-selected", "true")
        await menu.get_by_role("option", name=re.compile("完全访问权限")).click()
        await expect(menu).to_have_count(0)
        await expect(button).to_contain_text("完全访问权限")
        await expect(page.locator("p.composer-hint.warn")).to_contain_text("完全访问权限已开启")
        config = await (await page.request.get(self.fixture.base_url + "/api/connectors")).json()
        assert config["file_scope"] == "machine", config
        assert config["file_root"] == "", config
        await page.reload()
        await expect(page.locator(".scope-button")).to_contain_text("完全访问权限")
        await page.locator(".scope-button").click()
        await page.get_by_role("listbox", name="文件访问级别").get_by_role(
            "option", name=re.compile("仅工作文件夹")
        ).click()
        await expect(page.locator(".scope-button")).to_contain_text("仅工作文件夹")
        config = await (await page.request.get(self.fixture.base_url + "/api/connectors")).json()
        assert config["file_scope"] == "workspace", config
        return {"scope_round_trip": True}

    async def chat_sse_reconnect(self, page):
        model = self.fixture.model
        model.stream_release.clear()
        model.stream_finish_release.clear()
        connections = []
        reconnected = asyncio.Event()
        cut = {"active": False, "cursor": 0}

        await page.evaluate("""() => {
          window.__uiSseEvents = [];
          const Original = window.EventSource;
          window.EventSource = class extends Original {
            constructor(...args) {
              super(...args);
              this.addEventListener('message', event => window.__uiSseEvents.push(JSON.parse(event.data)));
            }
          };
        }""")

        def observe_request(request):
            parsed = urlparse(request.url)
            if parsed.path.endswith("/events"):
                connections.append(int(parse_qs(parsed.query).get("after", ["0"])[0]))
                if cut["active"]:
                    reconnected.set()

        page.on("request", observe_request)
        await self.send(page, "UI_STREAM 验证断线续传")
        await expect(page.locator(".live-reply")).to_contain_text("流式 Markdown 验证")
        prefix_events = await page.evaluate("window.__uiSseEvents")
        cut["cursor"] = max(event["id"] for event in prefix_events)
        cut["active"] = True
        # End the real HTTP stream early, without sending a settled event.
        # Chromium's offline emulation does not reliably cut existing localhost sockets.
        self.fixture.sse_cut.clear()
        self.fixture.cut_sse = True
        async with asyncio.timeout(5):
            await self.fixture.sse_cut.wait()
        await expect(page.get_by_text("连接暂时中断，正在重新连接…", exact=True)).to_be_visible()
        await page.screenshot(path=str(self.artifacts / "sse-reconnecting.png"))
        async with asyncio.timeout(7):
            await reconnected.wait()
        assert connections[-1] == cut["cursor"] > connections[0], connections
        await expect(page.get_by_text("连接暂时中断，正在重新连接…", exact=True)).to_have_count(0)
        model.stream_release.set()
        live = page.locator(".live-reply")
        await expect(live.get_by_role("link", name="本地参考链接")).to_be_visible()
        assert "MARKDOWN_DONE" not in await live.inner_text()
        await expect(live.get_by_role("heading", name="流式 Markdown 验证")).to_have_count(1)
        await expect(live.locator("pre code")).to_have_text('print("markdown-ok")\n')
        model.stream_finish_release.set()
        await self.wait_reply(page, "MARKDOWN_DONE")
        events = await page.evaluate("window.__uiSseEvents")
        deltas = [event for event in events if event["event_type"] == "message_delta"]
        assert len({event["id"] for event in deltas}) == len(deltas), "A delta was delivered twice"
        assert "".join(event["payload"]["text"] for event in deltas) == MARKDOWN
        await expect(page.locator("article.message.assistant")).to_have_count(1)
        await page.wait_for_timeout(5000)
        assert connections[-1] >= cut["cursor"]
        await expect(page.get_by_text("连接暂时中断，正在重新连接…", exact=True)).to_have_count(0)
        await page.screenshot(path=str(self.artifacts / "sse-recovered.png"))
        return {"after_cursors": connections, "closed_after_event": cut["cursor"],
                "delta_count": len(deltas), "reconstructed_characters": len(MARKDOWN),
                "body_matches": True, "duplicate_deltas": False}

    async def session_recovery(self, page):
        await self.memories(page)
        statuses = []
        page.on("response", lambda response: statuses.append(
            {"path": urlparse(response.url).path, "status": response.status}
        ) if "/api/" in response.url else None)
        await page.context.add_cookies([{
            "name": "orbit_session", "value": "invalid-isolated-session",
            "url": self.fixture.base_url, "httpOnly": True, "sameSite": "Strict",
        }])
        await page.get_by_role("button", name="刷新列表", exact=True).click()
        await expect(page.get_by_role("button", name="刷新列表", exact=True)).to_be_enabled()
        await expect(page.get_by_role("alert")).to_have_count(0)
        await expect(page.locator(".conversation-summary").first).to_be_visible()
        assert {"path": "/api/conversations", "status": 401} in statuses, statuses
        assert {"path": "/api/session", "status": 200} in statuses, statuses
        assert statuses[-1] == {"path": "/api/conversations", "status": 200}, statuses
        cookies = await page.context.cookies()
        assert next(cookie for cookie in cookies if cookie["name"] == "orbit_session")["value"] != "invalid-isolated-session"
        return {"requests": statuses, "cookie_reissued": True, "visible_error": False}

    async def chat_error_close(self, page):
        async def fail_send(route):
            if route.request.method == "POST":
                await route.fulfill(status=503, json={"detail": "隔离发送失败，请重试"})
            else:
                await route.continue_()

        await page.route("**/api/conversations/*/messages", fail_send)
        await self.send(page, "发送失败保留草稿")
        await expect(page.get_by_role("alert")).to_contain_text("隔离发送失败")
        await expect(page.get_by_label("给 Orbit 发消息")).to_have_value("发送失败保留草稿")
        await page.get_by_role("button", name="关闭错误提示").click()
        await expect(page.get_by_role("alert")).to_have_count(0)
        await page.unroute("**/api/conversations/*/messages", fail_send)
        await page.get_by_role("button", name="发送消息", exact=True).click()
        await self.wait_reply(page, "CHAT_DONE")

    async def chat_clarification(self, page):
        await self.send(page, "UI_ASK 请先澄清范围")
        await expect(page.get_by_text("你希望整理哪个月份的资料？", exact=True)).to_be_visible()
        await self.send(page, "2026 年 9 月", keyboard=True)
        await self.wait_reply(page, "CLARIFY_DONE：收到补充，2026 年 9 月")
        await self.send(page, "UI_ASK 再补充一个问题")
        await expect(page.get_by_text("你希望整理哪个月份的资料？", exact=True)).to_be_visible()
        await page.get_by_role("button", name="停止回复", exact=True).click()
        await expect(page.get_by_role("button", name="重新回复", exact=True)).to_be_visible()
        tasks = await (await page.request.get(self.fixture.base_url + "/api/tasks")).json()
        stopped = next(task for task in tasks if task["goal"] == "UI_ASK 再补充一个问题")
        assert stopped["status"] == "CANCELLED"
        async with asyncio.timeout(5):
            while True:
                rows = await (await page.request.get(self.fixture.base_url + "/api/conversations")).json()
                row = next(row for row in rows if row["id"] == stopped["conversation_id"])
                if row["summary_status"] == "ready" and row["summary_message_count"] == row["message_count"]:
                    break
                await asyncio.sleep(0.1)

    async def chat_stop_retry(self, page):
        self.fixture.model.stop_started.clear()
        await self.send(page, "UI_STOP 请连续输出")
        async with asyncio.timeout(10):
            await self.fixture.model.stop_started.wait()
        await expect(page.locator(".live-reply")).to_contain_text("连续输出第 20 段")
        transcript = page.get_by_role("region", name="对话内容")
        await transcript.hover()
        await page.mouse.wheel(0, -30000)
        await expect(page.locator(".live-reply")).to_contain_text("连续输出第 25 段")
        # 读者上翻后新分片不得把它拉回底部。wheel 与渲染存在竞争，允许重试几次，
        # 但每次重试后都再等新分片落地——真被拉回底部时仍然会失败。
        pulled_back = True
        for _ in range(3):
            if await transcript.evaluate("el => el.scrollTop") < 200:
                pulled_back = False
                break
            await page.mouse.wheel(0, -30000)
            await asyncio.sleep(0.3)
        assert not pulled_back, "New chunks pulled the reader back to the bottom"
        await expect(page.get_by_role("button", name="停止回复", exact=True)).to_be_visible()
        await expect(page.get_by_role("button", name="停止回复", exact=True)).to_be_in_viewport()
        await page.get_by_role("button", name="停止回复", exact=True).click()
        await expect(page.get_by_role("button", name="停止回复", exact=True)).to_have_count(0)
        tasks = await (await page.request.get(self.fixture.base_url + "/api/tasks")).json()
        stopped = next(task for task in tasks if "UI_STOP" in task["goal"])
        assert stopped["status"] == "CANCELLED", "Stop click did not cancel the backend response"
        await expect(page.locator("article.message.assistant").last).to_contain_text("连续输出第")
        await expect(page.get_by_role("button", name="发送消息", exact=True)).to_be_disabled()
        await page.get_by_role("button", name="重新回复", exact=True).click()
        await self.wait_reply(page, "STOP_RETRY_DONE")
        await self.send(page, "停止后继续普通聊天")
        await self.wait_reply(page, "CHAT_DONE")
        await self.new_chat(page)
        await self.send(page, "UI_RETRY 模拟失败再重试")
        await expect(page.get_by_role("button", name="重试这次回复", exact=True)).to_be_visible()
        await page.get_by_role("button", name="重试这次回复", exact=True).click()
        await self.wait_reply(page, "RETRY_DONE")
        await expect(page.get_by_role("button", name="重试这次回复", exact=True)).to_have_count(0)

    async def responsive(self, page):
        for width in (320, 768, 1024, 1440):
            await page.set_viewport_size({"width": width, "height": 1000})
            for view in ("工作台", "连接与设置", "对话摘要"):
                await self.nav(page, view)
                await expect(page.locator("main").first).to_be_visible()
                await page.evaluate("async () => { await Promise.allSettled(document.getAnimations().map(a => a.finished)); }")
                await expect(page.get_by_role("button", name="关闭导航")).to_have_count(0)
                overflow = await page.evaluate("({width: innerWidth, scroll: document.documentElement.scrollWidth})")
                assert overflow["scroll"] <= overflow["width"], {"viewport": width, "view": view, **overflow}
                name = {"工作台": "chat", "连接与设置": "settings", "对话摘要": "summaries"}[view]
                await page.screenshot(path=str(self.artifacts / f"viewport-{width}-{name}.png"), full_page=True)
        await page.set_viewport_size({"width": 320, "height": 850})
        await page.get_by_role("button", name="打开导航").click()
        await expect(page.get_by_role("button", name="关闭导航")).to_be_visible()
        await page.get_by_role("button", name="关闭导航").click(position={"x": 310, "y": 300})
        await expect(page.get_by_role("button", name="关闭导航")).to_have_count(0)
        await self.nav(page, "新对话")
        await self.send(page, "移动端聊天")
        await self.wait_reply(page, "CHAT_DONE")
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")


async def main(args):
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        raise RuntimeError("Build the frontend before running this script.")
    temporary = Path(tempfile.mkdtemp(prefix="orbit-ui-check-"))
    artifacts = Path(args.artifacts).resolve() if args.artifacts else temporary / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    fixture = UIFixture(temporary, f"http://127.0.0.1:{port}")
    server = uvicorn.Server(uvicorn.Config(fixture.app, log_level="warning", lifespan="on"))
    service = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(15):
            while not server.started:
                if service.done():
                    await service
                    raise RuntimeError("The isolated backend failed to start.")
                await asyncio.sleep(0.05)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=not args.headed)
            try:
                checks = Checks(browser, fixture, artifacts)
                for name, method in [
                    ("navigation, suggestions and empty composer", checks.navigation),
                    ("vault page: plaintext toggle, site account add and remove, login hint", checks.vault),
                    ("all settings fields, saves, validation, checks and browser actions", checks.settings_forms),
                    ("summary search, clear, refresh, open, keyboard, regenerate and retry", checks.summaries),
                    ("summary fetch error and retry button", checks.summary_retry_button),
                    ("actual SSE deltas, Markdown elements, link and keyboard send", checks.chat_stream_markdown),
                    ("chat zero-confirmation and activity disclosure", checks.chat_zero_confirmation),
                    ("chat send failure, retained draft and error close", checks.chat_error_close),
                    ("chat clarification and continuing reply", checks.chat_clarification),
                    ("chat stop, subsequent send and failed reply retry", checks.chat_stop_retry),
                    ("all pages at four widths and mobile navigation/send", checks.responsive),
                    ("SSE reconnect resumes by cursor without missing or duplicate text", checks.chat_sse_reconnect),
                    ("expired session refresh restores cookie and reloads list", checks.session_recovery),
                    ("chat file-scope picker switches, persists and restores", checks.chat_file_scope),
                ]:
                    if args.only and method.__name__ not in args.only:
                        continue
                    await checks.run(name, method)
                report = {"results": checks.results, "page_errors": checks.errors,
                          "fixture": str(temporary), "mocked": ["model", "platform browser", "GitHub transport"]}
                (artifacts / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"Artifacts: {artifacts}", flush=True)
                if checks.errors or any(row["status"] == "failed" for row in checks.results):
                    raise SystemExit(1)
            finally:
                await browser.close()
    finally:
        server.should_exit = True
        await service
        listener.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headed", action="store_true", help="Show the test browser")
    parser.add_argument("--artifacts", help="Store screenshots/report here instead of a temporary folder")
    parser.add_argument("--only", action="append", help="Run only this check method; may be repeated")
    asyncio.run(main(parser.parse_args()))
