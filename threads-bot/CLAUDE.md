# Data Hoarder（資料夾仍叫 `threads-bot/`）

## 一句話描述

Telegram bot：使用者傳任意網址（Threads / Instagram / FB / YouTube / Medium / 任何網頁）或純文字 → Playwright headless Chromium 爬取（Threads 走 GraphQL + `data-sjs` script tag + 登入 cookie；其他走 Open Graph + 主要文字）→ Claude Haiku 產出摘要／分類／標籤／情緒 → 寫入 Notion Database。

> 資料夾名 `threads-bot/` 是歷史包袱（最初只支援 Threads），現在已多平台，對外暱稱 **Data Hoarder**。改名要協調 Railway Root Directory，記錄在頂層 TODO.md。

---

## 檔案結構

```
threads-bot/
├── bot.py            # 全部邏輯：爬蟲、Claude 分析、Notion 寫入、Telegram handlers
├── run_local.py      # 本地啟動：load_dotenv() 後呼叫 bot.main()
├── requirements.txt
├── Dockerfile        # 部署用（mcr.microsoft.com/playwright/python:v1.48.0-jammy）
├── railway.json      # Railway 部署設定
├── .dockerignore
├── .env.example      # 環境變數範本（不含真實金鑰）
├── .env              # 真實金鑰（不要 commit）
└── CLAUDE.md         # 本檔
```

---

## 環境變數（.env）

| 變數 | 格式 | 說明 |
|------|------|------|
| `TELEGRAM_TOKEN` | `1234567890:AAFxxx` | @BotFather 申請 |
| `NOTION_TOKEN` | `ntn_xxx` | Notion Internal Integration Secret |
| `NOTION_DATABASE_ID` | 32 位英數 | 從 Notion Database URL 抓 |
| `ANTHROPIC_API_KEY` | `sk-ant-api03-xxx` | console.anthropic.com |
| `ALLOWED_USER_ID` | 整數 | 你的 Telegram User ID；設 `0` 表示不限制 |

---

## 依賴套件

```
python-telegram-bot==21.6   # Telegram Bot 框架（async）
playwright==1.48.0          # headless Chromium 爬蟲
notion-client==2.2.1        # Notion 官方 SDK
anthropic>=0.50.0           # Claude API（原釘 0.39.0，因 httpx proxies 相容性問題升為開放下界）
jmespath==1.0.1             # 解析 GraphQL JSON
python-dotenv==1.0.1        # 讀 .env
tenacity==9.0.0             # 指數退避重試（網路 / rate limit）
```

執行環境：Python 3.12，Windows 11，路徑 `C:\Users\acer\AppData\Local\Programs\Python\Python312\`

---

## 安裝與啟動

```powershell
cd C:\Users\acer\Downloads\threads.bot

# 第一次
pip install -r requirements.txt
python -m playwright install chromium

# 啟動
python run_local.py
```

停止：Ctrl+C。**不要同時跑多個實例**，會造成 `Conflict: terminated by other getUpdates request`。

---

## 程式架構

```
run_local.py
  └── load_dotenv()
  └── bot.main()
        └── Application (python-telegram-bot)
              ├── /start   → start()
              ├── /stats   → stats()       # 今天爬了幾則
              ├── /recent  → recent()      # 最近 5 則
              └── 任何文字訊息 → handle_message()
```

### handle_message() — 訊息入口

1. 權限檢查：`effective_user.id` 必須等於 `ALLOWED_USER_ID`（若非 0）
2. `extract_urls(text)` 解析所有 Threads URL
3. 對每個 URL 依序執行：
   - `find_existing_notion_page(url)` → 若 Notion 已有同 URL 直接回傳既有 page URL，跳過後續步驟
   - `scrape_thread_url(url)`
   - `analyze_with_claude(post["text"])`（用 `asyncio.to_thread` 包成 async）
   - `write_to_notion(post, analysis)`（同上）
4. 每步驟即時 edit 同一則 Telegram 訊息顯示進度
5. 全部完成後一次回覆所有結果（重複者標記 ⏭）

### extract_urls(text) — URL 解析

```python
re.findall(r"https?://(?:www\.)?threads\.(?:net|com)/[^\s]+", text)
```

額外處理：Telegram 有時把同一個 URL 重複黏在一起（沒有空格），用第二個 `https://` 出現位置截斷，並去重。

### scrape_thread_url(url) — 爬蟲

**方法**：Playwright headless Chromium，攔截 Threads 的 GraphQL response。

**關鍵設定**：
- `wait_until="domcontentloaded"`（不用 `networkidle`，因為 Threads SPA 的持久連線永遠不會停，會 Timeout）
- `wait_for_timeout(5000)` 等 5 秒讓 GraphQL 跑完
- User-Agent 偽裝成 macOS Chrome 120

