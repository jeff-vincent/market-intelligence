"""Market Consciousness — Entity Extractor

Listens for scored_items (PASS band), runs T3 classification via the LLM gateway,
then T4 deep analysis on survivors. Extracts entities and relationships into
the knowledge graph in MongoDB.
"""
import asyncio
import json
import logging
import os
import signal
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np
from aiohttp import web
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
import redis.asyncio as aioredis

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("entity-extractor")

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017/mc")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
LLM_GATEWAY_URL = os.environ.get("LLM_GATEWAY_URL", "http://localhost:8082")

db = None
rd = None
http = None
running = True


async def init_clients():
    global db, rd, http
    mongo = AsyncIOMotorClient(MONGO_URL)
    db = mongo.get_default_database(default="mc")
    rd = aioredis.from_url(REDIS_URL, decode_responses=True)
    http = httpx.AsyncClient(timeout=60.0)

    await db.entities.create_index("name")
    await db.entities.create_index("type")
    await db.entity_relationships.create_index("from_entity_id")
    await db.entity_relationships.create_index("to_entity_id")
    log.info("Entity extractor initialized")


async def llm_chat(tier: str, messages: list) -> str:
    resp = await http.post(f"{LLM_GATEWAY_URL}/v1/chat", json={"tier": tier, "messages": messages})
    resp.raise_for_status()
    return resp.json()["content"]


async def llm_embed(text: str) -> list[float]:
    resp = await http.post(f"{LLM_GATEWAY_URL}/v1/embed", json={"input": text})
    resp.raise_for_status()
    return resp.json()["embedding"]


async def t3_classify(item: dict) -> bool:
    """Quick T3 classification — is this worth deep analysis?"""
    text = f"{item.get('title', '')}\n{item.get('excerpt', '')[:150]}"
    result = await llm_chat("t3", [
        {"role": "system", "content": "You are a relevance classifier. Answer ONLY 'yes' or 'no'."},
        {"role": "user", "content": (
            f"Is this content about developer tools, infrastructure, CI/CD, "
            f"local dev environments, containers, Kubernetes, or platform engineering?\n\n"
            f"{text}"
        )},
    ])
    return result.strip().lower().startswith("yes")


async def t4_analyze(item: dict) -> dict | None:
    """Deep T4 analysis — extract entities, relationships, and summary."""
    text = item.get("raw_body", "") or item.get("excerpt", "")
    if not text.strip():
        return None

    # Chunk long text, take first 2000 chars for analysis
    text = text[:2000]

    result = await llm_chat("t4", [
        {"role": "system", "content": (
            "You are an entity extraction system for market intelligence about "
            "developer tools, infrastructure, and platform engineering.\n\n"
            "Extract ONLY named, specific entities that would be worth tracking over time:\n"
            "- TOOLS/PROJECTS: specific software, frameworks, or open-source projects (e.g. Kubernetes, Terraform, ArgoCD)\n"
            "- COMPANIES/ORGS: companies, foundations, or organizations (e.g. HashiCorp, CNCF, Vercel)\n"
            "- CONCEPTS: specific technical patterns or architectural approaches worth tracking (e.g. GitOps, service mesh, eBPF)\n\n"
            "DO NOT extract:\n"
            "- Reddit/forum usernames or authors\n"
            "- Generic programming terms (e.g. logs, containers, YAML, main, dev)\n"
            "- Job titles or career terms\n"
            "- Single letters, abbreviations without meaning, or vague terms\n\n"
            "Use canonical names: 'Kubernetes' not 'k8s' or 'K8s'. Prefer full product names.\n\n"
            "Return valid JSON with this structure:\n"
            '{"entities": [{"name": "...", "type": "tool|company|concept", "summary": "one sentence about why this entity matters"}], '
            '"relationships": [{"from": "...", "to": "...", "type": "competes_with|built_by|uses|part_of|integrates_with"}], '
            '"item_summary": "one paragraph summary of the content"}\n\n'
            "Quality over quantity. Only include entities you are confident are specific, named things worth tracking."
        )},
        {"role": "user", "content": f"Title: {item.get('title', '')}\n\n{text}"},
    ])

    try:
        # Try to parse JSON from the response
        cleaned = result.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(cleaned)
    except json.JSONDecodeError:
        log.warning("T4 returned non-JSON: %s", result[:200])
        return None


