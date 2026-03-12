"""Market Consciousness — Relevance Filter

Listens for raw_items on Redis pub/sub, embeds them via the LLM gateway,
scores against the problem-space vector, and bands items as PASS/WEAK/DROP.
"""
import asyncio
import json
import logging
import os
import signal

import httpx
import numpy as np
from aiohttp import web
from motor.motor_asyncio import AsyncIOMotorClient
import redis.asyncio as aioredis
from bson import ObjectId
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("relevance-filter")

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017/mc")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
LLM_GATEWAY_URL = os.environ.get("LLM_GATEWAY_URL", "http://localhost:8082")

T2_PASS_THRESHOLD = float(os.environ.get("T2_PASS_THRESHOLD", "0.72"))
T2_WEAK_THRESHOLD = float(os.environ.get("T2_WEAK_THRESHOLD", "0.45"))

db = None
rd = None
http_client = None
problem_vector = None
running = True


async def init_clients():
    global db, rd, http_client
    mongo = AsyncIOMotorClient(MONGO_URL)
    db_name = MONGO_URL.rsplit("/", 1)[-1].split("?")[0] or "mc"
    db = mongo[db_name]
    rd = aioredis.from_url(REDIS_URL, decode_responses=True)
    http_client = httpx.AsyncClient(timeout=30.0)
    log.info("Clients initialized")


async def get_embedding(text: str) -> list[float]:
    """Get an embedding from the LLM gateway."""
    resp = await http_client.post(
        f"{LLM_GATEWAY_URL}/v1/embed",
        json={"input": text},
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    a_arr = np.array(a)
    b_arr = np.array(b)
    dot = np.dot(a_arr, b_arr)
    norm = np.linalg.norm(a_arr) * np.linalg.norm(b_arr)
    if norm == 0:
        return 0.0
    return float(dot / norm)


async def load_problem_vector():
    """Load or initialize the problem-space vector."""
    global problem_vector
    ps = await db.problem_space.find_one(sort=[("version", -1)])
    if ps and ps.get("embedding"):
        problem_vector = ps["embedding"]
        log.info("Loaded problem-space vector (version %d)", ps.get("version", 1))
        return

    # Seed a default problem-space description
    description = (
        "The friction between writing code locally and running it in production, "
        "especially for multi-service and agent-based architectures. Developer tools "
        "that bridge local development and cloud deployment. Kubernetes, containers, "
        "CI/CD, local dev environments, and platform engineering."
    )
    embedding = await get_embedding(description)
    await db.problem_space.insert_one({
        "version": 1,
        "description": description,
        "embedding": embedding,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    })
    problem_vector = embedding
    log.info("Seeded problem-space vector")


async def score_item(item_id: str):
    """Embed an item, score it, band it, and publish if PASS."""
    item = await db.raw_items.find_one({"_id": ObjectId(item_id)})
    if not item:
        log.warning("Item not found: %s", item_id)
        return

    if item.get("t2_band"):
        return  # already scored

    text = f"{item.get('title', '')}\n\n{item.get('excerpt', '')}"
    if not text.strip():
        return

    try:
        embedding = await get_embedding(text)
    except Exception as e:
        log.warning("Embedding error for %s: %s", item_id, e)
        return

    score = cosine_similarity(embedding, problem_vector)

    if score > T2_PASS_THRESHOLD:
        band = "PASS"
    elif score > T2_WEAK_THRESHOLD:
        band = "WEAK"
    else:
        band = "DROP"

    await db.raw_items.update_one(
        {"_id": ObjectId(item_id)},
        {"$set": {
            "embedding": embedding,
            "t2_score": score,
            "t2_band": band,
            "processed_at": datetime.now(timezone.utc),
        }},
    )

    if band == "DROP":
        # Decrement source health
        if item.get("source_id"):
            await db.sources.update_one(
                {"_id": item["source_id"]},
                {"$inc": {"health_score": -0.005}},
            )

    if band in ("PASS", "WEAK"):
        await rd.publish("scored_items", json.dumps({
            "id": item_id,
            "band": band,
            "score": score,
        }))

    log.info("Scored %s: %.3f → %s", item_id, score, band)


async def listen_raw_items():
    """Subscribe to raw_items channel and score each."""
    pubsub = rd.pubsub()
    await pubsub.subscribe("raw_items")
    log.info("Listening for raw_items")

    async for msg in pubsub.listen():
        if not running:
            break
        if msg["type"] != "message":
            continue
        try:
            data = json.loads(msg["data"])
            await score_item(data["id"])
        except Exception as e:
            log.error("Score error: %s", e)


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
    log.info("Health check server on :%d", port)


async def main():
    await init_clients()
    await run_server()

    # Wait for the gateway to come up before loading the vector
    for attempt in range(30):
        try:
            resp = await http_client.get(f"{LLM_GATEWAY_URL}/healthz")
            if resp.status_code == 200:
                break
        except Exception:
            pass
        log.info("Waiting for LLM gateway...")
        await asyncio.sleep(5)

    await load_problem_vector()
    await listen_raw_items()


if __name__ == "__main__":
    def handle_signal(sig, frame):
        global running
        running = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    asyncio.run(main())
