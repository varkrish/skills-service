"""
agentskill.sh marketplace client.

API: https://agentskillhub.dev/api/v1/search?q=<query>
Response shape: {"skills": [{slug, name, description, totalInstalls, sourceIdentifier}, ...]}

sourceIdentifier is "owner/repo" on GitHub (e.g. "anthropics/skills").

NOTE: The API returns max 10 results per query with no pagination that works
reliably. To enumerate all ~80+ skills we fan out across a set of broad queries
and deduplicate by slug.
"""
import asyncio
import logging
import time
import httpx

logger = logging.getLogger(__name__)

AGENTSKILL_API = "https://agentskillhub.dev/api/v1/search"

# Broad queries that together cover most/all available skills.
# Chosen empirically — common words/substrings that appear in many SKILL.md files.
BROWSE_QUERIES = [
    "the", "ab", "code", "skill", "agent", "build", "test",
    "api", "data", "app", "cloud", "git", "ci", "dev", "web", "ai",
    "video", "plan", "er", "an",
]

# Simple in-process cache so browsing the page doesn't hammer the API
_browse_cache: dict = {"ts": 0.0, "skills": []}
CACHE_TTL = 300  # 5 minutes


def _parse_source(source_identifier: str) -> tuple[str, str]:
    parts = source_identifier.split("/", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", source_identifier


def _normalize(raw_skill: dict) -> dict:
    source = raw_skill.get("sourceIdentifier", "")
    owner, repo = _parse_source(source)
    slug = raw_skill.get("slug", f"{owner}-{repo}".lower())
    return {
        "slug": slug,
        "name": raw_skill.get("name", slug),
        "description": raw_skill.get("description", ""),
        "owner": owner,
        "repo": repo,
        "sourceIdentifier": source,
        "totalInstalls": raw_skill.get("totalInstalls", 0),
    }


async def _fetch_one(client: httpx.AsyncClient, q: str) -> list[dict]:
    try:
        r = await client.get(AGENTSKILL_API, params={"q": q}, timeout=8)
        r.raise_for_status()
        return r.json().get("skills", [])
    except Exception as exc:
        logger.debug("marketplace query %r failed: %s", q, exc)
        return []


async def search_marketplace(q: str, limit: int = 10) -> list[dict]:
    """Search agentskill.sh for skills matching q."""
    async with httpx.AsyncClient() as client:
        raw = await _fetch_one(client, q)
    return [_normalize(s) for s in raw[:limit]]


async def browse_all_marketplace() -> list[dict]:
    """
    Return all discoverable marketplace skills by fanning out across
    BROWSE_QUERIES in parallel and deduplicating by slug.
    Results are cached for CACHE_TTL seconds.
    """
    now = time.monotonic()
    if _browse_cache["skills"] and now - _browse_cache["ts"] < CACHE_TTL:
        return _browse_cache["skills"]

    async with httpx.AsyncClient() as client:
        batches = await asyncio.gather(
            *[_fetch_one(client, q) for q in BROWSE_QUERIES],
            return_exceptions=True,
        )

    seen: set[str] = set()
    result: list[dict] = []
    for batch in batches:
        if isinstance(batch, list):
            for s in batch:
                norm = _normalize(s)
                if norm["slug"] not in seen:
                    seen.add(norm["slug"])
                    result.append(norm)

    # Sort by totalInstalls desc, then name
    result.sort(key=lambda s: (-s["totalInstalls"], s["name"]))

    _browse_cache["ts"] = now
    _browse_cache["skills"] = result
    logger.info("Marketplace browse: discovered %d unique skills", len(result))
    return result


async def fetch_skill_md(owner: str, repo: str) -> str:
    """Fetch SKILL.md from GitHub. Tries main then master."""
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for branch in ("main", "master"):
            r = await client.get(
                f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/SKILL.md"
            )
            if r.status_code == 200:
                return r.text
    raise ValueError(f"SKILL.md not found for {owner}/{repo} (tried main, master)")


async def trigger_reindex(skills_service_url: str) -> None:
    """Signal skills-service to rebuild its vector index."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"{skills_service_url.rstrip('/')}/reload")
    except httpx.RequestError:
        pass  # non-fatal
