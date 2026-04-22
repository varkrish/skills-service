"""
agentskill.sh marketplace client.
Searches agentskillhub.dev and fetches SKILL.md files from GitHub.
"""
import httpx

AGENTSKILL_API = "https://agentskillhub.dev/api/v1/search"


async def search_marketplace(q: str, limit: int = 10) -> list[dict]:
    """Search agentskill.sh marketplace. Returns raw API results."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(AGENTSKILL_API, params={"q": q, "limit": limit})
        r.raise_for_status()
    return r.json().get("results", r.json() if isinstance(r.json(), list) else [])


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
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(f"{skills_service_url.rstrip('/')}/reload")
