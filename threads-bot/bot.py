"""
靈感收集機器人 (Telegram Bot)
- 收任何 URL（Threads / YouTube / X / IG / 一般網頁）或純文字
- Claude 分析摘要 / 分類 / 關鍵字 / 原文摘錄
- 寫入 Notion Database
"""
import os
import re
import json
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path

from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from aiohttp import web
from playwright.async_api import async_playwright
from notion_client import Client as NotionClient
from anthropic import Anthropic
from tenacity import retry, stop_after_attempt, wait_exponential, before_sleep_log
import jmespath

# ==== 環境變數 ====
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
THREADS_STATE_JSON = os.environ.get("THREADS_STATE_JSON")  # 可選：登入後的 storage_state JSON
BOT_USER_ID = int(TELEGRAM_TOKEN.split(":")[0])  # bot 自己的 user id = token 冒號前的數字
INGEST_SECRET = os.environ.get("INGEST_SECRET")  # webhook 認證用的 secret，不設則 webhook 不啟動

# ==== Clients & 共用狀態 ====
notion = NotionClient(auth=NOTION_TOKEN)
claude = Anthropic(api_key=ANTHROPIC_API_KEY)
TZ_TAIPEI = timezone(timedelta(hours=8))
CATEGORIES = ["AI科技", "生活風格", "學習成長", "設計創意"]
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# 共用 browser + 序列化 scrape
_browser = None
_playwright = None
_browser_lock = asyncio.Lock()
SCRAPE_SEM = asyncio.Semaphore(1)


def _allowed(update: Update) -> bool:
    if not ALLOWED_USER_ID:
        return True
    uid = update.effective_user.id
    return uid == ALLOWED_USER_ID or uid == BOT_USER_ID


# ==== 平台偵測 ====
PLATFORM_RULES = [
    ("Threads", re.compile(r"threads\.(net|com)", re.I)),
    ("YouTube", re.compile(r"(youtube\.com|youtu\.be)", re.I)),
    ("X", re.compile(r"(twitter\.com|x\.com)", re.I)),
    ("Instagram", re.compile(r"instagram\.com", re.I)),
    ("TikTok", re.compile(r"tiktok\.com", re.I)),
    ("Facebook", re.compile(r"facebook\.com|fb\.watch", re.I)),
]


def detect_platform(url: str) -> str:
    for name, pat in PLATFORM_RULES:
        if pat.search(url):
            return name
    return "Web"


# ==== 共用 browser ====
async def _ensure_browser():
    global _browser, _playwright
    if _browser is not None:
        return _browser
    async with _browser_lock:
        if _browser is None:
            _playwright = await async_playwright().start()
            _browser = await _playwright.chromium.launch(headless=True)
    return _browser


async def _block_heavy_resources(route):
    if route.request.resource_type in {"image", "media", "font"}:
        await route.abort()
    else:
        await route.continue_()


# ==== Threads 專用 scrape（GraphQL 攔截） ====
def _extract_image_urls(node: dict) -> list[str]:
    urls = []
    single = jmespath.search("image_versions2.candidates[0].url", node)
    if single:
        urls.append(single)
    for u in (jmespath.search("carousel_media[*].image_versions2.candidates[0].url", node) or []):
        if u and u not in urls:
            urls.append(u)
    return urls


def _nested_lookup(key: str, obj):
    """遞迴在 dict/list 中找所有 key 對應的值。"""
    results = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                results.append(v)
            results.extend(_nested_lookup(key, v))
    elif isinstance(obj, list):
        for item in obj:
            results.extend(_nested_lookup(key, item))
    return results


def _extract_post_from_html(html: str) -> dict | None:
    """Threads 2026 改版：資料嵌在 <script type=\"application/json\" data-sjs> 內。"""
    pattern = r'<script[^>]*type="application/json"[^>]*data-sjs[^>]*>(.*?)</script>'
    matches = re.findall(pattern, html, re.DOTALL)
    log.info(f"[scrape_threads] HTML 中找到 {len(matches)} 個 data-sjs script tags")

    # Path 1: 找含 thread_items 的（登入用戶版）
    candidates = [m for m in matches if "thread_items" in m]
    log.info(f"[scrape_threads] 含 thread_items: {len(candidates)} 個")
    for raw in candidates:
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for items in _nested_lookup("thread_items", data):
            if isinstance(items, list):
                for it in items:
                    post = it.get("post") if isinstance(it, dict) else None
                    if post and isinstance(post, dict):
                        return post

    # Path 2: 找含 caption 的任何 script tag，nested_lookup 拼湊資料（未登入版常用）
    cap_candidates = [m for m in matches if '"caption"' in m and '"text"' in m]
    log.info(f"[scrape_threads] 含 caption+text: {len(cap_candidates)} 個")
    for raw in cap_candidates:
        try:
            data = json.loads(raw)
        except Exception:
            continue
        captions = _nested_lookup("caption", data)
        for cap in captions:
            if isinstance(cap, dict) and isinstance(cap.get("text"), str) and cap["text"].strip():
                username = ""
                for u in _nested_lookup("user", data):
                    if isinstance(u, dict) and isinstance(u.get("username"), str):
                        username = u["username"]
                        break
                return {
                    "caption": cap,
                    "user": {"username": username},
                    "image_versions2": {},
                }
    return None


