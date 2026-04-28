import os
import logging
import threading
import base64
import requests
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

def get_clean_content(url: str) -> str:
    """使用 Jina Reader 取得乾淨的網頁內容"""
    try:
        jina_url = f"https://r.jina.ai/{url}"
        response = requests.get(jina_url, timeout=30)
        response.raise_for_status()
        return response.text
    except Exception as e:
        logging.error(f"Jina Reader 失敗: {e}, 改用原始 URL")
        # 如果 Jina 失敗,降級到原始請求
        try:
            response = requests.get(url, timeout=30)
            return response.text[:10000]  # 限制大小
        except:
            return ""

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

    prompt_text = """請分析以下內容並用繁體中文回覆,格式如下(每行一個欄位):

1. 標題:15字內的吸引人標題(若是社群貼文請提取關鍵訊息,不要複製@username)
2. 摘要:用條列式重點整理主要內容
   AI工具直接寫工具名稱和用途,不需完整句子
3. 來源:Instagram/Threads/Twitter/YouTube/Facebook/LinkedIn等
4. 標籤:2個最相關的繁體中文標籤
5. 作者:作者名稱或帳號(若無法判斷留空)
6. 內容:完整的純文字內容(去除 HTML 和廣告)

"""

    if url:
        # 使用 Jina Reader 取得乾淨內容
        clean_content = get_clean_content(url)
        if clean_content:
            prompt_text += f"\n\n網址:{url}\n\n網頁內容:\n{clean_content[:8000]}"  # 限制長度
        else:
            prompt_text += f"\n\n網址:{url}\n(無法取得內容,僅使用網址資訊)"
    
    if text:
        prompt_text += f"\n\n使用者提供的額外說明:\n{text}"
    
    if image_data:
        prompt_text += "\n\n圖片已附加,請分析圖片內容。"
    
    if not url and not text and not image_data:
        prompt_text += "\n\n(警告:沒有提供任何內容)"

    prompt_text += "\n\n請用以下格式回覆,不要加任何其他說明:\n標題:xx\n摘要:xx\n來源:xx\n標籤:xx\n作者:xx\n內容:xxx"

    content.append({"type": "text", "text": prompt_text})

    # 🔥 改用 Haiku 4.6 - 省錢 85%
    message = claude.messages.create(
        model="claude-haiku-4-20250514",  # 從 Sonnet 改成 Haiku
        max_tokens=1024,
        messages=[{"role": "user", "content": content}]
    )

    text_resp = message.content[0].text
    result = {}
    for line in text_resp.strip().split("\n"):
        if ":" in line:
            key, value = line.split(":", 1)
            result[key.strip()] = value.strip()

    return result

def save_to_notion(url: str, data: dict):
    today = datetime.now().strftime("%Y-%m-%d")
    notion.pages.create(
        parent={"database_id": NOTION_DATABASE_ID},
        properties={
            "標題": {"title": [{"text": {"content": data.get("標題", "待整理標題")}}]},
            "摘要": {"rich_text": [{"text": {"content": data.get("摘要", "")}}]},
            "來源": {"select": {"name": data.get("來源", "其他")}},
            "標籤": {"select": {"name": data.get("標籤", "其他")}},
            "連結": {"url": url if url else "https://placeholder.com"},
            "作者": {"rich_text": [{"text": {"content": data.get("作者", "")}}]},
            "內容": {"rich_text": [{"text": {"content": data.get("內容", "")[:2000]}}]},  # Notion 限制
            "收藏日期": {"date": {"start": today}},
        }
    )

def process_url(url: str = "", text: str = "", image_data: str = "", image_media_type: str = "") -> dict:
    result = analyze_with_claude(url, text, image_data, image_media_type)
    save_to_notion(url, result)
    return result

# ══ Telegram Bot 處理器 ══════════════════════
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if not url.startswith("http"):
        await update.message.reply_text("請傳送有效網址(http 開頭)")
        return

    await update.message.reply_text("🤖 處理中...")

    try:
        data = process_url(url=url)
        await update.message.reply_text(
            f"✅ 已儲存!\n\n"
            f"📄 {data.get('標題', '待整理標題')}\n"
            f"🔖 {data.get('摘要', '無摘要')} 📌 {data.get('來源', '其他')}\n"
            f"✍️ 作者: {data.get('作者', '未知')}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ 處理失敗: {str(e)}")

# ══ Flask HTTP Server ══════════════════════
flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "ok", "message": "Bot is running on Render!"})

@flask_app.route("/save", methods=["POST"])
def save_link():
    data = request.get_json()
    if not data or "url" not in data:
        return jsonify({"error": "缺少 url 參數"}), 400

    url = data["url"].strip()
    text = data.get("text", "").strip()
    if not url.startswith("http"):
        return jsonify({"error": "無效網址"}), 400

    try:
        result = process_url(url=url, text=text)
        return jsonify({"status": "ok", **result})
    except Exception as e:
        logging.error(f"Flask /save error: {e}")
        return jsonify({"error": str(e)}), 500

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    print(f"Flask server 啟動於 port {os.environ.get('PORT', 5000)}")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot 啟動,等待訊息...")
    app.run_polling()
