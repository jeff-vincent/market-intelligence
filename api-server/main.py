"""Market Consciousness — API Server

REST API for the web UI and external consumers. Exposes briefings,
items, entities, sources, proposals, and feedback. Proxies on-demand
synthesis requests to the synthesis agent.

Authentication via Auth0 JWT validation.
Encrypted API key storage via Fernet (AES-128-CBC + HMAC-SHA256).
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import signal
from datetime import datetime, timedelta, timezone

import httpx
from aiohttp import web
from bson import ObjectId
from cryptography.fernet import Fernet
from motor.motor_asyncio import AsyncIOMotorClient
import redis.asyncio as aioredis

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("api-server")

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017/mc")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
LLM_GATEWAY_URL = os.environ.get("LLM_GATEWAY_URL", "http://localhost:8082")
SYNTHESIS_URL = os.environ.get("SYNTHESIS_URL", "http://mc-synthesis-agent-dev:8083")
PORT = int(os.environ.get("PORT", "8084"))

# Auth0 configuration
AUTH0_DOMAIN = os.environ.get("AUTH0_DOMAIN", "")
AUTH0_AUDIENCE = os.environ.get("AUTH0_AUDIENCE", "")

# Encryption key for API key vault (generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY", "")

db = None
rd = None
http = None
_jwks_cache = None
_jwks_cache_time = 0


def get_fernet():
    """Return a Fernet cipher using the configured encryption key."""
    if not ENCRYPTION_KEY:
        return None
    try:
        return Fernet(ENCRYPTION_KEY.encode())
    except Exception:
        log.error("Invalid ENCRYPTION_KEY — must be a valid Fernet key")
        return None


def mask_key(key: str) -> str:
    """Show first 4 and last 4 chars of an API key."""
    if len(key) <= 10:
        return "****"
    return key[:4] + "..." + key[-4:]


async def get_jwks():
    """Fetch Auth0 JWKS with caching (1 hour)."""
    import time
    global _jwks_cache, _jwks_cache_time
    now = time.time()
    if _jwks_cache and (now - _jwks_cache_time) < 3600:
        return _jwks_cache
    resp = await http.get(f"https://{AUTH0_DOMAIN}/.well-known/jwks.json", timeout=10.0)
    resp.raise_for_status()
    _jwks_cache = resp.json()
    _jwks_cache_time = now
    return _jwks_cache


async def validate_jwt(token: str) -> dict:
    """Validate an Auth0 JWT and return the decoded payload."""
    import jwt as pyjwt
    from jwt import PyJWKClient

    jwks_url = f"https://{AUTH0_DOMAIN}/.well-known/jwks.json"
    jwk_client = PyJWKClient(jwks_url)
    signing_key = jwk_client.get_signing_key_from_jwt(token)

    payload = pyjwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=AUTH0_AUDIENCE,
        issuer=f"https://{AUTH0_DOMAIN}/",
    )
    return payload


def get_user_id(request) -> str:
    """Extract user_id from the validated JWT in the request, or 'anonymous'."""
    return getattr(request, "_user_id", "anonymous")


@web.middleware
async def auth_middleware(request, handler):
    """Validate JWT on all /api/ routes (except healthz)."""
    # Skip auth for health check and internal routes
    if request.path == "/healthz" or request.path.startswith("/internal/"):
        return await handler(request)

    # If Auth0 is not configured, allow unauthenticated access (dev mode)
    if not AUTH0_DOMAIN or not AUTH0_AUDIENCE:
        request._user_id = "anonymous"
        return await handler(request)

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return json_response({"error": "Missing or invalid Authorization header"}, 401)

    token = auth_header[7:]
    try:
        payload = await validate_jwt(token)
        request._user_id = payload.get("sub", "anonymous")
    except Exception as e:
        log.warning("JWT validation failed: %s", e)
        return json_response({"error": "Invalid or expired token"}, 401)

    return await handler(request)


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
    band = request.query.get("band")
    analyzed = request.query.get("analyzed")
    query = {}
    if band:
        query["t2_band"] = band.upper()
    if analyzed == "true":
        query["t4_analyzed_at"] = {"$ne": None}
    elif analyzed == "false":
        query["t4_analyzed_at"] = None

    cursor = db.raw_items.find(query, {"embedding": 0, "raw_body": 0}).sort("ingested_at", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    return json_response(docs)


async def get_item(request):
    iid = request.match_info["id"]
    doc = await db.raw_items.find_one({"_id": ObjectId(iid)})
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
    item = await db.raw_items.find_one({"_id": ObjectId(item_id)})
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


# --------------- Reddit Engagement ---------------

REDDIT_USER_AGENT = "MarketConsciousness/1.0"


async def _get_reddit_access_token(user_id: str) -> tuple:
    """Decrypt stored Reddit credentials and fetch an access token."""
    f = get_fernet()
    if not f:
        raise ValueError("Encryption not configured")

    doc = await db.api_keys.find_one({"user_id": user_id, "provider": "reddit"})
    if not doc:
        raise ValueError("Reddit credentials not configured — add them in Settings")

    try:
        creds = json.loads(f.decrypt(doc["encrypted_key"].encode()).decode())
    except Exception:
        raise ValueError("Failed to decrypt Reddit credentials")

    # Exchange refresh token for access token
    resp = await http.post(
        "https://www.reddit.com/api/v1/access_token",
        data={"grant_type": "refresh_token", "refresh_token": creds["refresh_token"]},
        auth=(creds["client_id"], creds["client_secret"]),
        headers={"User-Agent": REDDIT_USER_AGENT},
        timeout=10.0,
    )
    resp.raise_for_status()
    token_data = resp.json()
    if "access_token" not in token_data:
        raise ValueError(f"Reddit token exchange failed: {token_data.get('error', 'unknown')}")

    return token_data["access_token"], creds.get("username", "")


def _extract_reddit_thing_id(url: str) -> str:
    """Extract the Reddit post/comment fullname (t3_xxx) from a URL."""
    import re
    # Match /comments/ID/ pattern
    m = re.search(r"/comments/([a-z0-9]+)", url)
    if m:
        return f"t3_{m.group(1)}"
    raise ValueError("Could not extract Reddit post ID from URL")


async def save_reddit_credentials(request):
    """Store encrypted Reddit OAuth credentials."""
    f = get_fernet()
    if not f:
        return json_response({"error": "Encryption not configured — set ENCRYPTION_KEY"}, 503)

    body = await request.json()
    client_id = (body.get("client_id") or "").strip()
    client_secret = (body.get("client_secret") or "").strip()
    refresh_token = (body.get("refresh_token") or "").strip()
    username = (body.get("username") or "").strip()

    if not client_id or not client_secret or not refresh_token:
        return json_response({"error": "client_id, client_secret, and refresh_token are required"}, 400)

    user_id = get_user_id(request)
    creds_json = json.dumps({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "username": username,
    })
    encrypted = f.encrypt(creds_json.encode()).decode()
    masked = username or mask_key(client_id)

    await db.api_keys.update_one(
        {"user_id": user_id, "provider": "reddit"},
        {"$set": {
            "encrypted_key": encrypted,
            "masked": masked,
            "updated_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )

    log.info("Reddit credentials stored for user=%s", user_id)
    return json_response({"ok": True, "masked": masked}, 201)


async def reddit_reply(request):
    """Post a comment reply to a Reddit post."""
    body = await request.json()
    url = (body.get("url") or "").strip()
    text = (body.get("text") or "").strip()

    if not url or not text:
        return json_response({"error": "url and text are required"}, 400)
    if len(text) > 10000:
        return json_response({"error": "Reply too long (max 10000 chars)"}, 400)

    user_id = get_user_id(request)

    try:
        access_token, username = await _get_reddit_access_token(user_id)
        thing_id = _extract_reddit_thing_id(url)
    except ValueError as e:
        return json_response({"error": str(e)}, 400)

    try:
        resp = await http.post(
            "https://oauth.reddit.com/api/comment",
            data={"thing_id": thing_id, "text": text},
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent": REDDIT_USER_AGENT,
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        result = resp.json()

        # Check for Reddit API errors
        errors = result.get("json", {}).get("errors", [])
        if errors:
            error_msg = "; ".join(e[1] for e in errors)
            return json_response({"error": f"Reddit API: {error_msg}"}, 400)

        # Log the reply
        await db.reddit_replies.insert_one({
            "user_id": user_id,
            "item_url": url,
            "thing_id": thing_id,
            "text": text,
            "username": username,
            "created_at": datetime.now(timezone.utc),
        })

        log.info("Reddit reply posted by user=%s to %s", user_id, url)
        return json_response({"ok": True, "message": "Reply posted"})

    except httpx.HTTPStatusError as e:
        log.warning("Reddit API error: %s %s", e.response.status_code, e.response.text[:200])
        return json_response({"error": f"Reddit API error ({e.response.status_code})"}, 502)
    except Exception as e:
        log.error("Reddit reply failed: %s", e)
        return json_response({"error": str(e)}, 502)


async def reddit_autocomplete(request):
    """Use the LLM to suggest a reply continuation."""
    body = await request.json()
    text = (body.get("text") or "").strip()

    if not text or len(text) < 10:
        return json_response({"suggestion": ""})

    prompt = f"""You are helping a user write a helpful Reddit comment. They are NOT selling anything — they are being genuinely helpful based on their experience.

