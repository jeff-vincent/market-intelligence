"""Market Consciousness — LLM Gateway

Central proxy for all LLM and embedding calls. Handles:
- Model routing by tier (T3 → cheap, T4 → capable, embed → embedding model)
- Rate limiting per provider
- Provider fallback (OpenAI ↔ Anthropic)
- Cost tracking per tier/agent/day
- Budget enforcement

Reads API keys from:
1. Encrypted key vault (via api-server internal endpoint) — preferred
2. Environment variables (OPENAI_API_KEY, ANTHROPIC_API_KEY) — fallback
"""
import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from datetime import date, datetime, timezone

import httpx
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("llm-gateway")

# Fallback env vars (used when no vault keys are available)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MONTHLY_BUDGET = float(os.environ.get("MONTHLY_LLM_BUDGET_USD", "100"))

# API server internal endpoint for decrypting stored keys
API_SERVER_URL = os.environ.get("API_SERVER_URL", "http://mc-api-server-dev:8084")

# Cache vault keys in memory (TTL-based)
_vault_cache = {}  # {provider: {"key": str, "expires": float}}
VAULT_CACHE_TTL = 300  # 5 minutes

# Model routing by tier
TIER_MODELS = {
    "embed":      {"provider": "openai", "model": "text-embedding-3-small"},
    "t3":         {"provider": "anthropic", "model": "claude-3-5-haiku-20241022"},
    "t4":         {"provider": "anthropic", "model": "claude-sonnet-4-20250514"},
    "synthesis":  {"provider": "anthropic", "model": "claude-sonnet-4-20250514"},
    "discovery":  {"provider": "anthropic", "model": "claude-sonnet-4-20250514"},
}

# Approximate costs per 1K tokens (input/output)
COST_PER_1K = {
    "text-embedding-3-small": {"input": 0.00002, "output": 0},
    "claude-3-5-haiku-20241022": {"input": 0.001, "output": 0.005},
    "claude-sonnet-4-20250514": {"input": 0.003, "output": 0.015},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
}

# Cost ledger: {date_str: {tier: total_cost}}
cost_ledger = defaultdict(lambda: defaultdict(float))
# Rate tracking: {provider: [timestamps]}
rate_tracker = defaultdict(list)
RATE_LIMIT_RPM = {"openai": 500, "anthropic": 300}

openai_client = None
anthropic_client = None
_http_client = None


async def get_vault_key(provider: str) -> str:
    """Fetch a decrypted API key from the vault, with in-memory caching."""
    now = time.time()
    cached = _vault_cache.get(provider)
    if cached and cached["expires"] > now:
        return cached["key"]

    # Try fetching from api-server internal endpoint
    # For now, use "anonymous" as user_id (single-tenant); in multi-tenant, pass real user_id
    try:
        resp = await _http_client.get(
            f"{API_SERVER_URL}/internal/keys/anonymous/{provider}",
            timeout=5.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            key = data.get("key", "")
            if key:
                _vault_cache[provider] = {"key": key, "expires": now + VAULT_CACHE_TTL}
                return key
    except Exception as e:
        log.debug("Vault key fetch failed for %s: %s", provider, e)

    return ""


def init_providers():
    """Initialise providers from env vars. Vault keys are fetched lazily."""
    global openai_client, anthropic_client
    if OPENAI_API_KEY:
        import openai
        openai_client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
        log.info("OpenAI provider initialized (env)")
    if ANTHROPIC_API_KEY:
        import anthropic
        anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        log.info("Anthropic provider initialized (env)")

    if not openai_client and not anthropic_client:
        log.info("No env API keys — will check vault at request time")


async def ensure_provider(provider: str):
    """Lazily initialise a provider from vault if not already set."""
    global openai_client, anthropic_client

    if provider == "openai" and not openai_client:
        key = await get_vault_key("openai")
        if key:
            import openai
            openai_client = openai.AsyncOpenAI(api_key=key)
            log.info("OpenAI provider initialized (vault)")

    if provider == "anthropic" and not anthropic_client:
        key = await get_vault_key("anthropic")
        if key:
            import anthropic
            anthropic_client = anthropic.AsyncAnthropic(api_key=key)
            log.info("Anthropic provider initialized (vault)")


def check_rate_limit(provider: str) -> bool:
    now = time.time()
    window = [t for t in rate_tracker[provider] if now - t < 60]
    rate_tracker[provider] = window
    limit = RATE_LIMIT_RPM.get(provider, 300)
    return len(window) < limit


def record_rate(provider: str):
    rate_tracker[provider].append(time.time())


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = COST_PER_1K.get(model, {"input": 0.001, "output": 0.005})
    return (input_tokens / 1000 * rates["input"]) + (output_tokens / 1000 * rates["output"])


def record_cost(tier: str, cost: float):
    today = date.today().isoformat()
    cost_ledger[today][tier] += cost


def get_monthly_spend() -> float:
    today = date.today()
    total = 0.0
    for day_str, tiers in cost_ledger.items():
        try:
            d = date.fromisoformat(day_str)
            if d.month == today.month and d.year == today.year:
                total += sum(tiers.values())
        except ValueError:
            pass
    return total


async def handle_embed(request):
    """POST /v1/embed — generate an embedding."""
    body = await request.json()
    text = body.get("input", "")

    await ensure_provider("openai")

    if not openai_client:
        return web.json_response({"error": "OpenAI not configured"}, status=503)

    if not check_rate_limit("openai"):
        return web.json_response({"error": "Rate limited"}, status=429)

    monthly = get_monthly_spend()
    if monthly >= MONTHLY_BUDGET:
        return web.json_response({"error": "Monthly budget exceeded", "spend": monthly}, status=429)

    try:
        record_rate("openai")
        resp = await openai_client.embeddings.create(
            model="text-embedding-3-small",
            input=text,
        )
        embedding = resp.data[0].embedding
        tokens = resp.usage.total_tokens
        cost = estimate_cost("text-embedding-3-small", tokens, 0)
        record_cost("embed", cost)

        return web.json_response({"embedding": embedding, "tokens": tokens, "cost": cost})
    except Exception as e:
        log.error("Embed error: %s", e)
        return web.json_response({"error": str(e)}, status=500)


async def chat_anthropic(model: str, messages: list) -> dict:
    """Call Anthropic API."""
    system_msg = None
    user_msgs = []
    for m in messages:
        if m["role"] == "system":
            system_msg = m["content"]
        else:
            user_msgs.append(m)

    kwargs = {"model": model, "max_tokens": 4096, "messages": user_msgs}
    if system_msg:
        kwargs["system"] = system_msg

    resp = await anthropic_client.messages.create(**kwargs)
    content = resp.content[0].text
    input_tokens = resp.usage.input_tokens
    output_tokens = resp.usage.output_tokens
    return {"content": content, "input_tokens": input_tokens, "output_tokens": output_tokens}


async def chat_openai(model: str, messages: list) -> dict:
    """Call OpenAI API."""
    resp = await openai_client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=4096,
    )
    content = resp.choices[0].message.content
    input_tokens = resp.usage.prompt_tokens
    output_tokens = resp.usage.completion_tokens
    return {"content": content, "input_tokens": input_tokens, "output_tokens": output_tokens}