def _get_meta(html: str, prop: str) -> str | None:
    """彈性匹配 <meta property=X content=Y> 或 <meta content=Y property=X>，引號 ' 或 " 都支援。"""
    for pattern in [
        rf'<meta\s+[^>]*property=["\']{re.escape(prop)}["\'][^>]*content=["\']([^"\']*)["\']',
        rf'<meta\s+[^>]*content=["\']([^"\']*)["\'][^>]*property=["\']{re.escape(prop)}["\']',
        rf'<meta\s+[^>]*name=["\']{re.escape(prop)}["\'][^>]*content=["\']([^"\']*)["\']',
    ]:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _extract_from_og_meta(html: str) -> dict | None:
    desc = _get_meta(html, "og:description")
    title = _get_meta(html, "og:title")
    image = _get_meta(html, "og:image")
    log.info(f"[scrape_threads] og 偵測: description={'有' if desc else '無'}({len(desc) if desc else 0}字), title={'有' if title else '無'}, image={'有' if image else '無'}")
    if not desc or not desc.strip():
        return None
    text = desc
    author_raw = title or ""
    author_match = re.search(r'@(\w+)', author_raw)
    author = author_match.group(1) if author_match else author_raw.split(" on Threads")[0].strip()
    return {
        "text": text,
        "author": author,
        "image_urls": [image] if image else [],
    }


async def _scrape_threads(url: str) -> dict:
    log.info(f"[scrape_threads] start url={url}")
    browser = await _ensure_browser()
    captured = []
    storage_state = None
    if THREADS_STATE_JSON:
        try:
            storage_state = json.loads(THREADS_STATE_JSON)
            log.info(f"[scrape_threads] 使用登入 cookie ({len(storage_state.get('cookies', []))} cookies)")
        except Exception as e:
            log.warning(f"[scrape_threads] THREADS_STATE_JSON parse 失敗: {e}")
    context = await browser.new_context(user_agent=USER_AGENT, storage_state=storage_state)
    await context.route("**/*", _block_heavy_resources)
    page = await context.new_page()

    async def _on_response(response):
        if "/graphql/query" in response.url or "BarcelonaPostPageQuery" in response.url:
            try:
                captured.append(await response.json())
            except Exception as e:
                log.warning(f"[scrape_threads] 攔截到 graphql 但 parse json 失敗: {e}")

    page.on("response", _on_response)
    final_url = None
    page_status = None
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        final_url = page.url
        page_status = resp.status if resp else None
        log.info(f"[scrape_threads] page loaded status={page_status} final_url={final_url}")
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 8
        while not captured and loop.time() < deadline:
            await asyncio.sleep(0.2)
        if captured:
            await asyncio.sleep(0.6)
        html = await page.content()
    except Exception as e:
        log.error(f"[scrape_threads] page.goto 失敗: {type(e).__name__}: {e}")
        html = ""
    finally:
        await context.close()

    log.info(f"[scrape_threads] captured={len(captured)} graphql responses, html_len={len(html)}")

    for data in captured:
        node = jmespath.search("data.data.containing_thread.thread_items[*].post | [0]", data)
        if node:
            log.info(f"[scrape_threads] ✅ jmespath 命中 node, author={node.get('user', {}).get('username', '')}")
            return {
                "text": (node.get("caption") or {}).get("text", ""),
                "author": node.get("user", {}).get("username", ""),
                "image_urls": _extract_image_urls(node),
            }

    if captured:
        log.warning(
            f"[scrape_threads] 抓到 {len(captured)} 個 graphql response 但 jmespath 都找不到節點。"
            f"第一個 response 的 top-level keys: {list(captured[0].keys()) if isinstance(captured[0], dict) else type(captured[0]).__name__}"
        )

    # 新版 Threads (2026) 改用 server-side render，資料嵌在 HTML script tag
    post_node = _extract_post_from_html(html)
    if post_node:
        log.info(f"[scrape_threads] ✅ HTML script tag fallback 命中, author={post_node.get('user', {}).get('username', '')}")
        return {
            "text": (post_node.get("caption") or {}).get("text", ""),
            "author": post_node.get("user", {}).get("username", ""),
            "image_urls": _extract_image_urls(post_node),
        }

    match = re.search(r'"caption":\{"text":"([^"]+)"', html)
    if match:
        log.info("[scrape_threads] ⚠️ regex fallback 命中（無作者/圖片）")
        return {"text": match.group(1), "author": "", "image_urls": []}

    # 最後保底：Open Graph meta（摘要而非完整內文，但保證有東西）
    og = _extract_from_og_meta(html)
    if og:
        log.info(f"[scrape_threads] ⚠️ og:meta fallback 命中, author={og['author']}, text_len={len(og['text'])}")
        return og

    log.warning(
        f"[scrape_threads] ❌ 全部失敗。captured={len(captured)}, html_len={len(html)}, "
        f"final_url={final_url}, page_status={page_status}"
    )
    return {"text": "", "author": "", "image_urls": []}


