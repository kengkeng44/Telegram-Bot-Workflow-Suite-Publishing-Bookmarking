import os
import logging
import threading
import base64
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
import anthropic
from notion_client import Client
from flask import Flask, request, jsonify

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")

notion = Client(auth=NOTION_TOKEN)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

logging.basicConfig(level=logging.INFO)

def analyze_with_claude(url: str = "", text: str = "", image_data: str = "", image_media_type: str = "") -> dict:
    content = []

    if image_data:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image_media_type or "image/jpeg",
                "data": image_data
            }
        })

    prompt_text = """你是一個社群內容分類助手。請仔細分析這個內容，回傳以下資訊：

1. 統整標題：用15字內描述這篇內容的核心主題，後面加上括弧寫來源簡述
   格式範例：「台北大縱走第七段健行紀錄（Threads）」、「Claude API自動化實作分享（@username）」

2. 分類：從以下選一個最符合的：
   AI科技、商業財經、生活風格、設計創意、學習成長、健康醫療、娛樂休閒、其他

3. 平台：Instagram、Threads、Twitter、YouTube、Facebook、LinkedIn、其他

4. 摘要：用2到3句話說明這篇內容的重點

5. 原文摘錄：從內文中抓出最有價值的一句話，沒有內文就留空

6. 待行動：值得深讀、參考實作、靈感收藏、資訊存檔"""

    if url:
        prompt_text += f"\n\n連結：{url}"
    if text:
        prompt_text += f"\n\n以下是貼文內文，請優先根據這個分析：\n{text}"
    if image_data:
        prompt_text += "\n\n請根據上方截圖內容分析。"
    if not url and not text and not image_data:
        prompt_text += "\n\n（無法取得內容，請根據現有資訊推測）"

    prompt_text += "\n\n請用以下格式回答，不要多餘文字：\n統整標題：xxx\n分類：xxx\n平台：xxx\n摘要：xxx\n原文摘錄：xxx\n待行動：xxx"

    content.append({"type": "text", "text": prompt_text})

    message = claude.messages.create(
        model="claude-3-5-sonnet-20240620",
        max_tokens=1024,
        messages=[{"role": "user", "content": content}]
    )

    text_resp = message.content[0].text
    result = {}
    for line in text_resp.strip().split("\n"):
        if "：" in line:
            key, value = line.split("：", 1)
            result[key.strip()] = value.strip()

    return result

def save_to_notion(url: str, data: dict):
    today = datetime.now().strftime("%Y-%m-%d")
    notion.pages.create(
        parent={"database_id": NOTION_DATABASE_ID},
        properties={
            "標題": {"title": [{"text": {"content": data.get("統整標題", "未知標題")}}]},
            "分類": {"select": {"name": data.get("分類", "其他")}},
            "平台": {"select": {"name": data.get("平台", "其他")}},
            "連結": {"url": url if url else "https://placeholder.com"},
            "摘要": {"rich_text": [{"text": {"content": data.get("摘要", "")}}]},
            "原文摘錄": {"rich_text": [{"text": {"content": data.get("原文摘錄", "")}}]},
            "待行動": {"rich_text": [{"text": {"content": data.get("待行動", "")}}]},
            "儲存日期": {"date": {"start": today}},
        }
    )

def process_url(url: str = "", text: str = "", image_data: str = "", image_media_type: str = "") -> dict:
    result = analyze_with_claude(url, text, image_data, image_media_type)
    save_to_notion(url, result)
    return result

# ── Telegram Bot ──────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if not url.startswith("http"):
        await update.message.reply_text("請傳送一個連結（http 開頭）")
        return

    await update.message.reply_text("⏳ 分析中...")

    try:
        data = process_url(url=url)
        await update.message.reply_text(
            f"✅ 已儲存！\n\n"
            f"📌 {data.get('統整標題', '未知標題')}\n"
            f"🏷️ {data.get('分類', '其他')} ｜ 📱 {data.get('平台', '其他')}\n"
            f"📝 {data.get('摘要', '')}\n"
            f"🎯 待行動：{data.get('待行動', '')}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ 發生錯誤：{str(e)}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ 讀取截圖中...")

    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        file_bytes = await file.download_as_bytearray()
        image_data = base64.b64encode(file_bytes).decode("utf-8")

        caption = update.message.caption or ""

        data = process_url(text=caption, image_data=image_data, image_media_type="image/jpeg")

        await update.message.reply_text(
            f"✅ 已儲存！\n\n"
            f"📌 {data.get('統整標題', '未知標題')}\n"
            f"🏷️ {data.get('分類', '其他')} ｜ 📱 {data.get('平台', '其他')}\n"
            f"📝 {data.get('摘要', '')}\n"
            f"🎯 待行動：{data.get('待行動', '')}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ 發生錯誤：{str(e)}")

# ── Flask HTTP Server ──────────────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/save", methods=["POST"])
def save_link():
    data = request.get_json()
    if not data or "url" not in data:
        return jsonify({"error": "缺少 url 欄位"}), 400

    url = data["url"].strip()
    text = data.get("text", "").strip()
    if not url.startswith("http"):
        return jsonify({"error": "不是有效的連結"}), 400

    try:
        result = process_url(url=url, text=text)
        return jsonify({"status": "ok", **result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@flask_app.route("/save-image", methods=["POST"])
def save_image():
    if "image" not in request.files:
        return jsonify({"error": "缺少圖片"}), 400

    file = request.files["image"]
    image_bytes = file.read()
    image_data = base64.b64encode(image_bytes).decode("utf-8")
    media_type = file.content_type or "image/jpeg"

    url = request.form.get("url", "").strip()
    text = request.form.get("text", "").strip()

    try:
        result = process_url(
            url=url,
            text=text,
            image_data=image_data,
            image_media_type=media_type
        )
        return jsonify({"status": "ok", **result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
def run_flask():
    flask_app.run(host="0.0.0.0", port=5000)

if __name__ == "__main__":
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    print("Flask server 已啟動，port 5000")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    print("Bot 已啟動，等待訊息...")
    app.run_polling()