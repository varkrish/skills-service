"""
agentskill.sh marketplace client.

API: https://agentskillhub.dev/api/v1/search?q=<query>
Response shape: {"skills": [{slug, name, description, totalInstalls, sourceIdentifier}, ...]}

sourceIdentifier is "owner/repo" on GitHub (e.g. "anthropics/skills").
"""
import httpx

AGENTSKILL_API = "https://agentskillhub.dev/api/v1/search"
MAX_LIMIT = 10  # API rejects values above 10


def _parse_source(source_identifier: str) -> tuple[str, str]:
    """Parse 'owner/repo' into (owner, repo). Falls back to ('', source_identifier)."""
    parts = source_identifier.split("/", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", source_identifier


async def search_marketplace(q: str, limit: int = 10) -> list[dict]:
    """
    Search agentskill.sh marketplace.
    Returns a normalized list compatible with the manager UI.
    """
    safe_limit = min(limit, MAX_LIMIT)
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(AGENTSKILL_API, params={"q": q, "limit": safe_limit})
        r.raise_for_status()

    raw = r.json()
    skills = raw.get("skills", raw if isinstance(raw, list) else [])

    results = []
    for s in skills:
        source = s.get("sourceIdentifier", "")
        owner, repo = _parse_source(source)
        slug = s.get("slug", f"{owner}-{repo}".lower())
        results.append({
            "slug": slug,
            "name": s.get("name", slug),
            "description": s.get("description", ""),
            "owner": owner,
            "repo": repo,
            "sourceIdentifier": source,
            "totalInstalls": s.get("totalInstalls", 0),
        })
    return results


async def fetch_skill_md(owner: str, repo: str) -> str:
    """
    Fetch SKILL.md from GitHub for a given owner/repo.
    Tries main branch first, then master.
    """
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for branch in ("main", "master"):
            url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/SKILL.md"
            r = await client.get(url)
            if r.status_code == 200:
                return r.text
    raise ValueError(f"SKILL.md not found for {owner}/{repo} (tried main, master)")


async def trigger_reindex(skills_service_url: str) -> None:
    """Tell the skills-service to rebuild its index."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"{skills_service_url.rstrip('/')}/reload")
    except httpx.RequestError:
        pass  # non-fatal — reindex can be triggered manually
