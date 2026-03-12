# Building Market Consciousness — Blog Series

A 6-part series documenting the build of a production multi-agent market intelligence system, from architecture to deployment. Each post covers a real build session, not a tutorial written after the fact.

## Series

| # | Title | Services Built | What It Proves |
|---|-------|---------------|----------------|
| 1 | [The Architecture Nobody Asked For](01-architecture.md) | None (design only) | Why multi-agent systems need real infrastructure, not notebooks |
| 2 | [The Cheap Stuff First](02-crawler-and-relevance.md) | `crawler`, `relevance-filter`, `llm-gateway` | Ingestion pipeline, Redis pub/sub, tiered cost filtering |
| 3 | [Teaching a System to Remember](03-entity-extraction.md) | `entity-extractor` | Knowledge graph, spaCy NER, entity caching with TTLs |
| 4 | [The Slow Loop](04-discovery-and-synthesis.md) | `discovery-agent`, `synthesis-agent` | Strategic reasoning, source health, cross-signal synthesis |
| 5 | [Making It Legible](05-ui-and-feedback.md) | `api-server`, `web-ui`, `briefing-agent` | Operator feedback loop, the "newspaper" interface |
| 6 | [Running It For Real](06-production.md) | None (ops only) | kindling snapshot → production, cost tracking, lessons learned |

## Conventions

- Each post is written as the build happens, not retroactively
- Code snippets are from the actual codebase, not simplified examples
- Mistakes and dead ends are documented — this isn't a highlight reel
- Each post ends with the system in a deployable state for that stage
