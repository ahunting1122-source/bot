"""Exercise the local workbench without changing user settings or conversations."""

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto("http://127.0.0.1:6185", wait_until="networkidle")
        await page.get_by_role("heading", name="把想法交给我， 一起完成下一件事。").wait_for()
        await page.get_by_role("button", name="整理本地资料").click()
        assert "工作目录" in await page.get_by_label("给助手一个任务").input_value()
        await page.get_by_role("button", name="连接与设置", exact=True).click()
        await page.get_by_role("heading", name="连接你的工作空间").wait_for()
        assert await page.get_by_label("模型名称", exact=True).count() == 1
        await page.get_by_role("button", name="长期记忆", exact=True).click()
        await page.get_by_role("heading", name="让助手更了解你").wait_for()
        await page.get_by_role("button", name="开启新对话").click()
        for width in (320, 768, 1024, 1440):
            await page.set_viewport_size({"width": width, "height": 1000})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
        path = Path(__file__).resolve().parents[1] / "data" / "artifacts" / "workbench-preview.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(path), full_page=True)
        assert not errors, errors
        print("UI smoke passed: workbench, suggestions, settings, memories, four viewport widths.")
        print(f"Screenshot: {path}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
