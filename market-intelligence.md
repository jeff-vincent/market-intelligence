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
8. [Configuration & Secrets](#8-configuration--secrets)
9. [Cost Model](#9-cost-model)
10. [Web UI](#10-web-ui)
11. [Deployment with kindling](#11-deployment-with-kindling)
12. [Development Guide](#12-development-guide)

---

## 1. Overview

Market Consciousness is a continuously running multi-agent system that builds and maintains a living model of a market or problem space. Unlike conventional competitive trackers that monitor a fixed list of known competitors, it is designed to find signals you didn't know to look for — emerging communities, weak early indicators, adjacent problem solvers, and ecosystem shifts before they become mainstream.

### What it is not

- A keyword alert system (Mention, Google Alerts)
- A competitor monitor with a hardcoded source list
- A scraper that dumps raw content at you

### What it is

A system that maintains a **map of where your problem space has a presence** — and continuously redraws that map based on what it finds. The source list is itself a living artifact. By the time you've hardcoded a source list, it's already partially stale.

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

Agents are not a linear pipeline. They communicate asynchronously via Kafka topics and can task each other mid-cycle. This is what distinguishes the system from an ETL job with LLM calls bolted on.

```
                         ┌──────────────┐
                    ┌───▶│   Crawler    │◀──── on-demand tasks
                    │    └──────┬───────┘
                    │           │ raw_items topic
                    │           ▼
                    │    ┌──────────────┐
                    │    │  Relevance   │
                    │    │   Filter     │
                    │    └──────┬───────┘
                    │           │ scored_items topic
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
                    │     │ entity_tasks topic
                    │     ▼
                    │  ┌──────────┐
                    └──│Synthesis │──── spawns targeted crawls
                       │  Agent   │
                       └──┬───────┘
                          │ synthesis_requests topic
                          ▼
                       ┌──────────┐
                       │Briefing  │──── Slack / email / web UI
                       │  Agent   │
                       └──────────┘
```

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
**Trigger:** New `raw_items` records (event-driven via Kafka)  
**LLM calls:** Embeddings only (~$0.002 per 1,000 items)

Embeds each item and scores it via cosine similarity against the problem-space embedding vector stored in pgvector. Classifies into three bands:

| Band | Score | Action |
|------|-------|--------|
| PASS | > 0.72 | Forward to Entity Extractor |
| WEAK | 0.45 – 0.72 | Queue for Discovery Agent's next slow-loop run |
| DROP | < 0.45 | Discard. Decrement source health score. |

Thresholds are configurable and auto-adjust under budget pressure. If projected monthly spend exceeds the configured ceiling, the PASS threshold is raised automatically, reducing downstream volume.

The problem-space vector is updated incrementally as the operator marks items useful or noise. The system gets better calibrated the more you use it.

---

### 3.3 Entity Extractor

**Role:** Knowledge graph maintenance  
**Loop:** Fast  
**Trigger:** PASS-scored items from Relevance Filter  
**LLM calls:** Small/fast model (Haiku or GPT-4o-mini), short context windows. ~$0.01–0.05 per item at T3. Full model for T4 on chunk-filtered content.

Runs the T3 and T4 filter stages (see [Section 4](#4-tiered-cost-filter)). For items that pass T3, pulls the highest-relevance content chunks, sends to a full model for entity extraction and relationship inference.

Writes entity records and relationship edges to Postgres. Maintains entity summaries with staleness TTLs — if an entity reappears within its TTL with no new material, the cached summary is used and T4 is skipped.

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

All proposals require operator approval before becoming active. The Discovery Agent never autonomously expands the source list.

---

### 3.5 Synthesis Agent

**Role:** Cross-signal reasoning  
**Loop:** Slow  
**Trigger:** Daily scheduled run, or on-demand via API  
**LLM calls:** Full model. Runs on summaries-of-summaries, never raw content. ~$1–3 per daily run.

The only agent that reasons *across* the full entity graph. Takes the day's T4-processed item summaries plus relevant entity context and asks: *what's the narrative here?*

Not a summariser — an interpreter. Allowed to be speculative and must say so. Example output:

> "Three independent signals this week suggest the dev tools market is bifurcating around local-first vs. cloud-native tooling philosophies. The volume of job postings for 'platform engineering' roles has increased 40% in the entity graph over 30 days, coinciding with two new open-source projects in the space. This may indicate the problem is being widely recognised before a dominant solution has emerged — worth watching."

Can spawn targeted Crawler tasks if it identifies context gaps ("I need more on this entity before I can say something meaningful").

---

### 3.6 Briefing Agent

**Role:** Delivery and feedback loop  
**Loop:** Post-synthesis  
**Trigger:** Synthesis Agent completion; web UI requests  
**LLM calls:** Minimal — formatting only. Near-zero cost.

Formats and delivers briefings. Tracks what the operator has already seen so content is never repeated. Records feedback signals back to Postgres.

**Delivery channels:** Slack webhook, email (SMTP), web UI API.

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

Every item that passes T1 is embedded and scored against the problem-space vector in pgvector. Three output bands: PASS, WEAK, DROP (thresholds defined above in 3.2).

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

PostgreSQL with pgvector extension. All timestamps UTC.

### 6.1 `raw_items`

```sql
CREATE TABLE raw_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    url             TEXT NOT NULL UNIQUE,
    url_hash        BYTEA NOT NULL,           -- SHA-256 first 16 bytes, indexed
    source_id       UUID REFERENCES sources(id),
    title           TEXT,
    excerpt         TEXT,                     -- first 500 chars of body
    raw_body        TEXT,                     -- full content, TTL-expired after 30 days
    embedding       VECTOR(1536),             -- T2 embedding
    t2_score        FLOAT,                    -- cosine similarity vs problem-space vector
    t2_band         TEXT,                     -- PASS | WEAK | DROP
    t3_pass         BOOLEAN,                  -- null until T3 runs
    t3_reason       TEXT,                     -- one-line classifier output
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at    TIMESTAMPTZ               -- null until T4 completes
);

CREATE INDEX ON raw_items(url_hash);
CREATE INDEX ON raw_items(t2_band) WHERE t2_band = 'PASS';
CREATE INDEX ON raw_items(ingested_at);
```

### 6.2 `entities`

```sql
CREATE TABLE entities (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                TEXT NOT NULL,
    type                TEXT NOT NULL,        -- company | person | tool | concept | community
    summary             TEXT,                 -- latest T4-generated summary
    summary_embedding   VECTOR(1536),
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    summary_ttl         TIMESTAMPTZ,          -- re-analyse after this if new items surface
    watch_level         TEXT DEFAULT 'PASSIVE', -- ACTIVE | PASSIVE | SUSPENDED
    metadata            JSONB DEFAULT '{}'    -- urls, handles, tags, operator notes
);

CREATE INDEX ON entities(type);
CREATE INDEX ON entities(watch_level);
CREATE INDEX ON entities USING ivfflat (summary_embedding vector_cosine_ops);
```

### 6.3 `entity_relationships`

```sql
CREATE TABLE entity_relationships (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_entity_id  UUID REFERENCES entities(id),
    to_entity_id    UUID REFERENCES entities(id),
    relationship    TEXT NOT NULL,            -- e.g. "competes_with", "built_by", "uses", "mentioned_with"
    strength        FLOAT DEFAULT 0.5,        -- 0–1, updated based on signal frequency
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    evidence_count  INT DEFAULT 1
);

CREATE INDEX ON entity_relationships(from_entity_id);
CREATE INDEX ON entity_relationships(to_entity_id);
```

### 6.4 `sources`

```sql
CREATE TABLE sources (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    url                 TEXT NOT NULL,
    type                TEXT NOT NULL,        -- reddit | hn | github | newsletter | discord_edge | rss | custom
    poll_interval_mins  INT DEFAULT 240,      -- auto-adjusted by health score
    health_score        FLOAT DEFAULT 0.5,    -- 0–1
    last_crawled_at     TIMESTAMPTZ,
    last_signal_at      TIMESTAMPTZ,          -- last time a PASS item was found
    status              TEXT DEFAULT 'PENDING_REVIEW', -- ACTIVE | SUSPENDED | PENDING_REVIEW
    proposed_by         TEXT NOT NULL,        -- SYSTEM | OPERATOR | DISCOVERY_AGENT
    approved_at         TIMESTAMPTZ,          -- null for pending sources
    metadata            JSONB DEFAULT '{}'
);

CREATE INDEX ON sources(status);
CREATE INDEX ON sources(last_crawled_at) WHERE status = 'ACTIVE';
```

### 6.5 `briefings`

```sql
CREATE TABLE briefings (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    type                TEXT NOT NULL,        -- daily | weekly | alert | on_demand
    narrative           TEXT NOT NULL,        -- synthesis agent output
    items_considered    INT,
    entity_ids          UUID[],
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at        TIMESTAMPTZ,
    operator_feedback   TEXT                  -- useful | noise | partial
);
```

### 6.6 `problem_space`

```sql
CREATE TABLE problem_space (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    version         INT NOT NULL DEFAULT 1,
    description     TEXT NOT NULL,            -- seed description
    embedding       VECTOR(1536) NOT NULL,    -- current problem-space vector
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    update_reason   TEXT                      -- what triggered the embedding update
);
```

---

## 7. Services

Eight services. All containerised. Declared as kindling dependencies where applicable.

| Service | Language | Scales | Responsibility |
|---------|----------|--------|----------------|
| `crawler` | Python | Horizontal | Fetch, parse, deduplicate. Stateless. |
| `relevance-filter` | Python | Horizontal | T2 embedding + scoring. Eventual consistency acceptable. |
| `entity-extractor` | Python | Vertical | T3 + T4 analysis. Maintains entity cache. |
| `discovery-agent` | Python | Single | Slow loop. Weekly cadence. Cron-triggered. |
| `synthesis-agent` | Python | Single | Daily + on-demand. Reads entity graph + summaries. |
| `briefing-agent` | Python | Single | Formatting and delivery. Slack, email, web. |
| `api-server` | Python / FastAPI | Horizontal | REST API for web UI and `/ingest` endpoint. |
| `web-ui` | TypeScript / React | Horizontal | Operator interface. |

### Service communication

- **Kafka topics:** `raw_items`, `scored_items`, `entity_tasks`, `discovery_proposals`, `synthesis_requests`
- **Synchronous:** `api-server` calls `synthesis-agent` directly for on-demand queries
- **On-demand crawl tasks:** Any agent can publish to `raw_items` with `priority: high` to trigger an immediate targeted crawl

---

## 8. Configuration & Secrets

### Environment variables

```bash
# LLM providers (at least one required)
ANTHROPIC_API_KEY=
OPENAI_API_KEY=

# Separate key for embedding calls (allows independent budget tracking)
EMBED_API_KEY=

# Which models to use at each tier
T3_MODEL=claude-haiku-4-5-20251001        # or gpt-4o-mini
T4_MODEL=claude-sonnet-4-6                 # or gpt-4o
SYNTHESIS_MODEL=claude-sonnet-4-6

# Delivery
SLACK_WEBHOOK_URL=
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASS=
BRIEFING_EMAIL=

# Optional: higher-rate GitHub crawling
GITHUB_TOKEN=

# Budget guard
MONTHLY_LLM_BUDGET_USD=100

# Filter thresholds (overridable, auto-adjust under budget pressure)
T2_PASS_THRESHOLD=0.72
T2_WEAK_THRESHOLD=0.45
```

### kindling secrets

```bash
kindling secrets set ANTHROPIC_API_KEY=<key>
kindling secrets set OPENAI_API_KEY=<key>
kindling secrets set SLACK_WEBHOOK_URL=<url>
kindling secrets set SMTP_PASS=<password>
kindling secrets set GITHUB_TOKEN=<token>
```

---

## 9. Cost Model

Based on 10,000 raw items ingested per day across all sources.

| Component | Daily volume | Daily cost | Notes |
|-----------|-------------|------------|-------|
| T1 deduplication | 10,000 → ~2,000 | ~$0.00 | Redis ops only |
| T2 embeddings | 2,000 → ~200 | ~$0.04 | ada-002 or equivalent |
| T3 fast classify | 200 → ~20 | ~$0.02 | Small model, tiny context |
| T4 deep analysis | ~20 items | ~$0.40 | Full model, chunk-filtered |
| Synthesis Agent | 1 run/day | ~$0.80 | Summaries-of-summaries |
| Discovery Agent | 1 run/week | ~$2.00/wk | Full model, strict token budget |
| Briefing Agent | 1–3 runs/day | ~$0.05 | Template-driven |
| **Monthly total** | | **~$45–90** | Before entity cache warm-up |

After ~60 days, entity cache hits become frequent and T4 cost drops by roughly 30%. The system gets cheaper to run over time.

### Budget guard

Monthly LLM spend is tracked in real time. If projected spend exceeds `MONTHLY_LLM_BUDGET_USD`:

1. T2 PASS threshold is raised (reduces downstream volume)
2. Discovery Agent run frequency is halved
3. Operator is notified via Slack

Budget is a first-class system parameter. It is consulted before any LLM call is made.

### Provider fallback

T3 and T4 agents support both Anthropic and OpenAI. Configurable routing: cheapest available, primary with fallback, or explicit per-tier assignment. Embed calls and completion calls are tracked separately for cost attribution.

---

## 10. Web UI

The UI is not a dashboard. Dashboards imply you know what you want to look at. This is a **newspaper front page** — the system decides what matters today, presents it as stories, and invites you to engage or dismiss.

### Primary view

**Top stories:** 3–5 synthesis narratives from the last 24 hours. Each has a headline, 2-sentence summary, and a "why this matters" annotation generated by the Synthesis Agent.

**Entity spotlight:** One entity the system has noticed gaining signal velocity this week.

**New sources:** Sources proposed by the Discovery Agent awaiting operator approval. Approve, dismiss, or mark "watch but don't surface."

**Weak signals:** WEAK-band items surfaced for operator review. The potentially interesting things the system isn't confident about yet. This is the most valuable surface — the Discovery Agent feeds off these.

### Feedback

Every surfaced item has a minimal feedback control: **Useful / Noise / Interesting-but-not-now.**

Feedback is written to Postgres and used to update the problem-space embedding at T2. The system learns your taste from behaviour, not configuration.

The most important UI element is the **"why did you surface this?"** explainer on every item. Without it, the system is a black box and you can't train it. With it, every dismissal is an informative signal.

### Entity graph view

Secondary view. Nodes sized by signal velocity, edges labeled by relationship type. Useful for occasional exploration: who is adjacent to whom, what concepts cluster together, where are new nodes appearing for the first time.

### Source manager

List of all active, suspended, and pending sources with health scores and last-signal timestamps. Operator can approve Discovery proposals, adjust poll cadence, or force-suspend noisy sources. The main interface for steering the system's monitoring scope.

---

## 11. Deployment with kindling

This project is designed to run on [kindling](https://kindling.sh) — a local Kubernetes dev environment with AI-generated CI/CD and auto-provisioned dependencies.

### Declared dependencies

In the kindling manifest:

```yaml
dependencies:
  - postgres       # primary store + pgvector extension
  - redis          # deduplication set, bloom filter, entity summary cache
  - kafka          # async agent-to-agent message bus
```

### Getting started

```bash
# Install kindling
brew install kindling-sh/tap/kindling

# Bootstrap local cluster
kindling init

# Register CI runner
kindling runners -u <user> -r <owner/repo> -t <pat>

# Check project readiness
kindling analyze

# Generate CI workflow
kindling generate -k $ANTHROPIC_API_KEY -r .

# Set secrets
kindling secrets set ANTHROPIC_API_KEY=<key>
kindling secrets set SLACK_WEBHOOK_URL=<url>

# Deploy
git push origin main

# Start inner dev loop
kindling sync -d market-consciousness --restart
```

### Why this is a meaningful kindling field test

This project exercises every major kindling capability in non-trivial ways:

- **Multi-service topology:** 8 cooperating services with real inter-service communication, not a monolith with internal modules
- **All three core dependencies:** Postgres, Redis, and Kafka are each genuinely required — not bolted on for show
- **Tight inner dev loop:** Agent prompts and relevance thresholds require rapid iteration — exactly the workflow `kindling sync` is built for
- **Secrets under load:** Multiple API keys with separation of concerns, tested under realistic multi-provider conditions
- **Path to production:** The system is designed to run indefinitely — `kindling snapshot` and a real cluster deployment, not a demo that's torn down after the talk

Every friction point encountered during development is a bug report or feature request in disguise.

---

## 12. Development Guide

### Local setup

```bash
# Clone and enter repo
git clone <repo>
cd market-consciousness

# Bootstrap cluster (first time only)
kindling init

# Set required secrets
kindling secrets set ANTHROPIC_API_KEY=sk-ant-...
kindling secrets set OPENAI_API_KEY=sk-...   # optional fallback

# Deploy all services
git push origin main

# Watch logs
kubectl logs -f -l app=discovery-agent
kubectl logs -f -l app=relevance-filter
```

### Onboarding (first run)

```bash
# Seed the system with your problem space description
curl -X POST http://localhost:8000/onboard \
  -H "Content-Type: application/json" \
  -d '{
    "description": "The friction between writing code locally and running it in production...",
    "user_description": "Solo developers and small teams building containerised apps...",
    "example_urls": [
      "https://example.com/relevant-post-1",
      "https://example.com/relevant-post-2",
      "https://example.com/relevant-post-3"
    ]
  }'
```

This triggers the initial source bootstrap and kicks off the first slow-loop Discovery run.

### Iterating on agent prompts

```bash
# Start inner dev loop
kindling sync -d market-consciousness --restart

# Edit prompts in services/synthesis-agent/prompts/
# Changes sync and restart the service in under a second
```

### Approving new sources

```bash
# List pending sources
curl http://localhost:8000/sources?status=PENDING_REVIEW

# Approve
curl -X PATCH http://localhost:8000/sources/<id> \
  -d '{"status": "ACTIVE"}'
```

### Ingesting content manually

```bash
# Ingest a URL
curl -X POST http://localhost:8000/ingest \
  -d '{"url": "https://some-newsletter.com/issue/42"}'

# Ingest pasted text
curl -X POST http://localhost:8000/ingest \
  -d '{"text": "...", "source_label": "private-slack-export"}'
```

### Triggering an on-demand synthesis

```bash
curl -X POST http://localhost:8000/synthesise \
  -d '{"query": "What are the emerging patterns in local dev tooling this week?"}'
```

---

## Appendix: Agent Prompt Sketches

These are starting points. Expect to iterate significantly based on what the system surfaces in the first two weeks.

### Discovery Agent — source proposal prompt

```
You are the discovery agent for a market intelligence system monitoring the following problem space:

{problem_space_description}

You have been given a set of WEAK-relevance items from the past week — content that scored in the 
0.45–0.72 relevance band and may indicate adjacent spaces worth monitoring more closely.

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

---

*Built to run on [kindling](https://kindling.sh). Apache 2.0.*
