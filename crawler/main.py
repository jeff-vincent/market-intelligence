"""Market Consciousness — Crawler Service

Fetches content from configured sources (RSS, HN, Reddit),
deduplicates via Redis, stores in MongoDB, publishes to Redis pub/sub.
"""
import asyncio
import hashlib
import json
import logging
import os
import signal
import sys
from datetime import datetime, timedelta, timezone

import aiohttp
import feedparser
from aiohttp import web
from bs4 import BeautifulSoup
from motor.motor_asyncio import AsyncIOMotorClient
import redis.asyncio as aioredis

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("crawler")

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017/mc")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
POLL_DEFAULT_MINS = int(os.environ.get("POLL_DEFAULT_MINS", "240"))

mongo = None
db = None
rd = None
running = True


async def init_clients():
    global mongo, db, rd
    mongo = AsyncIOMotorClient(MONGO_URL)
    db_name = MONGO_URL.rsplit("/", 1)[-1].split("?")[0] or "mc"
    db = mongo[db_name]
    rd = aioredis.from_url(REDIS_URL, decode_responses=True)

    # Ensure indexes
    await db.sources.create_index("url", unique=True)
    await db.raw_items.create_index("url_hash", unique=True)
    await db.raw_items.create_index([("t2_band", 1)])
    log.info("MongoDB and Redis connected")


async def seed_default_sources():
    """Insert starter sources if the collection is empty."""
    count = await db.sources.count_documents({})
    if count > 0:
        return

    defaults = [
        {"url": "https://news.ycombinator.com/rss", "type": "rss", "poll_interval_mins": 60, "name": "Hacker News"},
        {"url": "https://www.reddit.com/r/kubernetes/.rss", "type": "rss", "poll_interval_mins": 120, "name": "r/kubernetes"},
        {"url": "https://www.reddit.com/r/devops/.rss", "type": "rss", "poll_interval_mins": 120, "name": "r/devops"},
        {"url": "https://www.reddit.com/r/selfhosted/.rss", "type": "rss", "poll_interval_mins": 240, "name": "r/selfhosted"},
        {"url": "https://blog.golang.org/feed.atom", "type": "rss", "poll_interval_mins": 1440, "name": "Go Blog"},
        {"url": "https://kubernetes.io/feed.xml", "type": "rss", "poll_interval_mins": 1440, "name": "Kubernetes Blog"},
        {"url": "https://www.cncf.io/blog/feed/", "type": "rss", "poll_interval_mins": 720, "name": "CNCF Blog"},
    ]
    for s in defaults:
        s.update({
            "health_score": 0.5,
            "status": "ACTIVE",
            "proposed_by": "SYSTEM",
            "last_crawled_at": None,
            "last_signal_at": None,
            "metadata": {},
            "created_at": datetime.now(timezone.utc),
        })
    await db.sources.insert_many(defaults)
    log.info("Seeded %d default sources", len(defaults))


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:32]


async def is_duplicate(url: str) -> bool:
    h = url_hash(url)
    added = await rd.sadd("seen_urls", h)
    return added == 0


async def fetch_rss(session: aiohttp.ClientSession, source: dict) -> list[dict]:
    """Fetch and parse an RSS/Atom feed."""
    items = []
    try:
        async with session.get(source["url"], timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                log.warning("RSS fetch %s returned %d", source["url"], resp.status)
                return items
            body = await resp.text()
    except Exception as e:
        log.warning("RSS fetch error %s: %s", source["url"], e)
        return items

    feed = feedparser.parse(body)
    for entry in feed.entries[:50]:
        link = entry.get("link", "")
        if not link:
            continue
        title = entry.get("title", "")
        summary = entry.get("summary", "")
        if summary:
            summary = BeautifulSoup(summary, "html.parser").get_text()[:500]
        items.append({"url": link, "title": title, "excerpt": summary, "raw_body": summary})
    return items


async def fetch_hn_top(session: aiohttp.ClientSession, source: dict) -> list[dict]:
    """Fetch top stories from HN Algolia API."""
    items = []
    try:
        url = "https://hn.algolia.com/api/v1/search_by_date?tags=story&hitsPerPage=30"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json()
    except Exception as e:
        log.warning("HN fetch error: %s", e)
        return items

    for hit in data.get("hits", []):
        link = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}"
        title = hit.get("title", "")
        items.append({"url": link, "title": title, "excerpt": title, "raw_body": ""})
    return items