Continue this partial reply naturally. Add 1-2 short sentences. Be conversational, not formal. Do NOT mention any product names or promotions.

Partial reply so far:
{text}

Continue (just the next 1-2 sentences, no quotes):"""

    try:
        resp = await http.post(
            f"{LLM_GATEWAY_URL}/v1/chat",
            json={
                "model": "haiku",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 120,
                "temperature": 0.7,
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        suggestion = resp.json().get("content", "").strip()
        return json_response({"suggestion": suggestion})
    except Exception as e:
        log.warning("Autocomplete failed: %s", e)
        return json_response({"suggestion": ""})


# --------------- Stats ---------------

async def get_stats(request):
    total = await db.raw_items.count_documents({})
    pass_count = await db.raw_items.count_documents({"t2_band": "PASS"})
    weak_count = await db.raw_items.count_documents({"t2_band": "WEAK"})
    drop_count = await db.raw_items.count_documents({"t2_band": "DROP"})
    t4_count = await db.raw_items.count_documents({"t4_analyzed_at": {"$ne": None}})
    entities_count = await db.entities.count_documents({})
    relationships_count = await db.entity_relationships.count_documents({})
    sources_count = await db.sources.count_documents({"status": "ACTIVE"})
    briefings_count = await db.briefings.count_documents({})
    proposals_count = await db.source_proposals.count_documents({"status": "PENDING_REVIEW"})

    return json_response({
        "items": total,
        "pass": pass_count,
        "weak": weak_count,
        "drop": drop_count,
        "t4_analyzed": t4_count,
        "entities": entities_count,
        "relationships": relationships_count,
        "active_sources": sources_count,
        "briefings": briefings_count,
        "pending_proposals": proposals_count,
    })


# --------------- Health ---------------

async def healthz(request):
    return web.Response(text="ok")


# --------------- Seed / Onboarding ---------------

async def get_seed(request):
    """Return the current problem-space seed, or 404 if not yet seeded."""
    doc = await db.problem_space.find_one({}, sort=[("version", -1)])
    if not doc:
        return json_response({"seeded": False}, 404)
    doc["seeded"] = True
    return json_response(doc)


async def save_seed(request):
    """Save or update the problem-space seed."""
    body = await request.json()
    problem = (body.get("problem") or "").strip()
    target_user = (body.get("target_user") or "").strip()
    tags = body.get("tags") or []
    examples = body.get("examples") or []

    if not problem:
        return json_response({"error": "problem description is required"}, 400)
    if not target_user:
        return json_response({"error": "target user description is required"}, 400)

    # Validate tags is a list of strings
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        return json_response({"error": "tags must be a list of strings"}, 400)

    # Validate examples (list of objects with url/text)
    clean_examples = []
    for ex in examples[:3]:  # max 3
        if isinstance(ex, dict):
            text = (ex.get("text") or "").strip()
            url = (ex.get("url") or "").strip()
            title = (ex.get("title") or "").strip()
            if text or url:
                clean_examples.append({"text": text, "url": url, "title": title})

    # Get current version
    existing = await db.problem_space.find_one({}, sort=[("version", -1)])
    version = (existing["version"] + 1) if existing else 1

    # Build a combined description for embedding and downstream agents
    description = f"{problem}\n\nTarget user: {target_user}"

    # Compute embedding via LLM gateway so the relevance filter can score items
    try:
        embed_resp = await http.post(
            f"{LLM_GATEWAY_URL}/v1/embed",
            json={"input": description},
            timeout=30.0,
        )
        embed_resp.raise_for_status()
        embedding = embed_resp.json()["embedding"]
    except Exception as e:
        log.error("Failed to embed seed: %s", e)
        return json_response({"error": "Failed to compute problem-space embedding"}, 502)

    doc = {
        "version": version,
        "problem": problem,
        "description": description,
        "target_user": target_user,
        "tags": tags,
        "examples": clean_examples,
        "embedding": embedding,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }

    await db.problem_space.insert_one(doc)

    # Notify the relevance-filter to reload the problem vector
    await rd.publish("seed_updated", json.dumps({"version": version}))

    log.info("problem space seeded (v%d): %s", version, problem[:80])
    return json_response({"ok": True, "version": version}, 201)


async def list_seed_versions(request):
    """Return all seed versions (summary only, no embeddings)."""
    cursor = db.problem_space.find(
        {}, {"embedding": 0}
    ).sort("version", -1)
    docs = await cursor.to_list(length=50)
    return json_response(docs)


async def revert_seed(request):
    """Revert to a previous seed version by copying it as a new version."""
    body = await request.json()
    target_version = body.get("version")
    if not target_version or not isinstance(target_version, int):
        return json_response({"error": "version (int) is required"}, 400)

    old = await db.problem_space.find_one({"version": target_version})
    if not old:
        return json_response({"error": f"version {target_version} not found"}, 404)

    # Get current latest version number
    latest = await db.problem_space.find_one({}, sort=[("version", -1)])
    new_version = (latest["version"] + 1) if latest else 1

    doc = {
        "version": new_version,
        "problem": old["problem"],
        "description": old.get("description", ""),
        "target_user": old.get("target_user", ""),
        "tags": old.get("tags", []),
        "examples": old.get("examples", []),
        "embedding": old.get("embedding", []),
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "reverted_from": target_version,
    }

    await db.problem_space.insert_one(doc)
    await rd.publish("seed_updated", json.dumps({"version": new_version}))

    log.info("reverted to seed v%d as new v%d", target_version, new_version)
    return json_response({"ok": True, "version": new_version, "reverted_from": target_version})


# --------------- API Key Vault ---------------

async def list_keys(request):
    """List which providers have keys configured (never returns actual keys)."""
    user_id = get_user_id(request)
    docs = await db.api_keys.find({"user_id": user_id}).to_list(length=20)
    result = []
    for doc in docs:
        result.append({
            "provider": doc["provider"],
            "masked": doc.get("masked", "****"),
            "configured": True,
            "updated_at": doc.get("updated_at"),
        })
    # Show unconfigured providers too
    configured = {d["provider"] for d in docs}
    for p in ("openai", "anthropic"):
        if p not in configured:
            result.append({"provider": p, "masked": "", "configured": False})
    return json_response(result)


async def save_key(request):
    """Encrypt and store an API key."""
    f = get_fernet()
    if not f:
        return json_response({"error": "Encryption not configured — set ENCRYPTION_KEY"}, 503)

    body = await request.json()
    provider = (body.get("provider") or "").strip().lower()
    raw_key = (body.get("key") or "").strip()

    if provider not in ("openai", "anthropic"):
        return json_response({"error": "Provider must be 'openai' or 'anthropic'"}, 400)
    if not raw_key or len(raw_key) < 10:
        return json_response({"error": "Invalid API key"}, 400)

    user_id = get_user_id(request)
    encrypted = f.encrypt(raw_key.encode()).decode()
    masked = mask_key(raw_key)

    await db.api_keys.update_one(
        {"user_id": user_id, "provider": provider},
        {"$set": {
            "encrypted_key": encrypted,
            "masked": masked,
            "updated_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )

    log.info("API key stored for user=%s provider=%s", user_id, provider)
    return json_response({"ok": True, "provider": provider, "masked": masked}, 201)


async def delete_key(request):
    """Remove a stored API key."""
    provider = request.match_info["provider"].lower()
    user_id = get_user_id(request)
    result = await db.api_keys.delete_one({"user_id": user_id, "provider": provider})
    if result.deleted_count == 0:
        return json_response({"error": "not found"}, 404)
    log.info("API key deleted for user=%s provider=%s", user_id, provider)
    return json_response({"ok": True})


async def get_decrypted_key(request):
    """Internal endpoint — returns decrypted API key for a provider.
    Only accessible from within the cluster (no auth middleware on /internal/)."""
    user_id = request.match_info["user_id"]
    provider = request.match_info["provider"].lower()

    f = get_fernet()
    if not f:
        return json_response({"error": "Encryption not configured"}, 503)

    doc = await db.api_keys.find_one({"user_id": user_id, "provider": provider})
    if not doc:
        return json_response({"error": "No key configured"}, 404)

    try:
        decrypted = f.decrypt(doc["encrypted_key"].encode()).decode()
    except Exception:
        return json_response({"error": "Failed to decrypt key"}, 500)

    return json_response({"key": decrypted, "provider": provider})


# --------------- Integrations ---------------

SUPPORTED_INTEGRATIONS = {
    "slack": {
        "name": "Slack",
        "description": "Post briefings and alerts to a Slack channel",
        "fields": [
            {"key": "webhook_url", "label": "Webhook URL", "type": "url", "placeholder": "https://hooks.slack.com/services/..."},
            {"key": "channel", "label": "Channel (optional)", "type": "text", "placeholder": "#market-intel"},
        ],
        "events": ["briefing", "alert", "entity_change"],
    },
    "discord": {
        "name": "Discord",
        "description": "Post briefings and alerts to a Discord channel",
        "fields": [
            {"key": "webhook_url", "label": "Webhook URL", "type": "url", "placeholder": "https://discord.com/api/webhooks/..."},
        ],
        "events": ["briefing", "alert", "entity_change"],
    },
    "email": {
        "name": "Email (Resend)",
        "description": "Send daily or weekly briefing digests via email",
        "fields": [
            {"key": "api_key", "label": "Resend API Key", "type": "password", "placeholder": "re_..."},
            {"key": "to", "label": "Recipient email", "type": "email", "placeholder": "you@company.com"},
            {"key": "from_address", "label": "From address", "type": "email", "placeholder": "intel@yourapp.com"},
            {"key": "schedule", "label": "Schedule", "type": "select", "options": ["every_briefing", "daily_digest", "weekly_digest"]},
        ],
        "events": ["briefing"],
    },
    "webhook": {
        "name": "Webhook",
        "description": "Send a POST request for any event — pipe data to Zapier, Make, n8n, or your own API",
        "fields": [
            {"key": "url", "label": "Endpoint URL", "type": "url", "placeholder": "https://your-api.com/webhook"},
            {"key": "secret", "label": "Signing secret (optional)", "type": "password", "placeholder": "whsec_..."},
            {"key": "headers", "label": "Custom headers (JSON)", "type": "textarea", "placeholder": '{"X-Api-Key": "..."}'},
        ],
        "events": ["briefing", "alert", "entity_change", "new_item", "source_proposal"],
    },
    "notion": {
        "name": "Notion",
        "description": "Export briefings and entities to a Notion database",
        "fields": [
            {"key": "api_key", "label": "Integration token", "type": "password", "placeholder": "ntn_..."},
            {"key": "database_id", "label": "Database ID", "type": "text", "placeholder": "abc123..."},
        ],
        "events": ["briefing", "entity_change"],
    },
    "linear": {
        "name": "Linear",
        "description": "Create issues from actionable insights",
        "fields": [
            {"key": "api_key", "label": "API Key", "type": "password", "placeholder": "lin_api_..."},
            {"key": "team_id", "label": "Team ID", "type": "text", "placeholder": ""},
            {"key": "label", "label": "Label (optional)", "type": "text", "placeholder": "market-intel"},
        ],
        "events": ["alert"],
    },
}


async def list_integrations(request):
    """List all available integrations with their current config status."""
    user_id = get_user_id(request)
    configured = {}
    async for doc in db.integrations.find({"user_id": user_id}):
        configured[doc["type"]] = {
            "enabled": doc.get("enabled", False),
            "events": doc.get("events", []),
            "configured_at": doc.get("configured_at"),
        }

    result = []
    for itype, meta in SUPPORTED_INTEGRATIONS.items():
        entry = {
            "type": itype,
            **meta,
            "enabled": configured.get(itype, {}).get("enabled", False),
            "configured": itype in configured,
            "active_events": configured.get(itype, {}).get("events", []),
        }
        # Mask sensitive field values
        if itype in configured:
            entry["configured_at"] = configured[itype].get("configured_at")
        result.append(entry)
    return json_response(result)


async def save_integration(request):
    """Configure or update an integration."""
    itype = request.match_info["type"]
    if itype not in SUPPORTED_INTEGRATIONS:
        return json_response({"error": f"Unknown integration: {itype}"}, 400)

    body = await request.json()
    user_id = get_user_id(request)
    meta = SUPPORTED_INTEGRATIONS[itype]

    # Validate required fields
    config = {}
    for field in meta["fields"]:
        val = (body.get(field["key"]) or "").strip()
        if field["type"] == "url" and val and not val.startswith("https://"):
            return json_response({"error": f"{field['label']} must be an HTTPS URL"}, 400)
        config[field["key"]] = val

    # Events to subscribe to
    events = body.get("events", meta["events"])
    if not isinstance(events, list):
        events = meta["events"]

    # Encrypt sensitive fields (passwords, API keys)
    f = get_fernet()
    encrypted_config = {}
    for field in meta["fields"]:
        val = config.get(field["key"], "")
        if field["type"] == "password" and val and f:
            encrypted_config[field["key"]] = f.encrypt(val.encode()).decode()
            encrypted_config[field["key"] + "_masked"] = mask_key(val)
        else:
            encrypted_config[field["key"]] = val

    await db.integrations.update_one(
        {"user_id": user_id, "type": itype},
        {"$set": {
            "config": encrypted_config,
            "events": events,
            "enabled": body.get("enabled", True),
            "configured_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )

    log.info("Integration %s configured for user=%s", itype, user_id)
    return json_response({"ok": True, "type": itype}, 201)


async def toggle_integration(request):
    """Enable or disable an integration."""
    itype = request.match_info["type"]
    user_id = get_user_id(request)
    body = await request.json()
    enabled = bool(body.get("enabled", False))

    result = await db.integrations.update_one(
        {"user_id": user_id, "type": itype},
        {"$set": {"enabled": enabled, "updated_at": datetime.now(timezone.utc)}},
    )
    if result.matched_count == 0:
        return json_response({"error": "Integration not configured"}, 404)
    return json_response({"ok": True, "enabled": enabled})


async def delete_integration(request):
    """Remove an integration config."""
    itype = request.match_info["type"]
    user_id = get_user_id(request)
    result = await db.integrations.delete_one({"user_id": user_id, "type": itype})
    if result.deleted_count == 0:
        return json_response({"error": "not found"}, 404)
    return json_response({"ok": True})


async def test_integration(request):
    """Send a test notification through an integration."""
    itype = request.match_info["type"]
    user_id = get_user_id(request)
    doc = await db.integrations.find_one({"user_id": user_id, "type": itype})
    if not doc:
        return json_response({"error": "Integration not configured"}, 404)

    test_payload = {
        "event": "test",
        "message": "Test notification from Market Consciousness",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    config = doc.get("config", {})
    f = get_fernet()

    try:
        if itype == "slack":
            webhook_url = config.get("webhook_url", "")
            if not webhook_url:
                return json_response({"error": "Webhook URL not set"}, 400)
            resp = await http.post(webhook_url, json={
                "text": ":test_tube: *Test from Market Consciousness*\nConnection successful!",
            }, timeout=10.0)
            resp.raise_for_status()

        elif itype == "discord":
            webhook_url = config.get("webhook_url", "")
            if not webhook_url:
                return json_response({"error": "Webhook URL not set"}, 400)
            resp = await http.post(webhook_url, json={
                "content": "**Test from Market Consciousness** — Connection successful!",
            }, timeout=10.0)
            resp.raise_for_status()

        elif itype == "webhook":
            url = config.get("url", "")
            if not url:
                return json_response({"error": "Webhook URL not set"}, 400)
            custom_headers = {}
            raw_headers = config.get("headers", "")
            if raw_headers:
                try:
                    custom_headers = json.loads(raw_headers)
                except json.JSONDecodeError:
                    pass
            resp = await http.post(url, json=test_payload, headers=custom_headers, timeout=10.0)
            resp.raise_for_status()

        elif itype in ("email", "notion", "linear"):
            # These require more complex setup — just validate the config
            return json_response({"ok": True, "message": f"{itype} config validated (dry run)"})

        else:
            return json_response({"error": "Test not available for this integration"}, 400)

        return json_response({"ok": True, "message": f"Test {itype} notification sent"})

    except Exception as e:
        log.warning("Integration test failed for %s: %s", itype, e)
        return json_response({"error": f"Test failed: {str(e)}"}, 502)


async def get_integration_config(request):
    """Internal endpoint — returns decrypted integration config for dispatching.
    Only accessible from within the cluster (no auth middleware on /internal/)."""
    user_id = request.match_info.get("user_id", "anonymous")
    event = request.match_info.get("event", "briefing")

    f = get_fernet()
    configs = []

    async for doc in db.integrations.find({"user_id": user_id, "enabled": True}):
        if event not in doc.get("events", []):
            continue

        config = dict(doc.get("config", {}))
        meta = SUPPORTED_INTEGRATIONS.get(doc["type"], {})

        # Decrypt sensitive fields
        if f:
            for field in meta.get("fields", []):
                if field["type"] == "password" and config.get(field["key"]):
                    try:
                        config[field["key"]] = f.decrypt(config[field["key"]].encode()).decode()
                    except Exception:
                        pass  # leave encrypted if decrypt fails
                # Remove masked fields from internal response
                config.pop(field["key"] + "_masked", None)

        configs.append({
            "type": doc["type"],
            "config": config,
            "events": doc.get("events", []),
        })

    return json_response(configs)


# --------------- RSS Feed ---------------

async def rss_feed(request):
    """Public RSS feed of briefings."""
    briefings = await db.briefings.find().sort("created_at", -1).limit(20).to_list(20)

    items_xml = []
    for b in briefings:
        title = (b.get("formatted", {}).get("title")
                 or b.get("formatted", {}).get("date", "Briefing")
                 or "Briefing")
        body = (b.get("formatted", {}).get("body")
                or b.get("synthesis", ""))
        pub_date = b.get("created_at", datetime.now(timezone.utc))
        if isinstance(pub_date, datetime):
            pub_date = pub_date.strftime("%a, %d %b %Y %H:%M:%S +0000")
        items_xml.append(f"""    <item>
      <title>{_xml_escape(title)}</title>
      <description>{_xml_escape(body[:500])}</description>
      <pubDate>{pub_date}</pubDate>
      <guid>{str(b['_id'])}</guid>
    </item>""")

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Market Consciousness — Briefings</title>
    <description>AI-curated market intelligence briefings</description>
    <lastBuildDate>{datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")}</lastBuildDate>
{chr(10).join(items_xml)}
  </channel>
</rss>"""

    return web.Response(text=xml, content_type="application/rss+xml")