**攔截條件**：
```python
"/graphql/query" in response.url or "BarcelonaPostPageQuery" in response.url
```

**jmespath 解析路徑**（最容易因 Threads 改版而壞的地方）：
```
data.data.containing_thread.thread_items[*].post | [0]
```

**備援**（GraphQL 攔截失敗時）：從 HTML 用 regex 抓 caption：
```python
re.search(r'"caption":\{"text":"([^"]+)"', html)
```
備援只有文字，沒有 username / 讚數等欄位。

**容錯日誌**：若攔到 GraphQL response 但 jmespath 路徑解不到貼文（很可能 Threads 改版），會把第一筆原始 response dump 到 `last_failed_response.json`（與 `bot.py` 同目錄），方便對照新結構更新 jmespath 路徑。

**回傳結構**：
```python
{
    "text": str,            # 貼文內容
    "username": str,        # Threads username
    "like_count": int,
    "reply_count": int,
    "repost_count": int,
    "quote_count": int,
    "taken_at": int|None,   # Unix timestamp
    "image_urls": list[str], # GraphQL image_versions2 + carousel_media
    "url": str,
}
```

**圖片解析**：`_extract_image_urls()` 從 `image_versions2.candidates[0]`（Threads 預設第一個是最大張）抓單張，再從 `carousel_media[*]` 抓多圖貼文，去重後回傳。HTML 備援不取圖。

### analyze_with_claude(text) — AI 分析

**模型**：`claude-haiku-4-5-20251001`（速度快、便宜，分類任務夠用）

**Prompt** 要求回傳純 JSON（不含 markdown fence）：
```json
{
  "summary": "30 字以內的中文摘要",
  "category": "生活 | 工作 | 科技 | 財經 | 娛樂 | 學習 | 觀點 | 其他",
  "tags": ["關鍵字1", "關鍵字2", "關鍵字3"],
  "sentiment": "正面 | 中性 | 負面"
}
```

**後處理**：
- regex 清掉偶發的 ` ```json ` fence
- sentiment 正規化：`.split("/")[0].strip()` → 避免 Claude 把 `"正面 / 中性 / 負面"` 整串寫進 Notion Select

### find_existing_notion_page(url) — 重複偵測

查詢 Notion Database 的「原文連結」欄位是否已存在 `url`，存在則回傳既有 page URL，不存在回傳 `None`。查詢失敗（例如網路或 token 問題）時 log warning 並回傳 `None`，讓主流程繼續，避免去重檢查反而擋住正常寫入。

### write_to_notion(post, analysis) — 寫入 Notion

**Notion Database 欄位**（名稱一字不差，不同請改 `properties` 字典）：

| 欄位名 | 類型 | 值來源 |
|--------|------|--------|
| Name | Title | `analysis["summary"]`，截 100 字 |
| 原文 | Rich Text | `post["text"]`，截 2000 字 |
| 作者 | Rich Text | `post["username"]` |
| 分類 | Select | `analysis["category"]` |
| 情緒 | Select | `analysis["sentiment"]` |
| 標籤 | Multi-select | `analysis["tags"]` |
| 讚數 | Number | `post["like_count"]` |
| 留言數 | Number | `post["reply_count"]` |
| 轉發數 | Number | `post["repost_count"]` |
| 原文連結 | URL | `post["url"]` |
| 發文時間 | Date | `post["taken_at"]`（Unix → ISO，有值才寫） |

**圖片**：`post["image_urls"]` 前 10 張會以 `external image block` 加到 page children（Notion 接受 CDN URL，不必先下載再上傳）。

回傳：新建 page 的 `page["url"]`。

### /stats 與 /recent

- `_count_today_in_notion()`：依 `created_time >= 今天 00:00（台北時區 UTC+8）` 過濾，分頁累加。
- `_list_recent_in_notion(limit=5)`：以 `created_time desc` 取前 5 筆，組成 `{title, category, url, page_url}`。
- 兩個指令都檢查 `ALLOWED_USER_ID` 權限。

---

## 已修復的 Bug（本對話內）

| 問題 | 原因 | 修法 |
|------|------|------|
| `TypeError: unexpected keyword argument 'proxies'` | `anthropic==0.39.0` 與新版 httpx 不相容 | `pip install --upgrade anthropic`，requirements 改 `>=0.50.0` |
| `TimeoutError: Page.goto` | `networkidle` 在 Threads SPA 永遠等不到 | 改 `domcontentloaded` + `wait_for_timeout(5000)` |
| URL 重複黏在一起導致無效網址 | Telegram 訊息有時把同一 URL 重複 N 次且無空格 | `extract_urls()` 以第二個 `https://` 截斷 |
| sentiment 寫入 Notion 格式錯誤 | Claude 照範例把三個選項都寫進去 | `.split("/")[0].strip()` 只取第一個值 |
| `python-dotenv` 缺失 | requirements.txt 漏加 | 補上 `python-dotenv==1.0.1` |
| 死碼 `parse_thread()` | 定義了但從未呼叫 | 刪除 |
| 無效 model ID `claude-opus-4-5` | 該 ID 不存在 | 改 `claude-haiku-4-5-20251001` |

