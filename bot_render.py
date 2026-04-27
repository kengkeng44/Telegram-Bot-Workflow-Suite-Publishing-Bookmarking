import asyncio
import json
import logging
import os
import sys
import requests
from datetime import datetime

# Force stdout/stderr to be unbuffered so Render shows our logs immediately
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

print("=== bot_render.py starting ===", flush=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"FATAL: missing environment variable '{name}'", flush=True)
        sys.exit(1)
    print(f"env {name}: set ({len(val)} chars)", flush=True)
    return val


TELEGRAM_BOT_TOKEN = _require_env("TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = _require_env("ANTHROPIC_API_KEY")
NOTION_TOKEN = _require_env("NOTION_TOKEN")
NOTION_DATABASE_ID = _require_env("NOTION_DATABASE_ID")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
PORT = int(os.environ.get("PORT", 8080))
print(f"RENDER_EXTERNAL_URL: {RENDER_EXTERNAL_URL or '(empty - will use polling)'}", flush=True)
print(f"PORT: {PORT}", flush=True)

print("Importing telegram/anthropic/notion...", flush=True)
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
import anthropic
from notion_client import Client as NotionClient
print("Imports OK", flush=True)

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
notion = NotionClient(auth=NOTION_TOKEN)
print("Clients initialized", flush=True)


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


VALID_CATEGORIES = ("AI科技", "生活風格", "學習成長", "設計創意", "商業財經")


def _analyse(content: str, url: str) -> dict:
    prompt = f"""分析以下網頁內容，只回傳 JSON，不要其他文字。

URL: {url}
內容: {content[:3000]}

JSON 格式（嚴格遵守欄位名與限制）:
{{
  "標題": "貼文核心主題（30字內，不要包含作者名稱）",
  "作者": "作者帳號或姓名，盡量用 @handle 格式；找不到則空字串",
  "摘要": "用一句話總結重點（50字內）",
  "分類": "從以下五選一：AI科技 / 生活風格 / 學習成長 / 設計創意 / 商業財經",
  "原文摘錄": "從原文擷取最有代表性的一句話（80字內）"
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
            "標題": url[:80],
            "作者": "",
            "摘要": "無法解析內容",
            "分類": "AI科技",
            "原文摘錄": "",
        }

    # restrict 分類 to whitelist
    if result.get("分類") not in VALID_CATEGORIES:
        result["分類"] = "AI科技"

    result["平台"] = _detect_source(url)
    return result


_notion_props_cache: dict | None = None


def _db_properties() -> dict:
    """Return {name: type} for properties on the target Notion database."""
    global _notion_props_cache
    if _notion_props_cache is None:
        db = notion.databases.retrieve(NOTION_DATABASE_ID)
        _notion_props_cache = {
            name: prop["type"] for name, prop in db["properties"].items()
        }
        logger.info("Notion DB properties: %s", _notion_props_cache)
    return _notion_props_cache


def _save_to_notion(url: str, data: dict) -> str:
    prop_types = _db_properties()
    today = datetime.now().strftime("%Y-%m-%d")

    # Build title in "topic（@author）" format
    topic = (data.get("標題") or url)[:80]
    author = (data.get("作者") or "").strip()
    if author and not author.startswith("@"):
        author = "@" + author
    title = f"{topic}（{author}）" if author else topic

    candidates: dict = {
        "標題": {"title": [{"text": {"content": title[:100]}}]},
        "摘要": {"rich_text": [{"text": {"content": (data.get("摘要") or "")[:2000]}}]},
        "分類": {"select": {"name": data.get("分類", "AI科技")}},
        "原文摘錄": {"rich_text": [{"text": {"content": (data.get("原文摘錄") or "")[:2000]}}]},
        "連結": {"url": url},
        "平台": {"select": {"name": data.get("平台", "其他")}},
        "儲存日期": {"date": {"start": today}},
    }

    # 待行動: detect Status vs Select from schema (different API payload)
    todo_type = prop_types.get("待行動")
    if todo_type == "status":
        candidates["待行動"] = {"status": {"name": "待整理"}}
    elif todo_type == "select":
        candidates["待行動"] = {"select": {"name": "待整理"}}

    # only write properties that actually exist in the database
    properties = {k: v for k, v in candidates.items() if k in prop_types}
    missing = set(candidates) - set(properties)
    if missing:
        logger.warning("Skipping missing Notion properties: %s", missing)

    page = notion.pages.create(
        parent={"database_id": NOTION_DATABASE_ID},
        properties=properties,
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
            f"作者：{data.get('作者') or '未知'}\n"
            f"摘要：{data.get('摘要', 'N/A')}\n"
            f"分類：{data.get('分類', 'N/A')}\n"
            f"平台：{data.get('平台', 'N/A')}"
        )
        await msg.edit_text(reply)

    except Exception as e:
        logger.error("Processing error: %s", e)
        await msg.edit_text(f"❌ 處理失敗：{e}")


def main() -> None:
    print("Building Application...", flush=True)
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Handlers registered", flush=True)

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
    try:
        main()
    except Exception as e:
        import traceback
        print(f"FATAL: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        sys.exit(1)