# ==== 通用網頁 scrape（meta tags + 正文） ====
async def _scrape_generic(url: str) -> dict:
    browser = await _ensure_browser()
    context = await browser.new_context(user_agent=USER_AGENT)
    await context.route("**/*", _block_heavy_resources)
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1.5)  # 給 SPA 一點時間水合
        meta = await page.evaluate("""() => {
            const get = (sel) => document.querySelector(sel)?.content || '';
            return {
                title: document.title || '',
                description: get('meta[property="og:description"]') || get('meta[name="description"]'),
                site_name: get('meta[property="og:site_name"]'),
                image: get('meta[property="og:image"]') || get('meta[name="twitter:image"]'),
                body: (document.body?.innerText || '').slice(0, 5000),
            };
        }""")
    finally:
        await context.close()

    parts = [p for p in (meta["title"], meta["description"], meta["body"]) if p]
    return {
        "text": "\n\n".join(parts),
        "author": meta.get("site_name", ""),
        "image_urls": [meta["image"]] if meta.get("image") else [],
    }


async def scrape_url(url: str, platform: str) -> dict:
    """根據平台 dispatch 到對應 scraper。Browser 共用、SCRAPE_SEM 序列化。"""
    async with SCRAPE_SEM:
        if platform == "Threads":
            return await _scrape_threads(url)
        return await _scrape_generic(url)


# ==== Claude 分析 ====
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10),
       before_sleep=before_sleep_log(log, logging.WARNING), reraise=True)
def analyze_with_claude(text: str, platform: str, fallback_author: str = "") -> dict:
    prompt = f"""分析以下「{platform}」來源的內容，回傳純 JSON（不含 markdown fence）：
{{
  "title": "20 字內主題（不要含作者帳號）",
  "author": "原作者帳號或來源名稱；若提示為空且內容裡看得出作者就填，否則回空字串",
  "summary": "整體內容描述（50 字內中文）",
  "category": "從這四選一：{ ' / '.join(CATEGORIES) }",
  "excerpt": "從內容直接摘錄 1 句最關鍵原句（不可改寫）；若是純文字筆記沒明顯金句就回空字串",
  "keywords": ["3~5 個關鍵字"]
}}

提示：原作者 = {fallback_author!r}

內容：
{text[:6000]}
"""
    resp = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = re.sub(r"^```json\s*|\s*```$", "", resp.content[0].text.strip(), flags=re.MULTILINE).strip()
    result = json.loads(raw)
    if result.get("category") not in CATEGORIES:
        result["category"] = "AI科技"
    if not isinstance(result.get("keywords"), list):
        result["keywords"] = []
    if not result.get("author"):
        result["author"] = fallback_author
    return result


# ==== Notion ====
def _existing_urls_from_db() -> set[str]:
    urls, cursor = set(), None
    while True:
        kwargs = {"database_id": NOTION_DATABASE_ID, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.databases.query(**kwargs)
        for p in resp["results"]:
            for t in p["properties"].get("標題", {}).get("title", []):
                link = (t.get("text") or {}).get("link") or {}
                if link.get("url"):
                    urls.add(link["url"])
        if not resp.get("has_more"):
            break
        cursor = resp["next_cursor"]
    return urls


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10),
       before_sleep=before_sleep_log(log, logging.WARNING), reraise=True)
