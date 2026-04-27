import asyncio
import json
import logging
import os
import requests
from datetime import datetime

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
import anthropic
from notion_client import Client as NotionClient

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
PORT = int(os.environ.get("PORT", 8080))

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
notion = NotionClient(auth=NOTION_TOKEN)


def _read_with_jina(url: str) -> str:
    try:
        resp = requests.get(
            f"https://r.jina.ai/{url}",
            headers={"Accept": "text/plain"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.text[:5000]
    except Exception as e:
        logger.error("Jina error: %s", e)
        return ""


def _detect_source(url: str) -> str:
    u = url.lower()
    if "instagram.com" in u:
        return "Instagram"
    if "threads.net" in u:
        return "Threads"
    if "twitter.com" in u or "x.com" in u:
        return "Twitter"
    if "youtube.com" in u or "youtu.be" in u:
        return "YouTube"
    if "facebook.com" in u or "fb.com" in u:
        return "Facebook"
    if "linkedin.com" in u:
        return "LinkedIn"
    return "其他"


def _analyse(content: str, url: str) -> dict:
    prompt = f"""分析以下網頁內容，只回傳 JSON，不要其他文字。

URL: {url}
內容: {content[:3000]}

JSON 格式:
{{
  "標題": "文章標題 (50字以內)",
  "摘要": "重點摘要 (100字以內)",
  "標籤": "最相關的單一標籤，例如: 科技/設計/商業/美食/旅遊/生活/其他",
  "作者": "作者或帳號名稱，找不到則填空字串",
  "內容": "正文前200字"
}}"""

    try:
        msg = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        result = json.loads(text)
    except Exception as e:
        logger.error("Claude error: %s", e)
        result = {
            "標題": url[:100],
            "摘要": "無法解析內容",
            "標籤": "其他",
            "作者": "",
            "內容": "",
        }

    result["來源"] = _detect_source(url)
    return result


def _save_to_notion(url: str, data: dict) -> str:
    page = notion.pages.create(
        parent={"database_id": NOTION_DATABASE_ID},
        properties={
            "標題": {"title": [{"text": {"content": data.get("標題", url)[:100]}}]},
            "摘要": {"rich_text": [{"text": {"content": data.get("摘要", "")[:2000]}}]},
            "來源": {"select": {"name": data.get("來源", "其他")}},
            "標籤": {"select": {"name": data.get("標籤", "其他")}},
            "連結": {"url": url},
            "作者": {"rich_text": [{"text": {"content": data.get("作者", "")[:200]}}]},
            "內容": {"rich_text": [{"text": {"content": data.get("內容", "")[:2000]}}]},
            "收藏日期": {"date": {"start": datetime.now().strftime("%Y-%m-%d")}},
        },
    )
    return page["id"]


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "歡迎使用歸檔機器人！\n\n"
        "直接傳 URL 給我，我會自動：\n"
        "1. 用 Jina Reader 讀取網頁\n"
        "2. 用 Claude Haiku 分析摘要\n"
        "3. 存入你的 Notion 資料庫\n\n"
        "支援：Instagram · Threads · Twitter/X · YouTube · Facebook · LinkedIn"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()

    if not (text.startswith("http://") or text.startswith("https://")):
        await update.message.reply_text("請傳送有效的 URL（需以 http:// 或 https:// 開頭）")
        return

    url = text.split()[0]
    msg = await update.message.reply_text("⏳ 處理中，請稍候...")

    try:
        content = await asyncio.to_thread(_read_with_jina, url)
        data = await asyncio.to_thread(_analyse, content, url)
        await asyncio.to_thread(_save_to_notion, url, data)

        reply = (
            f"✅ 已儲存到 Notion\n\n"
            f"標題：{data.get('標題', 'N/A')}\n"
            f"摘要：{data.get('摘要', 'N/A')}\n"
            f"標籤：{data.get('標籤', 'N/A')}\n"
            f"來源：{data.get('來源', 'N/A')}\n"
            f"作者：{data.get('作者') or '未知'}"
        )
        await msg.edit_text(reply)

    except Exception as e:
        logger.error("Processing error: %s", e)
        await msg.edit_text(f"❌ 處理失敗：{e}")


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    if RENDER_EXTERNAL_URL:
        logger.info("Webhook mode — port %d, url %s", PORT, RENDER_EXTERNAL_URL)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=f"{RENDER_EXTERNAL_URL}/webhook",
            url_path="webhook",
            drop_pending_updates=True,
            stop_signals=None,
        )
    else:
        logger.info("Polling mode")
        app.run_polling(drop_pending_updates=True, stop_signals=None)


if __name__ == "__main__":
    main()
