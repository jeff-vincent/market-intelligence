"""Market Consciousness — Synthesis Agent

Runs daily. Reads the day's T4 summaries, entity context, and graph changes
to produce a strategic briefing. Can spawn targeted crawl tasks when it
detects gaps in coverage.
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
log = logging.getLogger("synthesis-agent")

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017/mc")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
LLM_GATEWAY_URL = os.environ.get("LLM_GATEWAY_URL", "http://localhost:8082")
SYNTHESIS_INTERVAL_HOURS = int(os.environ.get("SYNTHESIS_INTERVAL_HOURS", "24"))

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
    log.info("Synthesis agent initialized")


async def llm_chat(tier: str, messages: list) -> str:
    resp = await http.post(f"{LLM_GATEWAY_URL}/v1/chat", json={"tier": tier, "messages": messages})
    resp.raise_for_status()
    return resp.json()["content"]


async def get_todays_summaries() -> list[dict]:
    """Get items that were T4-analyzed today."""
    since = datetime.now(timezone.utc) - timedelta(hours=SYNTHESIS_INTERVAL_HOURS)
    return await db.raw_items.find({
        "t4_analyzed_at": {"$gte": since},
        "t4_summary": {"$exists": True, "$ne": ""},
    }).sort("t4_analyzed_at", -1).to_list(100)


async def get_entity_context(items: list[dict]) -> str:
    """Get entity context for the items being synthesized."""
    entity_names = set()
    for item in items:
        for name in item.get("t4_entities", []):
            entity_names.add(name)

    if not entity_names:
        return "No entities referenced."

    entities = await db.entities.find(
        {"name": {"$in": list(entity_names)}}
    ).to_list(100)

    lines = []
    for e in entities:
        lines.append(f"- {e['name']} ({e.get('type', '?')}): {e.get('summary', 'no summary')[:150]}")
    return "\n".join(lines) or "No entity details available."


async def get_graph_changes() -> str:
    """Summarize entity graph changes since yesterday."""
    since = datetime.now(timezone.utc) - timedelta(hours=SYNTHESIS_INTERVAL_HOURS)

    new_entities = await db.entities.find(
        {"first_seen_at": {"$gte": since}}
    ).to_list(50)

    updated_rels = await db.entity_relationships.find(
        {"last_seen_at": {"$gte": since}}
    ).to_list(50)

    lines = []
    if new_entities:
        lines.append(f"New entities ({len(new_entities)}):")
        for e in new_entities[:10]:
            lines.append(f"  + {e['name']} ({e.get('type', '?')})")

    if updated_rels:
        lines.append(f"Updated relationships ({len(updated_rels)}):")
        for r in updated_rels[:10]:
            # Look up entity names
            from_ent = await db.entities.find_one({"_id": r["from_entity_id"]})
            to_ent = await db.entities.find_one({"_id": r["to_entity_id"]})
            from_name = from_ent["name"] if from_ent else "?"
            to_name = to_ent["name"] if to_ent else "?"
            lines.append(f"  {from_name} --{r['relationship']}--> {to_name} (strength: {r.get('strength', 0):.2f})")

    return "\n".join(lines) or "No graph changes since last synthesis."


async def run_synthesis(query: str | None = None) -> str:
    """Execute a synthesis cycle and return the briefing ID."""
    items = await get_todays_summaries()
    entity_context = await get_entity_context(items)
    graph_changes = await get_graph_changes()

    problem_space = await db.problem_space.find_one(sort=[("version", -1)])
    ps_desc = problem_space.get("description", "") if problem_space else ""

    summaries_text = "\n".join([
        f"- [{i.get('title', '')}]: {i.get('t4_summary', '')[:200]}"
        for i in items
    ]) or "No T4 analyses available for this period."

    system_prompt = (
        "You are synthesising market intelligence.\n\n"
        f"Problem space: {ps_desc}\n\n"
        "Write a briefing: what does today's signal set suggest about the state "
        "of this space? What patterns are forming? What would you watch more closely?\n\n"
        "Be willing to speculate, but label speculation clearly. "
        "Quality over coverage. 3-5 paragraphs."
    )

    user_content = (
        f"Today's signals ({len(items)} items):\n{summaries_text}\n\n"
        f"Entity context:\n{entity_context}\n\n"
        f"Graph changes:\n{graph_changes}"
    )

    if query:
        user_content += f"\n\nSpecific question from operator: {query}"

    response = await llm_chat("synthesis", [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ])

    # Save briefing
    now = datetime.now(timezone.utc)
    briefing = {
        "date": now.strftime("%Y-%m-%d"),
        "synthesis": response,
        "item_count": len(items),
        "item_ids": [str(i["_id"]) for i in items],
        "entity_names": list(set(
            name for i in items for name in i.get("t4_entities", [])
        )),
        "query": query,
        "created_at": now,
    }
    result = await db.briefings.insert_one(briefing)

    # Publish for briefing agent
    await rd.publish("new_briefing", json.dumps({
        "id": str(result.inserted_id),
        "date": briefing["date"],
    }))

    log.info("Synthesis complete: %d items → briefing %s", len(items), result.inserted_id)
    return str(result.inserted_id)


async def check_for_gaps(items: list[dict]):
    """If synthesis identifies entities with thin coverage, request targeted crawls."""
    entity_names = set()
    for item in items:
        for name in item.get("t4_entities", []):
            entity_names.add(name)

    for name in entity_names:
        entity = await db.entities.find_one({"name": name})
        if not entity:
            continue
        # If entity was seen in signals but has no summary and evidence is thin
        rels = await db.entity_relationships.count_documents({
            "$or": [{"from_entity_id": entity["_id"]}, {"to_entity_id": entity["_id"]}]
        })
        if rels < 2 and not entity.get("summary"):
            await rd.publish("crawl_tasks", json.dumps({
                "type": "targeted",
                "query": f"{name} developer tools",
                "reason": f"Entity '{name}' has thin coverage ({rels} relationships, no summary)",
                "requested_by": "synthesis-agent",
            }))
            log.info("Requested targeted crawl for: %s", name)


async def handle_synthesise(request):
    """POST /synthesise — on-demand synthesis with an optional query."""
    body = await request.json()
    query = body.get("query")
    briefing_id = await run_synthesis(query=query)
    return web.json_response({"briefing_id": briefing_id})


async def synthesis_loop():
    """Run synthesis on a schedule."""
    # Wait for data to accumulate
    await asyncio.sleep(600)

    while running:
        try:
            items = await get_todays_summaries()
            if items:
                await run_synthesis()
                await check_for_gaps(items)
            else:
                log.info("No T4 items for synthesis — skipping")
        except Exception as e:
            log.error("Synthesis cycle error: %s", e)

        await asyncio.sleep(SYNTHESIS_INTERVAL_HOURS * 3600)


async def healthz(request):
    return web.Response(text="ok")


async def run_server():
    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_post("/synthesise", handle_synthesise)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8083)
    await site.start()
    log.info("Synthesis agent on :8083")


async def main():
    await init_clients()
    await run_server()
    await synthesis_loop()


if __name__ == "__main__":
    def handle_signal(sig, frame):
        global running
        running = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    asyncio.run(main())
