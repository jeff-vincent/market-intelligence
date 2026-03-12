# Part 6: Shipping It

*From `kindling snapshot` to production, the cost reality check, and what building a nine-service system on your laptop actually teaches you.*

---

The system works locally. Nine services, all running in Kind, all talking to each other through Redis pub/sub, all storing data in MongoDB. The entity graph has structure. The daily briefings are useful. The feedback loop is calibrating the system toward what I actually care about.

Now it needs to run somewhere that isn't my laptop.

## The Snapshot

`kindling snapshot` captures the full state of a running dev environment and produces a production-ready manifest. It's the bridge between "works on my machine" and "works on a server."

```bash
kindling snapshot --name market-consciousness --output ./deploy/
```

This produces:

- Kubernetes manifests for all nine services
- ConfigMaps for environment variables
- Secret references (not values — those are set separately in production)
- Service and Ingress definitions

The manifests are vanilla Kubernetes. No kindling-specific CRDs, no operator dependency in production. The operator is a dev tool — production is just Kubernetes.

### What the snapshot captures

Every DSE object in the cluster becomes a Deployment + Service pair. The operator resolves:

- Image references: `mc-crawler:dev` → the full registry path for production
- Environment variables: dev-specific values are flagged for replacement
- Resource requests: the snapshot includes the actual resource usage observed during dev, as a starting point for production resource requests
- Health checks: carried over directly
- Replica counts: defaults to 1, overridable in the output

### What it doesn't capture

- MongoDB data. The collections are ephemeral in dev. In production, Atlas handles persistence and backups.
- Redis state. Pub/sub channels are transient by design.
- LLM API keys. These are set via `kindling secrets` in dev and via your production secret management in prod.

## Production Deployment

I'm deploying to a DigitalOcean Kubernetes cluster. Three nodes, $36/month. The manifests from `kindling snapshot` deploy with `kubectl apply`:

```bash
# Set production secrets first
kubectl create secret generic mc-secrets \
  --from-literal=OPENAI_API_KEY=$OPENAI_API_KEY \
  --from-literal=ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY

# Deploy
kubectl apply -f ./deploy/
```

### Production topology

```
┌─────────────────────────────────────────────────┐
│  DigitalOcean Kubernetes (3 nodes, $36/mo)      │
│                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐      │
│  │ crawler  │  │ relevance│  │ entity   │      │
│  │          │  │ filter   │  │ extractor│      │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘      │
│       │              │              │            │
│  ┌────▼──────────────▼──────────────▼────┐      │
│  │          Redis (managed)              │      │
│  └────┬──────────────┬───────────────────┘      │
│       │              │                           │
│  ┌────▼─────┐  ┌─────▼────┐  ┌──────────┐      │
│  │ discovery│  │synthesis │  │ briefing │      │
│  │ agent    │  │ agent    │  │ agent    │      │
│  └──────────┘  └──────────┘  └────┬─────┘      │
│                                    │            │
│  ┌──────────┐  ┌──────────┐  ┌────▼─────┐      │
│  │  llm     │  │ api      │  │  web     │      │
│  │ gateway  │  │ server   │  │  ui      │      │
│  └──────────┘  └──────────┘  └──────────┘      │
│                                                  │
│  ┌───────────────────────────────────────┐      │
│  │  MongoDB Atlas + Vector Search (managed)  │      │
│  └───────────────────────────────────────┘      │
└─────────────────────────────────────────────────┘
```

In production, MongoDB is on Atlas (free tier to start, M10 when it matters) and Redis is a managed service (DigitalOcean Managed Redis). In dev, they were in-cluster pods provisioned by the kindling operator. The connection string changes from `mongodb://mc-crawler-dev-mongodb:27017/mc` to an Atlas URI; the application code doesn't change at all.

### Production secrets

```bash
kindling secrets set OPENAI_API_KEY=sk-...
```

In dev, this creates a Kubernetes secret in the Kind cluster. In production, the same keys go into whatever secret management you use. The LLM gateway is the only service that needs API keys — every other service talks to the gateway.

This is the payoff of the gateway pattern. Instead of nine services each needing an OpenAI key, one service needs keys. The attack surface for credential exposure is one pod, not nine.

## The Cost Reality Check

After 30 days of production operation, here are the actual numbers:

### Infrastructure

| Component | Monthly Cost |
|---|---|
| DigitalOcean K8s (3 nodes) | $36 |
| MongoDB Atlas (M10) | $57 |
| Managed Redis | $15 |
| **Infrastructure total** | **$108** |

### LLM costs (via the gateway's cost tracker)

| Tier/Agent | Daily | Monthly |
|---|---|---|
| T2 (embeddings) | $0.04 | $1.20 |
| T3 (cheap classify) | $0.02 | $0.60 |
| T4 (deep analysis) | $0.40 | $12.00 |
| Synthesis agent | $0.80 | $24.00 |
| Discovery agent | $2/run | $8.00 |
| On-demand queries | varies | ~$5.00 |
| **LLM total** | | **~$51** |

### Total: ~$159/month