async def handle_chat(request):
    """POST /v1/chat — route a chat completion by tier."""
    body = await request.json()
    tier = body.get("tier", "t3")
    messages = body.get("messages", [])

    if not messages:
        return web.json_response({"error": "No messages"}, status=400)

    monthly = get_monthly_spend()
    if monthly >= MONTHLY_BUDGET:
        return web.json_response({"error": "Monthly budget exceeded", "spend": monthly}, status=429)

    routing = TIER_MODELS.get(tier, TIER_MODELS["t3"])
    provider = routing["provider"]
    model = routing["model"]

    # Ensure provider is initialised (from vault if needed)
    await ensure_provider(provider)
    # Also try to init the fallback provider
    fallback = "openai" if provider == "anthropic" else "anthropic"
    await ensure_provider(fallback)

    # Try primary provider, fall back to the other
    providers_to_try = []
    if provider == "anthropic" and anthropic_client:
        providers_to_try.append(("anthropic", model, chat_anthropic))
        if openai_client:
            providers_to_try.append(("openai", "gpt-4o-mini", chat_openai))
    elif provider == "openai" and openai_client:
        providers_to_try.append(("openai", model, chat_openai))
        if anthropic_client:
            providers_to_try.append(("anthropic", "claude-3-5-haiku-20241022", chat_anthropic))
    elif anthropic_client:
        providers_to_try.append(("anthropic", model, chat_anthropic))
    elif openai_client:
        providers_to_try.append(("openai", "gpt-4o-mini", chat_openai))

    if not providers_to_try:
        return web.json_response({"error": "No providers configured"}, status=503)

    for prov_name, mdl, fn in providers_to_try:
        if not check_rate_limit(prov_name):
            continue
        try:
            record_rate(prov_name)
            result = await fn(mdl, messages)
            cost = estimate_cost(mdl, result["input_tokens"], result["output_tokens"])
            record_cost(tier, cost)
            return web.json_response({
                "content": result["content"],
                "model": mdl,
                "provider": prov_name,
                "tier": tier,
                "cost": cost,
                "input_tokens": result["input_tokens"],
                "output_tokens": result["output_tokens"],
            })
        except Exception as e:
            log.warning("Provider %s/%s failed: %s", prov_name, mdl, e)
            continue

    return web.json_response({"error": "All providers failed"}, status=503)


async def handle_costs(request):
    """GET /v1/costs — return cost tracking data."""
    return web.json_response({
        "monthly_spend": get_monthly_spend(),
        "monthly_budget": MONTHLY_BUDGET,
        "by_day": {day: dict(tiers) for day, tiers in cost_ledger.items()},
    })


async def healthz(request):
    providers = []
    if openai_client:
        providers.append("openai")
    if anthropic_client:
        providers.append("anthropic")
    return web.json_response({"status": "ok", "providers": providers})


async def main():
    global _http_client
    _http_client = httpx.AsyncClient()
    init_providers()

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_post("/v1/embed", handle_embed)
    app.router.add_post("/v1/chat", handle_chat)
    app.router.add_get("/v1/costs", handle_costs)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8082)
    await site.start()
    log.info("LLM Gateway listening on :8082")

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