---

## 常見錯誤排查

| 錯誤 | 原因 | 解法 |
|------|------|------|
| `Conflict: terminated by other getUpdates request` | 跑了多個 bot 實例 | `Get-Process python \| Stop-Process -Force`，只留一個實例 |
| `unauthorized` / `unknown error`（Notion） | Integration 沒授權給 Database | Database → `•••` → Connections → Add connection |
| `Could not find object`（Notion） | Database ID 錯或 integration 無權限 | 確認 ID 正確且 integration 已授權 |
| `ValueError: 無法抓取貼文資料` | 私人帳號，或 Threads GraphQL 結構改版 | 開 DevTools 看新 response 結構，更新 jmespath 路徑 |
| `json.JSONDecodeError` | Claude 回傳非 JSON | 偶發，重傳；或加強 prompt |

---

## 待辦（尚未實作）

（目前清單已清空 — 主要功能皆完成。後續可考慮：影片支援、定期 `/digest` 自動摘要當日內容、Notion 欄位 schema 自動建立等。）

## 已實作（近期新增）

- [x] **重複 URL 偵測**：`find_existing_notion_page()` 查 `原文連結` 欄位，同 URL 跳過並回傳既有 page
- [x] **jmespath 路徑容錯**：解析失敗時 dump 原始 response 到 `last_failed_response.json`
- [x] **重試機制**：`tenacity` 指數退避（詳見下節）
- [x] **`/stats`**：回今天台北時區 00:00 起寫入 Notion 的則數
- [x] **`/recent`**：列最近 5 則（Notion `created_time desc`）
- [x] **圖片**：抓 GraphQL `image_versions2` + `carousel_media`，以 external image block 寫入 Notion page children
- [x] **部署檔**：`Dockerfile`（playwright 官方 image）+ `railway.json` + `.dockerignore`

## 部署（Railway / Docker）

**為何用 Docker**：Playwright 需要系統層的 Chromium 依賴（fonts、libnss 等），用官方 `mcr.microsoft.com/playwright/python` image 可避免在裸機上裝一堆 apt 套件。

**Railway 部署步驟**：
1. push repo 到 GitHub
2. Railway → New → Deploy from GitHub → 選此 repo
3. Railway 會偵測到 `railway.json`，使用 `Dockerfile` build
4. 在 Variables 分頁加入 4 個環境變數（`TELEGRAM_TOKEN`、`NOTION_TOKEN`、`NOTION_DATABASE_ID`、`ANTHROPIC_API_KEY`、可選 `ALLOWED_USER_ID`）
5. Deploy；Railway 會自動 restart on failure（`railway.json` 設了上限 10）

**本機 Docker 測試**：
```powershell
docker build -t threads-bot .
docker run --rm --env-file .env threads-bot
```

**注意**：本地不用 Docker 跑可繼續用 `python run_local.py`；Dockerfile 只給部署用。同一個 Telegram bot token 同時跑兩個實例會 `Conflict: terminated by other getUpdates request`，部署上線記得停本地。

## 重試策略（tenacity）

| 函式 | 重試次數 | 重試的例外 | 退避 |
|------|---------|-----------|------|
| `_fetch_threads_page` | 3 | `PlaywrightTimeoutError` | 2s → 4s → 8s（max 15s） |
| `analyze_with_claude` | 4 | `APIConnectionError` / `RateLimitError` / `APIStatusError` | 2s → 4s → 8s → 16s（max 30s） |
| `_query_notion_by_url` | 3 | `RequestTimeoutError` / `APIResponseError` | 2s → 4s → 8s（max 15s） |
| `write_to_notion` | 3 | 同上 | 同上 |

設計原則：
- 只重試「暫時性」例外（網路、超時、rate limit）。`ValueError`（私人帳號）、`json.JSONDecodeError`（Claude 格式問題）等永久性失敗不重試。
- `_fetch_threads_page` 把 Playwright 的 browser 生命週期包在裡面，每次重試都重開 browser，避免狀態污染。
- `find_existing_notion_page` 用內外兩層：內層 `_query_notion_by_url` 做重試，外層仍把最終失敗轉成 `None`，保持「去重檢查失敗不擋寫入」的容錯行為。
- 所有重試都會 log warning（`before_sleep_log`），便於觀察是否頻繁退避。