def write_to_notion(source: dict, analysis: dict) -> str:
    """source 必含 'platform'；可選 'url'、'image_urls'"""
    author = analysis.get("author", "")
    title = analysis["title"][:60]
    if author:
        title = f"{title}（@{author}）" if not author.startswith("@") else f"{title}（{author}）"
    title = title[:100]

    title_text = {"content": title}
    if source.get("url"):
        title_text["link"] = {"url": source["url"]}

    properties = {
        "標題": {"title": [{"text": title_text}]},
        "摘要": {"rich_text": [{"text": {"content": analysis["summary"][:2000]}}]},
        "分類": {"select": {"name": analysis["category"]}},
        "狀態": {"select": {"name": "待整理"}},
        "原文摘錄": {"rich_text": [{"text": {"content": (analysis.get("excerpt") or "")[:2000]}}]},
        "平台": {"select": {"name": source["platform"]}},
        "關鍵字": {"multi_select": [{"name": k[:100]} for k in analysis.get("keywords", [])[:10] if k]},
    }

    try:
        page = notion.pages.create(parent={"database_id": NOTION_DATABASE_ID}, properties=properties)
    except Exception as e:
        if "狀態" in str(e) or "status" in str(e).lower():
            properties["狀態"] = {"status": {"name": "待整理"}}
            page = notion.pages.create(parent={"database_id": NOTION_DATABASE_ID}, properties=properties)
        else:
            raise

    image_urls = source.get("image_urls", [])[:10]
    if image_urls:
        notion.blocks.children.append(
            block_id=page["id"],
            children=[{"type": "image", "image": {"type": "external", "external": {"url": u}}} for u in image_urls],
        )
    return page["url"]


def _count_today_in_notion() -> int:
    today_start = datetime.now(TZ_TAIPEI).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    count, cursor = 0, None
    while True:
        kwargs = {
            "database_id": NOTION_DATABASE_ID,
            "filter": {"timestamp": "created_time", "created_time": {"on_or_after": today_start}},
            "page_size": 100,
        }
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.databases.query(**kwargs)
        count += len(resp["results"])
        if not resp.get("has_more"):
            break
        cursor = resp["next_cursor"]
    return count


def _list_recent_in_notion(limit: int = 5) -> list[dict]:
    resp = notion.databases.query(
        database_id=NOTION_DATABASE_ID,
        sorts=[{"timestamp": "created_time", "direction": "descending"}],
        page_size=limit,
    )
    items = []
    for p in resp["results"]:
        title_arr = p["properties"].get("標題", {}).get("title", [])
        title = title_arr[0]["text"]["content"] if title_arr else "（無標題）"
        cat = (p["properties"].get("分類", {}).get("select") or {}).get("name", "")
        items.append({"title": title, "category": cat, "page_url": p["url"]})
    return items


# ==== Telegram Handlers ====
async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💡 靈感收集機器人\n\n"
        "傳給我：\n"
        "• 任何網址（Threads / YouTube / X / IG / 一般文章）\n"
        "• 或純文字筆記\n\n"
        "我會自動摘要、分類、抽關鍵字、存進 Notion。\n\n"
        "/stats — 今天存了幾則\n"
        "/recent — 最近 5 則"
    )


