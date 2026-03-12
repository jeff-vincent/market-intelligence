# Part 1: The Architecture Nobody Asked For

*Building a multi-agent market intelligence system from scratch — starting with why most "agent" projects fail before they ship.*

---

I've been thinking about a problem for a while: I build developer tools, and I have no structured way to know what's happening in the space around me. I read Hacker News. I scroll Twitter. I skim newsletters. It's manual, inconsistent, and I definitely miss things.

So I'm building a system to do it for me. Not a keyword alert service — those exist and they're fine for known unknowns. I want something that finds signals I didn't know to look for. Communities I'm not in. Projects that aren't on my radar yet. Adjacent problem spaces that are converging with mine.

This is the first post in a series documenting the entire build. I'm shipping it on [kindling](https://kindling.sh), my own tool, which means this is also a stress test. Every friction point I hit is a bug I need to fix.

## The Problem with Most Agent Architectures

Most multi-agent demos are a linear chain of LLM calls with different system prompts. Agent A summarizes, Agent B classifies, Agent C writes a report. The "multi" in multi-agent is decorative — you could collapse the whole thing into one prompt and get roughly the same result.

The architecture I want is genuinely distributed. Agents that use fundamentally different methods. Agents that communicate asynchronously and can task each other. Agents that run at different speeds because their jobs have different time horizons.

Here's the topology:

```
                         ┌──────────────┐
                    ┌───▶│   Crawler    │◀──── on-demand tasks
                    │    └──────┬───────┘
                    │           │ raw_items channel
                    │           ▼
                    │    ┌──────────────┐
                    │    │  Relevance   │
                    │    │   Filter     │
                    │    └──────┬───────┘
                    │           │ scored_items channel
                    │     ┌─────┴──────┐
                    │     │            │
                    │     ▼            ▼
                    │  PASS          WEAK
                    │     │            │
                    │     ▼            ▼
                    │  ┌──────┐   ┌──────────┐
                    │  │Entity│   │Discovery │
                    │  │Extrac│   │  Agent   │──── proposes new sources
                    │  └──┬───┘   └──────────┘
                    │     │ 
                    │     ▼
                    │  ┌──────────┐
                    └──│Synthesis │──── spawns targeted crawls
                       │  Agent   │
                       └──┬───────┘
                          │
                          ▼
                       ┌──────────┐
                       │Briefing  │──── Slack / email / web UI
                       │  Agent   │
                       └──────────┘
```

Two things to notice:

1. **Two loops, two speeds.** The crawler, relevance filter, and entity extractor run continuously — the fast loop. The discovery agent runs weekly — the slow loop. These are fundamentally different jobs with different cost profiles and different failure modes.

2. **The arrow goes backward.** The synthesis agent can spawn targeted crawl tasks. The discovery agent proposes new sources for the crawler. This isn't a pipeline — it's a feedback system.

## Nine Services, Two Dependencies

The system decomposes into nine services:

| Service | Role | Speed |
|---------|------|-------|
| `crawler` | Fetch + parse + deduplicate | Fast, stateless, horizontal |
| `relevance-filter` | Embed + score against problem-space vector | Fast |
| `entity-extractor` | NER + knowledge graph + T3/T4 analysis | Fast |
| `discovery-agent` | Find what to watch next | Slow (weekly) |
| `synthesis-agent` | Cross-signal reasoning | Slow (daily) |
| `briefing-agent` | Format + deliver + collect feedback | Post-synthesis |
| `llm-gateway` | Central proxy for all LLM/embedding calls | Always on |
| `api-server` | REST API for UI + ingestion endpoint | Always on |
| `web-ui` | Operator interface | Always on |

Dependencies:

- **MongoDB Atlas** (with Atlas Vector Search) — document store, entity graph, source registry, briefing history, vector embeddings
- **Redis** — pub/sub for inter-agent messaging, deduplication set, entity summary cache

I originally specced Kafka for the message bus. I switched to Redis pub/sub. Here's why: Kafka gives you durable, replayable message streams with consumer groups and offset tracking. That's genuinely useful infrastructure for a system processing tens of thousands of events per second. This system processes maybe 10,000 items per *day*. Redis pub/sub handles that without breaking a sweat, and it's already there for deduplication and caching. Adding Kafka would mean a third dependency, another `kindling` service to manage, and ZooKeeper (or KRaft) running on a laptop. The engineering trade-off is obvious — Redis does three jobs instead of dedicated infrastructure for each.

## The Tiered Cost Filter

