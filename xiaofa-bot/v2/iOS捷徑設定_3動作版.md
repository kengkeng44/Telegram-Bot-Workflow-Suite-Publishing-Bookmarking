# 📱 iOS 捷徑 - 3 動作版(部署後使用)

## 部署完成後,你的固定 URL 會是:
```
https://你的專案名稱.onrender.com/save
```

---

## iOS 捷徑建立步驟

### 動作 1: 接收分享內容
```
Receive URLs and Text from Share Sheet
```

### 動作 2: 取得第一個 URL
```
Get URLs from Input
↓
Get First Item
```

### 動作 3: POST 到 Render
```
Get Contents of URL

設定:
- URL: https://你的專案名稱.onrender.com/save
- Method: POST
- Headers:
  - Content-Type: application/json
- Request Body (JSON):
{
  "url": "拖入「First Item」變數"
}
```

---

## 完整 JSON 範例(給進階使用者)

```json
{
  "url": "[這裡拖入 First Item 變數,不要打字]"
}
```

在捷徑編輯器中:
1. 點 Request Body
2. 選「JSON」
3. 在 `"url":` 後面的引號中間
4. **刪掉引號和裡面的文字**
5. 從變數選單**拖入「First Item」**

---

## 測試方法

1. 在 Safari 隨便開個網頁
2. 點分享 → 選你的捷徑
3. 等 3-5 秒
4. 打開 Notion 確認有新的一筆資料

---

## Telegram 路線(選用)

如果你還是想用 Telegram:
- 直接分享連結給你的 bot
- bot 會自動處理並存到 Notion
- 這個方式比捷徑慢一點,但可以加註解

兩種方式可以並存!

---

部署完成後記得把 URL 填進去!
