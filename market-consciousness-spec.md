# Market Consciousness

> A multi-agent system that builds a continuously updated model of a market or problem space — surfacing signals you didn't know to look for.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Agent Roster](#3-agent-roster)
4. [Tiered Cost Filter](#4-tiered-cost-filter)
5. [Source Discovery](#5-source-discovery)
6. [Data Model](#6-data-model)
7. [Services](#7-services)
8. [LLM Gateway](#8-llm-gateway)
9. [Web UI](#9-web-ui)
10. [Integrations](#10-integrations)
11. [Reddit Engagement](#11-reddit-engagement)
12. [Configuration & Secrets](#12-configuration--secrets)
13. [Cost Model](#13-cost-model)
14. [MCP Server (Planned)](#14-mcp-server-planned)
15. [Deployment with kindling](#15-deployment-with-kindling)
16. [Development Guide](#16-development-guide)

---

## 1. Overview

Market Consciousness is a continuously running multi-agent system that builds and maintains a living model of a market or problem space. Unlike conventional competitive trackers that monitor a fixed list of known competitors, it is designed to find signals you didn't know to look for — emerging communities, weak early indicators, adjacent problem solvers, and ecosystem shifts before they become mainstream.

### What it is not

- A keyword alert system (Mention, Google Alerts)
- A competitor monitor with a hardcoded source list
- A scraper that dumps raw content at you

### What it is

A system that maintains a **map of where your problem space has a presence** — and continuously redraws that map based on what it finds. The source list is itself a living artifact. By the time you've hardcoded a source list, it's already partially stale.

The operator interface is a full-featured web UI with pipeline QA, entity exploration, Reddit engagement, and configurable delivery integrations (Slack, Discord, Email, Webhooks, Notion, Linear). An MCP server is planned to expose the intelligence pipeline as tools for agent workflows.

### Core design principle

> Seed with intent, not with sources. Store the problem statement, not just the outputs of the problem statement. If the system ever gets confused about relevance, it can refer back to the original framing.

---

## 2. Architecture

### Two-speed design

The system runs two loops at fundamentally different speeds. Most monitoring systems only build the fast loop and wonder why they keep missing things.

```
┌─────────────────────────────────────────────────────────────┐
│  SLOW LOOP  (daily / weekly)                                │
│  Discovery — find what to watch next                        │
│  Expands and prunes the source map                          │
│  Asks: what should we be monitoring that we aren't yet?     │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  FAST LOOP  (hourly / continuous)                           │
│  Monitoring — watch things already deemed worth watching    │
│  Runs most of the volume, cheapest processing per item      │
│  Asks: what has changed since we last looked?               │
└─────────────────────────────────────────────────────────────┘
```

### Message-passing topology

Agents communicate asynchronously via Redis pub/sub channels. This is what distinguishes the system from an ETL job with LLM calls bolted on.

```
                         ┌──────────────┐
                    ┌───▶│   Crawler    │◀──── on-demand tasks (crawl_tasks channel)
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
                          │ new_briefing channel
                          ▼
                       ┌──────────┐
                       │Briefing  │──── Integrations (Slack / Discord / Email / etc.)
                       │  Agent   │
                       └──────────┘
```

All LLM calls are routed through the **LLM Gateway** service, which handles model selection, provider fallback, cost tracking, and rate limiting.

### Seeding

The system is seeded with a **description of the problem space**, not a list of sources. From this, the Discovery Agent bootstraps an initial source map using its training knowledge plus targeted searches.

**Onboarding collects three inputs:**

1. A one-sentence description of the tool and the problem it solves
2. A description of the target user and their primary frustration
3. Three pieces of content the operator recently found highly relevant — used to bootstrap the relevance embedding and calibrate what "signal" means for this deployment

The third input is the most important. You're not describing relevance abstractly — you're demonstrating it with examples. The system learns your taste before it runs a single crawl.

**Example seed for a tool like kindling:**

```
Problem: The friction between writing code locally and running it in production,
especially for multi-service and agent-based architectures.

User: Solo developers and small teams building containerised apps who lose hours
to CI/CD configuration and environment inconsistencies before a line of feature
code ships.
```

Note the framing: the problem the user has, not the product being built. This surfaces adjacent signals that a product-centric seed would miss.

---

## 3. Agent Roster

### 3.1 Crawler

**Role:** Stateless ingestion worker  
**Loop:** Fast  
**Trigger:** Scheduled tick (per-source cadence) or on-demand task message from any agent  
**LLM calls:** None

Fetches raw content from configured sources. Resolves redirects, extracts canonical URL and title, pulls body text. Writes to `raw_items` table. Checks Redis deduplication set before writing — seen URLs are dropped immediately.

Stateless and horizontally scalable. Multiple replicas run concurrently to handle large source lists without serialising on crawl cadence.

**Sources it handles:** RSS/Atom feeds, Reddit (via API), Hacker News (via Algolia API), GitHub trending/releases, job boards, newsletter archives, arbitrary URLs from on-demand tasks.

---

### 3.2 Relevance Filter

**Role:** Semantic triage  
**Loop:** Fast  
**Trigger:** New `raw_items` records (event-driven via Redis pub/sub)  
**LLM calls:** Embeddings only via LLM Gateway (~$0.002 per 1,000 items)

Embeds each item and scores it via cosine similarity against the problem-space embedding vector stored in MongoDB. Classifies into three bands:

| Band | Score | Action |
|------|-------|--------|
| PASS | ≥ 0.35 | Forward to Entity Extractor |
| WEAK | 0.20 – 0.35 | Queue for Discovery Agent's next slow-loop run |
| DROP | < 0.20 | Discard. Decrement source health score. |

Thresholds are configurable via environment variables (`T2_PASS_THRESHOLD`, `T2_WEAK_THRESHOLD`). The initial spec called for 0.72/0.45 — in practice, calibration against real-world content required lowering to 0.35/0.20 to produce a useful PASS yield.

Subscribes to the `seed_updated` Redis channel to reload the problem-space embedding when the operator updates the seed.

---

### 3.3 Entity Extractor

**Role:** Knowledge graph maintenance  
**Loop:** Fast  
**Trigger:** PASS-scored items from Relevance Filter  
**LLM calls:** T3 via Haiku (claude-3-5-haiku), T4 via Sonnet (claude-sonnet-4) — all routed through LLM Gateway.

Runs the T3 and T4 filter stages (see [Section 4](#4-tiered-cost-filter)). For items that pass T3, sends the full content to a Sonnet-class model for entity extraction and relationship inference.

The T4 prompt is tuned to extract canonical entity names (e.g. "Kubernetes" not "k8s", "React" not "React.js"). It skips `person` and `community` entity types — these produced low-quality noise in practice. Entity types extracted: `tool`, `company`, `concept`, `framework`, `platform`.

Writes entity records and relationship edges to MongoDB. Maintains a `backfill_unanalyzed()` routine that re-processes PASS items that haven't yet been through T4.

Updates `source_health` scores based on signal yield.

---

### 3.4 Discovery Agent

**Role:** Slow loop — finds what to watch  
**Loop:** Slow  
**Trigger:** Weekly scheduled run + WEAK-signal queue from Relevance Filter  
**LLM calls:** Full model (Sonnet or GPT-4o). Strict token budget per run. ~$5–15 per weekly run.

The most strategically important agent. Looks at WEAK-band items and asks: *should we be watching this more closely?*

**What it does:**
- Proposes new sources to add (written to `sources` with `status = PENDING_REVIEW`)
- Proposes new entities to watch
- Suggests adjustments to relevance embedding weights
- Periodically prompts itself: "given everything surfaced so far, what communities likely exist that we aren't monitoring?" — all suggestions go to verification queue before activation

All proposals require operator approval before becoming active. The Discovery Agent never autonomously expands the source list. Approval happens via the web UI source manager or the REST API.

---

### 3.5 Synthesis Agent

**Role:** Cross-signal reasoning  
**Loop:** Slow  
**Trigger:** Configurable interval (default 24h), or on-demand via `POST /api/synthesise`  
**LLM calls:** Full model (Sonnet) via LLM Gateway. Runs on summaries-of-summaries, never raw content. ~$1–3 per daily run.

The only agent that reasons *across* the full entity graph. Takes the day's T4-processed item summaries plus relevant entity context and asks: *what's the narrative here?*

Not a summariser — an interpreter. Allowed to be speculative and must say so. Example output:

> "Three independent signals this week suggest the dev tools market is bifurcating around local-first vs. cloud-native tooling philosophies. The volume of job postings for 'platform engineering' roles has increased 40% in the entity graph over 30 days, coinciding with two new open-source projects in the space. This may indicate the problem is being widely recognised before a dominant solution has emerged — worth watching."

Can spawn targeted Crawler tasks if it identifies context gaps ("I need more on this entity before I can say something meaningful") by publishing to the `crawl_tasks` Redis channel.

On-demand synthesis is triggered via `POST /api/synthesise` with an optional `query` parameter. The query is passed as additional context to the synthesis prompt — the agent answers the specific question using live entity and signal data rather than producing a generic briefing. When synthesis completes, the result is published to the `new_briefing` Redis channel, which the Briefing Agent picks up for delivery to configured integrations.

---

### 3.6 Briefing Agent

**Role:** Delivery and feedback loop  
**Loop:** Post-synthesis  
**Trigger:** `new_briefing` channel on Redis pub/sub  
**LLM calls:** Minimal — formatting only. Near-zero cost.

Formats and delivers briefings to all enabled integrations. Decrypts integration configs from MongoDB, then dispatches via the appropriate channel:

- **Slack** — Rich blocks with headline, narrative paragraphs, and supporting signals
- **Discord** — Embeds with markdown formatting
- **Email** — HTML email via Resend API
- **Webhook** — JSON POST with optional HMAC-SHA256 signature and custom headers
- **Notion** — Page creation in configured database
- **Linear** — Issue creation for alert-level signals

Tracks what the operator has already seen so content is never repeated. Records feedback signals (useful / noise / interesting-but-not-now) back to MongoDB via the API server.

**Delivery channels:** Slack, Discord, Email (Resend), Webhooks, Notion, Linear — all configurable via the web UI integrations panel.

---

## 4. Tiered Cost Filter

The most important architectural cost-control decision: **LLMs only see content that has already been eliminated by cheaper methods.** Each tier passes a fraction of its intake to the next.

```
Raw ingestion: ~10,000 items/day
      │
      ▼
┌─────────────────────────────────────────────────────┐
│  T1  Deduplication                                  │
│  URL hash + Bloom filter                            │
│  Cost: ~$0.00      Elimination: ~80–85%             │
└──────────────────────────┬──────────────────────────┘
                           │ ~2,000 items
                           ▼
┌─────────────────────────────────────────────────────┐
│  T2  Semantic Scoring                               │
│  Embedding cosine similarity vs problem-space vec   │
│  Cost: ~$0.02/day  Elimination: ~90%                │
└──────────────────────────┬──────────────────────────┘
                           │ ~200 items
                           ▼
┌─────────────────────────────────────────────────────┐
│  T3  Fast Classification                            │
│  Small model, title + 3-sentence excerpt only       │
│  Cost: ~$0.02/day  Elimination: ~90%                │
└──────────────────────────┬──────────────────────────┘
                           │ ~20 items
                           ▼
┌─────────────────────────────────────────────────────┐
│  T4  Deep Analysis                                  │
│  Full model, top-3 chunks only (not full document)  │
│  Cost: ~$0.40/day  Pass rate: 100% of T3            │
└─────────────────────────────────────────────────────┘
```

### T1 — Deduplication

No intelligence. Every ingested URL is SHA-256 hashed (first 16 bytes) and checked against a Redis set. A Bloom filter handles near-duplicate detection for the same content arriving via different URL paths.

Expected elimination: 80–85% of raw volume. Cost: effectively zero.

### T2 — Semantic Relevance Scoring

Every item that passes T1 is embedded (via LLM Gateway → OpenAI text-embedding-3-small) and scored against the problem-space vector in MongoDB. Three output bands: PASS, WEAK, DROP (thresholds: ≥0.35, ≥0.20, <0.20 — see 3.2).

Embeddings are ~1/50th the cost of a chat completion. Most content dies here cheaply.

The problem-space vector is a weighted average of:
- The seed description embedding
- The onboarding example content embeddings
- Incremental updates from operator feedback (useful/noise signals)

### T3 — Fast Classification

Small, fast model receives **only**: source domain, title, and a 150-character excerpt. Context windows are intentionally tiny.

```
System: You are a relevance classifier for the following problem space:
        {problem_space_description}
        Respond only with JSON: {"pass": true|false, "reason": "<15 words>"}

User:   Source: {domain}
        Title: {title}
        Excerpt: {excerpt_150_chars}
```

### T4 — Deep Analysis

Full document is chunked. Each chunk is embedded. Top 3 chunks by relevance score are selected and sent to a full model. A 3,000-word article typically yields 200–400 words of actually relevant material.

T4 output is stored as the item's canonical summary. If the same entity reappears within its staleness TTL with no new material, T4 is skipped and the cached summary is used. This compounds over time — the system gets cheaper as the entity cache matures.

---

## 5. Source Discovery

### 5.1 Initial Bootstrap

From the seed description, the Discovery Agent generates an initial source list by reasoning about where conversations about this problem space are likely to happen. This uses the model's training knowledge — it knows where developer communities congregate, which subreddits are active for which topics, which newsletters cover which spaces.

The initial list is approximate and expected to be imperfect. It only needs to be good enough to generate the first signals that will fuel organic expansion.

### 5.2 Organic Discovery Methods

**Follow the entities, not the sources**

When the Entity Extractor identifies a person or project worth watching, the Discovery Agent asks: where do they congregate? GitHub profile, personal site, X/Twitter bio. People usually tell you where the conversation is. One high-signal person typically reveals the community around them.

Content itself carries breadcrumbs. A blog post may link to a Substack, mention a Discord, reference a podcast. These are followed.

**Job postings as a source map**

Job descriptions for roles in the target problem space frequently list communities they expect candidates to participate in. The Crawler monitors curated job boards and the Entity Extractor flags community references for Discovery review. This is crowd-sourced source intelligence from hiring managers.

**Citation graph traversal**

Technical content has citations. Following them surfaces niche venues — workshops, working groups, mailing lists, IETF drafts, CNCF Slack channels — that never appear in keyword searches. These are often where infrastructure-level shifts happen before mainstream awareness.

**LLM bootstrapping with verification**

The Discovery Agent is periodically prompted:

> "Given everything you've seen about this problem space, what communities or outlets likely exist that we aren't monitoring? Be specific."

Hallucinated suggestions are expected and handled. All proposals go into a verification queue where the Crawler checks for existence and activity level before a source is activated.

### 5.3 Source Health Scoring

Each source carries a health score (0–1) updated on every crawl:

```
health_score = weighted_average(
  recency_score,        # days since last PASS item
  relevance_rate,       # PASS / total items, last 30 days
  signal_to_noise,      # PASS / DROP ratio at T2
  velocity_trend        # items/day vs. historical average
)
```

Sources below the health threshold are automatically suspended. Sources that were suspended but generate fresh signals (detected via entity references or external links) are reactivated.

**Dead sources are themselves a signal.** If a previously active community goes quiet, that event is recorded and surfaced in the next Synthesis run.

### 5.4 Polling Cadence

Not all sources are polled at the same frequency. Cadence is assigned based on source velocity:

| Source type | Default cadence |
|-------------|----------------|
| GitHub releases | Every 2 hours |
| Active subreddits | Every 4 hours |
| Hacker News | Every 1 hour |
| Newsletters | Daily |
| Job boards | Every 6 hours |
| Low-health sources | Every 24 hours |
| Suspended sources | Not polled |

Cadence adjusts automatically based on health score. A previously-weekly source that starts producing daily signals gets promoted.

### 5.5 Private and Semi-Private Spaces

Many valuable conversations happen in paid Discords, private Slacks, and closed newsletters. Two mitigations:

**Edge monitoring:** Watch the public edges of private spaces — public threads referencing private discussions, conference talks distilling closed-community conversations, people sharing screenshots or summaries on open platforms.

**Operator ingestion endpoint:** `POST /ingest` accepts a URL, pasted text, or file upload. The system processes it, attributes it to a source category, and monitors that source's public edges more aggressively going forward. The operator is a first-class contributor to their own system.

---

## 6. Data Model

MongoDB (database: `mc`). All timestamps UTC.

### 6.1 `raw_items`

```json
{
  "_id": "ObjectId",
  "url": "https://...",
  "url_hash": "sha256-first-16-bytes",
  "source_id": "ObjectId (ref sources)",
  "title": "Article title",
  "excerpt": "first 500 chars of body",
  "raw_body": "full content",
  "embedding": [0.123, ...],          // 1536-dim, text-embedding-3-small
  "t2_score": 0.42,                   // cosine similarity vs problem-space vector
  "t2_band": "PASS | WEAK | DROP",
  "t3_pass": true,                    // null until T3 runs
  "t3_reason": "one-line classifier output",
  "t4_analysis": {                    // null until T4 completes
    "summary": "...",
    "entities": ["Kubernetes", "Docker"],
    "relationships": [{"from": "...", "to": "...", "type": "uses"}]
  },
  "source_type": "reddit | hn | rss | github",
  "ingested_at": "ISODate",
  "processed_at": "ISODate"           // null until T4 completes
}
```

Indexes: `url` (unique), `t2_band`, `ingested_at`, `source_id`.

### 6.2 `entities`

```json
{
  "_id": "ObjectId",
  "name": "Kubernetes",
  "type": "tool | company | concept | framework | platform",
  "summary": "latest T4-generated summary",
  "mentions": 12,
  "strength": 0.85,                   // 0–1, based on mention frequency and recency
  "first_seen_at": "ISODate",
  "last_updated_at": "ISODate",
  "source_items": ["ObjectId", ...],  // raw_items that mentioned this entity
  "metadata": {}
}
```

Indexes: `name` (unique), `type`, `strength`.

### 6.3 `entity_relationships`

```json
{
  "_id": "ObjectId",
  "from_entity": "Kubernetes",
  "to_entity": "Docker",
  "relationship": "competes_with | built_by | uses | part_of | integrates_with | mentioned_with",
  "strength": 0.5,
  "evidence_count": 3,
  "first_seen_at": "ISODate",
  "last_seen_at": "ISODate"
}
```

### 6.4 `sources`

```json
{
  "_id": "ObjectId",
  "url": "https://reddit.com/r/devops",
  "name": "r/devops",
  "type": "reddit | hn | github | rss | custom",
  "poll_interval_mins": 240,
  "health_score": 0.65,               // 0–1
  "last_crawled_at": "ISODate",
  "last_signal_at": "ISODate",
  "status": "ACTIVE | SUSPENDED | PENDING_REVIEW",
  "proposed_by": "SYSTEM | OPERATOR | DISCOVERY_AGENT",
  "created_at": "ISODate"
}
```

Indexes: `status`, `health_score`.

### 6.5 `briefings`

```json
{
  "_id": "ObjectId",
  "type": "daily | weekly | alert | on_demand",
  "narrative": "synthesis agent output",
  "items_considered": 15,
  "items": [{"title": "...", "url": "...", "score": 0.42, "summary": "..."}],
  "entity_changes": [{"name": "...", "change": "new | trending | declining"}],
  "created_at": "ISODate",
  "delivered_at": "ISODate",
  "operator_feedback": "useful | noise | partial"
}
```

### 6.6 `problem_space` (versioned)

```json
{
  "_id": "ObjectId",
  "version": 1,
  "problem": "The friction between writing code locally and running it in production...",
  "target_user": "Solo developers and small teams building containerised apps...",
  "description": "problem + target_user concatenated",
  "tags": ["kubernetes", "CI/CD", "developer tooling"],
  "examples": [
    {"text": "...", "url": "https://...", "title": "Relevant post title"}
  ],
  "embedding": [0.123, ...],          // 1536-dim, generated from description
  "created_at": "ISODate",
  "updated_at": "ISODate",
  "reverted_from": null                // set to version number if this was a revert
}
```

Each save increments `version`. Revert copies the target version as a new version, preserving full history. On save or revert, `seed_updated` is published to Redis to trigger relevance filter reload.

### 6.7 Additional collections

| Collection | Purpose |
|---|---|
| `source_proposals` | Discovery Agent's proposed new sources, pending operator review |
| `feedback` | Operator ratings on items (useful / noise / interesting) |
| `api_keys` | Encrypted API credentials (Fernet AES-128-CBC + HMAC-SHA256), keyed by user_id + provider |
| `integrations` | Configured notification channels (Slack, Discord, Email, etc.), encrypted secrets |
| `reddit_replies` | Log of Reddit engagement — user_id, item_url, thing_id, text, timestamp |

---

## 7. Services

Ten services. All containerised. Deployed via `kindling load` or CI workflow.

| Service | Language | Framework | Port | Responsibility |
|---------|----------|-----------|------|----------------|
| `crawler` | Python | aiohttp | 8086 | Fetch, parse, deduplicate. Stateless. Owns shared MongoDB + Redis deps. |
| `relevance-filter` | Python | aiohttp | 8081 | T2 embedding + scoring via LLM Gateway. |
| `llm-gateway` | Python | aiohttp | 8082 | Centralised LLM routing, cost tracking, rate limiting, provider fallback. |
| `entity-extractor` | Python | aiohttp | 8088 | T3 + T4 analysis. Maintains entity graph in MongoDB. |
| `discovery-agent` | Python | aiohttp | 8087 | Slow loop. Proposes new sources. Configurable cadence (default 24h). |
| `synthesis-agent` | Python | aiohttp | 8083 | Daily + on-demand. Reads entity graph + summaries. |
| `briefing-agent` | Python | aiohttp | 8085 | Formatting and delivery to configured integrations. |
| `api-server` | Python | aiohttp | 8084 | REST API for web UI, seed management, integrations, Reddit, and ingestion. |
| `web-ui` | Node.js | Express | 3000 | Operator interface. SPA with Auth0 login, pipeline QA, entity explorer. |

### Service communication

- **Redis pub/sub channels:** `raw_items`, `scored_items`, `seed_updated`, `new_briefing`, `crawl_tasks`
- **Synchronous:** `api-server` proxies `POST /api/synthesise` to `synthesis-agent`; all services call `llm-gateway` for LLM operations
- **On-demand crawl tasks:** Synthesis Agent publishes to `crawl_tasks` when it detects coverage gaps

### Shared infrastructure

- **MongoDB 7** — single instance, database `mc`, owned by the Crawler service's dependency declaration
- **Redis** — pub/sub message bus + deduplication cache, also owned by Crawler

---

## 8. LLM Gateway

All LLM calls are routed through a centralised gateway service (`llm-gateway`, port 8082). No agent calls OpenAI or Anthropic directly.

### Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /v1/embed` | Generate embeddings (OpenAI text-embedding-3-small) |
| `POST /v1/chat` | Chat completions with tier-based model routing |
| `GET /v1/costs` | Cost tracking per tier/day |

### Model routing

| Tier | Model | Provider | Use |
|------|-------|----------|-----|
| `embed` | text-embedding-3-small | OpenAI | T2 semantic scoring, seed embeddings |
| `t3` | claude-3-5-haiku-20241022 | Anthropic | Fast classification gate |
| `t4` | claude-sonnet-4-20250514 | Anthropic | Deep entity analysis |
| `synthesis` | claude-sonnet-4-20250514 | Anthropic | Cross-signal reasoning |
| `discovery` | claude-sonnet-4-20250514 | Anthropic | Source proposal generation |
| `autocomplete` | claude-3-5-haiku-20241022 | Anthropic | Reddit reply suggestions |

### Features

- **Provider fallback:** Anthropic ↔ OpenAI automatic failover
- **Cost tracking:** Per-tier daily spend, monthly budget enforcement (default $100 USD)
- **Rate limiting:** Anthropic 300 RPM, OpenAI 500 RPM with queue management
- **Vault integration:** Fetches API keys from the encrypted vault (`api_keys` collection) with 5-minute in-memory cache — no keys in environment variables after initial bootstrap

---

## 9. Web UI

The web UI is a full-featured operator interface — not just a configuration panel. It serves as the primary surface for pipeline QA, entity exploration, briefing consumption, and system management.

**Stack:** Node.js Express server serving a vanilla HTML5/CSS/JS SPA. No build step. Dark theme UI with Auth0 OAuth login.

### Tabs

**Home** — Landing page with hero section, pipeline visualization, feature grid, and Auth0 login card (Google/GitHub/email providers).

**Briefings** — Latest synthesis narratives with item counts and entity changes. Feedback controls (useful / noise / interesting).

**Pipeline** — Full pipeline QA view:
- Filter bar: band (PASS/WEAK/DROP), analyzed status, source type
- Score meters showing T2 cosine similarity per item
- T4 analysis sections showing extracted entities and summaries
- Entity chips linking to the entity view
- Reddit reply panels for Reddit-sourced items (see [Section 11](#11-reddit-engagement))

**Entities** — Knowledge graph explorer:
- Type summary (tool, company, concept, framework, platform)
- Strength indicators per entity
- Relationship display with depth-1 traversal
- Click-through to source items

**Sources** — Source manager with health scores, status toggles, and pending proposals from Discovery Agent.

**Settings** — API key vault (encrypted at rest), integration configuration, budget ceiling, Reddit credentials.

### Auth

Auth0 SPA SDK with JWT RS256 validation. The Express server provides an `/auth/config` endpoint for dynamic Auth0 configuration. All `/api/*` routes require a valid JWT; `/healthz` and `/internal/*` are bypassed. A dev-mode fallback allows anonymous access when `AUTH0_DOMAIN` is not set.

### Data export

- `GET /rss` — Public RSS feed of briefings (XML)
- CSV export available for items, entities, and sources via the API

---

## 10. Integrations

Six notification channels, all configurable via the web UI Settings tab. The Briefing Agent dispatches to all enabled integrations when a new briefing is generated.

### Supported types

| Type | Transport | Config fields | Supported events |
|------|-----------|---------------|------------------|
| **Slack** | Webhook | `webhook_url`, `channel` (opt) | briefing, alert, entity_change |
| **Discord** | Webhook | `webhook_url` | briefing, alert, entity_change |
| **Email** | Resend API | `api_key`, `to`, `from_address`, `schedule` | briefing |
| **Webhook** | HTTP POST | `url`, `signing_secret` (opt), `headers` (JSON) | briefing, alert, entity_change, new_item, source_proposal |
| **Notion** | API | `api_key`, `database_id` | briefing, entity_change |
| **Linear** | API | `api_key`, `team_id`, `label` (opt) | alert |

### API endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/integrations` | GET | List available types + current config status |
| `/api/integrations/{type}` | POST | Save configuration |
| `/api/integrations/{type}` | PATCH | Toggle enable/disable |
| `/api/integrations/{type}` | DELETE | Remove configuration |
| `/api/integrations/{type}/test` | POST | Send test notification |
| `/internal/integrations/{user_id}/{event}` | GET | Internal: fetch enabled configs for dispatch |

### Dispatch flow

1. Synthesis Agent completes → publishes to `new_briefing` Redis channel
2. Briefing Agent receives → formats briefing
3. For each enabled integration: decrypts config, dispatches via appropriate transport
4. Slack: rich blocks with headline + narrative + supporting signals
5. Discord: embeds with markdown
6. Webhook: JSON POST with optional HMAC-SHA256 signature
7. Email: HTML email via Resend API

All integration secrets are encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA256).

---

## 11. Reddit Engagement

In-dashboard Reddit engagement for items sourced from Reddit. Operators can reply to Reddit posts directly from the Pipeline tab without leaving the UI.

### Features

- **Reply panel** on pipeline cards for Reddit-sourced items (detected by URL pattern)
- **5 suggested response starters** — context-aware, pulling from extracted entities
- **LLM-powered autocomplete** — toggle for Haiku-generated conversational reply suggestions (no product pitches)
- **Reddit OAuth** — credentials stored encrypted (client_id, client_secret, refresh_token, username)

### API endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/reddit/credentials` | POST | Store encrypted Reddit OAuth credentials |
| `/api/reddit/reply` | POST | Post comment to Reddit (extracts thing_id from URL, exchanges refresh token) |
| `/api/reddit/autocomplete` | POST | LLM-powered reply completion via Haiku |

### Flow

1. Operator stores Reddit app credentials via Settings tab (encrypted with Fernet)
2. On pipeline card, clicks "Reply" → sees suggested starters
3. Optionally toggles autocomplete for LLM-generated reply text
4. Submits → credentials decrypted → refresh token exchanged for access token → comment posted via `oauth.reddit.com/api/comment`
5. Reply logged in `reddit_replies` collection with timestamp

---

## 12. Configuration & Secrets

### Environment variables

All services receive these via the kindling DSE manifest:

```bash
# Service-specific
PORT=<service-port>                       # Set per service
MONGO_URL=mongodb://devuser:devpass@mc-crawler-dev-mongodb:27017/mc?authSource=admin
REDIS_URL=redis://mc-crawler-dev-redis:6379/0
LLM_GATEWAY_URL=http://mc-llm-gateway-dev:8082

# LLM Gateway only
ANTHROPIC_API_KEY=                        # Bootstrap key, vault-managed after setup
OPENAI_API_KEY=

# Relevance Filter
T2_PASS_THRESHOLD=0.35
T2_WEAK_THRESHOLD=0.20

# Synthesis Agent
SYNTHESIS_INTERVAL_HOURS=24

# Discovery Agent
DISCOVERY_INTERVAL_HOURS=24
SOURCE_HEALTH_SUSPEND_THRESHOLD=0.15

# Web UI
AUTH0_DOMAIN=                             # Optional — anonymous mode if unset
AUTH0_CLIENT_ID=
AUTH0_AUDIENCE=
API_URL=http://mc-api-server-dev:8084
```

### kindling secrets

```bash
kindling secrets set ANTHROPIC_API_KEY=<key>
kindling secrets set OPENAI_API_KEY=<key>
```

Additional secrets (Reddit, integration tokens, etc.) are stored encrypted in the MongoDB vault via the web UI, not as Kubernetes secrets.

### Encryption

All sensitive credentials stored in MongoDB are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) using the `cryptography` library. The Fernet key is derived from a server-side secret. Keys are decrypted only at the moment of use and never logged.

---

## 13. Cost Model

Based on 10,000 raw items ingested per day across all sources. All LLM costs flow through the LLM Gateway's cost tracker.

| Component | Daily volume | Daily cost | Notes |
|-----------|-------------|------------|-------|
| T1 deduplication | 10,000 → ~2,000 | ~$0.00 | Redis ops only |
| T2 embeddings | 2,000 → ~200 | ~$0.04 | text-embedding-3-small |
| T3 fast classify | 200 → ~20 | ~$0.02 | claude-3-5-haiku, tiny context |
| T4 deep analysis | ~20 items | ~$0.40 | claude-sonnet-4, full content |
| Synthesis Agent | 1 run/day + ad hoc | ~$0.80–2.00 | Ad hoc via POST /api/synthesise |
| Discovery Agent | 1 run/day | ~$0.50 | claude-sonnet-4, strict token budget |
| Briefing Agent | 1–3 runs/day | ~$0.05 | Template-driven, minimal LLM |
| **Monthly total** | | **~$45–90** | Before entity cache warm-up |

After ~60 days, entity cache hits become frequent and T4 cost drops by roughly 30%. The system gets cheaper to run over time.

### Budget guard

Monthly LLM spend is tracked by the LLM Gateway in real time. Budget enforcement is configurable via `MONTHLY_LLM_BUDGET_USD` (default $100).

### Provider fallback

The LLM Gateway supports both Anthropic and OpenAI with automatic failover. Embed calls and completion calls are tracked separately for cost attribution.

---

## 14. MCP Server (Planned)

The MCP server will expose the intelligence pipeline as a set of tools that any MCP-compatible agent, IDE extension, or orchestration layer can call directly. This is the next major feature to be built.

### Planned tools

#### `get_briefing`

Returns the latest synthesised briefing, or the briefing for a specified date.

```json
{
  "name": "get_briefing",
  "description": "Get the latest market intelligence briefing for this problem space",
  "inputSchema": {
    "type": "object",
    "properties": {
      "date": {
        "type": "string",
        "description": "ISO date string. Omit for latest.",
        "format": "date"
      },
      "type": {
        "type": "string",
        "enum": ["daily", "weekly"],
        "default": "daily"
      }
    }
  }
}
```

#### `query_signals`

Triggers an on-demand synthesis run against a specific question. Returns a focused narrative rather than a general briefing.

```json
{
  "name": "query_signals",
  "description": "Ask a specific question about the current state of the market. Triggers live synthesis.",
  "inputSchema": {
    "type": "object",
    "required": ["query"],
    "properties": {
      "query": {
        "type": "string",
        "description": "e.g. 'What is the emerging sentiment around local-first developer tooling this week?'"
      },
      "lookback_days": {
        "type": "integer",
        "default": 7
      }
    }
  }
}
```

#### `get_entities`

Returns entities from the knowledge graph, queryable by name, type, or recency.

```json
{
  "name": "get_entities",
  "description": "Query the competitive landscape knowledge graph",
  "inputSchema": {
    "type": "object",
    "properties": {
      "type": {
        "type": "string",
        "enum": ["company", "tool", "concept", "framework", "platform"]
      },
      "name": {
        "type": "string",
        "description": "Fuzzy match against entity name"
      },
      "min_strength": {
        "type": "number",
        "description": "Filter to entities with strength above this threshold (0–1)"
      },
      "limit": {
        "type": "integer",
        "default": 20
      }
    }
  }
}
```

#### `get_sources`

Returns the current source list with health scores and status.

```json
{
  "name": "get_sources",
  "description": "List monitored sources with health scores and signal rates",
  "inputSchema": {
    "type": "object",
    "properties": {
      "status": {
        "type": "string",
        "enum": ["ACTIVE", "SUSPENDED", "PENDING_REVIEW"]
      }
    }
  }
}
```

#### `ingest_content`

Pipes content directly into the pipeline. Useful for agents that encounter relevant material mid-task.

```json
{
  "name": "ingest_content",
  "description": "Feed a URL or text content directly into the intelligence pipeline",
  "inputSchema": {
    "type": "object",
    "properties": {
      "url": {
        "type": "string"
      },
      "text": {
        "type": "string"
      },
      "source_label": {
        "type": "string",
        "description": "Optional label for attribution (e.g. 'private-slack-export')"
      }
    }
  }
}
```

#### `get_pipeline_stats`

Returns pipeline health metrics — item counts by band, entity counts, source health, briefing history.

```json
{
  "name": "get_pipeline_stats",
  "description": "Get pipeline health metrics and item counts by processing stage",
  "inputSchema": {
    "type": "object",
    "properties": {}
  }
}
```

### Integration

The MCP server will run as a new service in the cluster, calling the API server for data access and the synthesis agent for on-demand queries. Once deployed:

```bash
# Expose for local agent integration
kindling expose mcp-server

# Claude Code / Cursor / Windsurf: add to MCP config
{
  "mcpServers": {
    "market-consciousness": {
      "url": "http://localhost:8090/mcp"
    }
  }
}
```

---

## 15. Deployment with kindling

This project is designed to run on [kindling](https://kindling.sh) — a local Kubernetes dev environment with AI-generated CI/CD and auto-provisioned dependencies.

### Declared dependencies

In the kindling manifest (owned by the Crawler service):

```yaml
dependencies:
  - type: mongodb
    version: "7"
  - type: redis
```

### Getting started

```bash
# Install kindling
brew install kindling-sh/tap/kindling

# Bootstrap local cluster
kindling init

# Register CI runner
kindling runners -u <user> -r <owner/repo> -t <pat>

# Generate CI workflow
kindling generate -k $ANTHROPIC_API_KEY -r .

# Set bootstrap secrets
kindling secrets set ANTHROPIC_API_KEY=<key>
kindling secrets set OPENAI_API_KEY=<key>

# Deploy all services
git push origin main

# Or build + load individual services
kindling load -s <service-name> --context ./<service-dir>
```

### Why this is a meaningful kindling field test

This project exercises every major kindling capability in non-trivial ways:

- **Multi-service topology:** 10 cooperating services with real inter-service communication via Redis pub/sub
- **Shared dependencies:** MongoDB and Redis are genuinely required — not bolted on for show
- **Tight inner dev loop:** Agent prompts and relevance thresholds require rapid iteration — exactly the workflow `kindling sync` is built for
- **Secrets under load:** API keys managed through encrypted vault, multiple providers
- **Path to production:** The system is designed to run indefinitely — `kindling snapshot` and a real cluster deployment, not a demo that's torn down after the talk

Every friction point encountered during development is a bug report or feature request in disguise.

---

## 16. Development Guide

### Local setup

```bash
# Clone and enter repo
git clone <repo>
cd market-intelligence

# Bootstrap cluster (first time only)
kindling init

# Set required secrets
kindling secrets set ANTHROPIC_API_KEY=sk-ant-...
kindling secrets set OPENAI_API_KEY=sk-...

# Deploy all services
git push origin main

# Or build individual services
kindling load -s mc-api-server --context ./api-server
kindling load -s mc-web-ui --context ./web-ui

# Check status
kindling status

# Watch logs
kindling logs
```

### Onboarding (first run)

Open the web UI at `http://localhost:3000`. Log in via Auth0, then navigate to the seed configuration to define your problem space. The seed accepts:

1. A problem description
2. A target user description
3. Tags and example content URLs

Saving triggers the initial source bootstrap and kicks off the first crawl cycle.

### Iterating on agent prompts

```bash
# Build and reload a single service
kindling load -s mc-entity-extractor --context ./entity-extractor

# Or use sync for live reloading
kindling sync -d mc-entity-extractor --restart
```

### Approving new sources

```bash
# Via web UI: Sources tab → Pending proposals → Approve/Dismiss

# Via API (fallback)
curl http://localhost:8084/api/proposals?status=PENDING_REVIEW
curl -X PATCH http://localhost:8084/api/proposals/<id> \
  -H "Content-Type: application/json" \
  -d '{"status": "APPROVED"}'
```

### Triggering an on-demand synthesis

```bash
curl -X POST http://localhost:8084/api/synthesise \
  -H "Content-Type: application/json" \
  -d '{"query": "What are the emerging patterns in local dev tooling this week?"}'
```

### API server endpoints reference

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/seed` | GET/POST | Current problem-space seed |
| `/api/seed/versions` | GET | Seed version history |
| `/api/seed/revert` | POST | Revert to previous seed version |
| `/api/stats` | GET | Pipeline health metrics |
| `/api/items` | GET | Raw items with band/analyzed filters |
| `/api/entities` | GET | Entity list with type filter |
| `/api/entities/{id}` | GET | Entity detail with relationships |
| `/api/briefings` | GET | Briefing list |
| `/api/briefings/{id}` | GET | Briefing detail |
| `/api/sources` | GET | Source list with health scores |
| `/api/sources/{id}` | PATCH | Toggle source status |
| `/api/proposals` | GET | Discovery Agent proposals |
| `/api/proposals/{id}` | PATCH | Approve/dismiss proposal |
| `/api/feedback` | POST | Item feedback (useful/noise/interesting) |
| `/api/synthesise` | POST | On-demand synthesis |
| `/api/keys` | GET/POST/DELETE | Encrypted API key vault |
| `/api/integrations` | GET | Integration types and status |
| `/api/integrations/{type}` | POST/PATCH/DELETE | Manage integration config |
| `/api/integrations/{type}/test` | POST | Test notification |
| `/api/reddit/credentials` | POST | Store Reddit OAuth credentials |
| `/api/reddit/reply` | POST | Post Reddit reply |
| `/api/reddit/autocomplete` | POST | LLM-powered reply suggestion |
| `/rss` | GET | RSS feed of briefings |
| `/healthz` | GET | Health check |

---

## Appendix: Agent Prompt Sketches

These are starting points. Expect to iterate significantly based on what the system surfaces in the first two weeks.

### Discovery Agent — source proposal prompt

```
You are the discovery agent for a market intelligence system monitoring the following problem space:

{problem_space_description}

You have been given a set of WEAK-relevance items from the past week — content that scored in the 
0.20–0.35 relevance band and may indicate adjacent spaces worth monitoring more closely.

WEAK items:
{weak_items_summary}

Current source list:
{active_sources}

Your tasks:
1. Identify any patterns in the weak items that suggest we are missing a community or outlet
2. Propose up to 5 new sources to monitor, with a one-sentence rationale for each
3. Flag any existing sources that appear to be declining in relevance
4. Note any new entity types appearing that we haven't seen before

Be specific about source URLs. Mark any suggestions you are uncertain about as NEEDS_VERIFICATION.
```

### Synthesis Agent — daily briefing prompt

```
You are synthesising market intelligence for a team monitoring the following problem space:

{problem_space_description}

Today's signals (already filtered and summarised):
{item_summaries}

Entity context (relevant entities and recent changes):
{entity_context}

Write a briefing in the style of a thoughtful analyst — not a bullet-point summary but a 
narrative interpretation. What does today's signal set suggest about the state of the space?
What patterns are forming? What would you want to watch more closely?

Be willing to speculate, but label speculation clearly. 
Aim for 3–5 paragraphs. Quality over coverage.
```

### Synthesis Agent — ad hoc query prompt

```
You are answering a specific question about a market space on behalf of the system operator.

Problem space:
{problem_space_description}

Operator question:
{query}

Relevant signals from the last {lookback_days} days:
{item_summaries}

Relevant entity context:
{entity_context}

Answer the question directly and specifically, using the signal data above as evidence.
Be willing to say when the data is insufficient to answer confidently — don't extrapolate 
beyond what the signals support. Label any speculation clearly.
Aim for 2–3 focused paragraphs.
```

---

*Built to run on [kindling](https://kindling.sh). Apache 2.0.*
