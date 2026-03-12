# Part 5: Making It Legible

*Building the API server, web UI, and the feedback loop that makes the system learn from its operator.*

---

At the end of Part 4, the system is producing daily briefings and maintaining a knowledge graph. But the only way to see any of it is to query MongoDB directly. The whole point of this project is to turn signal into something an operator can act on — which means an interface.

Three more services: the api-server, the web-ui, and the briefing-agent. Together they turn a pipeline into a product.

## The API Server

FastAPI, because this is a read-heavy CRUD app with a few async endpoints for on-demand synthesis, and FastAPI handles that without ceremony.

### Core endpoints

```python
from fastapi import FastAPI, Query
from datetime import date

app = FastAPI(title="Market Consciousness API")

# Briefings
@app.get("/api/briefings")
async def list_briefings(since: date = None, limit: int = 10):
    """Daily synthesis outputs, newest first."""

@app.get("/api/briefings/{briefing_id}")
async def get_briefing(briefing_id: int):
    """Single briefing with linked items and entities."""

# Items
@app.get("/api/items")
async def list_items(
    tier: str = Query(None, regex="^T[1-4]$"),
    since: date = None,
    entity: str = None,
    limit: int = 50,
):
    """Browse items by tier, date, or entity association."""

# Entity graph
@app.get("/api/entities")
async def list_entities(type: str = None, min_strength: float = 0.3):
    """Active entities, optionally filtered by type or strength."""

@app.get("/api/entities/{entity_id}/graph")
async def entity_graph(entity_id: int, depth: int = 2):
    """Entity with relationships, N levels deep."""

# Sources
@app.get("/api/sources")
async def list_sources(status: str = None):
    """All sources with health scores."""

@app.post("/api/sources/{source_id}/toggle")
async def toggle_source(source_id: int):
    """Activate or suspend a source."""

# Discovery proposals
@app.get("/api/proposals")
async def list_proposals(status: str = "pending"):
    """Source proposals from the discovery agent."""

@app.post("/api/proposals/{proposal_id}/approve")
async def approve_proposal(proposal_id: int):
    """Approve a proposed source."""

@app.post("/api/proposals/{proposal_id}/dismiss")
async def dismiss_proposal(proposal_id: int):
    """Dismiss a proposed source."""

# Feedback
@app.post("/api/items/{item_id}/feedback")
async def submit_feedback(item_id: int, feedback: FeedbackInput):
    """Mark an item as useful, noise, or interesting-but-not-now."""

# On-demand synthesis
@app.post("/api/synthesise")
async def on_demand_synthesis(query: SynthesisQuery):
    """Run the synthesis agent with a specific question."""
```

Nothing surprising. The interesting decisions are all about what the endpoints return — specifically, how much context to include with each object.

A briefing response includes the item summaries it was synthesized from. An item response includes its entity associations. An entity response includes its relationship graph. Everything links to everything else because the value is in the connections.

### The feedback collection

```javascript
// feedback collection
{
  item_id: ObjectId("..."),
  rating: "useful",  // "useful" | "noise" | "interesting"
  created_at: ISODate()
}
```

Three options, not five. Not a star rating. Not a thumbs up/down. Three categories that each produce a different system response:

- **useful**: Boosts the source's health score. Increases the relevance of associated entities. The T2 embedding adapts toward this item's vector (slowly — exponential moving average with α=0.01).
- **noise**: Opposite direction. Source health decreases. Entity relevance decays faster. If multiple items from the same source are marked noise in a week, the source gets flagged for review.
- **interesting**: The most important one. "This is relevant but I don't need to act on it now." No negative signal — the system was right to surface it. But it allows the operator to differentiate "useful and actionable" from "useful and background context."

### Feedback propagation

When an operator marks an item, the feedback propagates:

```python
async def propagate_feedback(item_id: int, rating: str):
    item = await get_item(item_id)
    source = await get_source(item["source_id"])
    entities = await get_item_entities(item_id)
    
    if rating == "useful":
        await adjust_source_health(source["id"], delta=0.05)
        for entity in entities:
            await adjust_entity_strength(entity["id"], delta=0.02)
        await nudge_problem_vector(item["embedding"], alpha=0.01)
        
    elif rating == "noise":
        await adjust_source_health(source["id"], delta=-0.03)
        for entity in entities:
            await adjust_entity_strength(entity["id"], delta=-0.01)
        await nudge_problem_vector(item["embedding"], alpha=-0.005)
```

The asymmetry is intentional. Positive feedback reinforces gently. Negative feedback corrects more gently still. The system should be slow to contract and moderate to expand. Better to surface one extra noise item than to miss a genuine signal.

## The Web UI

React. No framework opinions beyond that — this is a dashboard, not a SaaS app. It has four views.

### The briefing view

The default view. Today's briefing in full, with a list of previous briefings in a sidebar. Each briefing shows:

- The synthesis text (3-5 paragraphs, see Part 4)
- The key items it drew from, as expandable cards
- Entities mentioned, linked to the graph view
- A "what changed" sidebar showing new entities and relationship changes

Design principle: newspaper front page. The briefing is the editorial. The items are the articles. The entities are the actors. You can go as deep as you want, but the top-level view should be readable in two minutes.

### The item feed

A chronological feed of all items that passed T2, with tier indicators (T2/T3/T4). Each item shows its summary, source, entities, and feedback buttons. You can filter by entity, source, or tier.

This is where the operator spends the most time in the first two weeks — reviewing items, giving feedback, calibrating the system. After that, the briefing view takes over.

### The entity graph

A force-directed graph visualization of entities and relationships. Nodes are entities, edges are relationships. Node size reflects strength. Edge thickness reflects relationship frequency.

This view is fascinating and mostly useless for daily operation. It's excellent for the discovery agent review — when you see a cluster of entities forming that aren't well-connected to your core domain, that's either a drift or an emerging adjacent space worth watching.

```typescript
// Using d3-force for the layout
const simulation = d3.forceSimulation(nodes)
    .force("link", d3.forceLink(links).distance(d => 100 / d.weight))
    .force("charge", d3.forceManyBody().strength(-200))
    .force("center", d3.forceCenter(width / 2, height / 2));
```

### The source manager

Lists all sources with health scores, item counts, and status. Discovery proposals appear here with approve/dismiss buttons. This is the control plane — where the operator steers the system.

Columns: name, type, health score (color-coded), items/30d, pass rate, last check, status. Sortable by any column.

## The Briefing Agent

The briefing agent is the simplest agent in the system. It takes the synthesis output and formats it for delivery. Right now, "delivery" means making it available via the API. Future: email, Slack, webhooks.

```python
async def format_briefing(synthesis_output, items, entities):
    """
    Take raw synthesis and structure it for the UI.
    No LLM call — this is just formatting.
    """
    return {
        "date": date.today().isoformat(),
        "synthesis": synthesis_output,
        "key_items": [
            {
                "id": item["id"],
                "title": item["title"],
                "source": item["source_name"],
                "tier": item["tier"],
                "summary": item["t4_summary"] or item["t3_summary"],
                "entities": item["entity_names"],
            }
            for item in items
        ],
        "entity_changes": [
            {
                "entity": e["name"],
                "type": e["type"],
                "change": e["change_type"],  # new, strengthened, weakened, new_relationship
                "detail": e["change_detail"],
            }
            for e in entities
            if e.get("changed_today")
        ],
    }
```

Note: no LLM call. The synthesis agent already did the expensive work. The briefing agent is a formatter and delivery mechanism. It costs nothing to run.

Why make it a separate service? Because delivery will eventually have its own complexity — formatting for different channels, scheduling, user preferences. Keeping it separate means the synthesis agent doesn't need to know about any of that.

## The DSE Additions

