"""
產生 Instagram 登入 cookie 給 bot 用（IG 收藏夾自動同步）。

本機執行：
    python get_ig_cookies.py

會開瀏覽器讓你「自己」手動登入 Instagram（含 2FA）。腳本不會、也不需要你的密碼——
你直接在瀏覽器裡登入，登完按 Enter，它只把登入後的 storage_state JSON 印出來。

把印出來的整段貼到環境變數 INSTAGRAM_STATE_JSON（Infisical / Railway Variables），
另外把你的 IG 帳號填到 INSTAGRAM_USERNAME（組收藏夾網址 /<帳號>/saved/ 用）。
"""
import asyncio
import json
from playwright.async_api import async_playwright


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto("https://www.instagram.com/accounts/login/")
        print("\n=== 請在開啟的瀏覽器中自己登入 Instagram（含 2FA） ===")
        print("登入完成後（看到首頁），回來這個視窗按 Enter...")
        input()

        state = await context.storage_state()
        await browser.close()

    compact = json.dumps(state, separators=(",", ":"))
    print("\n=== ✅ 拿到 cookies ===")
    print(f"State size: {len(compact)} chars")
    print(f"Cookies count: {len(state.get('cookies', []))}\n")
    print("=" * 70)
    print("1) 複製下面整段（從 { 到 }）→ 環境變數 INSTAGRAM_STATE_JSON：")
    print("=" * 70)
    print(compact)
    print("=" * 70)
    print("2) 另外設 INSTAGRAM_USERNAME = 你的 IG 帳號（不含 @）")
    print("   收藏夾網址會組成 https://www.instagram.com/<帳號>/saved/")
    print("\n設好後，對 bot 傳 /sync instagram 測試；要排程自動跑就設 AUTO_SYNC_IG_HOURS。")


if __name__ == "__main__":
    asyncio.run(main())
