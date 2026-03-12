"""Market Consciousness — Briefing Agent

Listens for new_briefing events, formats briefings with linked items
and entity changes, and makes them available via the API.
No LLM calls — pure formatting and delivery.
"""
import asyncio
import json
import logging
import os
import signal
from datetime import datetime, timedelta, timezone

from aiohttp import web
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
import redis.asyncio as aioredis

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("briefing-agent")

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017/mc")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

db = None
rd = None
running = True


async def init_clients():
    global db, rd
    mongo = AsyncIOMotorClient(MONGO_URL)
    db_name = MONGO_URL.rsplit("/", 1)[-1].split("?")[0] or "mc"
    db = mongo[db_name]
    rd = aioredis.from_url(REDIS_URL, decode_responses=True)
    log.info("Briefing agent initialized")


async def format_briefing(briefing_id: str):
    """Take raw synthesis output and structure it for the UI."""
    briefing = await db.briefings.find_one({"_id": ObjectId(briefing_id)})
    if not briefing:
        log.warning("Briefing not found: %s", briefing_id)
        return

    # Fetch linked items
    item_ids = [ObjectId(i) for i in briefing.get("item_ids", []) if ObjectId.is_valid(i)]
    items = await db.raw_items.find({"_id": {"$in": item_ids}}).to_list(100) if item_ids else []

    key_items = []
    for item in items:
        source = await db.sources.find_one({"_id": item.get("source_id")}) if item.get("source_id") else None
        key_items.append({
            "id": str(item["_id"]),
            "title": item.get("title", ""),
            "source": source.get("name", "unknown") if source else "unknown",
            "tier": "T4" if item.get("t4_summary") else "T2",
            "summary": item.get("t4_summary") or item.get("excerpt", ""),
            "entities": item.get("t4_entities", []),
            "url": item.get("url", ""),
        })

    # Fetch entity changes for the day
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    changed_entities = await db.entities.find({
        "$or": [
            {"first_seen_at": {"$gte": since}},
            {"last_updated_at": {"$gte": since}},
        ]
    }).to_list(50)

    entity_changes = []
    for e in changed_entities:
        is_new = e.get("first_seen_at", datetime.min.replace(tzinfo=timezone.utc)) >= since
        entity_changes.append({
            "entity": e["name"],
            "type": e.get("type", ""),
            "change": "new" if is_new else "updated",
            "detail": e.get("summary", "")[:100],
        })

    # Save formatted version
    formatted = {
        "date": briefing.get("date", ""),
        "synthesis": briefing.get("synthesis", ""),
        "key_items": key_items,
        "entity_changes": entity_changes,
        "formatted_at": datetime.now(timezone.utc),
    }

    await db.briefings.update_one(
        {"_id": ObjectId(briefing_id)},
        {"$set": {"formatted": formatted}},
    )
    log.info("Formatted briefing %s: %d items, %d entity changes",
             briefing_id, len(key_items), len(entity_changes))


async def listen_briefings():
    """Listen for new briefings to format."""
    pubsub = rd.pubsub()
    await pubsub.subscribe("new_briefing")
    log.info("Listening for new_briefing events")

    async for msg in pubsub.listen():
        if not running:
            break
        if msg["type"] != "message":
            continue
        try:
            data = json.loads(msg["data"])
            await format_briefing(data["id"])
        except Exception as e:
            log.error("Briefing format error: %s", e)


async def healthz(request):
    return web.Response(text="ok")


async def run_server():
    app = web.Application()
    app.router.add_get("/healthz", healthz)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8085)
    await site.start()
    log.info("Briefing agent on :8085")


async def main():
    await init_clients()
    await run_server()
    await listen_briefings()


if __name__ == "__main__":
    def handle_signal(sig, frame):
        global running
        running = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    asyncio.run(main())
