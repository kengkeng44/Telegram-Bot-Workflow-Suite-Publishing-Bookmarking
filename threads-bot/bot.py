"""
靈感收集機器人 (Telegram Bot)
- 收任何 URL（Threads / YouTube / X / IG / 一般網頁）或純文字
- Claude 分析摘要 / 分類 / 關鍵字 / 原文摘錄
- 寫入 Notion Database
"""
import os
import re
import json
import base64
import shutil
import asyncio
import logging
import tempfile
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote_plus

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
INSTAGRAM_STATE_JSON = os.environ.get("INSTAGRAM_STATE_JSON")  # 可選：IG 登入 storage_state（跑 get_ig_cookies.py 產生）
INSTAGRAM_USERNAME = os.environ.get("INSTAGRAM_USERNAME", "")  # IG 帳號（組收藏夾網址 /<user>/saved/ 用）
BOT_USER_ID = int(TELEGRAM_TOKEN.split(":")[0])  # bot 自己的 user id = token 冒號前的數字
INGEST_SECRET = os.environ.get("INGEST_SECRET")  # webhook 認證用的 secret，不設則 webhook 不啟動
AUTO_SYNC_HOURS = int(os.environ.get("AUTO_SYNC_HOURS", "0"))  # Threads 排程同步間隔（小時），0 = 關閉
AUTO_SYNC_MAX = int(os.environ.get("AUTO_SYNC_MAX", "20"))  # 每次排程同步最多處理幾則
AUTO_SYNC_IG_HOURS = int(os.environ.get("AUTO_SYNC_IG_HOURS", "0"))  # IG 排程同步間隔（小時），0 = 關閉

# ==== Clients & 共用狀態 ====
notion = NotionClient(auth=NOTION_TOKEN)
claude = Anthropic(api_key=ANTHROPIC_API_KEY)
TZ_TAIPEI = timezone(timedelta(hours=8))
CATEGORIES = ["AI科技", "生活風格", "學習成長", "設計創意"]

# ==== 多模態（圖片 / 影片）設定 ====
MODEL_TEXT = "claude-haiku-4-5"        # 純文字：便宜快
MODEL_VISION = "claude-sonnet-4-6"     # 有圖片 / 影格：看圖理解力佳，仍便宜
MAX_IMAGES = 4                          # 一則最多送幾張原圖給 Claude
VIDEO_MAX_FRAMES = 12                   # 一支影片最多抽幾張影格
VIDEO_FRAME_WIDTH = 640                 # 影格縮放寬度（高度等比；512~768 是 LLM 視覺甜蜜點）
VIDEO_SCENE_THRESHOLD = 0.3            # ffmpeg 場景偵測閾值（轉場才抽，比固定間隔聰明）
FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"
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


def _extract_video_urls(node: dict) -> list[str]:
    """Threads/IG GraphQL node 的影片網址（單片 video_versions + 輪播）。"""
    urls = []
    single = jmespath.search("video_versions[0].url", node)
    if single:
        urls.append(single)
    for u in (jmespath.search("carousel_media[*].video_versions[0].url", node) or []):
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
                "video_urls": _extract_video_urls(node),
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
            "video_urls": _extract_video_urls(post_node),
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


# ==== Instagram 專用 scrape（cookie + GraphQL 攔截，og fallback） ====
def _ig_caption(node: dict) -> str:
    """IG node 取 caption：private-api（caption.text）與 web GraphQL（edge_media_to_caption）兩種 shape。"""
    c = node.get("caption")
    if isinstance(c, dict) and isinstance(c.get("text"), str):
        return c["text"]
    if isinstance(c, str):
        return c
    return jmespath.search("edge_media_to_caption.edges[0].node.text", node) or ""


def _ig_author(node: dict) -> str:
    return (jmespath.search("user.username", node)
            or jmespath.search("owner.username", node) or "")


