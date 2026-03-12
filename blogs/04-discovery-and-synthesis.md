# Part 4: The Slow Loop

*Building the discovery and synthesis agents — the parts that make this more than a feed reader.*

---

Everything built so far is a fast loop. Content comes in, gets filtered, gets analyzed, entities get extracted. It runs continuously, it's cheap, and it's necessary. But it's not interesting. A well-configured RSS reader with some NLP bolted on could do roughly the same thing.

The discovery agent and synthesis agent are the slow loop — the parts that make this system genuinely different. The discovery agent asks: *what should we be watching that we aren't yet?* The synthesis agent asks: *what does the pattern across today's signals actually mean?*

These agents run on a completely different cadence. Discovery runs weekly. Synthesis runs daily. They're expensive per run but they run rarely. And they operate on summaries-of-summaries, not raw content — the entity graph does the heavy lifting of compression.

## The Discovery Agent

This is the most strategically important agent in the system, and it does the least volume of work. One run per week. One prompt. Maybe $5–15 in LLM costs. But it's the agent that determines whether the system stays relevant over time or slowly goes stale.

### The problem it solves

Every monitoring system eventually monitors the wrong things. Communities migrate. New projects emerge. The landscape shifts. If your source list is static, you're monitoring where the conversation *was*, not where it *is*.

The discovery agent looks at the WEAK band — items that scored 0.45–0.72 at T2. Not obviously relevant. Not obviously irrelevant. The interesting middle ground where new signals first appear.

### The prompt

```python
async def run_discovery():
    weak_items = await get_weak_items_since_last_run()
    active_sources = await get_active_sources()
    entity_summary = await get_entity_graph_summary()

    response = await llm_gateway.chat(
        tier="discovery",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are the discovery agent for a market intelligence system.\n\n"
                    f"Problem space: {problem_space_description}\n\n"
                    "Your job is to identify gaps in our monitoring. "
                    "All proposals go to operator review — be specific, not cautious."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"WEAK-band items from the past week:\n{format_weak_items(weak_items)}\n\n"
                    f"Current active sources:\n{format_sources(active_sources)}\n\n"
                    f"Entity graph summary:\n{entity_summary}\n\n"
                    "Tasks:\n"
                    "1. Identify patterns in weak items suggesting communities we're missing\n"
                    "2. Propose up to 5 new sources with URLs and rationale\n"
                    "3. Flag existing sources that may be declining in relevance\n"
                    "4. Note new entity types we haven't tracked before\n"
                    "5. Given everything you've seen, what venues likely exist that we aren't monitoring?"
                ),
            },
        ],
    )
    
    proposals = parse_discovery_output(response)
    for proposal in proposals:
        await save_source_proposal(proposal)  # status = PENDING_REVIEW
```

### The critical constraint: no autonomous expansion

The discovery agent **never** activates a source on its own. Every proposal goes into a `PENDING_REVIEW` queue. The operator approves, dismisses, or marks "watch but don't surface."

This is a design choice, not a limitation. An agent that autonomously expands its own monitoring scope is an agent that can silently drift off-target. The discovery agent is an advisor. The operator decides.

### Source health scoring

While the discovery agent proposes new sources, the system also prunes old ones. Every source carries a health score:

```python
def compute_health_score(source):
    recency = days_since_last_pass_item(source)
    relevance_rate = pass_count_30d(source) / total_count_30d(source)
    signal_to_noise = pass_count_30d(source) / max(drop_count_30d(source), 1)
    
    # Weighted average
    return (
        0.3 * max(0, 1 - recency / 30) +   # decays over 30 days
        0.4 * relevance_rate +
        0.3 * min(signal_to_noise, 1)
    )
```

Sources below 0.15 health are automatically suspended. Sources that recover — maybe a newsletter starts covering a relevant topic again — are reactivated when entity references surface from other sources.

Dead sources are themselves a signal. If a previously active community goes quiet, the synthesis agent sees that in the entity graph and can interpret it.

## The Synthesis Agent

If the discovery agent asks "what should we watch?", the synthesis agent asks "what does it mean?"

This is the only agent that reasons across the full entity graph. It doesn't see raw items. It sees summaries-of-summaries: the day's T4 outputs, entity context, and relationship changes. The compression is massive — 10,000 ingested items become maybe 2,000 words of synthesized context.

### Daily synthesis

