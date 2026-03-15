"""Market Consciousness — MCP Server

Exposes the intelligence pipeline as MCP tools that any MCP-compatible
agent, IDE extension, or orchestration layer can call directly.

Tools:
  get_briefing       — latest or date-specific briefing
  query_signals      — on-demand synthesis against a question
  get_entities       — knowledge graph query
  get_sources        — source list with health scores
  ingest_content     — feed content into the pipeline
  get_pipeline_stats — pipeline health metrics
"""
import json
import logging
import os
from datetime import datetime

import httpx
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("mcp-server")

API_SERVER_URL = os.environ.get("API_SERVER_URL", "http://mc-api-server-dev:8084")
SYNTHESIS_URL = os.environ.get("SYNTHESIS_URL", "http://mc-synthesis-agent-dev:8083")
PORT = int(os.environ.get("PORT", "8090"))

mcp = FastMCP(
    "Market Consciousness",
    instructions=(
        "Market intelligence system that monitors a problem space and surfaces "
        "emerging signals, entities, and trends. Use these tools to query briefings, "
        "explore the knowledge graph, check pipeline health, and feed new content "
        "into the monitoring pipeline."
    ),
    host="0.0.0.0",
    port=PORT,
)

_http_client = None


def _client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=60.0)
    return _http_client