def _ig_media_urls(node: dict) -> tuple[list[str], list[str]]:
    """回傳 (image_urls, video_urls)，同時相容 private-api 與 web GraphQL 兩種 node shape。"""
    imgs = list(_extract_image_urls(node))         # image_versions2 / carousel_media（private-api）
    vids = list(_extract_video_urls(node))         # video_versions（private-api）
    # web GraphQL: display_url / video_url（單則）
    for key, bucket in (("display_url", imgs), ("video_url", vids)):
        v = node.get(key)
        if v and v not in bucket:
            bucket.append(v)
    # web GraphQL: 輪播 edge_sidecar_to_children
    for child in (jmespath.search("edge_sidecar_to_children.edges[*].node", node) or []):
        vu, du = child.get("video_url"), child.get("display_url")
        if vu and vu not in vids:
            vids.append(vu)
        elif du and du not in imgs:
            imgs.append(du)
    return imgs, vids


def _ig_extract_node(data) -> dict | None:
    """從攔截到的 IG JSON 找出貼文 media node。"""
    sc = jmespath.search("data.xdt_shortcode_media || data.shortcode_media", data)
    if isinstance(sc, dict):
        return sc
    for arr in _nested_lookup("items", data):
        if isinstance(arr, list):
            for it in arr:
                if isinstance(it, dict) and any(
                        k in it for k in ("image_versions2", "video_versions", "carousel_media")):
                    return it
    return None


def _ig_from_og(html: str) -> dict | None:
    """IG Open Graph 保底：og:description=caption、og:image、og:video（Reel）。"""
    desc = _get_meta(html, "og:description")
    title = _get_meta(html, "og:title") or ""
    image = _get_meta(html, "og:image")
    video = _get_meta(html, "og:video:secure_url") or _get_meta(html, "og:video")
    if not (desc or image or video):
        return None
    m = re.search(r'@([\w.]+)', title)
    author = m.group(1) if m else title.split(" on Instagram")[0].strip()
    vids = [video] if video and re.search(r"\.(mp4|m3u8|webm|mov)(\?|$)", video, re.I) else []
    return {"text": desc or "", "author": author,
            "image_urls": [image] if image else [], "video_urls": vids}