async def stats(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        await update.message.reply_text("⛔ 沒有權限")
        return
    try:
        n = await asyncio.to_thread(_count_today_in_notion)
        await update.message.reply_text(f"📊 今天已儲存 {n} 則")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def recent(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        await update.message.reply_text("⛔ 沒有權限")
        return
    try:
        items = await asyncio.to_thread(_list_recent_in_notion, 5)
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")
        return
    if not items:
        await update.message.reply_text("還沒有任何紀錄")
        return
    lines = ["📋 最近 5 則：\n"]
    for i, it in enumerate(items, 1):
        line = f"{i}. [{it['title']}]({it['page_url']})"
        if it["category"]:
            line += f"　🏷 {it['category']}"
        lines.append(line)
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)


_TRACKING_PARAMS = {
    # Threads / Meta
    "xmt", "slof", "igsh", "igshid", "fbclid",
    # Google / 廣告
    "gclid", "dclid", "gbraid", "wbraid",
    # YouTube 分享
    "si", "feature", "pp",
    # Mailchimp / 通用 newsletter
    "mc_eid", "mc_cid",
    # 通用 referral
    "ref", "ref_src", "ref_url", "source",
    # Branch / 連結深層
    "_branch_match_id", "_branch_referrer",
    # 雜項
    "spm", "share_source",
}
_TRACKING_PREFIXES = ("utm_",)


def _clean_url(url: str) -> str:
    """移除常見追蹤參數，回傳乾淨 URL；解析失敗就照原樣回。"""
    from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
    try:
        p = urlparse(url)
        kept = [
            (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_PARAMS
            and not any(k.lower().startswith(pref) for pref in _TRACKING_PREFIXES)
        ]
        return urlunparse(p._replace(query=urlencode(kept)))
    except Exception:
        return url


def extract_urls(text: str) -> list[str]:
    """抽出所有 http(s) URL，處理 URL 黏在一起的情況；自動清掉追蹤參數。"""
    raw = re.findall(r"https?://[^\s]+", text)
    out = []
    for r in raw:
        sec = re.search(r"https?://", r[8:])
        u = r[:8 + sec.start()] if sec else r
        u = _clean_url(u)
        if u not in out:
            out.append(u)
    return out


def _strip_urls(text: str) -> str:
    return re.sub(r"https?://[^\s]+", "", text).strip()


async def _process_one(source: dict) -> tuple[bool, str]:
    """處理一則來源，回傳 (success, 訊息行)"""
    if not source.get("text", "").strip():
        return False, "❌ 沒抓到內容"
    analysis = await asyncio.to_thread(analyze_with_claude, source["text"], source["platform"], source.get("author", ""))
    page_url = await asyncio.to_thread(write_to_notion, source, analysis)
    kw = "，".join(analysis.get("keywords", [])[:5])
    line = f"✅ {analysis['title']}\n    🏷 {analysis['category']} ｜ 📡 {source['platform']}"
    if kw:
        line += f"\n    🔖 {kw}"
    line += f"\n    🔗 {page_url}"
    return True, line


async def handle_message(update: Update, _: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    is_bot_self = update.effective_user and update.effective_user.id == BOT_USER_ID

    # 防無限迴圈：bot 自己發的訊息只在含 URL 時才處理
    # （否則 bot 回的「✅ 完成」會被當新訊息再處理）
    if is_bot_self and not re.search(r"https?://", text):
        log.debug("[handle_message] 略過 bot-self 非 URL 訊息: %r", text[:50])
        return

    if not _allowed(update):
        await update.message.reply_text("⛔ 沒有權限")
        return
    urls = extract_urls(text)

    # 構建處理清單：URL 們 + （如果還有非 URL 文字）一筆純文字
    jobs = []
    for u in urls:
        jobs.append(("url", u))
    leftover = _strip_urls(text)
    if leftover and (not urls or len(leftover) > 20):  # 有 URL 時，文字夠長才當獨立筆記
        jobs.append(("text", leftover))
    if not jobs:
        await update.message.reply_text("請傳網址或文字筆記")
        return

    total = len(jobs)
    msg = await update.message.reply_text(f"⏳ 共 {total} 則，準備中...")

    try:
        existing = await asyncio.to_thread(_existing_urls_from_db)
    except Exception:
        log.warning("dedup 預掃失敗", exc_info=True)
        existing = set()

    results = []
    for i, (kind, payload) in enumerate(jobs, 1):
        prefix = f"({i}/{total})"
        try:
            if kind == "url":
                url = payload
                if url in existing:
                    results.append(f"{prefix} ⏭ 已存過")
                    continue
                platform = detect_platform(url)
                await msg.edit_text(f"⏳ {prefix} 爬取 {platform}...")
                scraped = await scrape_url(url, platform)
                source = {**scraped, "url": url, "platform": platform}
            else:
                await msg.edit_text(f"⏳ {prefix} 純文字筆記...")
                source = {"text": payload, "author": "", "image_urls": [], "platform": "純文字"}

            await msg.edit_text(f"⏳ {prefix} Claude 分析...")
            ok, line = await _process_one(source)
            if ok and kind == "url":
                existing.add(payload)
            results.append(f"{prefix} {line}")
        except Exception as e:
            log.exception("處理失敗：%s", payload)
            results.append(f"{prefix} ❌ {type(e).__name__}: {e}")

    await msg.edit_text("完成！\n\n" + "\n\n".join(results), disable_web_page_preview=True)


RAILWAY_API_TOKEN = os.environ.get("RAILWAY_API_TOKEN")
RAILWAY_GRAPHQL = "https://backboard.railway.com/graphql/v2"


def _query_railway(query: str, variables: dict | None = None) -> dict:
    import httpx
    res = httpx.post(
        RAILWAY_GRAPHQL,
        json={"query": query, "variables": variables or {}},
        headers={"Authorization": f"Bearer {RAILWAY_API_TOKEN}"},
        timeout=15,
    )
    res.raise_for_status()
    payload = res.json()
    if payload.get("errors"):
        raise RuntimeError(f"Railway GraphQL errors: {payload['errors']}")
    return payload.get("data", {})


def get_railway_usage() -> dict:
    if not RAILWAY_API_TOKEN:
        return {"error": "RAILWAY_API_TOKEN 未設"}
    # Step 1: 用最簡單的 query 確認 token 有效
    try:
        me_data = _query_railway("query { me { id email name } }")
        me = me_data.get("me", {}) or {}
    except Exception as e:
        log.exception("Railway 基本查詢失敗")
        return {"error": f"token 無效或 schema 改了: {type(e).__name__}: {e}"}

    result = {
        "name": me.get("name") or me.get("email") or "?",
        "estimated_cost": None,
        "dashboard_url": "https://railway.com/account/usage",
    }

    # Step 2: 試著拿用量數字（schema 可能變動，失敗就跳過）
    try:
        from datetime import datetime, timezone
        ws_data = _query_railway("query { me { workspaces { edges { node { id name } } } } }")
        edges = ((ws_data.get("me") or {}).get("workspaces") or {}).get("edges") or []
        ws = (edges[0]["node"] if edges else None)
        if not ws:
            raise RuntimeError("workspaces 為空")

        now = datetime.now(timezone.utc)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        usage_query = """
        query usage($workspaceId: String!, $startDate: DateTime!, $endDate: DateTime!, $measurements: [MetricMeasurement!]!) {
            usage(workspaceId: $workspaceId, startDate: $startDate, endDate: $endDate, measurements: $measurements) {
                measurement
                value
            }
        }
        """
        usage_data = _query_railway(usage_query, {
            "workspaceId": ws["id"],
            "startDate": start.isoformat(),
            "endDate": now.isoformat(),
            "measurements": ["ESTIMATED_USAGE"],
        })
        items = usage_data.get("usage", []) or []
        cost = sum(float(i.get("value") or 0) for i in items if i.get("measurement") == "ESTIMATED_USAGE")
        result["workspace"] = ws.get("name")
        result["estimated_cost"] = cost
        result["period_start"] = start.strftime("%Y-%m-%d")
    except Exception as e:
        log.warning(f"Railway 用量查詢失敗（schema 可能不同），fallback 顯示 dashboard 連結。原因：{e}")

    return result


async def usage_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        await update.message.reply_text("⛔ 沒有權限")
        return
    await update.message.reply_text("⏳ 查 Railway 用量...")
    result = await asyncio.to_thread(get_railway_usage)
    if "error" in result:
        await update.message.reply_text(f"❌ {result['error']}")
        return
    cost = result.get("estimated_cost")
    if cost is not None:
        free = 5.00
        remaining = max(0.0, free - cost)
        bar_full = int(min(cost / free, 1.0) * 10)
        bar = "▰" * bar_full + "▱" * (10 - bar_full)
        msg = (
            f"📊 Railway 用量（{result.get('workspace', '?')}）\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"期間：{result['period_start']} ~ 今\n"
            f"已用：${cost:.2f}\n"
            f"剩餘：${remaining:.2f} / $5.00\n"
            f"{bar}  {cost / free * 100:.1f}%"
        )
    else:
        msg = (
            f"📊 Railway 帳號\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"已連線：{result['name']}\n"
            f"⚠️ 自動取用量失敗（Railway schema 改了）\n"
            f"請看：{result['dashboard_url']}"
        )
    await update.message.reply_text(msg, disable_web_page_preview=True)


# ==== /sync 同步 Threads 收藏夾 ====
THREADS_SAVED_URLS = [
    "https://www.threads.com/saved",
    "https://www.threads.net/saved",
]


async def _fetch_saved_post_urls(saved_page_url: str) -> tuple[list[str], str]:
    """登入 cookie 開 saved page → 滾動載入直到沒新內容 → 抽出所有貼文 URL。
    回傳 (post_urls, final_url)。"""
    storage_state = json.loads(THREADS_STATE_JSON) if THREADS_STATE_JSON else None
    if not storage_state:
        raise RuntimeError("THREADS_STATE_JSON 未設，先跑 python get_cookies.py 產生")
    browser = await _ensure_browser()
    context = await browser.new_context(user_agent=USER_AGENT, storage_state=storage_state)
    page = await context.new_page()
    seen: set[str] = set()
    final_url = saved_page_url
    try:
        await page.goto(saved_page_url, wait_until="domcontentloaded", timeout=30000)
        final_url = page.url
        log.info(f"[sync_threads] saved 頁 final URL: {final_url}")

        def _scan_html(html: str):
            for m in re.finditer(r'/(@[\w.]+)/post/([\w-]+)', html):
                seen.add(f"https://www.threads.com/{m.group(1)}/post/{m.group(2)}")

        # 初始 scan
        _scan_html(await page.content())
        log.info(f"[sync_threads] 初始載入: {len(seen)} 則")

        # 持續滾動直到沒新內容（最多 80 次、連續 4 次沒新就停）
        no_new_streak = 0
        for i in range(1, 81):
            prev_count = len(seen)
            # 滾到底；用 wheel 比 evaluate 更接近真人行為，較容易 trigger lazy load
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            await asyncio.sleep(2.0)
            _scan_html(await page.content())
            delta = len(seen) - prev_count
            log.info(f"[sync_threads] 滾動 {i}: +{delta}, 累計 {len(seen)}")
            if delta == 0:
                no_new_streak += 1
                if no_new_streak >= 4:
                    log.info(f"[sync_threads] 連續 4 次沒新貼文，停止滾動")
                    break
            else:
                no_new_streak = 0
    finally:
        await context.close()
    return list(seen), final_url


async def sync_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /sync threads          → 預設 5 則
    /sync threads all      → 全部新的
    /sync threads 30       → 自訂上限
    """
    if not _allowed(update):
        await update.message.reply_text("⛔ 沒有權限")
        return
    args = ctx.args or []
    target = (args[0].lower() if args else "threads")
    if target != "threads":
        await update.message.reply_text("目前只支援 `/sync threads [N|all]`")
        return

    if len(args) >= 2:
        if args[1].lower() == "all":
            max_count = 9999
        elif args[1].isdigit():
            max_count = max(1, int(args[1]))
        else:
            max_count = 5
    else:
        max_count = 5

    msg = await update.message.reply_text("⏳ 開啟 Threads 收藏夾...")
    found: list[str] = []
    final_url = ""
    last_err = ""
    for url in THREADS_SAVED_URLS:
        try:
            posts, final_url = await _fetch_saved_post_urls(url)
            if posts:
                found = posts
                break
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            log.warning(f"[sync_threads] 嘗試 {url} 失敗: {e}")
            continue

    if not found:
        await msg.edit_text(
            f"❌ 沒抓到任何貼文\n"
            f"final URL: {final_url}\n"
            f"最後錯誤: {last_err}"
        )
        return

    cleaned = [_clean_url(u) for u in found]
    existing = await asyncio.to_thread(_existing_urls_from_db)
    new_urls = [u for u in cleaned if u not in existing]
    if not new_urls:
        await msg.edit_text(
            f"✅ 收藏夾找到 {len(cleaned)} 則，全部已存在 Notion\n"
            f"📍 final URL: {final_url}\n"
            f"💡 如果數量明顯比實際少，可能是 saved 頁被導去首頁（cookie 過期或 URL 路徑錯）"
        )
        return

    target_list = new_urls[:max_count]
    total = len(target_list)
    await msg.edit_text(
        f"📥 收藏夾找到 {len(cleaned)} 則貼文，{len(new_urls)} 則尚未進 Notion\n"
        f"📍 final URL: {final_url}\n"
        f"⏳ 開始處理 {total} 則（每則約 10–20 秒）..."
    )

    success = skip = fail = 0
    fail_samples: list[str] = []
    for i, url in enumerate(target_list, 1):
        try:
            platform = detect_platform(url)
            scraped = await scrape_url(url, platform)
            source = {**scraped, "url": url, "platform": platform}
            ok, _line = await _process_one(source)
            if ok:
                success += 1
            else:
                skip += 1
        except Exception as e:
            fail += 1
            log.exception("[sync_threads] 處理 %s 失敗", url)
            if len(fail_samples) < 3:
                fail_samples.append(f"{url.split('/')[-1][:20]}: {type(e).__name__}")

        # 每 3 則更新一次進度（避免 Telegram rate limit）
        if i % 3 == 0 or i == total:
            try:
                await msg.edit_text(
                    f"⏳ 進度 {i}/{total}\n"
                    f"  ✅ {success}　⏭ {skip}　❌ {fail}"
                )
            except Exception:
                pass

    summary = (
        f"✅ 同步完成\n"
        f"━━━━━━━━━━━━━━\n"
        f"已處理：{total} 則\n"
        f"  ✅ 成功：{success}\n"
        f"  ⏭ 沒抓到內容：{skip}\n"
        f"  ❌ 失敗：{fail}"
    )
    if fail_samples:
        summary += "\n\n失敗範例：\n" + "\n".join(fail_samples)
    if len(new_urls) > max_count:
        summary += f"\n\n還剩 {len(new_urls) - max_count} 則沒處理，傳 `/sync threads all` 處理剩下的"
    await update.message.reply_text(summary, disable_web_page_preview=True)


# ==== HTTP Webhook（給 iOS Shortcut 等外部呼叫用）====
_ptb_bot = None  # post_init 後填入，給 webhook handler 用


async def _process_via_webhook(url: str):
    """從 HTTP webhook 觸發的處理流程，等同 handle_message 但沒有 update 物件。"""
    if not (_ptb_bot and ALLOWED_USER_ID):
        log.error("[webhook] bot 或 ALLOWED_USER_ID 沒準備好，跳過")
        return
    cleaned = _clean_url(url)
    msg = await _ptb_bot.send_message(chat_id=ALLOWED_USER_ID, text=f"⏳ Webhook 收到：{cleaned[:80]}")
    try:
        existing = await asyncio.to_thread(_existing_urls_from_db)
        if cleaned in existing:
            await msg.edit_text("⏭ 已存過")
            return
        platform = detect_platform(cleaned)
        await msg.edit_text(f"⏳ 爬取 {platform}...")
        scraped = await scrape_url(cleaned, platform)
        source = {**scraped, "url": cleaned, "platform": platform}
        await msg.edit_text("⏳ Claude 分析...")
        ok, line = await _process_one(source)
        await msg.edit_text(f"完成（webhook）\n\n{line}", disable_web_page_preview=True)
    except Exception as e:
        log.exception("[webhook] 處理失敗：%s", cleaned)
        try:
            await msg.edit_text(f"❌ {type(e).__name__}: {e}")
        except Exception:
            pass


async def _ingest_handler(request: web.Request) -> web.Response:
    secret = request.headers.get("X-Auth-Secret") or request.query.get("secret", "")
    if not INGEST_SECRET or secret != INGEST_SECRET:
        log.warning("[webhook] 401 from %s", request.remote)
        return web.Response(status=401, text="Unauthorized")
    url = request.query.get("url") or ""
    if request.method == "POST":
        ctype = (request.content_type or "").lower()
        try:
            if "json" in ctype:
                body = await request.json()
                url = body.get("url") or url
            elif "form" in ctype or "urlencoded" in ctype:
                form = await request.post()
                url = form.get("url") or url
            else:
                # 未指定 content-type：兩種都試
                try:
                    body = await request.json()
                    url = body.get("url") or url
                except Exception:
                    form = await request.post()
                    url = form.get("url") or url
        except Exception:
            pass
    if not url.startswith(("http://", "https://")):
        return web.Response(status=400, text="Missing or invalid url")
    asyncio.create_task(_process_via_webhook(url))
    return web.json_response({"status": "queued", "url": url})


async def _start_webhook_server(application):
    global _ptb_bot
    _ptb_bot = application.bot
    if not INGEST_SECRET:
        log.warning("[webhook] INGEST_SECRET 未設，webhook 伺服器不啟動")
        return
    web_app = web.Application()
    web_app.router.add_route("*", "/ingest", _ingest_handler)
    web_app.router.add_get("/health", lambda r: web.Response(text="ok"))
    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.environ.get("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("[webhook] 伺服器啟動在 :%d", port)


async def _shutdown_browser(_app):
    global _browser, _playwright
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright:
        await _playwright.stop()
        _playwright = None


def main():
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(_start_webhook_server)
        .post_shutdown(_shutdown_browser)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("recent", recent))
    app.add_handler(CommandHandler("usage", usage_cmd))
    app.add_handler(CommandHandler("sync", sync_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("靈感收集機器人 啟動中...")
    app.run_polling()


if __name__ == "__main__":
    main()
