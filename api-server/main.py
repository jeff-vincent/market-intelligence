"""Market Consciousness — API Server

REST API for the web UI and external consumers. Exposes briefings,
items, entities, sources, proposals, and feedback. Proxies on-demand
synthesis requests to the synthesis agent.
"""
import asyncio
import json
import logging
import os
import signal
from datetime import datetime, timedelta, timezone

import httpx
from aiohttp import web
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
import redis.asyncio as aioredis

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("api-server")

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017/mc")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
LLM_GATEWAY_URL = os.environ.get("LLM_GATEWAY_URL", "http://localhost:8082")
SYNTHESIS_URL = os.environ.get("SYNTHESIS_URL", "http://mc-synthesis-agent-dev:8083")
PORT = int(os.environ.get("PORT", "8084"))

db = None
rd = None
http = None


def json_serial(obj):
    """JSON serialiser for MongoDB types."""
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")


def json_response(data, status=200):
    return web.Response(
        text=json.dumps(data, default=json_serial),
        content_type="application/json",
        status=status,
    )


# --------------- Briefings ---------------

async def list_briefings(request):
    limit = int(request.query.get("limit", "20"))
    cursor = db.briefings.find().sort("created_at", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    return json_response(docs)


async def get_briefing(request):
    bid = request.match_info["id"]
    doc = await db.briefings.find_one({"_id": ObjectId(bid)})
    if not doc:
        return json_response({"error": "not found"}, 404)
    return json_response(doc)


# --------------- Items ---------------

async def list_items(request):
    limit = int(request.query.get("limit", "50"))
    tier = request.query.get("tier")
    entity = request.query.get("entity")
    query = {}
    if tier:
        query["tier"] = tier.upper()
    if entity:
        query["entities.name"] = {"$regex": entity, "$options": "i"}

    cursor = db.items.find(query).sort("fetched_at", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    return json_response(docs)


async def get_item(request):
    iid = request.match_info["id"]
    doc = await db.items.find_one({"_id": ObjectId(iid)})
    if not doc:
        return json_response({"error": "not found"}, 404)
    return json_response(doc)


# --------------- Entities ---------------

async def list_entities(request):
    limit = int(request.query.get("limit", "100"))
    etype = request.query.get("type")
    query = {}
    if etype:
        query["type"] = etype

    cursor = db.entities.find(query).sort("mention_count", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    return json_response(docs)


async def get_entity(request):
    eid = request.match_info["id"]
    doc = await db.entities.find_one({"_id": ObjectId(eid)})
    if not doc:
        return json_response({"error": "not found"}, 404)

    depth = int(request.query.get("depth", "1"))
    rels = []
    if depth >= 1:
        cursor = db.relationships.find(
            {"$or": [{"from_entity": doc["name"]}, {"to_entity": doc["name"]}]}
        )
        rels = await cursor.to_list(length=200)

    return json_response({"entity": doc, "relationships": rels})


# --------------- Sources ---------------

async def list_sources(request):
    cursor = db.sources.find().sort("health_score", -1)
    docs = await cursor.to_list(length=200)
    return json_response(docs)


async def toggle_source(request):
    sid = request.match_info["id"]
    body = await request.json()
    active = body.get("active")
    if active is None:
        return json_response({"error": "active field required"}, 400)
    result = await db.sources.update_one(
        {"_id": ObjectId(sid)},
        {"$set": {"active": bool(active), "updated_at": datetime.now(timezone.utc)}}
    )
    if result.matched_count == 0:
        return json_response({"error": "not found"}, 404)
    return json_response({"ok": True})


# --------------- Source Proposals ---------------

async def list_proposals(request):
    status = request.query.get("status", "PENDING_REVIEW")
    cursor = db.source_proposals.find({"status": status}).sort("proposed_at", -1)
    docs = await cursor.to_list(length=100)
    return json_response(docs)


async def review_proposal(request):
    pid = request.match_info["id"]
    body = await request.json()
    action = body.get("action")  # approve or dismiss
    if action not in ("approve", "dismiss"):
        return json_response({"error": "action must be approve or dismiss"}, 400)

    if action == "approve":
        proposal = await db.source_proposals.find_one({"_id": ObjectId(pid)})
        if not proposal:
            return json_response({"error": "not found"}, 404)
        await db.sources.insert_one({
            "name": proposal["name"],
            "url": proposal["url"],
            "source_type": proposal.get("source_type", "rss"),
            "poll_interval_mins": proposal.get("poll_interval_mins", 60),
            "health_score": 1.0,
            "active": True,
            "added_at": datetime.now(timezone.utc),
        })
        await db.source_proposals.update_one(
            {"_id": ObjectId(pid)},
            {"$set": {"status": "APPROVED", "reviewed_at": datetime.now(timezone.utc)}}
        )
    else:
        result = await db.source_proposals.update_one(
            {"_id": ObjectId(pid)},
            {"$set": {"status": "DISMISSED", "reviewed_at": datetime.now(timezone.utc)}}
        )
        if result.matched_count == 0:
            return json_response({"error": "not found"}, 404)

    return json_response({"ok": True})


# --------------- Feedback ---------------

async def submit_feedback(request):
    body = await request.json()
    item_id = body.get("item_id")
    rating = body.get("rating")  # useful, noise, interesting
    if not item_id or rating not in ("useful", "noise", "interesting"):
        return json_response({"error": "item_id and rating (useful|noise|interesting) required"}, 400)

    await db.feedback.insert_one({
        "item_id": ObjectId(item_id),
        "rating": rating,
        "created_at": datetime.now(timezone.utc),
    })

    # Propagate to source health
    item = await db.items.find_one({"_id": ObjectId(item_id)})
    if item and item.get("source_id"):
        delta = {"useful": 0.02, "interesting": 0.01, "noise": -0.05}.get(rating, 0)
        await db.sources.update_one(
            {"_id": item["source_id"]},
            {"$inc": {"health_score": delta}}
        )

    return json_response({"ok": True})


# --------------- On-demand Synthesis ---------------

async def synthesise(request):
    body = await request.json()
    query = body.get("query", "")
    try:
        resp = await http.post(
            f"{SYNTHESIS_URL}/synthesise",
            json={"query": query},
            timeout=60.0,
        )
        data = resp.json()
        return json_response(data)
    except Exception as e:
        log.error("synthesis proxy error: %s", e)
        return json_response({"error": str(e)}, 502)


# --------------- Stats ---------------

async def get_stats(request):
    items_count = await db.items.count_documents({})
    entities_count = await db.entities.count_documents({})
    sources_count = await db.sources.count_documents({"active": True})
    briefings_count = await db.briefings.count_documents({})
    t4_today = await db.items.count_documents({
        "tier": "T4",
        "fetched_at": {"$gte": datetime.now(timezone.utc) - timedelta(days=1)}
    })

    # LLM costs from gateway
    costs = None
    try:
        resp = await http.get(f"{LLM_GATEWAY_URL}/v1/costs", timeout=5.0)
        costs = resp.json()
    except Exception:
        pass

    return json_response({
        "items": items_count,
        "entities": entities_count,
        "active_sources": sources_count,
        "briefings": briefings_count,
        "t4_items_24h": t4_today,
        "llm_costs": costs,
    })


# --------------- Health ---------------

async def healthz(request):
    return web.Response(text="ok")


# --------------- Lifecycle ---------------

async def init_clients():
    global db, rd, http
    client = AsyncIOMotorClient(MONGO_URL)
    db = client.get_default_database(default="mc")
    rd = aioredis.from_url(REDIS_URL, decode_responses=True)
    http = httpx.AsyncClient()
    log.info("clients connected")


async def cleanup(app):
    if http:
        await http.aclose()


def handle_signal():
    raise SystemExit(0)


def main():
    loop = asyncio.new_event_loop()
    loop.run_until_complete(init_clients())

    app = web.Application()
    app.on_cleanup.append(cleanup)

    # Routes
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/api/stats", get_stats)

    app.router.add_get("/api/briefings", list_briefings)
    app.router.add_get("/api/briefings/{id}", get_briefing)

    app.router.add_get("/api/items", list_items)
    app.router.add_get("/api/items/{id}", get_item)

    app.router.add_get("/api/entities", list_entities)
    app.router.add_get("/api/entities/{id}", get_entity)

    app.router.add_get("/api/sources", list_sources)
    app.router.add_patch("/api/sources/{id}", toggle_source)

    app.router.add_get("/api/proposals", list_proposals)
    app.router.add_post("/api/proposals/{id}/review", review_proposal)

    app.router.add_post("/api/feedback", submit_feedback)
    app.router.add_post("/api/synthesise", synthesise)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    log.info("api-server listening on :%d", PORT)
    web.run_app(app, host="0.0.0.0", port=PORT, loop=loop)


if __name__ == "__main__":
    main()