async def _scrape_instagram(url: str) -> dict:
    log.info(f"[scrape_ig] start url={url}")
    storage_state = json.loads(INSTAGRAM_STATE_JSON) if INSTAGRAM_STATE_JSON else None
    browser = await _ensure_browser()
    captured = []
    context = await browser.new_context(user_agent=USER_AGENT, storage_state=storage_state)
    await context.route("**/*", _block_heavy_resources)
    page = await context.new_page()

    async def _on_response(response):
        u = response.url
        if "/graphql/query" in u or "/api/v1/media/" in u:
            try:
                captured.append(await response.json())
            except Exception:
                pass

    page.on("response", _on_response)
    html = ""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 8
        while not captured and loop.time() < deadline:
            await asyncio.sleep(0.2)
        if captured:
            await asyncio.sleep(0.6)
        html = await page.content()
    except Exception as e:
        log.error(f"[scrape_ig] page.goto 失敗: {type(e).__name__}: {e}")
    finally:
        await context.close()

    log.info(f"[scrape_ig] captured={len(captured)} responses, html_len={len(html)}")
    for data in captured:
        node = _ig_extract_node(data)
        if node:
            imgs, vids = _ig_media_urls(node)
            log.info(f"[scrape_ig] ✅ node 命中 author={_ig_author(node)} img={len(imgs)} vid={len(vids)}")
            return {"text": _ig_caption(node), "author": _ig_author(node),
                    "image_urls": imgs, "video_urls": vids}

    og = _ig_from_og(html)
    if og:
        log.info(f"[scrape_ig] ⚠️ og fallback 命中 author={og['author']}")
        return og

    log.warning(f"[scrape_ig] ❌ 全部失敗 html_len={len(html)}")
    return {"text": "", "author": "", "image_urls": [], "video_urls": []}


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
                video: get('meta[property="og:video:secure_url"]') || get('meta[property="og:video:url"]') || get('meta[property="og:video"]') || get('meta[property="twitter:player:stream"]'),
                body: (document.body?.innerText || '').slice(0, 5000),
            };
        }""")
    finally:
        await context.close()

    video = meta.get("video", "")
    # 只收看起來是影片檔的（避免 og:video 指到 player 頁面）
    video_urls = [video] if video and re.search(r"\.(mp4|m3u8|webm|mov)(\?|$)", video, re.I) else []
    parts = [p for p in (meta["title"], meta["description"], meta["body"]) if p]
    return {
        "text": "\n\n".join(parts),
        "author": meta.get("site_name", ""),
        "video_urls": video_urls,
        "image_urls": [meta["image"]] if meta.get("image") else [],
    }


async def scrape_url(url: str, platform: str) -> dict:
    """根據平台 dispatch 到對應 scraper。Browser 共用、SCRAPE_SEM 序列化。"""
    async with SCRAPE_SEM:
        if platform == "Threads":
            return await _scrape_threads(url)
        if platform == "Instagram":
            return await _scrape_instagram(url)
        return await _scrape_generic(url)


# ==== 多模態媒體處理（圖片下載 / 影片抽影格 / 地圖） ====
_IMG_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
             "gif": "image/gif", "webp": "image/webp"}


def _guess_media_type(url: str, content_type: str | None) -> str:
    if content_type and content_type.split(";")[0].strip() in _IMG_MIME.values():
        return content_type.split(";")[0].strip()
    ext = url.split("?")[0].rsplit(".", 1)[-1].lower()
    return _IMG_MIME.get(ext, "image/jpeg")


def _fetch_image_block(url: str) -> dict | None:
    """下載圖片 → base64 image content block。失敗回 None（不擋主流程）。"""
    import httpx
    try:
        r = httpx.get(url, timeout=15, follow_redirects=True,
                      headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        data = r.content
        if not data or len(data) > 5_000_000:  # 跳過空檔 / 過大（>5MB）
            return None
        media_type = _guess_media_type(url, r.headers.get("content-type"))
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type,
                       "data": base64.standard_b64encode(data).decode()},
        }
    except Exception as e:
        log.warning("[media] 圖片下載失敗 %s：%s", url[:80], e)
        return None


def _download_video(url: str) -> str | None:
    """下載影片到暫存檔，回傳路徑（呼叫端負責刪）。失敗回 None。"""
    import httpx
    try:
        fd, path = tempfile.mkstemp(suffix=".mp4")
        with os.fdopen(fd, "wb") as f:
            with httpx.stream("GET", url, timeout=60, follow_redirects=True,
                              headers={"User-Agent": USER_AGENT}) as r:
                r.raise_for_status()
                for chunk in r.iter_bytes(65536):
                    f.write(chunk)
        return path
    except Exception as e:
        log.warning("[media] 影片下載失敗 %s：%s", url[:80], e)
        return None


def extract_video_frames(video_path: str, max_frames: int = VIDEO_MAX_FRAMES) -> list[dict]:
    """用 ffmpeg 抽影格（場景偵測優先，太少則退回固定取樣），回傳 image content blocks。

    參考業界做法：場景轉場才抽（select=gt(scene,T)），縮到 ~640px、壓在 max_frames 內，
    讓 Claude 看畫面而不爆 token。ffmpeg 不存在或失敗則回空 list（不擋主流程）。
    """
    if not os.path.exists(video_path):
        return []
    tmpdir = tempfile.mkdtemp(prefix="frames_")
    try:
        scale = f"scale={VIDEO_FRAME_WIDTH}:-2"
        # 第一輪：場景偵測抽轉場影格
        scene_vf = f"select='gt(scene,{VIDEO_SCENE_THRESHOLD})',{scale}"
        cmd = [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-i", video_path,
               "-vf", scene_vf, "-vsync", "vfr", "-frames:v", str(max_frames),
               "-q:v", "4", os.path.join(tmpdir, "f%03d.jpg")]
        try:
            subprocess.run(cmd, check=True, timeout=120)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            log.warning("[media] ffmpeg 場景偵測失敗：%s", e)

        frames = sorted(Path(tmpdir).glob("f*.jpg"))
        # 退回方案：場景偵測抽不到（短片 / 無明顯轉場）→ 全片均勻取樣
        if len(frames) < 2:
            for p in frames:
                p.unlink(missing_ok=True)
            fps_vf = f"fps=1,{scale}"  # 每秒 1 張，再截斷到 max_frames
            cmd = [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-i", video_path,
                   "-vf", fps_vf, "-frames:v", str(max_frames),
                   "-q:v", "4", os.path.join(tmpdir, "f%03d.jpg")]
            try:
                subprocess.run(cmd, check=True, timeout=120)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
                log.warning("[media] ffmpeg 取樣失敗：%s", e)
            frames = sorted(Path(tmpdir).glob("f*.jpg"))

        blocks = []
        for p in frames[:max_frames]:
            data = p.read_bytes()
            if data:
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg",
                               "data": base64.standard_b64encode(data).decode()},
                })
        log.info("[media] 抽出 %d 張影格（%s）", len(blocks), os.path.basename(video_path))
        return blocks
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def maps_url(place: str) -> str:
    """組 Google 地圖搜尋連結（免費、不需 API key）。"""
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(place)}"


def collect_media_blocks(source: dict) -> tuple[list[dict], int, int]:
    """從 source 收集要送給 Claude 的視覺 content blocks。
    回傳 (blocks, 圖片數, 影格數)。會下載圖片並對影片抽影格。"""
    blocks, n_img, n_frame = [], 0, 0
    for u in source.get("image_urls", [])[:MAX_IMAGES]:
        blk = _fetch_image_block(u)
        if blk:
            blocks.append(blk)
            n_img += 1
    # 本地圖片檔（例如 Telegram 直接傳圖）
    for path in source.get("local_image_paths", [])[:MAX_IMAGES]:
        try:
            data = Path(path).read_bytes()
            if data and len(data) <= 5_000_000:
                blocks.append({"type": "image", "source": {"type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.standard_b64encode(data).decode()}})
                n_img += 1
        except Exception as e:
            log.warning("[media] 本地圖片讀取失敗 %s：%s", path, e)
    for vurl in source.get("video_urls", [])[:1]:  # 一則先處理第一支影片
        path = _download_video(vurl)
        if path:
            try:
                fb = extract_video_frames(path)
                blocks.extend(fb)
                n_frame += len(fb)
            finally:
                Path(path).unlink(missing_ok=True)
    # 已是本地影格路徑（例如 Telegram 直接傳影片）
    for path in source.get("local_video_paths", []):
        fb = extract_video_frames(path)
        blocks.extend(fb)
        n_frame += len(fb)
    return blocks, n_img, n_frame


# ==== Claude 分析 ====
def _build_prompt(text: str, platform: str, fallback_author: str, has_visual: bool) -> str:
    visual_hint = ""
    if has_visual:
        visual_hint = (
            "\n附帶的圖片 / 影片影格就是這則內容的視覺主體，請實際「看圖」理解畫面"
            "（人物、場景、招牌、菜色、商品、文字），別只靠文字。\n"
        )
    return f"""分析以下「{platform}」來源的內容，回傳純 JSON（不含 markdown fence）：
{{
  "title": "20 字內主題（不要含作者帳號）",
  "author": "原作者帳號或來源名稱；若提示為空且內容/畫面看得出作者就填，否則回空字串",
  "summary": "整體內容描述（50 字內中文，若有圖/影片要描述畫面實際看到什麼）",
  "category": "從這四選一：{ ' / '.join(CATEGORIES) }",
  "excerpt": "從內容直接摘錄 1 句最關鍵原句（不可改寫）；沒有就回空字串",
  "keywords": ["3~5 個關鍵字"],
  "place": "若內容/畫面是某個實體地點（餐廳、店家、景點、地址），填可在 Google 地圖搜到的『店名或地名（含城市/區域更好）』；不是地點就回空字串"
}}
{visual_hint}
提示：原作者 = {fallback_author!r}