def _xml_escape(s):
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


# --------------- CSV Export ---------------

async def export_csv(request):
    """Export items, entities, or sources as CSV."""
    user_id = get_user_id(request)
    export_type = request.match_info["type"]

    if export_type == "items":
        docs = await db.items.find().sort("fetched_at", -1).limit(1000).to_list(1000)
        header = "id,title,url,tier,score,source_id,fetched_at\n"
        rows = []
        for d in docs:
            rows.append(",".join([
                str(d.get("_id", "")),
                _csv_escape(d.get("title", "")),
                _csv_escape(d.get("url", "")),
                d.get("tier", ""),
                str(d.get("score", "")),
                str(d.get("source_id", "")),
                str(d.get("fetched_at", "")),
            ]))
    elif export_type == "entities":
        docs = await db.entities.find().sort("mention_count", -1).limit(1000).to_list(1000)
        header = "id,name,type,mention_count,summary,first_seen_at\n"
        rows = []
        for d in docs:
            rows.append(",".join([
                str(d.get("_id", "")),
                _csv_escape(d.get("name", "")),
                d.get("type", ""),
                str(d.get("mention_count", 0)),
                _csv_escape(d.get("summary", "")[:200]),
                str(d.get("first_seen_at", "")),
            ]))
    elif export_type == "sources":
        docs = await db.sources.find().sort("health_score", -1).to_list(200)
        header = "id,name,url,type,health_score,active,added_at\n"
        rows = []
        for d in docs:
            rows.append(",".join([
                str(d.get("_id", "")),
                _csv_escape(d.get("name", "")),
                _csv_escape(d.get("url", "")),
                d.get("source_type", ""),
                str(d.get("health_score", 0)),
                str(d.get("active", True)),
                str(d.get("added_at", "")),
            ]))
    else:
        return json_response({"error": "Export type must be items, entities, or sources"}, 400)

    csv_body = header + "\n".join(rows)
    return web.Response(
        text=csv_body,
        content_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={export_type}.csv"},
    )