async def process_items(items: list[dict], source_id) -> int:
    """Deduplicate, store, and publish items. Returns count stored."""
    stored = 0
    for item in items:
        if await is_duplicate(item["url"]):
            continue

        doc = {
            "url": item["url"],
            "url_hash": url_hash(item["url"]),
            "source_id": source_id,
            "title": item["title"],
            "excerpt": item.get("excerpt", ""),
            "raw_body": item.get("raw_body", ""),
            "embedding": None,
            "t2_score": None,
            "t2_band": None,
            "ingested_at": datetime.now(timezone.utc),
            "processed_at": None,
        }
        try:
            result = await db.raw_items.insert_one(doc)
            await rd.publish("raw_items", json.dumps({
                "id": str(result.inserted_id),
                "url": item["url"],
            }))
            stored += 1
        except Exception as e:
            # duplicate key is expected on race
            if "duplicate key" not in str(e).lower():
                log.warning("Insert error: %s", e)
    return stored


async def crawl_source(session: aiohttp.ClientSession, source: dict):
    """Crawl a single source."""
    src_type = source.get("type", "rss")
    if src_type == "rss":
        items = await fetch_rss(session, source)
    elif src_type == "hackernews":
        items = await fetch_hn_top(session, source)
    else:
        log.warning("Unknown source type: %s", src_type)
        return

    stored = await process_items(items, source["_id"])
    await db.sources.update_one(
        {"_id": source["_id"]},
        {"$set": {"last_crawled_at": datetime.now(timezone.utc)}},
    )
    log.info("Crawled %s: %d items fetched, %d new", source.get("name", source["url"]), len(items), stored)


async def crawl_loop():
    """Main crawl loop — polls sources based on their intervals."""
    async with aiohttp.ClientSession() as session:
        while running:
            now = datetime.now(timezone.utc)
            sources = await db.sources.find({"status": "ACTIVE"}).to_list(500)

            for source in sources:
                interval = source.get("poll_interval_mins", POLL_DEFAULT_MINS)
                last = source.get("last_crawled_at")
                if last and (now - last) < timedelta(minutes=interval):
                    continue
                try:
                    await crawl_source(session, source)
                except Exception as e:
                    log.error("Error crawling %s: %s", source.get("name", "?"), e)

            await asyncio.sleep(60)


async def listen_crawl_tasks():
    """Listen for targeted crawl tasks from other agents via Redis pub/sub."""
    pubsub = rd.pubsub()
    await pubsub.subscribe("crawl_tasks")
    log.info("Listening for crawl_tasks")

    async for msg in pubsub.listen():
        if msg["type"] != "message":
            continue
        try:
            task = json.loads(msg["data"])
            query = task.get("query", "")
            log.info("Targeted crawl task: %s (from %s)", query, task.get("requested_by", "unknown"))
            # For now, use HN Algolia search as the targeted crawl mechanism
            async with aiohttp.ClientSession() as session:
                url = f"https://hn.algolia.com/api/v1/search?query={query}&hitsPerPage=10"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    data = await resp.json()
                items = []
                for hit in data.get("hits", []):
                    link = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}"
                    items.append({"url": link, "title": hit.get("title", ""), "excerpt": hit.get("title", ""), "raw_body": ""})
                # Use a synthetic source_id for targeted crawls
                stored = await process_items(items, "targeted")
                log.info("Targeted crawl for '%s': %d items stored", query, stored)
        except Exception as e:
            log.error("Crawl task error: %s", e)


# Health check endpoint
async def healthz(request):
    return web.Response(text="ok")


async def run_server():
    app = web.Application()
    app.router.add_get("/healthz", healthz)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    log.info("Health check server on :8080")


async def main():
    await init_clients()
    await seed_default_sources()
    await run_server()

    await asyncio.gather(
        crawl_loop(),
        listen_crawl_tasks(),
    )


if __name__ == "__main__":
    def handle_signal(sig, frame):
        global running
        running = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    asyncio.run(main())