文字內容：
{text[:6000] if text else "（無文字，請以視覺為主）"}
"""


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10),
       before_sleep=before_sleep_log(log, logging.WARNING), reraise=True)
def analyze_with_claude(text: str, platform: str, fallback_author: str = "",
                        media_blocks: list[dict] | None = None) -> dict:
    media_blocks = media_blocks or []
    has_visual = bool(media_blocks)
    prompt = _build_prompt(text, platform, fallback_author, has_visual)
    content = [*media_blocks, {"type": "text", "text": prompt}]
    model = MODEL_VISION if has_visual else MODEL_TEXT
    resp = claude.messages.create(
        model=model,
        max_tokens=700,
        messages=[{"role": "user", "content": content}],
    )
    raw = re.sub(r"^```json\s*|\s*```$", "", resp.content[0].text.strip(), flags=re.MULTILINE).strip()
    result = json.loads(raw)
    if result.get("category") not in CATEGORIES:
        result["category"] = "AI科技"
    if not isinstance(result.get("keywords"), list):
        result["keywords"] = []
    if not result.get("author"):
        result["author"] = fallback_author
    if not isinstance(result.get("place"), str):
        result["place"] = ""
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

    children = []
    # Google 地圖連結（內容是實體地點時）
    place = (analysis.get("place") or "").strip()
    if place:
        link = maps_url(place)
        children.append({
            "type": "bookmark", "bookmark": {"url": link,
                "caption": [{"type": "text", "text": {"content": f"🗺️ {place}"}}]},
        })
    # 原圖（Notion 接受 CDN URL，直接外嵌）
    for u in source.get("image_urls", [])[:10]:
        children.append({"type": "image", "image": {"type": "external", "external": {"url": u}}})
    if children:
        try:
            notion.blocks.children.append(block_id=page["id"], children=children)
        except Exception:
            log.warning("[notion] 附加 children 失敗（地圖/圖片）", exc_info=True)
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
        "• 圖片 → Claude 直接看圖分析\n"
        "• 影片 / IG Reel → 自動抽影格看畫面\n"
        "• 或純文字筆記\n\n"
        "我會自動摘要、分類、抽關鍵字、看圖/影格、認出地點附 Google 地圖，存進 Notion。\n\n"
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
    # 收集視覺素材（下載圖片 + 影片抽影格）
    media_blocks, n_img, n_frame = await asyncio.to_thread(collect_media_blocks, source)
    if not source.get("text", "").strip() and not media_blocks:
        return False, "❌ 沒抓到內容"
    analysis = await asyncio.to_thread(
        analyze_with_claude, source.get("text", ""), source["platform"],
        source.get("author", ""), media_blocks)
    page_url = await asyncio.to_thread(write_to_notion, source, analysis)
    kw = "，".join(analysis.get("keywords", [])[:5])
    line = f"✅ {analysis['title']}\n    🏷 {analysis['category']} ｜ 📡 {source['platform']}"
    bits = []
    if n_img:
        bits.append(f"🖼 {n_img} 圖")
    if n_frame:
        bits.append(f"🎬 {n_frame} 影格")
    if analysis.get("place"):
        bits.append(f"🗺️ {analysis['place']}")
    if bits:
        line += "\n    " + " ｜ ".join(bits)
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


async def _download_tg_file(context, file_id: str, suffix: str) -> str:
    """下載 Telegram 檔案到暫存路徑，回傳路徑（呼叫端負責刪）。"""
    tg_file = await context.bot.get_file(file_id)
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    await tg_file.download_to_drive(path)
    return path


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """使用者直接傳影片 / GIF / 圓形短片（例：IG Reel 分享到 Telegram）→ 抽影格分析。"""
    if not _allowed(update):
        await update.message.reply_text("⛔ 沒有權限")
        return
    m = update.message
    media = m.video or m.animation or m.video_note
    if not media:
        return
    msg = await m.reply_text("⏳ 下載影片...")
    path = None
    try:
        path = await _download_tg_file(context, media.file_id, ".mp4")
        caption = m.caption or ""
        source = {"text": caption, "author": "", "image_urls": [],
                  "platform": "影片", "local_video_paths": [path]}
        await msg.edit_text("⏳ 抽影格 + Claude 分析...")
        ok, line = await _process_one(source)
        await msg.edit_text(line, disable_web_page_preview=True)
    except Exception as e:
        log.exception("處理影片失敗")
        await msg.edit_text(f"❌ {type(e).__name__}: {e}")
    finally:
        if path:
            Path(path).unlink(missing_ok=True)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """使用者直接傳圖片 → 多模態分析（圖不會外嵌進 Notion，因為沒有公開 URL）。"""
    if not _allowed(update):
        await update.message.reply_text("⛔ 沒有權限")
        return
    m = update.message
    if not m.photo:
        return
    msg = await m.reply_text("⏳ 下載圖片...")
    path = None
    try:
        path = await _download_tg_file(context, m.photo[-1].file_id, ".jpg")  # [-1] = 最大解析度
        caption = m.caption or ""
        source = {"text": caption, "author": "", "image_urls": [],
                  "platform": "圖片", "local_image_paths": [path]}
        await msg.edit_text("⏳ Claude 看圖分析...")
        ok, line = await _process_one(source)
        await msg.edit_text(line, disable_web_page_preview=True)
    except Exception as e:
        log.exception("處理圖片失敗")
        await msg.edit_text(f"❌ {type(e).__name__}: {e}")
    finally:
        if path:
            Path(path).unlink(missing_ok=True)


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


# ==== /sync 同步 Instagram 收藏夾 ====
def _ig_saved_urls() -> list[str]:
    """IG 收藏夾網址（需 INSTAGRAM_USERNAME）。"""
    user = INSTAGRAM_USERNAME.strip().lstrip("@")
    if not user:
        return []
    base = f"https://www.instagram.com/{user}/saved"
    return [f"{base}/all-posts/", f"{base}/"]


async def _fetch_ig_saved_post_urls(saved_page_url: str) -> tuple[list[str], str]:
    """IG 登入 cookie 開收藏頁 → 滾動載入 → 抽出所有貼文 / Reel URL。
    回傳 (post_urls, final_url)。"""
    storage_state = json.loads(INSTAGRAM_STATE_JSON) if INSTAGRAM_STATE_JSON else None
    if not storage_state:
        raise RuntimeError("INSTAGRAM_STATE_JSON 未設，先跑 python get_ig_cookies.py 產生")
    if not INSTAGRAM_USERNAME.strip():
        raise RuntimeError("INSTAGRAM_USERNAME 未設（組收藏夾網址要用）")
    browser = await _ensure_browser()
    context = await browser.new_context(user_agent=USER_AGENT, storage_state=storage_state)
    page = await context.new_page()
    seen: set[str] = set()
    final_url = saved_page_url
    try:
        await page.goto(saved_page_url, wait_until="domcontentloaded", timeout=30000)
        final_url = page.url
        log.info(f"[sync_ig] saved 頁 final URL: {final_url}")

        def _scan_html(html: str):
            for m in re.finditer(r'/(p|reel|tv)/([\w-]+)/', html):
                seen.add(f"https://www.instagram.com/{m.group(1)}/{m.group(2)}/")

        _scan_html(await page.content())
        log.info(f"[sync_ig] 初始載入: {len(seen)} 則")

        no_new_streak = 0
        for i in range(1, 81):
            prev_count = len(seen)
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            await asyncio.sleep(2.0)
            _scan_html(await page.content())
            delta = len(seen) - prev_count
            log.info(f"[sync_ig] 滾動 {i}: +{delta}, 累計 {len(seen)}")
            if delta == 0:
                no_new_streak += 1
                if no_new_streak >= 4:
                    log.info("[sync_ig] 連續 4 次沒新貼文，停止滾動")
                    break
            else:
                no_new_streak = 0
    finally:
        await context.close()
    return list(seen), final_url


# 各平台收藏夾設定：label → (saved_urls, fetch_fn)
def _saved_sources(target: str):
    if target in ("threads", "th"):
        return "Threads", THREADS_SAVED_URLS, _fetch_saved_post_urls
    if target in ("instagram", "ig"):
        return "Instagram", _ig_saved_urls(), _fetch_ig_saved_post_urls
    return None, [], None


async def _finish_sync(update, msg, found, final_url, label, max_count, last_err=""):
    """共用：把抓到的收藏夾 URL 清單去重、過濾已存、逐則跑多模態分析。"""
    if not found:
        await msg.edit_text(
            f"❌ {label} 沒抓到任何貼文\nfinal URL: {final_url}\n最後錯誤: {last_err}")
        return
    cleaned = [_clean_url(u) for u in found]
    existing = await asyncio.to_thread(_existing_urls_from_db)
    new_urls = [u for u in cleaned if u not in existing]
    if not new_urls:
        await msg.edit_text(
            f"✅ {label} 收藏夾找到 {len(cleaned)} 則，全部已在 Notion\n"
            f"📍 final URL: {final_url}\n"
            f"💡 數量明顯偏少的話，可能是收藏頁被導去首頁（cookie 過期或路徑錯）")
        return

    target_list = new_urls[:max_count]
    total = len(target_list)
    await msg.edit_text(
        f"📥 {label} 收藏夾找到 {len(cleaned)} 則，{len(new_urls)} 則尚未進 Notion\n"
        f"📍 final URL: {final_url}\n"
        f"⏳ 開始處理 {total} 則（每則約 10–20 秒）...")

    success = skip = fail = 0
    fail_samples: list[str] = []

    async def _one(u: str):
        platform = detect_platform(u)
        scraped = await scrape_url(u, platform)
        source = {**scraped, "url": u, "platform": platform}
        return await _process_one(source)

    for i, url in enumerate(target_list, 1):
        log.info(f"[sync_{label}] 處理 {i}/{total}: {url}")
        try:
            ok, _line = await asyncio.wait_for(_one(url), timeout=90.0)
            success += 1 if ok else 0
            skip += 0 if ok else 1
        except asyncio.TimeoutError:
            fail += 1
            if len(fail_samples) < 3:
                fail_samples.append(f"{url.rstrip('/').split('/')[-1][:20]}: Timeout 90s")
        except Exception as e:
            fail += 1
            log.exception("[sync_%s] 處理 %s 失敗", label, url)
            if len(fail_samples) < 3:
                fail_samples.append(f"{url.rstrip('/').split('/')[-1][:20]}: {type(e).__name__}")
        if i % 3 == 0 or i == total:
            try:
                await msg.edit_text(f"⏳ {label} 進度 {i}/{total}\n  ✅ {success}　⏭ {skip}　❌ {fail}")
            except Exception:
                pass

    summary = (
        f"✅ {label} 同步完成\n━━━━━━━━━━━━━━\n"
        f"已處理：{total} 則\n  ✅ 成功：{success}\n  ⏭ 沒抓到內容：{skip}\n  ❌ 失敗：{fail}")
    if fail_samples:
        summary += "\n\n失敗範例：\n" + "\n".join(fail_samples)
    if len(new_urls) > max_count:
        summary += f"\n\n還剩 {len(new_urls) - max_count} 則，傳 `/sync {label.lower()} all` 處理剩下的"
    await update.message.reply_text(summary, disable_web_page_preview=True)


async def sync_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /sync threads [N|all]     → 同步 Threads 收藏夾
    /sync instagram [N|all]   → 同步 IG 收藏夾（也可用 ig）
    不帶平台預設 threads。
    """
    if not _allowed(update):
        await update.message.reply_text("⛔ 沒有權限")
        return
    args = ctx.args or []
    target = (args[0].lower() if args else "threads")
    label, saved_urls, fetch_fn = _saved_sources(target)
    if not label:
        await update.message.reply_text("用法：`/sync threads [N|all]` 或 `/sync instagram [N|all]`")
        return
    if not saved_urls:
        await update.message.reply_text(
            "❌ Instagram 收藏夾需要先設 `INSTAGRAM_USERNAME` 與 `INSTAGRAM_STATE_JSON`"
            "（跑 `python get_ig_cookies.py` 產生 cookie）")
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

    msg = await update.message.reply_text(f"⏳ 開啟 {label} 收藏夾...")
    found: list[str] = []
    final_url = ""
    last_err = ""
    for url in saved_urls:
        try:
            posts, final_url = await fetch_fn(url)
            if posts:
                found = posts
                break
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            log.warning(f"[sync_{label}] 嘗試 {url} 失敗: {e}")
            continue

    await _finish_sync(update, msg, found, final_url, label, max_count, last_err)


