# Part 3: Teaching a System to Remember

*Building the entity extractor — where the system stops being a feed reader and starts building a model of the world.*

---

After Part 2, we have a pipeline that ingests 10,000 items a day and reduces them to ~200 that are semantically relevant to the problem space. That's useful, but it's still just a filtered feed. You could get roughly the same result from a well-configured RSS reader.

This is the post where the system starts thinking. The entity extractor takes PASS-scored items and does two things: T3/T4 analysis (the first real LLM calls in the system) and knowledge graph construction (entities, relationships, and the connections between them). When this is done, the system doesn't just know *what's relevant* — it knows *who's doing what* and *how things are connected*.

## T3: The Cheap Gatekeeper

T3 is the first LLM call in the pipeline, and it's deliberately minimal. The model sees three things: the source domain, the title, and a 150-character excerpt. That's it. No full document. No context window gymnastics.

```python
async def t3_classify(item: dict) -> dict:
    response = await llm_gateway.chat(
        tier="t3",
        messages=[
            {
                "role": "system",
                "content": (
                    f"You are a relevance classifier for this problem space:\n"
                    f"{problem_space_description}\n"
                    f"Respond only with JSON: {{\"pass\": true|false, \"reason\": \"<15 words>\"}}"
                ),
            },
            {
                "role": "user",
                "content": f"Source: {item['domain']}\nTitle: {item['title']}\nExcerpt: {item['excerpt'][:150]}",
            },
        ],
    )
    return json.loads(response)
```

Notice the call goes to `llm_gateway.chat()`, not to OpenAI or Anthropic directly. The gateway decides which provider and model handles this based on the `tier` parameter. T3 routes to the cheapest small model available — Haiku or GPT-4o-mini. If one provider is rate-limited, it fails over to the other.

T3 eliminates ~90% of what T2 passed through. 200 items become ~20. The classifier is surprisingly good with minimal context — most irrelevant items are obviously irrelevant even from the title alone. The 15-word reason constraint forces the model to be decisive rather than hedging.

## T4: Deep Analysis on the Survivors

The ~20 items that survive T3 are the ones worth understanding in detail. But even here, the model doesn't see the full document. That's wasteful — a 3,000-word blog post might have 300 words of material relevant to the problem space.

The document is chunked (500 tokens each with 50-token overlap). Each chunk is embedded via the llm-gateway. The top 3 chunks by cosine similarity to the problem-space vector are selected and sent to a full model.

```python
async def t4_analyze(item: dict) -> dict:
    chunks = chunk_text(item["raw_body"], chunk_size=500, overlap=50)
    chunk_embeddings = await llm_gateway.embed_batch(chunks)
    
    scored = [
        (chunk, cosine_similarity(emb, problem_space_vector))
        for chunk, emb in zip(chunks, chunk_embeddings)
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    top_chunks = [chunk for chunk, _ in scored[:3]]
    relevant_text = "\n\n---\n\n".join(top_chunks)

    response = await llm_gateway.chat(
        tier="t4",
        messages=[
            {
                "role": "system",
                "content": (
                    f"You are an analyst for this problem space:\n{problem_space_description}\n\n"
                    "Extract: (1) a 2-sentence summary, (2) named entities with types, "
                    "(3) relationships between entities. Respond as JSON."
                ),
            },
            {
                "role": "user",
                "content": f"Source: {item['domain']}\nTitle: {item['title']}\n\nRelevant excerpts:\n{relevant_text}",
            },
        ],
    )
    return json.loads(response)
```

T4 outputs structured data: a summary, a list of entities with types, and relationships between them. This is the raw material for the knowledge graph.

## The Knowledge Graph

Entities aren't just extracted and stored — they're connected. Every T4 analysis produces entity records and relationship edges that accumulate into a graph over time.

### Entity types

| Type | Examples |
|------|----------|
| `company` | Vercel, Render, Railway |
| `person` | Kelsey Hightower, Mitchell Hashimoto |
| `tool` | Terraform, Pulumi, Dagger |
| `concept` | platform engineering, internal developer platform |
| `community` | r/kubernetes, CNCF Slack, DevOps Weekly |