The original spec estimated $45–90 for LLM costs. Actual was $51. Infrastructure is $108 — Atlas is pricier than a managed Postgres would be, but Atlas Vector Search eliminates the need for pgvector entirely. No extension management, no index rebuild scripts, no flaky vector indexes in dev. Total system cost is $159/month for a market intelligence system that monitors ~200 sources, processes ~10,000 items/day, maintains a knowledge graph of ~2,000 entities, and produces a daily strategic briefing.

For context, a single Bloomberg Terminal costs $24,000/year. Obviously a different product entirely, but the point is: this system costs the same as a nice dinner, per month.

### Where the money actually goes

The LLM gateway's cost tracking made this visible immediately:

- **The synthesis agent is the biggest LLM cost.** $0.80/day, $24/month. This is expected — it runs the most expensive model (Claude Sonnet) on the most context.
- **T4 analysis is the second.** $12/month. About 50 items/day get full analysis at $0.008 each.
- **T2 embeddings are negligible.** $1.20/month for 10,000 embeddings/day. OpenAI's text-embedding-3-small is absurdly cheap.
- **T3 classification is almost free.** $0.60/month. Haiku at these volumes is essentially a rounding error.

The tiered cost filter works. 10,000 items enter at T1. ~3,000 pass T2. ~800 pass T3. ~50 get full T4 analysis. The funnel is 200:1 from input to expensive processing.

## What Kindling Got Right

Building this project, I was the user of my own tool for the first time on something genuinely complex. Some things worked better than I expected:

**`kindling sync` for prompt iteration is transformative.** The discovery and synthesis agents went through 15+ prompt revisions each. Each revision was: edit file → save → agent restarts → test. Sub-second feedback loops for LLM prompt engineering. This alone justified the tool for me.

**Dependency auto-injection eliminated an entire class of bugs.** `MONGO_URL` and `REDIS_URL` are injected automatically when you declare mongodb and redis dependencies. I never once had a misconfigured connection string during development.

**`kindling status` gave me a single view of nine services.** Without it, I would have been running `kubectl get pods` repeatedly. With it, I could see at a glance which services were running, which had crashed, and which were still building.

**The snapshot-to-production path worked on the first try.** The manifests it produced needed minor edits (Atlas connection string, resource limits tuning), but the structure was correct. I didn't have to rewrite any Kubernetes YAML.

## What Kindling Needs to Improve

**No native cron scheduling.** The discovery agent runs weekly, the synthesis agent runs daily. I had to add cron scheduling inside each service. Kindling could support `spec.schedule` for periodic workloads.

**`kindling logs` should support multiple services.** Right now it tails one service at a time. With nine services, I wanted `kindling logs --all` or at least `kindling logs mc-crawler-dev mc-llm-gateway-dev`. I was constantly switching between log streams.

**The snapshot should handle managed service migration.** It outputs the dev `MONGO_URL` (pointing to the in-cluster MongoDB), and I manually replace it with the Atlas connection string. The snapshot could ask: "This service uses mongodb — do you want to use Atlas in production?" and template the URL.

## What This Project Taught Me

### On building multi-agent systems

The agents don't need to be smart. They need to be specialized. The discovery agent does one thing. The synthesis agent does one thing. The entity extractor does one thing. Each is simple enough to fit in your head. The intelligence emerges from the interactions and the shared knowledge graph — not from any individual agent.

### On cost management

LLM costs are predictable if you engineer for predictability. The tiered filter is the key insight: don't send everything to the expensive model. Build a funnel. Track costs per tier. Set budgets. The LLM gateway made all of this visible and controllable from day one.

### On the feedback loop

The system got dramatically better after a week of operator feedback. Not because the models improved — because the system learned what "relevant" means for *this operator* in *this domain*. The problem vector shifted. Source health calibrated. Entity strength stabilized. The feedback loop is the product.

### On local-first development

Nine services, running locally, with sub-second file sync and instant restarts. I didn't touch a CI pipeline until I was ready to deploy. I didn't wait for builds. I didn't debug infrastructure. I built the thing, and then I shipped it. That's what kindling is for.

---

## The Full Architecture

At the end of this series, here's what's running:

| Service | Role | Cadence | LLM Usage |
|---|---|---|---|
| crawler | Fetch content from sources | Continuous | None |
| relevance-filter | T1 dedup + T2 embedding + T3 classify | Continuous | Embedding + Haiku |
| entity-extractor | T4 analysis + knowledge graph | Continuous | Sonnet (filtered) |
| llm-gateway | Centralized LLM routing + cost tracking | On demand | Routes all LLM calls |
| discovery-agent | Find new sources | Weekly | Sonnet (strict budget) |
| synthesis-agent | Daily briefing + interpretation | Daily | Sonnet |
| briefing-agent | Format + deliver briefings | Daily | None |
| api-server | REST API for UI | On demand | None |
| web-ui | Operator dashboard | On demand | None |

Nine services. Two databases. One operator. $159/month.

The code is at [repo link]. The kindling DSE files are in `.kindling/`. Run `kindling deploy -f .kindling/dev-environment.yaml` and the whole thing comes up.

---

*This series documented the build process for Market Consciousness, a multi-agent market intelligence system built entirely on kindling. If you're building something similar — multi-service, agent-based, needs to actually run somewhere — kindling might save you a lot of plumbing.*
