# Skills Service

Standalone FastAPI microservice that indexes [Cursor-style skill folders](https://docs.cursor.com/context/rules-for-ai) and provides semantic search over them. Framework-agnostic — works with any domain's skills (web frameworks, DevOps patterns, data pipelines, infrastructure, etc.). Used by AI agents in the [OPL Crew](https://github.com/varkrish/opl-ai-software-team) platform to discover coding conventions, architectural patterns, and best practices at query time.

## Why Embeddings?

AI agents need to find the right skill at the right time — but they don't know the exact skill name or filename upfront. A keyword search would fail when an agent asks "how do I containerize this app" but the relevant skill is named `containerfile-generator`. Embeddings solve this by converting both the query and skill content into vector representations that capture **meaning**, not just words.

**How it works**: At startup, the service reads every `SKILL.md`, splits it into chunks, and passes each chunk through a local embedding model (`BAAI/bge-small-en-v1.5`). The resulting vectors are stored in a LlamaIndex `VectorStoreIndex`. When an agent queries "app folder structure conventions", the query is embedded into the same vector space and the closest skill chunks are returned — even if no words overlap exactly.

**Advantages over keyword/filename lookup**:

- **Intent matching** — "how to deploy this app in a container" finds the containerfile skill, the compose skill, and the scaffold skill in ranked order, without needing exact terms
- **Cross-skill discovery** — a single query can surface relevant chunks from multiple skills across different domains (e.g., querying "appointment management" returns both the data model patterns skill and the API hooks reference skill)
- **Language-agnostic queries** — agents can describe what they need in natural language rather than constructing structured filters
- **Zero-config for new skills** — drop a `SKILL.md` into the skills directory, trigger `/reload`, and it's immediately searchable by meaning — no keyword tagging required
- **Small and fast** — the `bge-small-en-v1.5` model is 33MB, runs on CPU, and indexes 30+ skills in under 5 seconds. No GPU or external API needed

**Tag filtering** is available as an optional narrowing mechanism on top of semantic search for cases where agents want to restrict results to a specific domain (e.g., `tags: ["python"]` or `tags: ["devops"]`).

## Features

- **Semantic search** over skill documents using LlamaIndex + HuggingFace embeddings (`BAAI/bge-small-en-v1.5`)
- **Tag-based filtering** via YAML frontmatter in `SKILL.md` files
- **Multi-directory indexing** — combine skills from multiple sources (e.g., general + framework-specific)
- **MCP endpoint** at `/mcp` for direct agent integration via Model Context Protocol
- **Hot-reload** in development — edit skills and query immediately
- **Cache invalidation** — content-hash-based; automatically re-indexes when skill files change

## Quick Start

### Run standalone

```bash
pip install -e .
SKILLS_BASE_DIR=./path/to/skills uvicorn src.main:app --port 8090
```

### Run with Docker/Podman

```bash
podman build -t skills-service .
podman run -p 8090:8090 -v ./skills:/app/skills:ro skills-service
```

### Run as part of OPL Crew (dev compose)

This service is a **Git submodule** of [opl_ai_mono](https://github.com/varkrish/opl-crew-mono) at `skills-service/` (sibling to `opl-ai-software-team/`). Clone the mono repo with submodules:

```bash
git clone --recurse-submodules https://github.com/varkrish/opl-crew-mono.git opl_ai_mono
# If you already cloned without submodules:
cd opl_ai_mono && git submodule update --init skills-service
```

Dev compose under `opl-ai-software-team/` builds from `SKILLS_SERVICE_DIR` (default `../skills-service`). Optional override in the mono repo `.env`:

```bash
SKILLS_SERVICE_DIR=../skills-service
```

Then:

```bash
cd opl_ai_mono/opl-ai-software-team
podman compose -f compose.dev.yaml up skills-service
```

## API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness check |
| GET | `/health/ready` | Readiness (200 when index is built, 503 otherwise) |
| GET | `/skills` | List all discovered skills with metadata |
| POST | `/query` | Semantic search: `{"query": "...", "top_k": 3, "tags": ["python"]}` |
| POST | `/reload` | Trigger background re-index (returns 202) |
| * | `/mcp` | MCP SSE endpoint for agent tool integration |

### Query examples

```bash
# Find containerization patterns
curl -X POST http://localhost:8090/query \
  -H "Content-Type: application/json" \
  -d '{"query": "containerfile best practices", "top_k": 3}'

# Narrow to a specific domain using tags
curl -X POST http://localhost:8090/query \
  -H "Content-Type: application/json" \
  -d '{"query": "app scaffold folder structure", "top_k": 3, "tags": ["python"]}'
```

## Skill Folder Structure

Each skill is a directory containing a `SKILL.md` with YAML frontmatter. Skills are domain-agnostic — add skills for any technology or pattern:

```
skills/
  app-scaffold/
    SKILL.md
  containerfile-generator/
    SKILL.md
  react-component-patterns/
    SKILL.md
  kubernetes-deployment/
    SKILL.md
  api-design-patterns/
    SKILL.md
```

**SKILL.md format:**

```markdown
---
name: app-scaffold
description: Canonical folder structure for the target framework
tags:
  - python
  - scaffold
  - architecture
---

# App Scaffold
...skill content (patterns, templates, examples, rules)...
```

### Multi-directory support

Index skills from multiple sources by setting `SKILLS_BASE_DIRS` (colon-separated):

```bash
SKILLS_BASE_DIRS=/app/skills/general:/app/skills/team-specific:/app/skills/framework-specific
```

This lets you maintain a shared base of skills alongside team or project-specific ones.

## Configuration (environment variables)

| Variable | Default | Description |
|----------|---------|-------------|
| `SKILLS_BASE_DIR` | `/app/skills` | Single skill directory |
| `SKILLS_BASE_DIRS` | — | Colon-separated list of skill directories (overrides `SKILLS_BASE_DIR`) |
| `SKILLS_INDEX_CACHE_DIR` | `~/.crew-ai/skill_index_cache` | Persistent index cache location |
| `HF_HOME` | — | HuggingFace model cache directory |
| `HF_HUB_OFFLINE` | `0` | Set to `1` to use cached models without network access |

## Project Structure

```
skills-service/
├── src/
│   ├── main.py           # FastAPI app factory, REST routes, MCP mount
│   ├── discovery.py       # Skill folder scanner, frontmatter parser
│   ├── indexer.py         # LlamaIndex vector index builder + cache
│   ├── mcp_server.py      # FastMCP tools (query_skills, list_skills, reload_index)
│   └── config.py          # Pydantic settings from env vars
├── tests/
│   ├── test_api.py
│   ├── test_discovery.py
│   └── test_mcp_server.py
├── Containerfile          # Production image (UBI9 + Python 3.11)
└── pyproject.toml         # Dependencies
```

## Development

```bash
pip install -e ".[test]"
pytest
```

## License

MIT
