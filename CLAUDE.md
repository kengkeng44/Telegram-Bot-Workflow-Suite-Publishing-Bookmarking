# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository layout

This is a **two-package monorepo** of Telegram bots, both Python 3.12, both bridging Telegram ↔ Claude ↔ Notion / Threads:

- **`threads-bot/`** — *Data Hoarder*. The **production** bot. Multi-platform bookmarking pipeline (Telegram / iOS Shortcut webhook / scheduled scrape of Threads "saved" → Playwright scrape → Claude Haiku → Notion DB). All logic lives in a single `bot.py` (~1100 lines). Has its own [`threads-bot/CLAUDE.md`](threads-bot/CLAUDE.md) with the detailed architecture, Notion schema, scrape paths, and retry policies — read that file first when touching `threads-bot/`.
- **`xiaofa-bot/`** — *Auto-publishing bot*. **Work-in-progress / not production**. Pulls drafts from Notion, runs them through Claude, posts to Threads via the official Graph API. `bot.py` is the older Notion-saver variant; `xiaofa_bot.py` is the Threads publisher; `v2/bot_render.py` is a Render-deployed Jina-Reader variant. These three files don't form a coherent shipping product — treat changes here as exploratory.

> The folder `threads-bot/` is a historical name. The bot now handles Threads / IG / FB / YouTube / Medium / X / generic web / plain text — renaming is blocked on coordinating Railway's Root Directory setting (see `TODO.md`).

Top-level docs (`README.md`, `JOURNEY.md`, `LESSONS.md`, `TODO.md`) are user-facing narrative — don't treat them as authoritative API contracts. The bot code is the source of truth.

## Common commands

All commands run from inside the relevant subdirectory.

### `threads-bot/` (the active codebase)

```bash
cd threads-bot
pip install -r requirements.txt
python -m playwright install chromium       # one-time, needed by the scraper
cp .env.example .env                        # then fill in secrets
python run_local.py                         # starts polling

# Generate THREADS_STATE_JSON cookie blob (interactive Chromium login):
python get_cookies.py

# Container build (matches Railway deploy):
docker build -t threads-bot .
docker run --rm --env-file .env threads-bot
```

There is **no test suite, no linter config, no formatter config** — verification is by running the bot and exercising commands in Telegram. There is a `gitleaks` pre-commit hook (`.pre-commit-config.yaml`) that scans staged files for hardcoded secrets; install with `pip install pre-commit && pre-commit install`.

### `xiaofa-bot/`

```bash
cd xiaofa-bot
pip install -r requirements.txt
cp .env.example .env
python bot.py            # Notion-saver variant + Flask /save HTTP endpoint on :5000
python xiaofa_bot.py     # Threads auto-publishing variant
python get_token.py      # OAuth flow to mint a long-lived THREADS_ACCESS_TOKEN
```

`xiaofa-bot/v2/bot_render.py` is a separate Render-targeted deployment (see `xiaofa-bot/v2/render.yaml`).

## Critical operational constraints

- **Never run two bot instances against the same Telegram token.** Both will collide on `getUpdates` and tear each other down (`Conflict: terminated by other getUpdates request`). When deploying to Railway, stop the local `run_local.py` first.
- **The bot reads its own outgoing messages** (Telegram delivers them back through `getUpdates`). `handle_message` in `threads-bot/bot.py` skips bot-self messages unless they contain a URL — preserve this guard, otherwise `✅ 完成` replies get re-processed in an infinite loop.
- **Notion property names are Chinese and must match the database schema exactly** (`標題`, `摘要`, `分類`, `平台`, `關鍵字`, `狀態`, `原文摘錄`, ...). They are not configurable. If you rename one in code, the matching column in the user's Notion DB must also be renamed.
- **`狀態` has a status-vs-select fallback** in `write_to_notion`: it tries `select` first, retries as `status` if Notion rejects. Don't simplify that try/except away.
- **The Claude model is pinned to `claude-haiku-4-5-20251001`** in `threads-bot/bot.py`. `xiaofa-bot/xiaofa_bot.py` still references `claude-opus-4-5` (invalid ID — known issue, part of why xiaofa-bot is WIP). When updating models, also update the four-category constraint (`CATEGORIES`) and Notion `分類` Select options together.
- **The `/ingest` HTTP webhook uses `hmac.compare_digest`** for the auth secret comparison — don't change it to `==`, that re-introduces a timing attack.
- **`THREADS_STATE_JSON` is required for the full Threads pipeline.** Without it, the bot falls back through HTML script tags / OG meta / regex — which only return partial data (no like counts, often no images, sometimes only the OG description). `_fetch_saved_post_urls` (used by `/sync` and the scheduled job) hard-fails without it.