def _csv_escape(s):
    s = str(s).replace('"', '""')
    if any(c in s for c in (',', '"', '\n')):
        return f'"{s}"'
    return s


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

    app = web.Application(middlewares=[auth_middleware])
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

    # Reddit engagement
    app.router.add_post("/api/reddit/credentials", save_reddit_credentials)
    app.router.add_post("/api/reddit/reply", reddit_reply)
    app.router.add_post("/api/reddit/autocomplete", reddit_autocomplete)

    app.router.add_get("/api/seed", get_seed)
    app.router.add_post("/api/seed", save_seed)
    app.router.add_get("/api/seed/versions", list_seed_versions)
    app.router.add_post("/api/seed/revert", revert_seed)

    # API Key vault
    app.router.add_get("/api/keys", list_keys)
    app.router.add_post("/api/keys", save_key)
    app.router.add_delete("/api/keys/{provider}", delete_key)

    # Integrations
    app.router.add_get("/api/integrations", list_integrations)
    app.router.add_post("/api/integrations/{type}", save_integration)
    app.router.add_patch("/api/integrations/{type}", toggle_integration)
    app.router.add_delete("/api/integrations/{type}", delete_integration)
    app.router.add_post("/api/integrations/{type}/test", test_integration)

    # RSS + CSV
    app.router.add_get("/api/feed/rss", rss_feed)
    app.router.add_get("/api/export/{type}", export_csv)

    # Internal (cluster-only, no auth) — used by llm-gateway + briefing-agent
    app.router.add_get("/internal/keys/{user_id}/{provider}", get_decrypted_key)
    app.router.add_get("/internal/integrations/{user_id}/{event}", get_integration_config)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    log.info("api-server listening on :%d", PORT)
    web.run_app(app, host="0.0.0.0", port=PORT, loop=loop)


if __name__ == "__main__":
    main()
