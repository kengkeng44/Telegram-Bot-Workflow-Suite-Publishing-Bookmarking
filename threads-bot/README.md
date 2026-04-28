# Threads → Claude → Notion Telegram Bot

傳 Threads 貼文連結給 Telegram bot，自動爬內容 → Claude 分析 → 存進 Notion Database。

---

## 一、Notion Database 必須有的欄位

去你的 Notion Database 把欄位設成這樣（**名稱要一模一樣**，不一樣就改 `bot.py` 裡的 properties）：

| 欄位名 | 類型 |
|---|---|
| Name | Title（預設那欄） |
| 原文 | Text |
| 作者 | Text |
| 分類 | Select |
| 情緒 | Select |
| 標籤 | Multi-select |
| 讚數 | Number |
| 留言數 | Number |
| 轉發數 | Number |
| 原文連結 | URL |
| 發文時間 | Date |

---

## 二、安裝（本地測試）

```bash
# 1. 安裝套件
pip install -r requirements.txt
pip install python-dotenv

# 2. 安裝 Playwright 的瀏覽器
playwright install chromium

# 3. 複製 .env.example 成 .env，填入金鑰
cp .env.example .env

# 4. 啟動
python run_local.py
```

---

## 三、四把金鑰怎麼拿

### 1. TELEGRAM_TOKEN
- Telegram 找 `@BotFather` → `/newbot` → 給名字 → 拿到 token

### 2. NOTION_TOKEN（重點：用 secret_ 開頭那種）
- 去 https://notion.so/profile/integrations
- `+ New integration` → `Internal` → 建立
- 拿到 `secret_xxx` token
- **回到你的 Database** → 右上 `...` → `Connections` → 加入剛剛建立的 integration

⚠️ 不要用 `ntn_` 開頭的 OAuth token，會過期斷線

### 3. NOTION_DATABASE_ID
- 開啟 Notion Database 頁面
- 看網址：`notion.so/你的工作區/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx?v=...`
- 中間那串 32 位英數就是 Database ID

### 4. ANTHROPIC_API_KEY
- https://console.anthropic.com → API Keys → Create Key

### 5. ALLOWED_USER_ID
- Telegram 找 `@userinfobot`，對話一下會告訴你 ID

---

## 四、24 小時不關機部署

本地電腦關掉 bot 就停了，要持續運行有兩種免費方案：

### 方案 A：Railway（最簡單，推薦）
1. 把這個資料夾推上 GitHub
2. railway.app 連結你的 repo
3. 在 Railway 設定 Environment Variables（把 .env 內容貼上去）
4. Deploy → 24/7 運行

### 方案 B：自己家裡的舊電腦/樹莓派
- 跑 `python run_local.py` 然後讓它一直開著

---

## 五、使用方式

1. 在 Telegram 找你的 bot → `/start`
2. 直接貼 Threads 貼文連結進去
3. 等大約 10~20 秒 → 看到 ✅ 完成 + Notion 連結

---

## 六、常見問題

**Q: 抓不到貼文？**
- 檢查是否為公開貼文（私人帳號爬不到）
- Threads 改版時 jmespath 路徑可能要更新

**Q: Notion 寫入失敗？**
- 確認 integration 有加進 Database 的 Connections
- 確認欄位名稱跟 `bot.py` 對得上

**Q: Claude 回傳格式跑掉？**
- 偶爾會發生，重傳一次通常就好
