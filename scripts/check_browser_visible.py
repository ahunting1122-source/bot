"""Run with backend/.venv/Scripts/python.exe scripts/check_browser_visible.py.

Uses a temporary profile, opens public pages only, and checks actual Win32 windows.
"""

import asyncio
import ctypes
import json
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.browser import BrowserController, BrowserSessionError  # noqa: E402
from app.config import Settings  # noqa: E402

TARGET_URL = "https://www.bilibili.com/"
SECONDARY_URL = "https://github.com/"


def visible_windows(process_id: int) -> list[dict]:
    if sys.platform != "win32":
        return []
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    result = []

    @callback_type
    def inspect_window(window, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == process_id and user32.IsWindowVisible(window):
            text = ctypes.create_unicode_buffer(user32.GetWindowTextLengthW(window) + 1)
            user32.GetWindowTextW(window, text, len(text))
            rect = wintypes.RECT()
            user32.GetWindowRect(window, ctypes.byref(rect))
            if rect.right > rect.left and rect.bottom > rect.top:
                result.append(
                    {
                        "title": text.value,
                        "minimized": bool(user32.IsIconic(window)),
                        "width": rect.right - rect.left,
                        "height": rect.bottom - rect.top,
                    }
                )
        return True

    user32.EnumWindows(inspect_window, 0)
    return result


async def inspect_session(browser: BrowserController) -> dict:
    page = browser.page
    session = await browser.context.browser.new_browser_cdp_session()
    try:
        processes = await session.send("SystemInfo.getProcessInfo")
    finally:
        await session.detach()
    process_id = next(p["id"] for p in processes["processInfo"] if p["type"] == "browser")
    windows = await asyncio.to_thread(visible_windows, process_id)
    body = await page.locator("body").inner_text()
    return {
        "browser_pid": process_id,
        "visible_windows": windows,
        "body_characters": len(body.strip()),
        "title": await page.title(),
        "url": page.url,
    }


async def main():
    records = []
    with tempfile.TemporaryDirectory(prefix="orbit-visible-check-") as temporary:
        settings = Settings(
            _env_file=None,
            data_dir=Path(temporary),
            workspace=Path(temporary) / "workspace",
            browser_url=TARGET_URL,
            headless=False,
        )
        settings.prepare()
        browser = BrowserController(settings, None)
        try:
            for step, url in [
                ("open_bilibili", TARGET_URL),
                ("reopen_after_window_close", TARGET_URL),
                ("navigate_to_other_allowed_site", SECONDARY_URL),
            ]:
                if step == "reopen_after_window_close" and browser.context is not None:
                    await browser.context.close()
                try:
                    opened = await browser.open_session(url)
                    actual = await inspect_session(browser)
                    record = {"step": step, "status": "passed", **opened, **actual}
                    if not actual["visible_windows"] or not actual["body_characters"]:
                        record["status"] = "failed"
                        record["error"] = "No visible OS window or no page content"
                except BrowserSessionError as exc:
                    record = {
                        "step": step,
                        "status": "failed",
                        "stage": exc.stage,
                        "error": exc.message,
                    }
                except Exception as exc:
                    record = {"step": step, "status": "failed", "error_type": type(exc).__name__}
                records.append(record)
                print(json.dumps(record, ensure_ascii=True), flush=True)
        finally:
            await browser.close()
    return 0 if all(record["status"] == "passed" for record in records) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
