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

## 📦 專案架構
(這裡之後放我建議你畫的架構圖)