def _serialize(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok"})


@mcp.tool()
async def get_briefing(date: str | None = None, type: str = "daily") -> str:
    """Get the latest market intelligence briefing.

    Args:
        date: ISO date string (YYYY-MM-DD). Omit for latest.
        type: Briefing type — "daily" or "weekly".
    """
    params = {"limit": 1}
    resp = await _client().get(f"{API_SERVER_URL}/api/briefings", params=params)
    if resp.status_code != 200:
        return f"Error fetching briefings: {resp.status_code}"

    briefings = resp.json()
    if not briefings:
        return "No briefings available yet. The synthesis agent may not have run."

    if date:
        # Filter by date if provided
        for b in briefings:
            created = b.get("created_at", "")
            if created.startswith(date):
                return json.dumps(b, indent=2, default=_serialize)
        return f"No {type} briefing found for {date}."

    return json.dumps(briefings[0], indent=2, default=_serialize)


@mcp.tool()
async def query_signals(query: str, lookback_days: int = 7) -> str:
    """Ask a specific question about the current state of the market.

    Triggers a live synthesis run. The system will analyze recent signals
    and entity context to answer your question directly.

    Args:
        query: Your question, e.g. "What is the emerging sentiment around local-first developer tooling?"
        lookback_days: How many days of signals to consider (default 7).
    """
    payload = {"query": query, "lookback_days": lookback_days}
    resp = await _client().post(
        f"{API_SERVER_URL}/api/synthesise",
        json=payload,
    )
    if resp.status_code != 200:
        return f"Synthesis request failed: {resp.status_code} — {resp.text}"

    result = resp.json()
    narrative = result.get("narrative") or result.get("briefing", {}).get("narrative")
    if narrative:
        return narrative
    return json.dumps(result, indent=2, default=_serialize)


@mcp.tool()
async def get_entities(
    type: str | None = None,
    name: str | None = None,
    min_strength: float | None = None,
    limit: int = 20,
) -> str:
    """Query the competitive landscape knowledge graph.

    Returns entities (tools, companies, concepts, frameworks, platforms)
    extracted from monitored signals.

    Args:
        type: Filter by entity type — "tool", "company", "concept", "framework", "platform".
        name: Fuzzy match against entity name.
        min_strength: Filter to entities with strength above this threshold (0–1).
        limit: Maximum results to return (default 20).
    """
    params = {"limit": limit}
    if type:
        params["type"] = type

    resp = await _client().get(f"{API_SERVER_URL}/api/entities", params=params)
    if resp.status_code != 200:
        return f"Error fetching entities: {resp.status_code}"

    entities = resp.json()

    # Client-side filtering for name fuzzy match and strength
    if name:
        name_lower = name.lower()
        entities = [e for e in entities if name_lower in e.get("name", "").lower()]
    if min_strength is not None:
        entities = [e for e in entities if e.get("strength", 0) >= min_strength]

    entities = entities[:limit]

    if not entities:
        return "No entities match the query."

    # Format for readability
    lines = []
    for e in entities:
        line = f"• {e.get('name', '?')} ({e.get('type', '?')}) — strength: {e.get('strength', 0):.2f}, mentions: {e.get('mentions', 0)}"
        if e.get("summary"):
            line += f"\n  {e['summary'][:200]}"
        lines.append(line)
    return "\n".join(lines)


@mcp.tool()
async def get_sources(status: str | None = None) -> str:
    """List monitored sources with health scores and signal rates.

    Args:
        status: Filter by status — "ACTIVE", "SUSPENDED", or "PENDING_REVIEW".
    """
    resp = await _client().get(f"{API_SERVER_URL}/api/sources")
    if resp.status_code != 200:
        return f"Error fetching sources: {resp.status_code}"

    sources = resp.json()
    if status:
        sources = [s for s in sources if s.get("status") == status]

    if not sources:
        return f"No sources found{(' with status ' + status) if status else ''}."

    lines = []
    for s in sources:
        line = (
            f"• {s.get('name', s.get('url', '?'))} [{s.get('type', '?')}] "
            f"— health: {s.get('health_score', 0):.2f}, status: {s.get('status', '?')}"
        )
        if s.get("last_crawled_at"):
            line += f", last crawled: {s['last_crawled_at']}"
        lines.append(line)
    return "\n".join(lines)


@mcp.tool()
async def ingest_content(
    url: str | None = None,
    text: str | None = None,
    source_label: str | None = None,
) -> str:
    """Feed a URL or text content directly into the intelligence pipeline.

    Useful when you encounter relevant material that should be processed
    by the monitoring system.

    Args:
        url: URL to fetch and process.
        text: Raw text content to process directly.
        source_label: Optional attribution label (e.g. "private-slack-export").
    """
    if not url and not text:
        return "Provide either a url or text to ingest."

    payload = {}
    if url:
        payload["url"] = url
    if text:
        payload["text"] = text
    if source_label:
        payload["source_label"] = source_label

    resp = await _client().post(f"{API_SERVER_URL}/api/ingest", json=payload)
    if resp.status_code in (200, 201):
        return f"Content ingested successfully. It will be processed through the pipeline."
    return f"Ingestion failed: {resp.status_code} — {resp.text}"


@mcp.tool()
async def get_pipeline_stats() -> str:
    """Get pipeline health metrics — item counts by processing stage,
    entity and relationship counts, source health, and briefing history."""
    resp = await _client().get(f"{API_SERVER_URL}/api/stats")
    if resp.status_code != 200:
        return f"Error fetching stats: {resp.status_code}"

    stats = resp.json()

    total_items = stats.get("items", 0)
    pass_count = stats.get("pass", 0)
    weak_count = stats.get("weak", 0)
    drop_count = stats.get("drop", 0)
    t4_count = stats.get("t4_analyzed", 0)

    lines = [
        "## Pipeline Stats\n",
        f"**Items:** {total_items} total",
        f"  PASS: {pass_count} | WEAK: {weak_count} | DROP: {drop_count}",
        f"  T4 analyzed: {t4_count}",
        f"**Entities:** {stats.get('entities', 0)}",
        f"**Relationships:** {stats.get('relationships', 0)}",
        f"**Sources:** {stats.get('active_sources', 0)} active",
        f"**Briefings:** {stats.get('briefings', 0)}",
        f"**Pending proposals:** {stats.get('pending_proposals', 0)}",
    ]

    return "\n".join(lines)


if __name__ == "__main__":
    log.info("Starting MCP server on port %d", PORT)
    mcp.run(transport="streamable-http")