# ==== 排程：自動 sync（Threads / Instagram） ====
async def _scheduled_sync_job(context: ContextTypes.DEFAULT_TYPE):
    """JobQueue 排程觸發。context.job.data 帶平台（'threads' / 'instagram'），預設 threads。"""
    platform_key = (getattr(context.job, "data", None) or "threads")
    label, saved_urls, fetch_fn = _saved_sources(platform_key)
    if not (ALLOWED_USER_ID and label and saved_urls):
        log.warning(f"[auto_sync] {platform_key} 前置條件未滿足，跳過")
        return
    bot = context.bot
    msg = await bot.send_message(chat_id=ALLOWED_USER_ID, text=f"🤖 排程：開始同步 {label} 收藏...")
    try:
        found = []
        final_url = ""
        for url in saved_urls:
            try:
                posts, final_url = await fetch_fn(url)
                if posts:
                    found = posts
                    break
            except Exception as e:
                log.warning(f"[auto_sync] {url} 失敗: {e}")
        if not found:
            await msg.edit_text(f"🤖 排程：沒抓到貼文（cookie 可能過期）\nfinal URL: {final_url}")
            return

        cleaned = [_clean_url(u) for u in found]
        existing = await asyncio.to_thread(_existing_urls_from_db)
        new_urls = [u for u in cleaned if u not in existing]
        if not new_urls:
            await msg.edit_text(f"🤖 排程：{len(cleaned)} 則收藏，全部已在 Notion，無新增")
            return

        target = new_urls[:AUTO_SYNC_MAX]
        await msg.edit_text(
            f"🤖 排程：找到 {len(new_urls)} 則新貼文，處理 {len(target)} 則..."
        )

        success = skip = fail = 0
        for url in target:
            try:
                async def _do():
                    platform = detect_platform(url)
                    scraped = await scrape_url(url, platform)
                    return await _process_one({**scraped, "url": url, "platform": platform})
                ok, _ = await asyncio.wait_for(_do(), timeout=90.0)
                if ok:
                    success += 1
                else:
                    skip += 1
            except Exception:
                fail += 1
                log.exception("[auto_sync] %s 處理失敗", url)

        summary = (
            f"🤖 排程同步完成\n"
            f"━━━━━━━━━━━━━━\n"
            f"✅ 成功 {success}　⏭ 跳過 {skip}　❌ 失敗 {fail}"
        )
        if len(new_urls) > AUTO_SYNC_MAX:
            summary += f"\n剩 {len(new_urls) - AUTO_SYNC_MAX} 則沒處理（下次再跑或手動 /sync {label.lower()} all）"
        await msg.edit_text(summary)
    except Exception as e:
        log.exception("[auto_sync] 排程失敗")
        try:
            await msg.edit_text(f"🤖 排程同步失敗：{type(e).__name__}: {e}")
        except Exception:
            pass


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
    import hmac
    secret = request.headers.get("X-Auth-Secret") or request.query.get("secret", "")
    if not INGEST_SECRET or not hmac.compare_digest(secret, INGEST_SECRET):
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

    # 排程自動同步 Threads（AUTO_SYNC_HOURS > 0）
    if AUTO_SYNC_HOURS > 0 and app.job_queue:
        app.job_queue.run_repeating(
            _scheduled_sync_job,
            interval=AUTO_SYNC_HOURS * 3600,
            first=300,  # 啟動 5 分鐘後跑第一次
            name="auto_sync_threads",
            data="threads",
        )
        log.info(f"[main] 已註冊 Threads 排程同步：每 {AUTO_SYNC_HOURS} 小時、每次最多 {AUTO_SYNC_MAX} 則")
    # 排程自動同步 Instagram（AUTO_SYNC_IG_HOURS > 0）
    if AUTO_SYNC_IG_HOURS > 0 and app.job_queue:
        app.job_queue.run_repeating(
            _scheduled_sync_job,
            interval=AUTO_SYNC_IG_HOURS * 3600,
            first=420,  # 啟動 7 分鐘後跑第一次（跟 Threads 錯開）
            name="auto_sync_instagram",
            data="instagram",
        )
        log.info(f"[main] 已註冊 IG 排程同步：每 {AUTO_SYNC_IG_HOURS} 小時、每次最多 {AUTO_SYNC_MAX} 則")
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VIDEO | filters.ANIMATION | filters.VIDEO_NOTE, handle_video))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    log.info("靈感收集機器人 啟動中...")
    app.run_polling()


if __name__ == "__main__":
    main()
