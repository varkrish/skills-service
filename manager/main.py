"""
Skill Manager — management API + embedded UI for the OPL skills ecosystem.

Responsibilities:
  - Browse / search agentskill.sh marketplace (107,000+ skills)
  - Install skills from the marketplace (writes SKILL.md to marketplace volume)
  - List and delete locally installed marketplace skills
  - Trigger skills-service reindex after any write operation

skills-service (port 8090) stays read-only; this service (port 8091) owns writes.
"""
import logging
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import marketplace as mkt

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SKILLS_SERVICE_URL  = os.environ.get("SKILLS_SERVICE_URL", "http://skills-service:8090")
MARKETPLACE_DIR     = Path(os.environ.get("SKILLS_MARKETPLACE_DIR", "/app/skills/marketplace"))

MARKETPLACE_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class InstallRequest(BaseModel):
    owner: str   # GitHub owner, e.g. "obra"
    repo: str    # GitHub repo, e.g. "systematic-debugging"
    slug: str    # local directory name, e.g. "obra-systematic-debugging"


class DeleteRequest(BaseModel):
    slug: str


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Skill Manager", version="0.1.0")


# ---------------------------------------------------------------------------
# Marketplace endpoints
# ---------------------------------------------------------------------------


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "skill-manager"}


@app.get("/api/marketplace/browse")
async def browse_marketplace():
    """
    Return all discoverable marketplace skills by fanning out across
    broad queries and deduplicating. Results are cached for 5 minutes.
    """
    try:
        skills = await mkt.browse_all_marketplace()
        return {"results": skills, "count": len(skills)}
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Marketplace unreachable: {exc}")


@app.get("/api/marketplace/search")
async def search_marketplace(q: str, limit: int = 10):
    """Search agentskill.sh for skills matching q (min 2 chars)."""
    q = q.strip()
    if len(q) < 2:
        raise HTTPException(status_code=400, detail="Query must be at least 2 characters")
    try:
        results = await mkt.search_marketplace(q, min(limit, 10))
        return {"results": results, "query": q, "count": len(results)}
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Marketplace unreachable: {exc}")


@app.post("/api/marketplace/install", status_code=202)
async def install_skill(req: InstallRequest, background_tasks: BackgroundTasks):
    """
    Download SKILL.md from GitHub and save to the marketplace volume.
    Triggers skills-service reindex in the background.
    """
    import re as _re

    def _parse_gh(s: str) -> str:
        """Strip GitHub URL prefix if a full URL was passed."""
        s = s.strip().rstrip("/")
        m = _re.search(r"github\.com/([^/]+/[^/?\s]+)", s)
        return m.group(1) if m else s

    owner = _parse_gh(req.owner).split("/")[0] if "/" in _parse_gh(req.owner) else req.owner.strip()
    repo  = req.repo.strip()
    # If owner field contained a full "owner/repo" string, split it
    if "/" in owner:
        owner, repo = owner.split("/", 1)

    slug = req.slug.strip().lower().replace(" ", "-") or f"{owner}-{repo}".lower().replace("/", "-")
    skill_dir = MARKETPLACE_DIR / slug

    try:
        content = await mkt.fetch_skill_md(owner, repo, slug)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"GitHub unreachable: {exc}")

    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    logger.info("Installed skill %s from %s/%s", slug, owner, repo)

    background_tasks.add_task(mkt.trigger_reindex, SKILLS_SERVICE_URL)
    return {"status": "installed", "slug": slug}


@app.get("/api/installed")
async def list_installed():
    """List all skills installed via the marketplace."""
    skills = []
    for entry in sorted(MARKETPLACE_DIR.iterdir()):
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.exists():
            continue
        content = skill_md.read_text(encoding="utf-8")
        name = entry.name
        description = ""
        # Extract frontmatter name/description if present
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                import yaml
                try:
                    fm = yaml.safe_load(parts[1]) or {}
                    name = fm.get("name", entry.name)
                    description = fm.get("description", "")
                except Exception:
                    pass
        skills.append({
            "slug": entry.name,
            "name": name,
            "description": description,
            "size": skill_md.stat().st_size,
        })
    return {"skills": skills, "count": len(skills)}


@app.get("/api/github/scan")
async def scan_github_repo(repo_url: str):
    """
    Scan a GitHub repo for all SKILL.md files.
    Accepts full GitHub URLs or owner/repo format.
    Returns a list of discovered skills (not yet installed).
    """
    import re as _re
    url = repo_url.strip().rstrip("/").replace(".git", "")
    m = _re.search(r"github\.com/([^/]+)/([^/?\s]+)", url)
    if m:
        owner, repo = m.group(1), m.group(2)
    elif "/" in url:
        parts = url.split("/")
        owner, repo = parts[-2], parts[-1]
    else:
        raise HTTPException(status_code=400, detail="Provide a GitHub URL or owner/repo")

    try:
        skills = await mkt.scan_github_repo(owner, repo)
        installed_slugs = {d.name for d in MARKETPLACE_DIR.iterdir() if d.is_dir()}
        for s in skills:
            s["installed"] = s["slug"] in installed_slugs
        return {"owner": owner, "repo": repo, "skills": skills, "count": len(skills)}
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"GitHub unreachable: {exc}")


class BulkInstallRequest(BaseModel):
    owner: str
    repo: str
    slugs: list[str]   # which skills from the scan to install


@app.post("/api/github/install-bulk", status_code=202)
async def install_bulk(req: BulkInstallRequest, background_tasks: BackgroundTasks):
    """Install multiple skills from a scanned GitHub repo."""
    # Re-scan to get raw_urls, then write each SKILL.md
    try:
        all_skills = await mkt.scan_github_repo(req.owner, req.repo)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"GitHub unreachable: {exc}")

    by_slug = {s["slug"]: s for s in all_skills}
    installed, skipped, failed = [], [], []

    for slug in req.slugs:
        skill = by_slug.get(slug)
        if not skill:
            failed.append({"slug": slug, "reason": "not found in repo"})
            continue
        skill_dir = MARKETPLACE_DIR / slug
        try:
            if skill.get("raw_url"):
                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.get(skill["raw_url"])
                    r.raise_for_status()
                    content = r.text
            else:
                content = await mkt.fetch_skill_md(req.owner, req.repo, slug)
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
            installed.append(slug)
            logger.info("Bulk installed skill: %s", slug)
        except Exception as exc:
            failed.append({"slug": slug, "reason": str(exc)})

    if installed:
        background_tasks.add_task(mkt.trigger_reindex, SKILLS_SERVICE_URL)

    return {"installed": installed, "skipped": skipped, "failed": failed}


@app.delete("/api/installed/{slug}", status_code=204)
async def delete_skill(slug: str, background_tasks: BackgroundTasks):
    """Remove a marketplace-installed skill and trigger reindex."""
    skill_dir = MARKETPLACE_DIR / slug
    if not skill_dir.exists():
        raise HTTPException(status_code=404, detail=f"Skill '{slug}' not found")
    skill_md = skill_dir / "SKILL.md"
    if skill_md.exists():
        skill_md.unlink()
    try:
        skill_dir.rmdir()
    except OSError:
        pass  # dir not empty — leave it
    logger.info("Deleted marketplace skill: %s", slug)
    background_tasks.add_task(mkt.trigger_reindex, SKILLS_SERVICE_URL)


# ---------------------------------------------------------------------------
# Serve embedded UI — must be last
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    ui_path = Path(__file__).parent / "ui" / "index.html"
    return HTMLResponse(ui_path.read_text(encoding="utf-8"))
