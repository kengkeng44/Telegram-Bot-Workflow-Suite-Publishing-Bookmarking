# 🚀 Render 部署完整教學(30 分鐘)

## 📋 你會得到什麼

部署完成後:
✅ bot 24/7 運作,電腦可以關
✅ 固定網址 `https://xxx.onrender.com`,捷徑設定一次永久有效
✅ ngrok 直接淘汰
✅ Claude API 費用降 85%(Haiku) + 50%(Jina Reader) = **原本的 7.5%**

---

## 步驟 1:準備 GitHub (5 分鐘)

### 1.1 在你的電腦上建立新資料夾

```bash
# 在桌面建一個新資料夾
cd C:\Users\acer\Desktop
mkdir gui-sorter-render
cd gui-sorter-render
```

### 1.2 把我給你的檔案放進去

把這些檔案複製到 `C:\Users\acer\Desktop\gui-sorter-render`:
- `bot_render.py`
- `requirements.txt`
- `render.yaml`

**不要**放 `.env` 檔案(敏感資訊不能上傳 GitHub)

### 1.3 建立 .gitignore

在同一個資料夾建立 `.gitignore` 檔案:
```
.env
__pycache__/
*.pyc
venv/
```

### 1.4 初始化 Git 並推到 GitHub

```bash
git init
git add .
git commit -m "Initial commit for Render deployment"
```

然後到 GitHub:
1. 打開 https://github.com/new
2. Repository name: `gui-sorter-render`
3. 設為 **Private**(重要!)
4. 不要勾選任何初始檔案
5. Create repository

複製 GitHub 給你的指令,類似:
```bash
git remote add origin https://github.com/你的帳號/gui-sorter-render.git
git branch -M main
git push -u origin main
```

---

## 步驟 2:部署到 Render (10 分鐘)

### 2.1 註冊 Render

1. 打開 https://render.com
2. 用 GitHub 帳號登入
3. 授權 Render 存取你的 repo

### 2.2 建立 Web Service

1. 點右上角「New +」→ 「Web Service」
2. 連接你的 GitHub repo `gui-sorter-render`
3. 設定:
   - **Name**: `gui-sorter-bot`(或任何你喜歡的名字)
   - **Region**: Singapore(離台灣最近)
   - **Branch**: `main`
   - **Root Directory**: (留空)
   - **Runtime**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python bot_render.py`
   - **Instance Type**: `Free`

### 2.3 設定環境變數

在「Environment」區塊,點「Add Environment Variable」,加入:

| Key | Value |
|-----|-------|
| `TELEGRAM_BOT_TOKEN` | `<從 @BotFather 拿>` |
| `ANTHROPIC_API_KEY` | `<從 console.anthropic.com 拿，sk-ant-xxx>` |
| `NOTION_TOKEN` | `<從 notion.so/profile/integrations 拿>` |
| `NOTION_DATABASE_ID` | `<從 Notion Database URL 中間那串 32 位>` |

### 2.4 建立服務

點「Create Web Service」

Render 會開始部署,大約 3-5 分鐘。你會看到 log 跑過去。

### 2.5 取得你的固定 URL

部署完成後,左上角會顯示你的 URL:
```
https://gui-sorter-bot-xxxx.onrender.com
```

**這個 URL 永久不會變!**

---

## 步驟 3:測試(5 分鐘)

### 3.1 測試 Telegram Bot

1. 打開 Telegram,找到你的 bot
2. 傳一個連結給它
3. 等 10-30 秒(第一次會慢,喚醒中)
4. 應該會收到「✅ 已儲存!」訊息
5. 打開 Notion 確認

### 3.2 測試 Flask API

用 Postman 或 curl 測試:
```bash
curl -X POST https://gui-sorter-bot-xxxx.onrender.com/save \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.apple.com"}'
```

應該會回傳 JSON

---

## 步驟 4:建立 iOS 捷徑(5 分鐘)

照著 `iOS捷徑設定_3動作版.md` 做,只要 3 個動作!

關鍵:把 URL 改成你的 Render URL:
```
https://gui-sorter-bot-xxxx.onrender.com/save
```

---

## 步驟 5:保持服務醒著(選用,5 分鐘)

Render 免費版 15 分鐘沒流量會睡眠。

### 方法 A:Uptime Robot(推薦)

1. 註冊 https://uptimerobot.com (免費)
2. 新增監控:
   - Monitor Type: `HTTP(s)`
   - URL: `https://gui-sorter-bot-xxxx.onrender.com/`
   - Monitoring Interval: `5 分鐘`
3. 完成!服務永遠醒著

### 方法 B:接受冷啟動

- 不做任何事
- 第一次使用會等 30 秒喚醒
- 之後 15 分鐘內都是秒回

---

## 🎉 完成!

現在你有:
✅ 24/7 運作的 bot
✅ 固定 URL 的捷徑(設定一次永久有效)
✅ 費用降到原本的 7.5%
✅ 電腦可以關機了!

---

## 💰 費用計算

**Render 免費版**:
- 750 小時/月
- 如果用 Uptime Robot 會超過 → 月底停幾小時
- 或付費 $7/月,完全不停機

**Claude Haiku + Jina Reader**:
- 一天 30 篇 → 月費約 $1.5
- 一天 50 篇 → 月費約 $2.5

**總計**:$0-9/月(看你選擇)

---

## 🔧 之後要更新程式碼怎麼辦?

1. 改 `bot_render.py`
2. Git commit & push:
```bash
git add .
git commit -m "更新功能"
git push
```
3. Render 會自動重新部署(約 3 分鐘)

---

## ❓ 常見問題

**Q:第一次用要等很久**
A:冷啟動,正常。用 Uptime Robot 解決。

**Q:Render 顯示錯誤**
A:看 Logs,通常是環境變數沒設好。

**Q:想改回 Sonnet**
A:改 `bot_render.py` 第 71 行,model 改成 `claude-3-5-sonnet-20240620`

**Q:Notion 寫入失敗**
A:檢查資料庫欄位名稱是否完全一致(標題、摘要、來源...)

---

**準備好了嗎?開始部署吧!** 🚀

有任何問題隨時問我!
