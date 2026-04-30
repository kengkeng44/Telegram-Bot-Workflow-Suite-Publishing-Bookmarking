# JOURNEY

這個專案怎麼從「一個亂寫的 bot」長成「我覺得值得放 GitHub 的東西」。
時間軸 + 我每個轉折在想什麼。

---

## Day 0 — 一個 .env 跑在自己電腦的 bot

最初版本超粗糙：
- `bot.py` 寫死所有邏輯（爬 Threads / Claude / Notion 全擠在一起）
- 金鑰寫在 `.env` 跟 `config.py`
- 跑在自己電腦上，關機就停
- 沒人看得到 code

我覺得「能用就好」，沒什麼好分享的。

---

## Day 1 上午 — 想推 GitHub，發現一堆地雷

那天我想說「這個 bot 我自己覺得不錯，不如丟 GitHub 給朋友看」。

結果一掃，**`.env` 裡有一打活生生的 token**。
- 4 個 Telegram bot token
- 2 個 Notion integration token
- Anthropic API key
- Threads App Secret + Access Token
- 中央氣象署、OpenWeatherMap key

如果直接 push，等於把家門鑰匙寫在門上。

開始 rotate 全部金鑰。原以為 30 分鐘，最後花了 1 小時，因為發現 **「rotate 一次以後，這些金鑰還是住在我硬碟上」** —— 下次截圖、下次給 AI debug、下次備份雲端，又會洩漏一次。

→ 第一個體悟：**金鑰住在哪是設計問題，不是維護問題。**

---

## Day 1 下午 — Infisical 進場

決定試 Infisical。雖然多一個依賴，但邏輯很乾淨：

```
之前：金鑰住硬碟 (.env) → N 個地方手動同步
之後：金鑰住雲端，硬碟乾淨，rotate 一次自動更新所有環境
```

設定完，砍掉本機 `.env`。心理上有種「被綁的繩子鬆了」的感覺。

順手把 cheng.robot（另一個我跑很久的 bot）也一起搬上去，連 Railway integration 也設好。**一處改動，所有環境同步**。

---

## Day 1 晚 — Threads 改版打臉

部署到 Railway 後測了一條 Threads 連結，bot 回 `❌ 沒抓到內容`。

加 debug log 才發現：HTML 載入正常（300KB），但**完全沒有 `thread_items` 字眼**。Meta 在某個時間點把未登入用戶能拿到的資料砍光，所有東西都要登入後 client-side fetch。

我之前 code 能用，是因為當時 GraphQL 對訪客還開放。**沒人告訴你 API 要關，它就是默默關了。**

→ 第二個體悟：**爬蟲是建立在別人善意之上的契約，隨時可以單方面終止。**

---

## Day 1 深夜 — Cookie 法

研究後發現解法只有「用我的登入身份去爬」。

寫了 `get_cookies.py`：
1. 本機 Playwright 開 Threads → 我手動登入
2. Script 把 cookie 存成 JSON
3. 整段 JSON 當 secret 推到 Infisical
4. bot 啟動時讀進來，每個爬蟲 request 都帶我的登入身份

第一次成功爬到 `dustin_gmat` 的貼文那一刻，覺得**這套架構真的「對」了**。

但意識到 cookie 30–90 天會過期 → 加進 TODO「自動續 cookie 半自動方案」（之後做）。

---

## Day 2 早上 — iOS Shortcut 地獄

「我想直接從 Threads app 分享 → 自動處理。」

聽起來很簡單。實際上踩了**6 個雷**：

1. **iOS Shortcut UI 變數類型轉換** — 想塞變數進 URL field 永遠失敗（RTF→URL bug）
2. **`https://t.me/...` deep link 不跳 Telegram，跳 Safari** — Universal Links 在我手機上壞掉
3. **`tg://` URL scheme 也跳 Safari** — 不知道什麼原因
4. **Telegram 自家 Send Message action 不支援 bot** — bot 在 Telegram 不算 contact
5. **iOS Shortcut UI 一直把貼上的東西自動轉成藍色變數** — 連手打單一字母都會變
6. **手動修分隔符、加文字 action、加結合文字 action 都沒解** — 設定組合永遠錯