```yaml
---
# API Server
apiVersion: apps.example.com/v1alpha1
kind: DevStagingEnvironment
metadata:
  name: mc-api-server-dev
spec:
  deployment:
    image: mc-api-server:dev
    replicas: 1
    port: 8084
    env:
      - name: MONGO_URL
        value: "mongodb://mc-crawler-dev-mongodb:27017/mc"
      - name: REDIS_URL
        value: "redis://mc-crawler-dev-redis:6379/0"
      - name: LLM_GATEWAY_URL
        value: "http://mc-llm-gateway-dev:8082"
    healthCheck:
      path: /healthz
  service:
    port: 8084
    type: ClusterIP

---
# Web UI
apiVersion: apps.example.com/v1alpha1
kind: DevStagingEnvironment
metadata:
  name: mc-web-ui-dev
spec:
  deployment:
    image: mc-web-ui:dev
    replicas: 1
    port: 3000
    env:
      - name: API_URL
        value: "http://mc-api-server-dev:8084"
    healthCheck:
      path: /
  service:
    port: 3000
    type: ClusterIP

---
# Briefing Agent
apiVersion: apps.example.com/v1alpha1
kind: DevStagingEnvironment
metadata:
  name: mc-briefing-agent-dev
spec:
  deployment:
    image: mc-briefing-agent:dev
    replicas: 1
    port: 8085
    env:
      - name: MONGO_URL
        value: "mongodb://mc-crawler-dev-mongodb:27017/mc"
      - name: REDIS_URL
        value: "redis://mc-crawler-dev-redis:6379/0"
    healthCheck:
      path: /healthz
  service:
    port: 8085
    type: ClusterIP
```

Nine services deployed. To access the UI locally:

```bash
kindling expose mc-web-ui-dev
```

This creates an ingress rule routing traffic to the web-ui pod. The UI is now reachable at the Kind cluster's ingress address.

## The Feedback Loop in Practice

After a week of daily use, here's what the feedback loop actually did:

1. **Three sources got auto-suspended.** One was a blog that hadn't posted in 6 months. Two were subreddits that turned out to be mostly memes. Source health dropped below 0.15.

2. **The problem vector shifted.** The original embedding was seeded from the problem-space description. After 50+ feedback signals, it had drifted toward what the operator actually finds useful — which was slightly different from the initial description. The system learned the operator's real interests, not their stated interests.

3. **Entity strength stabilized.** In the first few days, every entity was new and everything looked important. By day 5, the truly relevant entities had strengthened and the noise entities had decayed. The entity graph went from a hairball to a meaningful structure.

4. **The discovery agent got better.** Its proposals in week 1 were obvious — mainstream sources the operator already knew about. By week 2, it was finding niche newsletters and smaller communities. The entity graph gave it vocabulary it didn't have before.

This is the system working as designed. Not right out of the box — after a week of operator involvement. The feedback loop is the product. The agents are the mechanism.

## What I Hit

- **CORS.** Always CORS. The web-ui runs on port 3000, the api-server on 8084. In-cluster requests are fine but local dev with hot reload needed CORS headers. `kindling sync` handles file syncing but doesn't help with network topology — I needed a simple CORS middleware in FastAPI.
- **The entity graph visualization tanks at >500 nodes.** D3 force simulation gets sluggish. Added a filter to show only entities above a strength threshold, defaulting to 0.3. Below that, entities are there but not rendered.
- **Feedback propagation needs debouncing.** An operator marking 10 items as noise in quick succession triggers 10 separate propagation cycles. Added a 5-second debounce that batches pending feedback before propagating.
- **The briefing agent feels like over-engineering right now.** It's 40 lines of code that could live inside the api-server. I'm keeping it separate because email/Slack delivery is coming, but today it's a service that barely justifies its pod.

---

*Next up: Part 6 — Shipping It. Using `kindling snapshot` to go from dev to production, the cost reality check, and what this project taught me about building with kindling.*
