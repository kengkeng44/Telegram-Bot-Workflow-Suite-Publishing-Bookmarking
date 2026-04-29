# LESSONS

從這個專案踩到的坑、學到的東西。給未來的自己 + 做類似專案的人。

---

## 🔐 Secret 管理

### 1. `.env` 檔本質上是會漏的

只要 secret 寫在硬碟上的檔案，遲早會漏：
- 開發時不小心 `cat .env` 或截圖上傳對話 → 進 LLM 紀錄檔
- `grep` 含金鑰的設定檔當作 debug → 整個檔案內容被 capture
- 同步軟體（OneDrive / Dropbox / iCloud）自動上傳到雲端
- 備份程式打包整個資料夾
- 不小心 `git add .` 跳過 `.gitignore`

**結論**：`.env` 適合本地開發 prototype，正式環境一律上 secret manager（Infisical / Doppler / 1Password）。

### 2. Secret manager 不是花錢買功能，是改變「金鑰住在哪」

| 之前 | 之後 |
|---|---|
| Secret 住硬碟（`.env` / config.py 寫死） | Secret 住 Infisical 雲端，硬碟乾淨 |
| Rotate 要改 N 個地方（本機 + Railway + Render…） | 改 Infisical 一處，所有環境自動同步 |
| 換新電腦要從備份找 `.env` | `infisical login` + `infisical run --` 就好 |

`bot.py` 的 code **完全不用改**，因為 `os.getenv()` 從哪讀都行 — Infisical CLI 把 secret 注入成環境變數。

### 3. 「跟 AI 互動 = 金鑰外洩通道」

debug 過程中，AI 為了看狀況可能：
- 主動 `cat .env` 看內容
- `grep` 含 secret 的檔
- 要你貼錯誤訊息（裡面可能有 secret）

整個對話會被存成 `.jsonl`，secret 永久留在 chat history 裡。

對策：
1. 給 AI 工作時，明確規則：**不要主動讀含 secret 的檔**
2. AI debug 印出 secret 時要先 `replace(secret, "<MASKED>")` 再 print
3. **API 錯誤訊息不要直接貼給 AI**，先自己看一眼有沒有 token 在裡面（Telegram、FB Graph 都很愛把 token / secret 原樣丟回 error message）
4. 一旦發現外洩 → **立刻 rotate**，不要僥倖

### 4. Telegram bot token 是 URL path，所以 logs 會直接洩

Telegram API endpoint 長這樣：
```
https://api.telegram.org/bot<TOKEN>/getUpdates
```

Token 在 URL path 裡，所以 HTTP request log 印 URL 就直接洩漏 token。Railway / Render / 任何 logging 工具預設都會印 URL。

**對策**：
- 不要把 Railway logs 直接截圖給人 / AI
- 要 debug 用 logs filter 過濾，只看你自己加的 `[scrape_xxx]` 之類，不要看到 Telegram polling URL
- 一旦 token 進 chat log，立刻 BotFather `/revoke`

---

## 🕵️ 爬蟲

### 5. 雲端 datacenter IP 跟家用 IP 對社群網站是兩種待遇

很多社群網站（Threads / X / Instagram）對 datacenter IP（Railway / Render / GCP / AWS）有特殊處理：
- 直接擋 / 給 captcha
- 或回傳「精簡版 HTML」（少了關鍵資料）

如果你本機（家用 IP）能爬、雲端不能爬 → 八成是這個。

**解法（從便宜到貴）**：
1. 本機跑（家用 IP，但要電腦不關機）
2. Cookie 法（讓 server-side bot 帶登入身份）
3. Residential proxy（付費，每月幾十 USD）
4. 改用官方 API（如果有，且不需要 App Review）

### 6. Threads / IG 改版策略：Server-side render + 沒登入給空殼

Threads 在 2025 後期把資料從 GraphQL fetch 改成 server-side render（資料嵌在 HTML 的 `<script type="application/json" data-sjs>`）。

但**未登入訪客拿到的 HTML 是空殼**（連 og:description meta 都不給），所有資料只在 client-side JS + 登入 cookie 才能載。

**寫爬蟲的 fallback 順序**（從快到慢、從具體到通用）：
1. 攔截 GraphQL response
2. HTML script tag 抽 JSON（`thread_items` key）
3. nested_lookup 找任何含 caption + username 的 dict
4. Open Graph meta tags（`og:description` / `og:title` / `og:image`）
5. 全失敗 → 印詳細 log 告訴自己哪一層卡住

### 7. Cookie 法：本機產生 → 推 secret manager → server-side bot 讀取

