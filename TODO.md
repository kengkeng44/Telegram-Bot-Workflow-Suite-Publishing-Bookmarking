# TODO

## 🟡 短期改善

### 自動續 Threads cookie（半自動方案）

**現況**：Threads 改版後爬未登入帳號拿不到內容，需要 `THREADS_STATE_JSON`（登入 cookie）。Cookie 30–90 天過期，目前需要手動跑 `get_cookies.py` 重新產生。

**目標**：bot 在每次爬蟲後，把 Playwright 拿到的最新 cookie 寫回 Infisical，cookie 持續被「續命」，幾乎不用手動更新。

**實作大綱**（30~50 行 code）：
1. `_scrape_threads()` 每次爬完，呼叫 `context.storage_state()` 拿最新 state
2. 跟啟動時的 cookie 比對，如果有差異 → 用 Infisical CLI 或 API 寫回 `THREADS_STATE_JSON`
3. 加 throttle：每天最多寫一次，避免每則貼文都觸發 API call
4. 用 Infisical Service Token（不是 user token）讓 bot 有寫入權限

**還是手動的部分**：
- 第一次必須手動跑 `get_cookies.py` 產生初始 cookie
- 連續 60+ 天沒爬東西，cookie 還是會過期
- Meta 偵測異常登入強制踢出時要重做
- 2FA / Captcha 觸發時要人介入

**Reference**：
- Infisical CLI write back: `infisical secrets set THREADS_STATE_JSON='...'`
- Playwright storage_state: https://playwright.dev/python/docs/auth

---

### 從 Threads / IG / FB 收藏夾自動同步

**動機**：與其手動把每個貼文連結轉給 bot，不如讓 bot 直接掃自己的收藏夾，找出新的、還沒進 Notion 的，自動處理。

**做法**（要 cookie 法先做完）：
1. 用 Playwright + 登入 cookie 開 `https://www.threads.com/@me/saved`（或 IG/FB 對應 saved page）
2. 模擬滾動載入所有貼文連結
3. 跟 Notion 既有資料比對，找出新的
4. 一個一個跑現有 `_process_one()` 邏輯

**指令設計**：
- `/sync threads` — 同步 Threads 收藏
- `/sync ig` — 同步 Instagram 收藏
- `/sync fb` — 同步 Facebook 收藏
- `/sync telegram` — 掃 Telegram「儲存的訊息」中的連結（**最安全的版本，不需登入 IG**）

**風險與注意**：
- IG / Threads 對 DM 自動化偵測敏感，**不要碰 DM**
- 收藏夾相對安全（讀自己已經收藏的資料）
- 加 rate limit（同步時逐則處理，避免短時間大量爬蟲）
- 推薦先做 Telegram Saved Messages 版本（風險為 0、UX 還可以）

---

## 🟢 想到再做

- [ ] xiaofa-bot 也接 Infisical（目前只有本機 .env）
- [ ] CWA / OWM API key 有空 rotate 一下（這次 chat 中洩漏到 log，雖然是免費 API）
- [ ] cheng.robot 的 `gmail credentials.json` / `token.pickle` 也搬到 Infisical（目前 token.pickle 是 base64 編碼存環境變數，credentials.json 還在硬碟）
- [ ] 加 GitHub Actions：每次 push 自動跑 gitleaks（目前只有 pre-commit，依賴開發者本機裝）
- [ ] 把 cheng.robot push 到 GitHub（目前還是本機資料夾，沒進 repo）
- [ ] 加 README badge / 部署狀態
