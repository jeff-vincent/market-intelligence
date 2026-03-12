# Part 2: The Cheap Stuff First

*Building the crawler and relevance filter — 10,000 items/day reduced to 200, no LLM calls, under $0.05.*

---

Every multi-agent system I've seen starts with the interesting part. The LLM reasoning. The synthesis. The creative agent prompts. Then the builder discovers they need to actually get data into the system and bolts on ingestion as an afterthought.

I'm going the other way. The crawler and relevance filter are the foundation. If they're sloppy, every downstream agent pays the tax — in cost, in noise, and in wasted model calls. The most important optimization in this entire system is ensuring that 98% of content never reaches an LLM.

This post covers building services 1 and 2, getting them deployed with kindling, and seeing real data flow through the pipeline.

## Service 1: The Crawler

The crawler is stateless. It fetches content from configured sources, extracts text, deduplicates against Redis, and publishes to a Redis channel. No intelligence. No scoring. Just reliable ingestion.

### What it handles

- RSS/Atom feeds (the bulk of volume)
- Reddit via the API (subreddits relevant to the problem space)
- Hacker News via the Algolia API (search endpoint, not scraping)
- GitHub trending and releases
- Arbitrary URLs from on-demand tasks (other agents can request targeted crawls)

### The Dockerfile

Nothing special here. Python, slim base, no build-time secrets.

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "main.py"]
```

Kaniko builds this without issue. No `--mount=type=cache`, no BuildKit platform args, no `.git` directory.

### Deduplication

Before anything else, every URL is SHA-256 hashed (first 16 bytes) and checked against a Redis set. Seen it before? Drop it immediately. This is T1 — the cheapest possible filter.

```python
def is_duplicate(url: str) -> bool:
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:32]
    return not redis_client.sadd("seen_urls", url_hash)
```

This eliminates 80-85% of raw volume. RSS feeds especially love repeating items across polls.

### Source scheduling

Not all sources are polled at the same frequency. Hacker News moves fast — every hour. Newsletters are daily. Low-health sources drop to every 24 hours. The crawler reads source configs from MongoDB and respects the `poll_interval_mins` field.

### Publishing

After deduplication, items go to two places:
1. The `raw_items` collection in MongoDB (permanent record)
2. The `raw_items` Redis pub/sub channel (triggers the relevance filter)

```python
# Write to MongoDB
await db.raw_items.insert_one({
    "url": url,
    "url_hash": url_hash,
    "source_id": source_id,
    "title": title,
    "excerpt": excerpt,
    "raw_body": body,
    "ingested_at": datetime.utcnow(),
})

# Notify relevance filter
redis_client.publish("raw_items", json.dumps({"id": str(item_id), "url": url}))
```

## Service 2: The Relevance Filter

This is where content meets the problem-space vector. Every item that passes T1 gets embedded and scored via cosine similarity. The output is three bands:

| Band | Score | Action |
|------|-------|--------|
| PASS | > 0.72 | Forward to entity extractor |
| WEAK | 0.45 – 0.72 | Queue for discovery agent's next slow run |
| DROP | < 0.45 | Discard. Decrement source health score. |

### The problem-space vector

This is the heart of the relevance system. During onboarding, the operator provides:

1. A description of the problem space
2. A description of the target user
3. Three examples of highly relevant content

All of these are embedded. The problem-space vector is a weighted average — the description carries the most weight, the examples calibrate it. This vector lives in MongoDB and is the reference point for every relevance score. Atlas Vector Search handles the index — no extensions to install, no `CREATE EXTENSION` incantations.

```python
async def compute_problem_space_vector(description: str, examples: list[str]) -> list[float]:
    desc_emb = await get_embedding(description)
    example_embs = [await get_embedding(ex) for ex in examples]
    
    # Description carries 60% weight, examples split the rest
    weights = [0.6] + [0.4 / len(example_embs)] * len(example_embs)
    all_embs = [desc_emb] + example_embs
    
    combined = np.average(all_embs, axis=0, weights=weights)
    return (combined / np.linalg.norm(combined)).tolist()
```

### Scoring

Every item is embedded using the same model (text-embedding-3-small) and scored against the problem-space vector. This costs about $0.04/day for 2,000 items. All embedding calls go through the `llm-gateway` service — the relevance filter never talks to OpenAI directly.

```python
# Calls the internal llm-gateway, not OpenAI directly
item_embedding = await get_embedding(f"{item.title}\n\n{item.excerpt}")
score = cosine_similarity(item_embedding, problem_space_vector)

if score > T2_PASS_THRESHOLD:
    band = "PASS"
elif score > T2_WEAK_THRESHOLD:
    band = "WEAK"
else:
    band = "DROP"
