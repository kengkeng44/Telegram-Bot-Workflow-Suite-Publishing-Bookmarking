import os
import logging
from dotenv import load_dotenv
import anthropic
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

load_dotenv()

XIAOFA_BOT_TOKEN = os.getenv("XIAOFA_BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
THREADS_ACCESS_TOKEN = os.getenv("THREADS_ACCESS_TOKEN")
THREADS_USER_ID = os.getenv("THREADS_USER_ID")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

logging.basicConfig(level=logging.INFO)

# ── 用 Claude 生成文案 ──────────────────────────────────────
def generate_post(draft: str) -> str:
    message = claude.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"""你是一個社群媒體文案專家。請根據以下草稿，生成一篇吸引人的貼文。

要求：
- 保留原本的核心意思
- 加上適合的 emoji
- 結尾加上2到3個相關 hashtag
- 文字自然流暢，不要太商業化
- 繁體中文

草稿：{draft}

請直接輸出貼文內容，不要加任何說明。"""
        }]
    )
    return message.content[0].text

# ── 發到 Threads ────────────────────────────────────────────
def post_to_threads(text: str) -> bool:
    try:
        # 第一步：建立容器
        res = requests.post(
            f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads",
            params={
                "media_type": "TEXT",
                "text": text,
                "access_token": THREADS_ACCESS_TOKEN,
            }
        )
        creation_id = res.json().get("id")
        if not creation_id:
            return False

        # 第二步：發布
        res2 = requests.post(
            f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads_publish",
            params={
                "creation_id": creation_id,
                "access_token": THREADS_ACCESS_TOKEN,
            }
        )
        return "id" in res2.json()
    except Exception as e:
        logging.error(f"Threads 發文錯誤：{e}")
        return False

# ── Telegram 指令處理 ───────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 我是小發！\n\n"
        "傳給我草稿，我幫你生成文案後發到各平台。\n\n"
        "直接傳文字給我就好 ✍️"
    )

async def handle_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    draft = update.message.text.strip()
    await update.message.reply_text("✍️ 生成文案中...")

    generated = generate_post(draft)
    context.user_data["generated_post"] = generated

    keyboard = [
        [InlineKeyboardButton("✅ 發到 Threads", callback_data="post_threads")],
        [InlineKeyboardButton("🔄 重新生成", callback_data="regenerate")],
        [InlineKeyboardButton("❌ 取消", callback_data="cancel")],
    ]
    context.user_data["draft"] = draft

    await update.message.reply_text(
        f"📝 生成結果：\n\n{generated}\n\n要發出去嗎？",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "post_threads":
        await query.edit_message_text("⏳ 發文中...")
        success = post_to_threads(context.user_data.get("generated_post", ""))
        if success:
            await query.edit_message_text("✅ 已成功發到 Threads！")
        else:
            await query.edit_message_text("❌ 發文失敗，請檢查 Token 是否過期")

    elif query.data == "regenerate":
        draft = context.user_data.get("draft", "")
        await query.edit_message_text("🔄 重新生成中...")
        generated = generate_post(draft)
        context.user_data["generated_post"] = generated

        keyboard = [
            [InlineKeyboardButton("✅ 發到 Threads", callback_data="post_threads")],
            [InlineKeyboardButton("🔄 重新生成", callback_data="regenerate")],
            [InlineKeyboardButton("❌ 取消", callback_data="cancel")],
        ]
        await query.edit_message_text(
            f"📝 生成結果：\n\n{generated}\n\n要發出去嗎？",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data == "cancel":
        await query.edit_message_text("❌ 已取消")

# ── 啟動 ────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ApplicationBuilder().token(XIAOFA_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_draft))
    app.add_handler(CallbackQueryHandler(handle_callback))
    print("小發已啟動 🚀")
    app.run_polling()