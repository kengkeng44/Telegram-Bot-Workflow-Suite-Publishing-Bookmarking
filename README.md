# Telegram Bot 工作流套件：自動化收藏 + AI 社群發文

這是一個為個人生產力打造的自動化系統。整合了 **Telegram** 作為入口，利用 **Claude AI** 進行分析與文案生成，並將資料流轉至 **Notion** 歸檔與 **Threads** 自動發布。

## 🚀 核心功能
* **智能收藏 (Collector):** 透過 Telegram 分享連結或截圖，系統自動分析內容並標籤化存入 Notion。
* **AI 創作發布 (Publisher):** 傳送簡單草稿，由 AI 生成社群風格文案並一鍵同步至 Threads（持續擴充中）。

## 🛠️ 技術棧
* **Language:** Python 3.12
* **Framework:** Flask (Webhook 接收)
* **AI Engine:** Claude API (Anthropic)
* **Integrations:** Notion API, Threads Graph API, Telegram Bot API
* **Infrastructure:** ngrok (Local Debug), Railway (Upcoming)

## 🗺️ 系統流程

### Collector（收藏流）
```mermaid
flowchart LR
    U[👤 使用者] -->|貼 Threads 連結| TG1[Telegram Bot]
    TG1 --> SCRAPE[Playwright 爬 GraphQL]
    SCRAPE --> CLAUDE1[Claude Haiku<br/>摘要/分類/情緒/標籤]
    CLAUDE1 --> NOTION1[(Notion Database)]
    NOTION1 -->|連結回覆| U
```

### Publisher（發文流）
```mermaid
flowchart LR
    U[👤 使用者] -->|草稿/想法| TG2[Telegram Bot]
    TG2 --> CLAUDE2[Claude<br/>產出社群文案]
    CLAUDE2 --> THREADS[Threads Graph API]
    THREADS -->|發文成功| U
```

## 📦 專案架構

### `threads-bot/` — Threads 收藏 → Notion
傳 Threads 貼文連結給 Telegram bot，自動爬內容（Playwright）→ Claude 分析（摘要／分類／情緒／標籤）→ 寫入 Notion Database。
適合用來「收藏 + 分類」別人的 Threads 貼文。

### `xiaofa-bot/` — 小發自動發文
- `bot.py` — 把任意文字／網址訊息丟進 Telegram，Claude 整理後寫進 Notion。
- `xiaofa_bot.py` — 透過 Threads Graph API 直接發文到自己的 Threads。
- `v2/` — 用 Render 部署（webhook 版）的版本，含 iOS 捷徑說明。

## 使用前

兩個專案各自 `cp .env.example .env` 後填入金鑰，然後 `pip install -r requirements.txt`。
詳細步驟看各子目錄的 README / CLAUDE.md / 部署文件。

## 🔐 安全

- `.env` 已被 `.gitignore` 排除。
- 本 repo 設有 [gitleaks](https://github.com/gitleaks/gitleaks) pre-commit hook 自動掃描 staged 檔案，攔截寫死的 API Key／Token。
  本機啟用方式：
  ```bash
  pip install pre-commit
  pre-commit install
  ```
- 建議用秘密管理服務（例：[Infisical](https://infisical.com/)）取代 `.env` 檔，金鑰永不落地。
