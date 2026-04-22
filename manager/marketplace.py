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


async def scan_github_repo(owner: str, repo: str) -> list[dict]:
    """
    Scan a GitHub repo for SKILL.md files and return all discovered skills.

    Checks these directory layouts (in priority order):
      .cursor/skills/<slug>/SKILL.md   — Cursor skill collections (e.g. vyogotech/frappe-apps-manager)
      skills/<slug>/SKILL.md           — agentskill.sh style monorepos
      <slug>/SKILL.md                  — flat monorepos
      SKILL.md                         — single-skill repos (root)
    """
    GITHUB_API = "https://api.github.com"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "skill-manager/1.0"}
    found: list[dict] = []

    async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=headers) as client:
        # Try each known skill directory prefix
        for prefix in (".cursor/skills", "skills"):
            url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{prefix}"
            r = await client.get(url)
            if r.status_code != 200:
                continue
            entries = r.json()
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if entry.get("type") != "dir":
                    continue
                slug = entry["name"]
                # Check if SKILL.md exists inside this dir
                skill_md_url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{prefix}/{slug}/SKILL.md"
                sr = await client.get(skill_md_url)
                if sr.status_code == 200:
                    meta = sr.json()
                    raw_url = meta.get("download_url", "")
                    # Fetch actual content for name/description from frontmatter
                    name, description = slug, ""
                    if raw_url:
                        cr = await client.get(raw_url)
                        if cr.status_code == 200:
                            name, description = _parse_frontmatter(cr.text, slug)
                    found.append({
                        "slug": slug,
                        "name": name,
                        "description": description,
                        "path": f"{prefix}/{slug}/SKILL.md",
                        "raw_url": raw_url,
                    })
            if found:
                break  # found skills in this prefix, don't search further

        # Fallback: check root SKILL.md (single-skill repo)
        if not found:
            r = await client.get(f"{GITHUB_API}/repos/{owner}/{repo}/contents/SKILL.md")
            if r.status_code == 200:
                meta = r.json()
                raw_url = meta.get("download_url", "")
                name, description = repo, ""
                if raw_url:
                    cr = await client.get(raw_url)
                    if cr.status_code == 200:
                        name, description = _parse_frontmatter(cr.text, repo)
                found.append({
                    "slug": repo.lower(),
                    "name": name,
                    "description": description,
                    "path": "SKILL.md",
                    "raw_url": raw_url,
                })

    return found


def _parse_frontmatter(content: str, fallback_name: str) -> tuple[str, str]:
    """Extract name and description from YAML frontmatter."""
    name, description = fallback_name, ""
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                import yaml
                fm = yaml.safe_load(parts[1]) or {}
                name = fm.get("name", fallback_name)
                description = fm.get("description", "")
            except Exception:
                pass
    return name, description


async def fetch_skill_md(owner: str, repo: str, slug: str = "") -> str:
    """
    Fetch SKILL.md from GitHub for a given owner/repo.

    Tries multiple candidate paths because the marketplace uses two layouts:
      1. Monorepo: skills/<slug>/SKILL.md  (e.g. anthropics/skills, agilebydesign/agilebydesign-skills)
      2. Single-skill repo: SKILL.md at root
      3. Repo name as subdirectory: <repo>/SKILL.md

    Both main and master branches are tried for each path.
    """
    candidates = []
    if slug:
        candidates += [f"skills/{slug}/SKILL.md", f"{slug}/SKILL.md"]
    candidates += ["SKILL.md"]
    if slug and slug != repo:
        candidates += [f".cursor/skills/{slug}/SKILL.md"]

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for branch in ("main", "master"):
            for path in candidates:
                url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
                r = await client.get(url)
                if r.status_code == 200:
                    logger.debug("Found SKILL.md at %s", url)
                    return r.text

    tried = ", ".join(candidates)
    raise ValueError(f"SKILL.md not found for {owner}/{repo} slug={slug!r} (tried: {tried})")


async def trigger_reindex(skills_service_url: str) -> None:
    """Signal skills-service to rebuild its vector index."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"{skills_service_url.rstrip('/')}/reload")
    except httpx.RequestError:
        pass  # non-fatal