```python
async def run_daily_synthesis():
    # Collect today's T4 summaries
    item_summaries = await get_todays_t4_summaries()
    
    # Get entity context — entities that were updated today + neighbors
    entity_context = await get_relevant_entity_context(item_summaries)
    
    # What changed in the graph?
    graph_changes = await get_graph_changes_since_yesterday()

    response = await llm_gateway.chat(
        tier="synthesis",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are synthesising market intelligence.\n\n"
                    f"Problem space: {problem_space_description}\n\n"
                    "Write a briefing: what does today's signal set suggest about the state "
                    "of this space? What patterns are forming? What would you watch more closely?\n\n"
                    "Be willing to speculate, but label speculation clearly. "
                    "Quality over coverage. 3-5 paragraphs."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Today's signals ({len(item_summaries)} items):\n"
                    f"{format_summaries(item_summaries)}\n\n"
                    f"Entity context:\n{format_entity_context(entity_context)}\n\n"
                    f"Graph changes:\n{format_graph_changes(graph_changes)}"
                ),
            },
        ],
    )
    
    briefing_id = await save_briefing(response, item_summaries, entity_context)
    return briefing_id
```

### Targeted crawl spawning

The synthesis agent can spawn targeted crawl tasks. If it identifies a gap — "I'm seeing signals about this entity but I don't have enough context to say something meaningful" — it publishes a request to the `crawl_tasks` Redis channel.

```python
# Synthesis agent can request targeted information
redis_client.publish("crawl_tasks", json.dumps({
    "type": "targeted",
    "query": "Dagger CI engine kubernetes adoption enterprise",
    "reason": "Entity 'Dagger' appeared in 3 signals this week but no substantive coverage in our sources",
    "requested_by": "synthesis-agent"
}))
```

The crawler picks this up and runs a targeted search. The results flow back through the normal pipeline — T1, T2, T3, T4 — but with `priority: high` so they're processed within the current synthesis cycle if possible.

This is the feedback arrow in the architecture diagram. The slow loop can task the fast loop. It's not a pipeline — it's a system with cycles.

### On-demand synthesis

Besides the daily scheduled run, the synthesis agent exposes an endpoint for on-demand queries routed through the api-server:

```
POST /synthesise
{"query": "What are the emerging patterns in local dev tooling this week?"}
```

This runs the same synthesis prompt but with the query as additional context. Useful when the operator has a specific question the daily briefing didn't address.

## The DSE Additions

```yaml
---
# Discovery Agent
apiVersion: apps.example.com/v1alpha1
kind: DevStagingEnvironment
metadata:
  name: mc-discovery-agent-dev
spec:
  deployment:
    image: mc-discovery-agent:dev
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

---
# Synthesis Agent
apiVersion: apps.example.com/v1alpha1
kind: DevStagingEnvironment
metadata:
  name: mc-synthesis-agent-dev
spec:
  deployment:
    image: mc-synthesis-agent:dev
    replicas: 1
    port: 8083
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
    port: 8083
    type: ClusterIP
```

## Iterating on Prompts

This is where `kindling sync` earns its keep more than anywhere else in the project. The discovery and synthesis prompts need constant iteration. The first version of the synthesis prompt produced generic summaries. The second version added "label speculation clearly" and got better. The third version added graph change context and got significantly better.

Each iteration cycle: edit the prompt template, save, `kindling sync` picks it up, the agent restarts, trigger an on-demand synthesis to see the result. Under a second per cycle. On any other setup, this would be a rebuild-and-redeploy cycle measured in minutes.

```bash
kindling sync -d mc-synthesis-agent-dev --restart
```

## What's Working at the End of Part 4

The system now has both loops running:

**Fast loop** (continuous): crawler → T1 dedup → T2 embedding → T3 classify → T4 analyze → entity extraction

**Slow loop**: discovery agent (weekly, proposes new sources) + synthesis agent (daily, interprets signals)

Six services deployed: crawler, relevance-filter, llm-gateway, entity-extractor, discovery-agent, synthesis-agent.

The system is producing daily briefings. They're documents in MongoDB right now — no delivery mechanism, no UI, no feedback loop. That's Part 5.

## What I Hit

- **The synthesis prompt needs entity context or it's useless.** The first version only got item summaries and produced generic "here's what happened today" output. Adding entity context and graph changes was the difference between a summary and an interpretation.
- **Discovery agent hallucinations are expected.** It proposed monitoring a subreddit that doesn't exist. It proposed a newsletter that stopped publishing in 2023. This is fine — the verification queue is the fix. The Crawler checks proposed URLs for existence and recent activity before anything reaches the operator.
- **Weekly discovery is too infrequent at the start.** In the first two weeks, when the source list is thin, the discovery agent should run daily. I added a `discovery_cadence` config that starts at "daily" and shifts to "weekly" after the source count exceeds a threshold.
- **The LLM gateway's cost tracking is the single most useful thing I've built.** I can see, per agent, per tier, exactly where money is going. The synthesis agent costs $0.80/day. The discovery agent costs $2/run. T3 costs almost nothing. Without centralized tracking, this would be invisible.

---

*Next up: Part 5 — Making It Legible. The API server, web UI, briefing agent, and the feedback loop that makes the system learn from its operator.*
