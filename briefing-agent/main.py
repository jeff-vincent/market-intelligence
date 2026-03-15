"""Market Consciousness — Briefing Agent

Listens for new_briefing events, formats briefings with linked items
and entity changes, dispatches notifications to configured integrations,
and makes briefings available via the API.
"""
import asyncio
import json
import logging
import os
import signal
import hmac
import hashlib
from datetime import datetime, timedelta, timezone

from aiohttp import web
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
import redis.asyncio as aioredis
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("briefing-agent")

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017/mc")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
API_SERVER_URL = os.environ.get("API_SERVER_URL", "http://localhost:8084")

db = None
rd = None
_http_client = None
running = True


async def init_clients():
    global db, rd, _http_client
    mongo = AsyncIOMotorClient(MONGO_URL)
    db = mongo.get_default_database(default="mc")
    rd = aioredis.from_url(REDIS_URL, decode_responses=True)
    _http_client = httpx.AsyncClient(timeout=15.0)
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
        first_seen = e.get("first_seen_at", datetime.min)
        if first_seen.tzinfo is None:
            first_seen = first_seen.replace(tzinfo=timezone.utc)
        is_new = first_seen >= since
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

    # Dispatch notifications to enabled integrations
    await dispatch_notifications(briefing_id, formatted)


# --------------- Notification Dispatch ---------------

async def dispatch_notifications(briefing_id: str, formatted: dict):
    """Fetch enabled integrations and dispatch the briefing to each."""
    try:
        resp = await _http_client.get(
            f"{API_SERVER_URL}/internal/integrations/anonymous/briefing"
        )
        if resp.status_code != 200:
            log.warning("Failed to fetch integrations: %d", resp.status_code)
            return
        integrations = resp.json()
    except Exception as e:
        log.warning("Could not reach api-server for integrations: %s", e)
        return

    if not integrations:
        return

    title = formatted.get("date", "New Briefing")
    synthesis = formatted.get("synthesis", "")
    n_items = len(formatted.get("key_items", []))
    n_entities = len(formatted.get("entity_changes", []))
    summary = f"**{title}** — {n_items} key items, {n_entities} entity changes\n\n{synthesis[:500]}"

    for integration in integrations:
        itype = integration["type"]
        config = integration.get("config", {})
        try:
            if itype == "slack":
                await _dispatch_slack(config, summary, formatted)
            elif itype == "discord":
                await _dispatch_discord(config, summary, formatted)
            elif itype == "webhook":
                await _dispatch_webhook(config, briefing_id, formatted)
            elif itype == "email":
                await _dispatch_email(config, title, summary)
            else:
                log.debug("Dispatch not implemented for %s, skipping", itype)
        except Exception as e:
            log.error("Dispatch to %s failed: %s", itype, e)


async def _dispatch_slack(config: dict, summary: str, formatted: dict):
    webhook_url = config.get("webhook_url", "")
    if not webhook_url:
        return

    # Build a rich Slack message
    items_text = ""
    for item in formatted.get("key_items", [])[:5]:
        items_text += f"\n• <{item.get('url', '#')}|{item.get('title', 'Item')}> ({item.get('source', 'unknown')})"

    entity_text = ""
    for e in formatted.get("entity_changes", [])[:5]:
        emoji = ":new:" if e["change"] == "new" else ":arrows_counterclockwise:"
        entity_text += f"\n{emoji} *{e['entity']}* ({e['type']}) — {e['detail']}"

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f":briefcase: {formatted.get('date', 'Briefing')}", "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": formatted.get("synthesis", "")[:2000]}},
    ]
    if items_text:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Key Items*{items_text}"}})
    if entity_text:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Entity Changes*{entity_text}"}})

    await _http_client.post(webhook_url, json={"blocks": blocks, "text": summary[:200]})
    log.info("Dispatched briefing to Slack")


async def _dispatch_discord(config: dict, summary: str, formatted: dict):
    webhook_url = config.get("webhook_url", "")
    if not webhook_url:
        return

    # Discord embed
    embeds = [{
        "title": f"Briefing — {formatted.get('date', 'Today')}",
        "description": formatted.get("synthesis", "")[:2048],
        "color": 0x5865F2,
        "fields": [],
    }]

    items = formatted.get("key_items", [])[:5]
    if items:
        items_val = "\n".join(f"• [{i.get('title', 'Item')}]({i.get('url', '#')}) ({i.get('source', '')})" for i in items)
        embeds[0]["fields"].append({"name": "Key Items", "value": items_val[:1024]})

    entities = formatted.get("entity_changes", [])[:5]
    if entities:
        ent_val = "\n".join(f"{'🆕' if e['change'] == 'new' else '🔄'} **{e['entity']}** — {e['detail']}" for e in entities)
        embeds[0]["fields"].append({"name": "Entity Changes", "value": ent_val[:1024]})

    await _http_client.post(webhook_url, json={"embeds": embeds})
    log.info("Dispatched briefing to Discord")


async def _dispatch_webhook(config: dict, briefing_id: str, formatted: dict):
    url = config.get("url", "")
    if not url:
        return

    payload = json.dumps({
        "event": "briefing",
        "briefing_id": briefing_id,
        "data": formatted,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }, default=str)

    headers = {"Content-Type": "application/json"}

    # Parse custom headers
    raw_headers = config.get("headers", "")
    if raw_headers:
        try:
            custom = json.loads(raw_headers)
            headers.update(custom)
        except json.JSONDecodeError:
            pass

    # HMAC signature if signing secret is provided
    secret = config.get("secret", "")
    if secret:
        sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        headers["X-Signature-256"] = f"sha256={sig}"

    await _http_client.post(url, content=payload, headers=headers)
    log.info("Dispatched briefing to webhook %s", url)


async def _dispatch_email(config: dict, title: str, body: str):
    api_key = config.get("api_key", "")
    to = config.get("to", "")
    from_addr = config.get("from_address", "briefings@mc.local")
    if not api_key or not to:
        return

    resp = await _http_client.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "from": from_addr,
            "to": [to],
            "subject": f"Market Consciousness — {title}",
            "text": body,
        },
    )
    if resp.status_code < 300:
        log.info("Dispatched briefing email to %s", to)
    else:
        log.warning("Resend email failed: %d %s", resp.status_code, resp.text)


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