### Relationships

Relationships have types, strengths, and evidence counts. A relationship gets stronger every time a new item surfaces the same connection.

```python
async def upsert_relationship(from_id, to_id, rel_type, item_id):
    existing = await db.entity_relationships.find_one({
        "from_entity_id": from_id,
        "to_entity_id": to_id,
        "relationship": rel_type,
    })
    
    if existing:
        await db.entity_relationships.update_one(
            {"_id": existing["_id"]},
            {
                "$set": {"last_seen_at": datetime.utcnow()},
                "$inc": {"evidence_count": 1},
                "$min": {"strength": min(existing["strength"] + 0.1, 1.0)},
            },
        )
    else:
        await db.entity_relationships.insert_one({
            "from_entity_id": from_id,
            "to_entity_id": to_id,
            "relationship": rel_type,
            "strength": 0.5,
            "first_seen_at": datetime.utcnow(),
            "last_seen_at": datetime.utcnow(),
            "evidence_count": 1,
        })
```

Relationships decay if they stop being reinforced. An entity pair that was "mentioned_with" six months ago but never since gets a decayed strength score. The graph stays current without explicit pruning.

### Entity summary caching

This is a cost optimization that compounds over time. Every entity gets a summary with a staleness TTL. If the entity reappears in a new item but nothing materially new has been said, T4 is skipped entirely and the cached summary is used.

```python
async def should_reanalyze(entity_id: str, new_content: str) -> bool:
    entity = await db.entities.find_one({"_id": entity_id})
    
    if entity.get("summary_ttl") and entity["summary_ttl"] > datetime.utcnow():
        # Still fresh — check if the new content adds anything
        new_emb = await llm_gateway.embed(new_content)
        existing_emb = await get_entity_embedding(entity_id)
        similarity = cosine_similarity(new_emb, existing_emb)
        
        if similarity > 0.92:
            return False  # Same information, skip T4
    
    return True
```

After ~60 days of operation, entity cache hits become frequent. The system gets meaningfully cheaper to run as its knowledge base matures. The spec estimates a 30% drop in T4 costs once the cache is warm.

## The New Collections

Two new collections join the schema from Part 2:

```javascript
// entities collection
{
  name: "Dagger",
  type: "tool",
  summary: "CI/CD engine that runs pipelines as code...",
  summary_embedding: [0.012, -0.034, ...],  // 1536-dim vector
  first_seen_at: ISODate(),
  last_updated_at: ISODate(),
  summary_ttl: ISODate(),
  watch_level: "PASSIVE",
  metadata: {}
}

// entity_relationships collection
{
  from_entity_id: ObjectId("..."),
  to_entity_id: ObjectId("..."),
  relationship: "competes_with",
  strength: 0.7,
  first_seen_at: ISODate(),
  last_seen_at: ISODate(),
  evidence_count: 4
}
```

Indexes:

```javascript
db.entities.createIndex({ type: 1 })
db.entities.createIndex({ watch_level: 1 })
db.entity_relationships.createIndex({ from_entity_id: 1 })
db.entity_relationships.createIndex({ to_entity_id: 1 })
```

The vector search index on `entities.summary_embedding` follows the same Atlas Vector Search pattern from Part 2 — brute-force in local dev, automatic in Atlas.

## The LLM Gateway

This is the service that makes the cost model actually work. Every LLM call — embeddings, T3 classification, T4 analysis, and later synthesis and discovery — routes through a single `llm-gateway` service.

### What it does

- **Rate limiting**: Per-provider, per-tier. OpenAI and Anthropic have different rate limits. The gateway tracks requests-per-minute and queues when limits are approached.
- **Provider fallback**: T3 is configured as "cheapest available." If Anthropic returns a 429, the gateway switches to OpenAI for that call. The calling agent doesn't know or care.
- **Cost tracking**: Every call is logged with token counts, cost, tier, and calling agent. This feeds the budget guard and the cost dashboard.
- **Model routing**: The `tier` parameter maps to a model. T3 → Haiku. T4 → Sonnet. Embeddings → text-embedding-3-small. Changeable in one place.
- **Budget enforcement**: Before making any call, the gateway checks projected monthly spend against `MONTHLY_LLM_BUDGET_USD`. If over budget, it returns a budget-exceeded error. The calling agent can decide what to do — skip, queue for later, or use cached results.