## Architecture in one paragraph (for `threads-bot/`)

Three independent entry points all funnel into the same per-URL pipeline. **(1)** `MessageHandler` for Telegram text messages → `handle_message` → `extract_urls` (with tracking-param stripping via `_clean_url`). **(2)** `aiohttp` HTTP server on `PORT` exposing `POST /ingest` (auth: `X-Auth-Secret` header) → `_process_via_webhook`. **(3)** `JobQueue.run_repeating` (every `AUTO_SYNC_HOURS`) and `/sync` command → `_fetch_saved_post_urls` scrolls the Threads "saved" page and harvests post URLs. All three converge on `scrape_url` (dispatches to `_scrape_threads` or `_scrape_generic` under a single `SCRAPE_SEM = Semaphore(1)` guard against a shared `_browser`) → `analyze_with_claude` (off-thread via `asyncio.to_thread`, JSON output validated against `CATEGORIES`) → `write_to_notion` (off-thread, with the `select`/`status` fallback). De-duplication is done up-front by pre-loading existing URLs from the Notion DB's title-link field via `_existing_urls_from_db`.

The Threads scrape has a **five-layer fallback chain** (GraphQL response interception → `data-sjs` script JSON with `thread_items` → loose `data-sjs` with `caption` → Open Graph meta → bare regex on `"caption":{"text":"..."}`). Logs at INFO level identify which layer succeeded — preserve the `[scrape_threads] ✅` / `⚠️` markers when refactoring; they are the primary debugging signal in Railway logs.

## When things break

- Threads scrape suddenly returns empty `text`: their server-side render structure changed. Check Railway logs for `[scrape_threads] captured=N graphql responses` vs `❌ 全部失敗`. The jmespath path `data.data.containing_thread.thread_items[*].post | [0]` is the most fragile point — cross-reference against a fresh DevTools `/graphql/query` response. The bot writes a debug dump to `last_failed_response.json` (gitignored) when it captures GraphQL but can't extract.
- `/sync` reports "0 posts" with `final URL` redirected to the home page: `THREADS_STATE_JSON` cookies expired. Re-run `get_cookies.py` and update the env var (or Infisical secret).
- Notion writes fail with `unauthorized`: the integration is not connected to that Database. Notion DB → `•••` → Connections → add the integration.
- Railway `/usage` reports "schema 改了": Railway's GraphQL API drifts; the code already falls back to surfacing the dashboard URL. Don't add hard error handling here.

## Conventions

- **Comments and user-facing strings are in Traditional Chinese.** Match the existing language when editing — don't translate to English.
- **Single-file modules.** `threads-bot/bot.py` is intentionally one big file; resist the urge to split into packages unless the user asks. Prior refactors that fragmented it were reverted.
- **Tenacity retries are scoped to transient failures only** (`PlaywrightTimeoutError`, `APIConnectionError`, `RateLimitError`, `RequestTimeoutError`). Don't add retries for `ValueError` or `JSONDecodeError` — those mean Threads structure changed or Claude returned garbage, and retrying just delays the real fix.
- **Browser lifecycle**: `_ensure_browser` lazily creates one shared Chromium; `_shutdown_browser` is registered as `post_shutdown`. Per-request `context` is created and `await context.close()`-ed in a `finally` — keep this pattern; one browser, many contexts.
