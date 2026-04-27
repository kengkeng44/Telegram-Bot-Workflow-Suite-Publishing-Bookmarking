# Telegram 歸檔機器人 — 完整指南

## 這個 Bot 是什麼？

你傳一個 URL 給 Telegram Bot，它會自動：

1. 用 **Jina Reader** 讀取網頁全文
2. 用 **Claude Haiku** 分析標題、摘要、標籤、作者
3. 把結果寫進你的 **Notion 資料庫**

不用開瀏覽器，不用自己整理，一個 URL 搞定收藏。

---

## 支援的平台

Instagram · Threads · Twitter/X · YouTube · Facebook · LinkedIn · 其他任何網址

---

## 系統架構

```
[你的手機 Telegram]
        ↓  傳 URL
[Telegram Bot API]
        ↓  Webhook
[Render 伺服器 · bot_render.py]
        ↓  讀網頁
[Jina Reader API · r.jina.ai]
        ↓  分析內容
[Anthropic Claude Haiku API]
        ↓  儲存
[Notion API · 你的資料庫]
```

---

## Notion 資料庫欄位（貼文整理庫）

| 欄位名稱 | 類型 | 說明 |
|---------|------|------|
| 標題 | Title | 格式 `主題（@作者）`，例如 `Claude多工省用量（@quantbear0214）` |
| 摘要 | Rich Text | AI 自動生成的一句話摘要（50 字內） |
| 分類 | Select | 五選一：AI科技 / 生活風格 / 學習成長 / 設計創意 / 商業財經 |
| 待行動 | Status 或 Select | 預設填 `待整理`（Bot 自動判斷型別） |
| 原文摘錄 | Rich Text | 從原文擷取最有代表性的一句話（80 字內） |
| 連結 | URL | 原始貼文 URL |
| 平台 | Select | 自動偵測：Instagram / Threads / Twitter / YouTube / Facebook / LinkedIn / 其他 |
| 建立時間 | Created time | Notion 自動產生，**Bot 不寫入** |
| 儲存日期 | Date | 處理當下的日期 |

> Bot 會在啟動時讀取資料庫 schema，**自動跳過不存在的欄位**，不會 crash。
> Select 欄位若值是新的，Notion 會自動建立選項。
> `分類` 欄位由 Claude 強制從 5 個白名單選一，超出範圍會 fallback 為 `AI科技`。

---

## 環境變數（Render 設定）

| 變數名稱 | 說明 |
|---------|------|
| `TELEGRAM_BOT_TOKEN` | 從 @BotFather 取得 |
| `ANTHROPIC_API_KEY` | 從 console.anthropic.com 取得 |
| `NOTION_TOKEN` | 從 notion.so/profile/integrations 取得 |
| `NOTION_DATABASE_ID` | Notion 資料庫網址中的 32 位 ID |
| `RENDER_EXTERNAL_URL` | Render 自動注入，不需手動設定 |
| `PORT` | Render 自動注入，不需手動設定 |

---

## 部署步驟（Render 免費版）

### 前置準備

**1. Notion Integration 設定**

1. 前往 `https://www.notion.so/profile/integrations`
2. 點「+ 新建整合」→ 名稱隨意，類型選「內部」
3. 複製 token（`ntn_XXXXX`）
4. 開啟你的 Notion 資料庫 → 右上角 `···` → 連接 → 選剛建立的整合

**2. Telegram Bot 建立**

1. 在 Telegram 找 @BotFather
2. 輸入 `/newbot`，依指示設定名稱
3. 複製 Bot Token

### 部署到 Render

1. 前往 [render.com](https://render.com) 登入
2. 點 **New → Web Service**
3. 連接 GitHub → 選此 repo
4. Branch 選 `claude/deploy-telegram-bot-render-E8NQC`
5. 確認設定：
   - Runtime: Python 3
   - Build Command: `pip install --upgrade pip && pip install -r requirements.txt`
   - Start Command: `python -u bot_render.py`
   - Plan: **Free**
6. 在 **Environment** 填入 4 個環境變數
7. 點 **Create Web Service**

---

## 使用方式

直接在 Telegram 傳 URL 給 Bot：

```
https://www.threads.net/@someone/post/xxxxx
```

Bot 回覆格式：
```
✅ 已儲存到 Notion

標題：文章標題
摘要：100字以內的內容摘要
標籤：科技
來源：Threads
作者：@someone
```

---

## 常見問題

### Bot 沒有回應
- 確認 Render 服務是否在運作（Logs 有無輸出）
- Render 免費版閒置 15 分鐘後會休眠，第一則訊息可能慢 30 秒

### 儲存失敗：欄位不存在
- 去 Notion 資料庫新增對應欄位
- Bot 會自動跳過不存在的欄位，所以部分欄位缺少不影響運作

### Notion 403 錯誤
- 確認 Integration 有連接到資料庫（資料庫 → `···` → 連接）
- 重新產生 Notion Token 並更新 Render 環境變數

### Build 失敗
- 確認 `requirements.txt` 中有 `python-telegram-bot[webhooks]`（注意中括號）

---

## 專案檔案說明

| 檔案 | 用途 |
|------|------|
| `bot_render.py` | 主程式：Telegram 處理 + Claude 分析 + Notion 寫入 |
| `requirements.txt` | Python 套件清單 |
| `render.yaml` | Render 部署設定 |
| `runtime.txt` | 指定 Python 版本（3.11.9） |
| `.gitignore` | 排除 .env、__pycache__ 等不需 commit 的檔案 |

---

## 技術版本

- Python 3.11.9
- python-telegram-bot 21.11.1（含 webhooks extra）
- anthropic 0.97.0
- Claude Haiku (`claude-haiku-4-5-20251001`)
- notion-client 3.0.0
- Jina Reader（免費，無需 API Key）
