"""
rss_fetcher.py  —  Phase 1.3
─────────────────────────────
Polls all active RSS sources every FETCH_INTERVAL_MINUTES minutes.
Each new article goes through the exact same pipeline as user submissions:
  normalize → hash → duplicate check → similarity check → pending → admin
"""

import asyncio
import logging
from datetime import datetime

import feedparser

import database as db

# ── Configuration ─────────────────────────────────────────────────────────────
FETCH_INTERVAL_MINUTES = 15   # How often to poll all feeds (change as you like)
# ──────────────────────────────────────────────────────────────────────────────

logger = logging.getLogger(__name__)


def parse_feed(url: str) -> list[dict]:
    """
    Download and parse one RSS feed URL.
    Returns a list of articles, each as a dict with:
      title, description, link, published
    feedparser handles all the messy RSS/Atom differences for us.
    """
    try:
        feed = feedparser.parse(url)
    except Exception as e:
        logger.error(f"feedparser crashed on {url}: {e}")
        return []

    articles = []
    for entry in feed.entries:
        title       = getattr(entry, "title",   "") or ""
        description = getattr(entry, "summary", "") or ""
        link        = getattr(entry, "link",    "") or ""

        # Some feeds give a published date, others don't
        published = ""
        if hasattr(entry, "published"):
            published = entry.published

        articles.append({
            "title":       title.strip(),
            "description": description.strip(),
            "link":        link.strip(),
            "published":   published,
        })

    return articles


def build_post_text(source_name: str, article: dict) -> str:
    """
    Build the text that will be stored and shown in Telegram.
    Format:
        📰 Source Name
        Title of the article

        Short description of the article...

        🔗 https://link-to-article.com
    """
    parts = []

    if article["title"]:
        parts.append(article["title"])

    # Trim description to 400 chars to keep messages readable
    desc = article["description"]
    if desc:
        # Strip simple HTML tags that sometimes appear in RSS descriptions
        import re
        desc = re.sub(r"<[^>]+>", "", desc)   # remove <p>, <b>, etc.
        desc = re.sub(r"\s+", " ", desc).strip()
        if len(desc) > 400:
            desc = desc[:400] + "…"
        parts.append(desc)

    if article["link"]:
        parts.append(f"🔗 {article['link']}")

    return "\n\n".join(parts)


async def process_one_article(
    source_name: str,
    source_url_feed: str,
    article: dict,
    notify_callback,          # async function(post_id) that sends to admins
):
    """
    Run one RSS article through the full pipeline.
    notify_callback is the send_to_admin function from bot.py.
    """
    text = build_post_text(source_name, article)
    if not text.strip():
        return  # Skip empty articles

    # Quick pre-check: if this exact URL was already submitted, skip it.
    # This is our first line of defence against re-fetching the same article.
    norm = db.normalize_text(text)
    h    = db.make_hash(norm)

    already_seen = await db.hash_already_seen(h)
    if already_seen:
        logger.debug(f"[{source_name}] Already seen: {article['title'][:60]}")
        return

    # Insert into the posts table (same as user submission)
    post_id = await db.add_post(
        content_type="text",
        text=text,
        user_id=None,             # no user — this came from RSS
        file_id=None,
        source_type="rss",
        source_name=source_name,
        source_url=article["link"],
    )
    await db.log_action(post_id, "rss_fetched")

    logger.info(f"[{source_name}] New article → post #{post_id}: {article['title'][:60]}")

    # Send to admin (same function used for user submissions)
    await notify_callback(post_id)


async def fetch_all_sources(notify_callback):
    """
    Fetch every active RSS source once.
    Called on a schedule by run_rss_fetcher().
    """
    sources = await db.get_active_sources()

    if not sources:
        logger.info("RSS fetcher: no active sources configured.")
        return

    logger.info(f"RSS fetcher: checking {len(sources)} source(s)…")

    for source in sources:
        source_name = source["name"]
        feed_url    = source["url"]

        logger.info(f"  Fetching: {source_name} ({feed_url})")
        articles = parse_feed(feed_url)
        logger.info(f"  Found {len(articles)} article(s) in feed.")

        for article in articles:
            try:
                await process_one_article(
                    source_name=source_name,
                    source_url_feed=feed_url,
                    article=article,
                    notify_callback=notify_callback,
                )
            except Exception as e:
                logger.error(f"  Error processing article from {source_name}: {e}")

        # Small pause between sources to be polite to their servers
        await asyncio.sleep(2)


async def run_rss_fetcher(notify_callback):
    """
    Infinite loop: fetch all sources, wait FETCH_INTERVAL_MINUTES, repeat.
    Run this with asyncio.gather() alongside the Telegram bot polling.
    """
    logger.info(
        f"RSS fetcher started. Will poll every {FETCH_INTERVAL_MINUTES} minute(s)."
    )

    while True:
        try:
            await fetch_all_sources(notify_callback)
        except Exception as e:
            logger.error(f"RSS fetcher cycle error: {e}")

        logger.info(f"RSS fetcher: sleeping {FETCH_INTERVAL_MINUTES} min until next poll…")
        await asyncio.sleep(FETCH_INTERVAL_MINUTES * 60)