### The interface

Other services call the gateway over HTTP. It looks like a simplified OpenAI API:

```python
# In any agent service
async def llm_chat(tier: str, messages: list) -> str:
    resp = await httpx.post(
        f"http://mc-llm-gateway-dev:8082/v1/chat",
        json={"tier": tier, "messages": messages}
    )
    resp.raise_for_status()
    return resp.json()["content"]

async def llm_embed(text: str) -> list[float]:
    resp = await httpx.post(
        f"http://mc-llm-gateway-dev:8082/v1/embed",
        json={"input": text}
    )
    resp.raise_for_status()
    return resp.json()["embedding"]
```

### Why a service, not a library

You could put rate limiting and cost tracking in a shared Python package. But then every service needs the API keys. Every service tracks its own costs. Every service implements its own fallback logic. And if you want to change the T3 model from Haiku to GPT-4o-mini, you redeploy five services instead of one.

A centralized gateway means one set of API keys (managed via `kindling secrets`), one cost ledger, one place to change model routing, and one rate limiter that has a global view of usage. The trade-off is one more network hop per call. At 20 T4 items per day, the latency is irrelevant.

## The DSE Addition

The entity extractor and LLM gateway join the manifest:

```yaml
---
# LLM Gateway — central proxy for all LLM/embedding calls
apiVersion: apps.example.com/v1alpha1
kind: DevStagingEnvironment
metadata:
  name: mc-llm-gateway-dev
spec:
  deployment:
    image: mc-llm-gateway:dev
    replicas: 1
    port: 8082
    healthCheck:
      path: /healthz
  service:
    port: 8082
    type: ClusterIP

---
# Entity Extractor
apiVersion: apps.example.com/v1alpha1
kind: DevStagingEnvironment
metadata:
  name: mc-entity-extractor-dev
spec:
  deployment:
    image: mc-entity-extractor:dev
    replicas: 1
    port: 8080
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
    port: 8080
    type: ClusterIP
```

The API keys live only on the gateway — `kindling secrets set OPENAI_API_KEY` and `kindling secrets set ANTHROPIC_API_KEY`. The entity extractor, relevance filter, and every future agent just need the gateway URL.

## What's Working at the End of Part 3

The system now:

- Ingests and deduplicates (T1)
- Embeds and scores (T2)
- Classifies with a small model via the LLM gateway (T3)
- Deep-analyzes survivors with a full model via chunk filtering (T4)
- Extracts entities and relationships into a knowledge graph
- Caches entity summaries to reduce future T4 costs
- Tracks all LLM costs centrally through the gateway

We're at 4 services deployed: crawler, relevance-filter, llm-gateway, entity-extractor. The daily cost is about $0.50 — almost entirely T4, with the gateway handling the routing.

The entity graph is the foundation for everything that comes next. The discovery agent uses it to reason about what's missing. The synthesis agent uses it to tell stories across signals. Without the graph, those agents would just be running prompts against raw content. With it, they have memory.

## What I Hit

- **Entity deduplication is hard.** "Kubernetes", "k8s", "K8s", and "Kube" are the same entity. The extractor needs a normalization pass, and even then fuzzy matching is imperfect. For now I'm using embedding similarity on entity names — if two entities embed within 0.95 similarity, they're merged. It's not perfect but it's good enough to start.
- **The LLM gateway needs health checks.** If the gateway is down, every agent is blind. The `/healthz` endpoint needs to verify it can actually reach at least one provider, not just that the Flask process is alive. I added a provider reachability check with a 5-second cache.
- **T4 JSON parsing.** Full models don't always return valid JSON, even when you ask nicely. The entity extractor has a retry-with-repair loop: if `json.loads` fails, it sends the response back to the model with "fix this JSON." Works 99% of the time. The 1% gets logged and skipped.

---

*Next up: Part 4 — The Slow Loop. The discovery agent and synthesis agent, where the system starts reasoning about what it doesn't know yet.*
