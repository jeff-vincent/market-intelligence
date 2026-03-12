"""Market Consciousness — Discovery Agent

Runs on a schedule (daily initially, weekly once sources stabilize).
Analyzes WEAK-band items and the entity graph to propose new sources.
All proposals go to operator review — no autonomous source activation.
"""
import asyncio
import json
import logging
import os
import signal
from datetime import datetime, timedelta, timezone

import httpx
from aiohttp import web
from motor.motor_asyncio import AsyncIOMotorClient
import redis.asyncio as aioredis

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("discovery-agent")

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017/mc")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
LLM_GATEWAY_URL = os.environ.get("LLM_GATEWAY_URL", "http://localhost:8082")
DISCOVERY_INTERVAL_HOURS = int(os.environ.get("DISCOVERY_INTERVAL_HOURS", "24"))
SOURCE_HEALTH_SUSPEND_THRESHOLD = float(os.environ.get("SOURCE_HEALTH_SUSPEND_THRESHOLD", "0.15"))

db = None
rd = None
http = None
running = True


async def init_clients():
    global db, rd, http
    mongo = AsyncIOMotorClient(MONGO_URL)
    db_name = MONGO_URL.rsplit("/", 1)[-1].split("?")[0] or "mc"
    db = mongo[db_name]
    rd = aioredis.from_url(REDIS_URL, decode_responses=True)
    http = httpx.AsyncClient(timeout=120.0)

    await db.source_proposals.create_index("status")
    log.info("Discovery agent initialized")


async def llm_chat(tier: str, messages: list) -> str:
    resp = await http.post(f"{LLM_GATEWAY_URL}/v1/chat", json={"tier": tier, "messages": messages})
    resp.raise_for_status()
    return resp.json()["content"]


async def get_weak_items_since(since: datetime) -> list[dict]:
    """Get WEAK-band items since last run."""
    return await db.raw_items.find({
        "t2_band": "WEAK",
        "ingested_at": {"$gte": since},
    }).sort("t2_score", -1).limit(50).to_list(50)


async def get_entity_summary() -> str:
    """Get a text summary of the entity graph."""
    entities = await db.entities.find().sort("last_updated_at", -1).limit(30).to_list(30)
    if not entities:
        return "No entities tracked yet."
    lines = []
    for e in entities:
        lines.append(f"- {e['name']} ({e.get('type', '?')}): {e.get('summary', 'no summary')[:100]}")
    return "\n".join(lines)


async def suspend_unhealthy_sources():
    """Auto-suspend sources below the health threshold."""
    result = await db.sources.update_many(
        {"status": "ACTIVE", "health_score": {"$lt": SOURCE_HEALTH_SUSPEND_THRESHOLD}},
        {"$set": {"status": "SUSPENDED"}},
    )
    if result.modified_count > 0:
        log.info("Suspended %d unhealthy sources", result.modified_count)


async def run_discovery():
    """Execute a discovery cycle."""
    since = datetime.now(timezone.utc) - timedelta(hours=DISCOVERY_INTERVAL_HOURS)
    weak_items = await get_weak_items_since(since)
    active_sources = await db.sources.find({"status": "ACTIVE"}).to_list(200)
    entity_summary = await get_entity_summary()

    if not weak_items and not active_sources:
        log.info("No data for discovery — skipping")
        return

    weak_text = "\n".join([
        f"- [{i.get('t2_score', 0):.2f}] {i.get('title', '')[:100]}"
        for i in weak_items[:20]
    ]) or "No weak-band items this period."

    source_text = "\n".join([
        f"- {s.get('name', s.get('url', '?'))} (health: {s.get('health_score', 0):.2f})"
        for s in active_sources
    ])

    problem_space = await db.problem_space.find_one(sort=[("version", -1)])
    ps_desc = problem_space.get("description", "") if problem_space else ""

    response = await llm_chat("discovery", [
        {
            "role": "system",
            "content": (
                "You are the discovery agent for a market intelligence system.\n\n"
                f"Problem space: {ps_desc}\n\n"
                "Your job is to identify gaps in our monitoring. "
                "All proposals go to operator review — be specific, not cautious.\n\n"
                "Return valid JSON with this structure:\n"
                '{"proposals": [{"url": "...", "name": "...", "type": "rss|reddit|newsletter", '
                '"rationale": "..."}], '
                '"declining_sources": ["source name", ...], '
                '"observations": "free text about patterns you see"}'
            ),
        },
        {
            "role": "user",
            "content": (
                f"WEAK-band items from the past period:\n{weak_text}\n\n"
                f"Current active sources:\n{source_text}\n\n"
                f"Entity graph summary:\n{entity_summary}\n\n"
                "Tasks:\n"
                "1. Identify patterns in weak items suggesting communities we're missing\n"
                "2. Propose up to 5 new sources with URLs and rationale\n"
                "3. Flag existing sources that may be declining in relevance\n"
                "4. Note new entity types we haven't tracked before"
            ),
        },
    ])

    try:
        cleaned = response.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        log.warning("Discovery returned non-JSON: %s", response[:300])
        return

    now = datetime.now(timezone.utc)
    for prop in data.get("proposals", []):
        await db.source_proposals.insert_one({
            "url": prop.get("url", ""),
            "name": prop.get("name", ""),
            "type": prop.get("type", "rss"),
            "rationale": prop.get("rationale", ""),
            "status": "PENDING_REVIEW",
            "proposed_at": now,
        })

    log.info("Discovery complete: %d proposals, observations: %s",
             len(data.get("proposals", [])),
             data.get("observations", "")[:200])


async def discovery_loop():
    """Run discovery on a schedule."""
    # Wait for initial data to accumulate
    await asyncio.sleep(300)

    while running:
        try:
            await suspend_unhealthy_sources()
            await run_discovery()
        except Exception as e:
            log.error("Discovery cycle error: %s", e)

        # Sleep until next cycle
        await asyncio.sleep(DISCOVERY_INTERVAL_HOURS * 3600)


async def healthz(request):
    return web.Response(text="ok")


async def run_server():
    app = web.Application()
    app.router.add_get("/healthz", healthz)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()


async def main():
    await init_clients()
    await run_server()
    log.info("Discovery agent started (interval: %dh)", DISCOVERY_INTERVAL_HOURS)
    await discovery_loop()


if __name__ == "__main__":
    def handle_signal(sig, frame):
        global running
        running = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    asyncio.run(main())
