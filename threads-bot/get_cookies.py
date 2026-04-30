"""
產生 Threads 登入 cookie 給 bot 用。

本機執行：
    cd C:\\Users\\acer\\Downloads\\threads.bot
    python get_cookies.py

會開瀏覽器讓你手動登入 Threads（用 Instagram 帳號）。
登完按 Enter，整段 storage state JSON 印到 console。
複製整段 → Infisical 的 THREADS_STATE_JSON secret。
"""
import asyncio
import json
from playwright.async_api import async_playwright


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto("https://www.threads.com/login")
        print("\n=== 請在開啟的瀏覽器中登入 Threads（用 Instagram 帳號） ===")
        print("登入完成後（看到首頁 feed），回來這個視窗按 Enter...")
        input()

        state = await context.storage_state()
        await browser.close()

    compact = json.dumps(state, separators=(",", ":"))
    print("\n=== ✅ 拿到 cookies ===")
    print(f"State size: {len(compact)} chars")
    print(f"Cookies count: {len(state.get('cookies', []))}\n")
    print("=" * 70)
    print("複製下面整段（從 { 到 }）貼到 Infisical 的 THREADS_STATE_JSON secret：")
    print("=" * 70)
    print(compact)
    print("=" * 70)
    print("\n貼到 Infisical 後，存檔。Auto-sync 30 秒內推到 Railway。")


if __name__ == "__main__":
    asyncio.run(main())