```
[本機] python get_cookies.py → 開瀏覽器手動登入 → 存 storage_state JSON
            ↓
[Infisical] 整段 JSON 當 secret (THREADS_STATE_JSON)
            ↓
[Railway] bot 從 env var 讀 → Playwright new_context(storage_state=...) → 帶登入身份爬
```

Cookie 通常 30–90 天過期，要重做。Meta 強制改密碼或踢出時也要重做。

---

## 🚂 部署

### 8. Railway / Render 的 Service Variables 跟 Secret Manager 整合是「網頁設一次就好」

Infisical → Railway 設定流程：
1. 在 Railway 建 Project Token（Project Settings → Tokens，不是 Account 層級）
2. 在 Infisical（Project 層）→ Integrations → App Connections → 用 Token 建 Railway connection
3. 然後再去 Secret Syncs → 建 sync 規則（指定 source environment / destination service）

**容易卡住的點**：Infisical UI 把這拆成兩層
- **App Connection** = 「能連上 Railway」（建立認證）
- **Secret Sync** = 「規則：要把哪些 secret 推到哪」

只建 App Connection 不會推任何東西。

### 9. Mono-repo 部署到 Railway 要設 Root Directory

如果你 GitHub repo 結構是：
```
repo/
├── threads-bot/
│   ├── bot.py
│   ├── Dockerfile
│   └── requirements.txt
└── xiaofa-bot/
    ├── bot.py
    └── ...
```

Railway 預設從 repo 根目錄找 Dockerfile / package.json，會 build 失敗。**要去 Service Settings → Source → Add Root Directory** 填 `threads-bot`（或 `xiaofa-bot`）。

---

## 🛡️ 防呆

### 10. Pre-commit hook 攔住 secret 進 git history 比 .gitignore 可靠

`.gitignore` 只阻擋「`git add .`」誤加 `.env`，但**沒擋住「直接 hardcode 在 .py 檔的 secret」**。

`gitleaks` 會掃 staged 檔案內容，看有沒有 `sk-ant-`、`ntn_`、`AAxx`-style Telegram token 等高熵字串，掃到就 reject commit。

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.21.2
    hooks:
      - id: gitleaks
```

```bash
pip install pre-commit
pre-commit install
```

**注意**：pre-commit hook 是本機檔案，每個開發者要各自 `pre-commit install`。團隊正式用要再加 GitHub Actions 跑 gitleaks 在 PR 上把關。

### 11. Rotate 流程：「從 leak 起算的 5 分鐘窗口」

Secret 一旦被視為外洩，必須在 5 分鐘內 rotate 完，避免被掃描程式撿走。

順序（重要 → 不重要）：
1. **API key 類**（Anthropic / OpenAI）：付費服務，被盜用會直接燒錢
2. **資料寫入類**（Notion Token / Telegram Bot Token）：被盜用會破壞資料
3. **OAuth secret**（Threads App Secret）：被盜用可能假冒你的 app
4. **API key 免費類**（CWA / OpenWeatherMap）：被盜最多吃光配額

每換完一個立刻：
- 更新 secret manager（不是更新 `.env`）
- 觸發 deployment 重啟
- 確認新 token 有作用（傳一條測試訊息）

---

## 🧠 思考方式

### 12. 「Secret 永遠在哪裡」是設計問題，不是維護問題

Rotate 100 次都是治標。真正治本是改變「secret 住在哪」：

```
住硬碟 → 漏的速度 = O(時間 × 操作次數)
住 secret manager → 漏的速度 ≈ 只有人為失誤
```

當你發現自己「又在 rotate 一次相同金鑰」就該停下來想：**是不是該把它從硬碟搬走了？**

### 13. AI 寫 code 不是問題，AI 看 secret 才是問題

AI 幫你 debug、寫爬蟲、改 architecture 都很有幫助。但**任何時候 AI 接觸到原始 secret 值（不論是讀檔、grep、看 error）都是外洩事件**，因為 chat history 是永久的。

對 AI 工作：給「結構」、「錯誤訊息（遮過 secret）」、「行為描述」，不要給「值本身」。

### 14. 早期專案 vs 成熟專案的安全模型不同

剛開始時：
- 寫死在 `.env` ← OK，先求功能跑起來
- 沒做 token rotate ← OK，還沒人在意

開始上 GitHub / 部署 / 給人看時：
- 必須 secret manager
- 必須 pre-commit hook
- 必須有 rotate 流程

**轉折點是「第一次 push 到公開 repo」**。從那一刻起，所有 secret 都要假設可能曾經外洩過。
