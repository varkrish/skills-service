"""
agentskill.sh marketplace client.

API: https://agentskill.sh/api/skills
Response shape: {
    "data": [{slug, name, description, githubOwner, githubRepo, githubPath, ...}],
    "total": int,
    "page": int,
    "limit": int,
    "totalPages": int,
    "hasMore": bool,
}

The API supports:
  - Search:     ?q=<query>
  - Pagination: ?page=N&limit=N  (max 100 per page)
  - Sorting:    ?sort=trending|top|hot|latest
  - Categories: ?category=development|marketing|...
"""
import asyncio
import logging
import os
import httpx

logger = logging.getLogger(__name__)

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

AGENTSKILL_API = "https://agentskill.sh/api/skills"
MAX_PAGE_SIZE = 100


def _normalize(raw_skill: dict) -> dict:
    owner = raw_skill.get("githubOwner", "")
    repo = raw_skill.get("githubRepo", "")
    raw_slug = raw_skill.get("slug", f"{owner}-{repo}".lower())
    # agentskill.sh slugs are "owner/skill-name" — strip the owner prefix
    # so local directory names stay clean (e.g. "k8s-operator" not "hawkli-1994/k8s-operator")
    slug = raw_slug.split("/", 1)[-1] if "/" in raw_slug else raw_slug
    # Preserve the full slug for the agentskill.sh page URL: /@owner/skill-name
    marketplace_url = f"https://agentskill.sh/@{raw_slug}" if raw_slug else ""
    github_path = raw_skill.get("githubPath", "")
    # Normalize case-insensitive filename variants (e.g. "skill.md" → "SKILL.md")
    branch = raw_skill.get("githubBranch", "")
    return {
        "slug": slug,
        "name": raw_skill.get("name", slug),
        "description": raw_skill.get("description", ""),
        "owner": owner,
        "repo": repo,
        "sourceIdentifier": f"{owner}/{repo}" if owner and repo else "",
        "installCount": raw_skill.get("installCount", 0),
        "contentQualityScore": raw_skill.get("contentQualityScore", 0),
        "securityScore": raw_skill.get("securityScore", 0),
        "category": raw_skill.get("category", ""),
        "platforms": raw_skill.get("platforms", []),
        "tags": raw_skill.get("tags", []),
        "githubPath": github_path,
        "githubBranch": branch,
        "avatarUrl": raw_skill.get("avatarUrl", ""),
        "trendingScore": raw_skill.get("trendingScore", 0),
        "marketplaceUrl": marketplace_url,
    }


async def search_marketplace(
    q: str = "",
    page: int = 1,
    limit: int = 20,
    sort: str = "",
    category: str = "",
) -> dict:
    """
    Search agentskill.sh marketplace.

    Returns dict with keys: results, total, page, totalPages, hasMore.
    """
    limit = min(max(limit, 1), MAX_PAGE_SIZE)
    params: dict = {"page": page, "limit": limit}
    if q:
        params["q"] = q
    if sort:
        params["sort"] = sort
    if category:
        params["category"] = category

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(AGENTSKILL_API, params=params)
        r.raise_for_status()
        data = r.json()

    skills = [_normalize(s) for s in data.get("data", [])]
    return {
        "results": skills,
        "total": data.get("total", len(skills)),
        "page": data.get("page", page),
        "totalPages": data.get("totalPages", 1),
        "hasMore": data.get("hasMore", False),
    }


def _github_headers() -> dict[str, str]:
    """Build GitHub API headers, with optional auth token for higher rate limits."""
    h = {"Accept": "application/vnd.github+json", "User-Agent": "skill-manager/1.0"}
    token = GITHUB_TOKEN
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def scan_github_repo(owner: str, repo: str) -> list[dict]:
    """
    Scan a GitHub repo for every SKILL.md file regardless of directory layout.

    Uses the Git tree API (single recursive call) to find all paths ending in
    SKILL.md, then fetches content in parallel from raw.githubusercontent.com
    for name/description extraction.

    When the tree API is rate-limited (403), falls back to probing common raw
    URLs directly (raw.githubusercontent.com is never rate-limited).
    """
    GITHUB_API = "https://api.github.com"
    headers = _github_headers()

    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=headers) as client:
        tree: list[dict] = []
        branch = "main"
        for b in ("main", "master"):
            r = await client.get(
                f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{b}",
                params={"recursive": "1"},
            )
            if r.status_code == 200:
                tree = r.json().get("tree", [])
                branch = b
                break
            if r.status_code == 403:
                logger.warning("GitHub tree API rate-limited for %s/%s, trying raw fallback", owner, repo)
                break

        if not tree:
            return await _scan_via_raw_probing(client, owner, repo)

        skill_paths: list[str] = [
            item["path"] for item in tree
            if item.get("type") == "blob"
            and item["path"].endswith("SKILL.md")
        ]

        if not skill_paths:
            return []

        raw_base = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}"

        async def _fetch_one(path: str) -> dict | None:
            parts = path.split("/")
            slug = parts[-2] if len(parts) >= 2 else repo.lower()

            raw_url = f"{raw_base}/{path}"
            name, description = slug, ""
            try:
                cr = await client.get(raw_url)
                if cr.status_code == 200:
                    name, description = _parse_frontmatter(cr.text, slug)
            except Exception:
                pass

            return {
                "slug": slug,
                "name": name,
                "description": description,
                "path": path,
                "raw_url": raw_url,
            }

        results = await asyncio.gather(
            *[_fetch_one(p) for p in skill_paths],
            return_exceptions=True,
        )
        return [r for r in results if isinstance(r, dict)]


async def _scan_via_raw_probing(
    client: httpx.AsyncClient, owner: str, repo: str,
) -> list[dict]:
    """
    Last-resort scanner when the GitHub API is rate-limited.
    Probes raw.githubusercontent.com directly (never rate-limited)
    for common SKILL.md locations.
    """
    branch = "main"
    for b in ("main", "master"):
        r = await client.get(
            f"https://raw.githubusercontent.com/{owner}/{repo}/{b}/README.md"
        )
        if r.status_code == 200:
            branch = b
            break

    raw_base = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}"
    candidates = [
        "skill/SKILL.md",
        "SKILL.md",
    ]

    for path in candidates:
        raw_url = f"{raw_base}/{path}"
        cr = await client.get(raw_url)
        if cr.status_code == 200:
            slug = repo.lower()
            name, description = _parse_frontmatter(cr.text, slug)
            return [{
                "slug": slug,
                "name": name,
                "description": description,
                "path": path,
                "raw_url": raw_url,
            }]

    logger.warning(
        "GitHub API rate-limited and raw probing found nothing for %s/%s. "
        "Set GITHUB_TOKEN env var for 5000 requests/hour.",
        owner, repo,
    )
    return []


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


async def fetch_skill_md(
    owner: str, repo: str, slug: str = "", github_path: str = "",
) -> str:
    """
    Fetch SKILL.md from GitHub for a given owner/repo.

    If github_path is provided (from agentskill.sh metadata), it is tried
    first as the exact path. Otherwise falls back to probing multiple
    candidate paths for different repo layouts.
    """
    candidates = []
    if github_path:
        candidates.append(github_path)
    if slug:
        candidates += [
            f"skills/{slug}/SKILL.md",
            f"skill/{slug}/SKILL.md",
            f"{slug}/SKILL.md",
        ]
    candidates += ["SKILL.md", "skill/SKILL.md"]
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
        pass