The single most important architectural decision: **LLMs only see content that has already been eliminated by cheaper methods.**

```
Raw ingestion: ~10,000 items/day
      │
      ▼
  T1  Deduplication           (~$0.00/day)   → 80-85% eliminated
      │  ~2,000 items
      ▼
  T2  Embedding similarity    (~$0.04/day)   → 90% eliminated
      │  ~200 items
      ▼
  T3  Fast classification     (~$0.02/day)   → 90% eliminated
      │  ~20 items
      ▼
  T4  Deep analysis           (~$0.40/day)   → Full model, chunk-filtered
```

10,000 items come in. 20 get a full LLM analysis. Monthly cost: $45–90.

T1 is a SHA-256 hash check against a Redis set. No intelligence. Pure deduplication.

T2 is an embedding comparison — cosine similarity against the problem-space vector stored in MongoDB Atlas Vector Search. Embeddings cost 1/50th of a chat completion. Most content dies here.

T3 sends only the title and a 150-character excerpt to a small model (Haiku or GPT-4o-mini). Tiny context window, binary yes/no output. Cheap.

T4 is the expensive one — but it only sees ~20 items per day. And even then, it doesn't see the full document. The document is chunked, each chunk is embedded, and only the top 3 relevant chunks are sent to the model. A 3,000-word article yields maybe 300 words of actually relevant material.

Every LLM and embedding call — T2, T3, T4, synthesis, discovery — routes through a single `llm-gateway` service. This is where rate limiting, API key rotation, provider fallback, and cost tracking live. No agent talks to OpenAI or Anthropic directly. The gateway tracks spend per tier, per agent, per day, and enforces the budget ceiling before a call is made. When a provider returns a 429, the gateway handles backoff and failover — the calling agent never knows.

The system gets cheaper over time. Entity summaries are cached with TTLs. If the same entity reappears and nothing new has been said, T4 is skipped entirely.

## Seeding With Intent, Not Sources

Most market monitoring tools start with a source list. You tell it: watch these 15 subreddits, these 20 blogs, these 5 newsletters. The problem is that by the time you've hardcoded a source list, it's already partially stale. Communities move. Newsletters die. New venues emerge.

This system is seeded with a *problem description*:

> "The friction between writing code locally and running it in production, especially for multi-service and agent-based architectures."

From this, the Discovery Agent bootstraps an initial source list using its training knowledge. The initial list is approximate and expected to be imperfect. It only needs to be good enough to generate the first signals that fuel organic expansion.

The onboarding also collects three pieces of content the operator found highly relevant. These aren't sources — they're examples of what "signal" looks like for this deployment. The system learns your taste before it runs a single crawl.

## The kindling Deployment

Here's where it gets real. This is an 8-service system with MongoDB and Redis. On any other platform, just getting the infrastructure running is a project in itself.

With kindling, the DSE manifest declares everything:

```yaml
# crawler gets mongodb + redis as shared deps
dependencies:
  - mongodb
  - redis
```

All 8 services are declared in a single manifest. `kindling deploy` provisions both dependencies, builds all images via Kaniko, and deploys everything to a local Kind cluster. `MONGO_URL` and `REDIS_URL` are auto-injected — no config files, no `.env` juggling.

For local dev, kindling provisions a MongoDB pod in the Kind cluster. For production, the connection string points at a MongoDB Atlas cluster — same driver, same queries, and Atlas Vector Search handles the embedding indexes automatically. No extension installation, no index maintenance scripts.

The inner dev loop is where kindling earns its keep on this project. Agent prompts and relevance thresholds require constant iteration. `kindling sync` live-reloads a single service without rebuilding the whole stack. Change a prompt template, save the file, it's running in the cluster in under a second.

## What's Next

In Part 2, I'll build the crawler and relevance filter — the ingestion pipeline that handles 10,000 items/day and reduces them to ~200 worth scoring. This is the fast loop, the cheap stuff, and the foundation everything else depends on.

The goal by the end of Part 2 is a deployed system that:
- Crawls RSS feeds, Hacker News, and Reddit
- Deduplicates via Redis
- Embeds and scores every item against a problem-space vector
- Stores everything in MongoDB
- Is live on a local Kind cluster via `kindling deploy`

No LLM calls yet. Just plumbing. The expensive stuff comes later, and it only matters if the cheap stuff is solid.

---

*This series documents the build of [Market Consciousness](https://github.com/kindling-sh/market-consciousness), a multi-agent market intelligence system running on [kindling](https://kindling.sh).*