花了大概 **2 小時**鬥這個 UI，連 ChatGPT 跟搜尋都解不了。最後接受現實：**iOS Shortcuts UI 在我這台手機上就是有 bug**。

→ 第三個體悟：**「免按鍵」要付出的工程代價，可能比按那一下還大。**

---

## Day 2 中午 — 折衷：HTTP webhook + Telegram 內建分享

換思路：

**方向 A**：寫一個 `/ingest` HTTP endpoint，繞過 Telegram，給未來用
- 加 aiohttp 在 bot.py 裡並行跑
- 加 INGEST_SECRET 自訂門禁
- 任何工具都可以 POST URL 進來自動處理（curl、Mac shortcuts、n8n、IFTTT）

**方向 B**：日常還是用 Telegram 內建分享
- 看到貼文 → 分享 → Telegram → bot → 按傳送
- 4 個 tap，但保證能用

A 解決「未來自動化」的可能性，B 解決「現在能用」的需求。**兩個都做了**。

---

## Day 2 下午 — `/sync threads`

「我已經有上百則 saved 在 Threads，能不能 bot 自己去掃？」

實作：
- 用現有 cookie 開 saved 頁
- 滾動載入 + 抽貼文連結
- 跟 Notion 比對 → 新的自動處理

**第一次跑只抓到 1 則。** 想不通為什麼。

加了 final URL 的 log → 發現問題不是 code，是**我本機 cookie 是錯的帳號**：登入的是我 alt 帳號，alt 帳號 saved 只有 1 則；主帳號才有上百則。

換用主帳號跑一次 `get_cookies.py` → 推 Infisical → bot 收到上百則。

→ 第四個體悟：**「找不到」的 bug 通常不在解析邏輯，在輸入資料的來源假設。**

---

## 系統現況（Day 2 結尾）

```
[Threads / IG / FB / 任何網頁]
        │
        ├── 手機分享 (4 tap) ──────────┐
        ├── /ingest webhook ──────────┤
        └── /sync threads (批次掃)──→ Telegram bot
                                          ↓
                                  Threads scraper (cookie auth)
                                          ↓
                                  Claude Haiku (摘要/分類/情緒/標籤)
                                          ↓
                                  Notion Database
```

配套：
- Infisical 雲端 secret，硬碟 0 殘留
- Railway 自動部署
- gitleaks pre-commit
- `/usage`、`/sync`、`/recent`、`/stats` 指令
- 開放的 `/ingest` HTTP API

---

## 我學到的

1. **Secret 管理是設計問題不是維護問題** — `.env` 一定會漏，找 secret manager
2. **跟 AI 互動 = 永久 chat log** — 任何看到 secret 的 debug 流程都是洩漏事件
3. **爬蟲是脆弱的契約** — 平台改版你只能跟著改
4. **iOS Shortcut UI 不可信賴** — 硬要自動化，工程代價可能高過手動操作
5. **「找不到」常常是輸入錯，不是邏輯錯** — bug 出現時先質疑前提
6. **早期專案的安全模型轉折點 = 第一次 push 公開 repo**

---

## 還沒做完的（在 [TODO.md](TODO.md)）

- 自動續 cookie（半自動）
- `/sync` 多帳號支援
- `/sync` 加 Telegram Saved Messages 來源
- 修 `/usage` Railway schema
- 改名「Data Hoarder」一氣呵成版
- xiaofa-bot 接 Infisical
- GitHub Actions gitleaks

---

寫這份不是炫耀做了多少，是怕**下次自己回來看時，忘了當時為什麼做這些選擇**。

如果你也在做類似專案，歡迎踩我踩過的雷。
