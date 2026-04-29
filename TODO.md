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

### 「Data Hoarder」改名（一氣呵成版）

對外文字描述已改成「Data Hoarder」（README、CLAUDE.md），但資料夾名 / 服務名 / GitHub repo 名 / Telegram bot 還是 `threads-bot` / `jenho_threads_bot`。一次改完的順序：

#### A. 子目錄改名（要協調 Railway，最容易出包）

```powershell
# 1. 先去 Railway dashboard 開好 → 進 Data hoarder service → Settings → Source 那頁
# 2. 在 PowerShell 執行：
cd C:\Users\acer\Desktop\github-upload
git mv threads-bot data-hoarder-bot
# 同步本機開發資料夾（可選）：
# Rename-Item C:\Users\acer\Downloads\threads.bot C:\Users\acer\Downloads\data-hoarder-bot
git add .
git commit -m "Rename threads-bot/ → data-hoarder-bot/"
git push
# 3. 立刻去 Railway → Settings → Source → Root Directory → 改成 data-hoarder-bot → Save
# 4. Railway 會重新 build，幾分鐘後 ACTIVE
```

⚠️ 如果第 3 步來不及做，Railway build 會失敗（找不到 Dockerfile）— 沒關係，改完 Root Directory 再點 Redeploy 就好。

#### B. GitHub repo 改名

1. 開 https://github.com/kengkeng44/Telegram-Bot-Workflow-Suite-Publishing-Bookmarking/settings
2. **Repository name** → 改成例如 `Data-Hoarder` → Rename
3. 改完 GitHub 會自動 redirect 舊 URL，**Railway 不用立刻改**（git remote 也還能用）
4. 想徹底乾淨：本機更新 git remote
   ```powershell
   cd C:\Users\acer\Desktop\github-upload
   git remote set-url origin https://github.com/kengkeng44/Data-Hoarder.git
   git remote -v   # 確認
   ```

#### C. Telegram bot 改名

1. BotFather → `/setname` → 選 `@jenho_threads_bot` → 輸入新顯示名（例 `Data Hoarder`）
2. BotFather → `/setusername` → 選 `@jenho_threads_bot` → 輸入新 username（例 `jenho_data_hoarder_bot`）
   - ⚠️ 改 username = `t.me/jenho_threads_bot` 連結失效，新連結變 `t.me/jenho_data_hoarder_bot`
   - Token **不會變**，bot 不用重新部署
3. 改 bot 簡介（可選）：BotFather → `/setdescription`

#### D. Infisical project 改名

1. https://app.infisical.com → 進 `threads-bot` project
2. 左下 Settings → General → Project Name → 改成 `data-hoarder-bot` → Save
3. 不影響 token 或同步，本機 `.infisical.json` 用 ID 鎖定，名字改了不會壞

#### E. Notion Database 改名（隨意）

直接在 Notion 把 Database 改名。`bot.py` 用 `NOTION_DATABASE_ID` 找資料庫，跟名字無關。

---

### 修 `/usage` 的 Railway GraphQL schema

**現況**：`/usage` 第一段 `me { id email name }` 能查（token 有效），但第二段 `workspaces { edges { node {} } }` + `usage(...)` 在 2026-04 失敗（HTTP 400），目前 fallback 給 dashboard 連結。

**怎麼修**：去 https://railway.com/account/usage 用 Chrome DevTools → Network → 看 dashboard 自己打的 GraphQL request，把它的 query + variables 抄出來貼進 `get_railway_usage()`。Railway schema 沒公開文件、定期會改，靠 reverse engineering 比較準。

---

## 🟢 想到再做

- [ ] xiaofa-bot 也接 Infisical（目前只有本機 .env）
- [ ] CWA / OWM API key 有空 rotate 一下（這次 chat 中洩漏到 log，雖然是免費 API）
- [ ] cheng.robot 的 `gmail credentials.json` / `token.pickle` 也搬到 Infisical（目前 token.pickle 是 base64 編碼存環境變數，credentials.json 還在硬碟）
- [ ] 加 GitHub Actions：每次 push 自動跑 gitleaks（目前只有 pre-commit，依賴開發者本機裝）
- [ ] 把 cheng.robot push 到 GitHub（目前還是本機資料夾，沒進 repo）
- [ ] 加 README badge / 部署狀態