```

### Budget pressure auto-adjustment

If the monthly LLM spend projection exceeds `MONTHLY_LLM_BUDGET_USD`, the PASS threshold is raised automatically. This reduces downstream volume without operator intervention. The system degrades gracefully under budget pressure — it becomes more selective, not noisy.

### Source health scoring

Every DROP decrements the source's health score. If a source consistently produces content that scores below 0.45, it's not relevant to this problem space. After enough drops, the source is automatically suspended.

This is quiet but important — the source list prunes itself over time.

## The DSE Manifest

Both services declared in a single kindling manifest. The crawler owns mongodb and redis. The relevance filter references the crawler's dependencies by service name.

```yaml
# Crawler — owns the shared dependencies
apiVersion: apps.example.com/v1alpha1
kind: DevStagingEnvironment
metadata:
  name: mc-crawler-dev
spec:
  deployment:
    image: mc-crawler:dev
    replicas: 1
    port: 8080
    healthCheck:
      path: /healthz
  service:
    port: 8080
    type: ClusterIP
  dependencies:
    - type: mongodb
      version: "7"
    - type: redis

---
# Relevance Filter — shares crawler's mongodb + redis
apiVersion: apps.example.com/v1alpha1
kind: DevStagingEnvironment
metadata:
  name: mc-relevance-filter-dev
spec:
  deployment:
    image: mc-relevance-filter:dev
    replicas: 1
    port: 8080
    env:
      - name: MONGO_URL
        value: "mongodb://mc-crawler-dev-mongodb:27017/mc"
      - name: REDIS_URL
        value: "redis://mc-crawler-dev-redis:6379/0"
    healthCheck:
      path: /healthz
  service:
    port: 8080
    type: ClusterIP
```

`kindling deploy -f .kindling/dev-environment.yaml` — MongoDB, Redis, both services, running locally. No Docker Compose. No Helm charts.

## Collections (So Far)

Two collections at this stage. More will come as we add services.

```javascript
// sources collection
{
  url: "https://news.ycombinator.com",
  type: "hackernews",
  poll_interval_mins: 60,
  health_score: 0.5,
  last_crawled_at: ISODate(),
  last_signal_at: ISODate(),
  status: "ACTIVE",
  proposed_by: "SYSTEM",
  metadata: {}
}

// raw_items collection
{
  url: "https://example.com/article",
  url_hash: "a1b2c3d4...",
  source_id: ObjectId("..."),
  title: "Article Title",
  excerpt: "First 200 chars...",
  raw_body: "Full text...",
  embedding: [0.012, -0.034, ...],  // 1536-dim vector
  t2_score: 0.78,
  t2_band: "PASS",
  ingested_at: ISODate(),
  processed_at: ISODate()
}

// problem_space collection
{
  version: 1,
  description: "The friction between writing code locally and...",
  embedding: [0.008, -0.021, ...],  // 1536-dim vector
  created_at: ISODate(),
  updated_at: ISODate()
}
```

The vector search index on `raw_items.embedding` is created via Atlas Vector Search:

```json
{
  "type": "vectorSearch",
  "fields": [{
    "type": "vector",
    "path": "embedding",
    "numDimensions": 1536,
    "similarity": "cosine"
  }]
}
```

In local dev (kindling's in-cluster MongoDB), vector search uses a brute-force scan — fine for dev volumes. In production on Atlas, the index is automatic and handles scale.

## The Inner Dev Loop

This is where building on kindling matters. The crawler's source parsing logic needs iteration — every RSS feed has its own quirks, Reddit's API has rate limits, Hacker News returns data in a different shape. With `kindling sync`, I change the parsing code, save the file, and it's running in the cluster immediately. No rebuild. No redeploy.

```bash
kindling sync -d mc-crawler-dev --restart
```

The relevance filter needs similar iteration — tuning the T2 thresholds, testing the embedding model, adjusting the problem-space vector weights. Same workflow. Change, save, live.

## What's Working at the End of Part 2

At this point the system:

- Crawls RSS, HN, and Reddit on configurable schedules
- Deduplicates via Redis (T1: ~85% elimination)
- Embeds and scores every item against the problem-space vector (T2: ~90% more eliminated)
- Stores everything in MongoDB with vector embeddings
- Tracks source health and auto-suspends noisy sources
- Runs locally on a Kind cluster via kindling

No LLM chat completions yet. Total daily cost: ~$0.04 for embeddings. The expensive stuff comes in Part 3 when the entity extractor starts doing T3 and T4 analysis on the ~200 items that make it through.

## What I Hit

A few things I'll note for honesty:

- **Vector search in local dev vs. Atlas**: In the Kind cluster, MongoDB doesn't have Atlas Vector Search. For dev, I'm doing a brute-force cosine similarity in Python on the embeddings stored in MongoDB — works fine for 2,000 items/day. In production on Atlas, the vector search index handles this natively. The application code is the same either way; only the query path differs.
- **Redis pub/sub isn't persistent**: If the relevance filter is down when the crawler publishes, those messages are lost. For this system at this volume, that's fine — the items are in MongoDB and can be reprocessed. But it's a known trade-off vs. Kafka's durability.
- **Rate limits**: Reddit's API has aggressive rate limiting. The crawler needs backoff logic from day one, not added later.

These aren't showstoppers. They're the kind of thing you only discover by building, which is the whole point of this series.

---

*Next up: Part 3 — Teaching a System to Remember. The entity extractor, knowledge graph, and the T3/T4 analysis pipeline where LLMs finally earn their keep.*