async def upsert_entity(entity_data: dict) -> ObjectId:
    """Insert or update an entity in MongoDB."""
    name = entity_data["name"]
    existing = await db.entities.find_one({"name": name})
    now = datetime.now(timezone.utc)

    if existing:
        update = {"$set": {"last_updated_at": now}}
        if entity_data.get("summary"):
            update["$set"]["summary"] = entity_data["summary"]
        await db.entities.update_one({"_id": existing["_id"]}, update)
        return existing["_id"]
    else:
        doc = {
            "name": name,
            "type": entity_data.get("type", "concept"),
            "summary": entity_data.get("summary", ""),
            "summary_embedding": None,
            "first_seen_at": now,
            "last_updated_at": now,
            "summary_ttl": None,
            "watch_level": "PASSIVE",
            "strength": 0.5,
            "metadata": {},
        }
        result = await db.entities.insert_one(doc)
        return result.inserted_id


async def upsert_relationship(from_id, to_id, rel_type: str):
    """Insert or strengthen a relationship."""
    existing = await db.entity_relationships.find_one({
        "from_entity_id": from_id,
        "to_entity_id": to_id,
        "relationship": rel_type,
    })
    now = datetime.now(timezone.utc)

    if existing:
        new_strength = min(existing.get("strength", 0.5) + 0.1, 1.0)
        await db.entity_relationships.update_one(
            {"_id": existing["_id"]},
            {"$set": {"last_seen_at": now, "strength": new_strength},
             "$inc": {"evidence_count": 1}},
        )
    else:
        await db.entity_relationships.insert_one({
            "from_entity_id": from_id,
            "to_entity_id": to_id,
            "relationship": rel_type,
            "strength": 0.5,
            "first_seen_at": now,
            "last_seen_at": now,
            "evidence_count": 1,
        })


async def process_item(item_id: str, band: str):
    """Full extraction pipeline for a single item."""
    item = await db.raw_items.find_one({"_id": ObjectId(item_id)})
    if not item:
        return

    # Only PASS items get T3/T4 — WEAK items go to discovery agent
    if band != "PASS":
        return

    # T3 — quick gate
    try:
        passes_t3 = await t3_classify(item)
    except Exception as e:
        log.warning("T3 error for %s: %s", item_id, e)
        return

    if not passes_t3:
        log.info("T3 rejected: %s", item.get("title", "")[:80])
        return

    # T4 — deep analysis
    try:
        analysis = await t4_analyze(item)
    except Exception as e:
        log.warning("T4 error for %s: %s", item_id, e)
        return

    if not analysis:
        return

    # Store entities (skip low-quality extractions)
    entity_ids = {}
    skip_types = {"person", "community"}
    for ent in analysis.get("entities", []):
        name = (ent.get("name") or "").strip()
        if not name or len(name) < 2:
            continue
        if ent.get("type", "").lower() in skip_types:
            continue
        eid = await upsert_entity(ent)
        entity_ids[name] = eid

    # Store relationships
    for rel in analysis.get("relationships", []):
        from_id = entity_ids.get(rel.get("from"))
        to_id = entity_ids.get(rel.get("to"))
        if from_id and to_id:
            await upsert_relationship(from_id, to_id, rel.get("type", "mentioned_with"))

    # Update the item with T4 results
    await db.raw_items.update_one(
        {"_id": ObjectId(item_id)},
        {"$set": {
            "t4_summary": analysis.get("item_summary", ""),
            "t4_entities": [e["name"] for e in analysis.get("entities", [])],
            "t4_analyzed_at": datetime.now(timezone.utc),
        }},
    )

    log.info("Extracted %d entities, %d relationships from: %s",
             len(analysis.get("entities", [])),
             len(analysis.get("relationships", [])),
             item.get("title", "")[:80])


async def listen_scored_items():
    """Subscribe to scored_items and process PASS items."""
    pubsub = rd.pubsub()
    await pubsub.subscribe("scored_items")
    log.info("Listening for scored_items")

    async for msg in pubsub.listen():
        if not running:
            break
        if msg["type"] != "message":
            continue
        try:
            data = json.loads(msg["data"])
            await process_item(data["id"], data.get("band", "PASS"))
        except Exception as e:
            log.error("Process error: %s", e)


async def healthz(request):
    return web.Response(text="ok")


async def run_server():
    app = web.Application()
    app.router.add_get("/healthz", healthz)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


async def backfill_unanalyzed():
    """Re-analyze PASS items that haven't been T4-analyzed yet."""
    cursor = db.raw_items.find(
        {"t2_band": "PASS", "t4_analyzed_at": None},
        {"_id": 1},
    )
    ids = [str(doc["_id"]) async for doc in cursor]
    if not ids:
        return
    log.info("Backfilling %d un-analyzed PASS items", len(ids))
    for item_id in ids:
        try:
            await process_item(item_id, "PASS")
        except Exception as e:
            log.error("Backfill error for %s: %s", item_id, e)
    log.info("Backfill complete")


async def main():
    await init_clients()
    await run_server()
    await backfill_unanalyzed()
    await listen_scored_items()


if __name__ == "__main__":
    def handle_signal(sig, frame):
        global running
        running = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    asyncio.run(main())
