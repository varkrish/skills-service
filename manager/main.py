"""
Skill Manager — management API + embedded UI for the OPL skills ecosystem.

All write operations are async:
  - POST endpoints return 202 + job_id immediately
  - GET /api/jobs/{job_id} lets callers poll for progress
  - Downloads run concurrently via asyncio.gather
"""
import asyncio
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Literal

import httpx
import yaml
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import marketplace as mkt

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SKILLS_SERVICE_URL = os.environ.get("SKILLS_SERVICE_URL", "http://skills-service:8090")
MARKETPLACE_DIR    = Path(os.environ.get("SKILLS_MARKETPLACE_DIR", "/app/skills/marketplace"))

MARKETPLACE_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# In-memory job store  {job_id: JobStatus}
# ---------------------------------------------------------------------------

JobState = Literal["pending", "running", "done", "failed"]

class JobStatus(BaseModel):
    id: str
    state: JobState = "pending"
    total: int = 0
    installed: list[str] = []
    failed: list[dict] = []
    message: str = ""

_jobs: dict[str, JobStatus] = {}


def _new_job(total: int = 0) -> JobStatus:
    job = JobStatus(id=str(uuid.uuid4()), total=total)
    _jobs[job.id] = job
    return job

# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

_GH_RE = re.compile(r"github\.com/([^/]+)/([^/?\s#]+)")

def _parse_gh_url(raw: str) -> tuple[str, str]:
    raw = raw.strip().rstrip("/").replace(".git", "")
    m = _GH_RE.search(raw)
    if m:
        return m.group(1), m.group(2)
    parts = [p for p in raw.split("/") if p]
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    raise ValueError(f"Cannot parse GitHub owner/repo from: {raw!r}")

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class InstallRequest(BaseModel):
    owner: str
    repo: str
    slug: str

class BulkInstallRequest(BaseModel):
    owner: str
    repo: str
    slugs: list[str]

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Skill Manager", version="0.2.0")

# ---------------------------------------------------------------------------
# Job store endpoint
# ---------------------------------------------------------------------------

@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job

# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "skill-manager", "version": "0.2.0"}

# ---------------------------------------------------------------------------
# Marketplace — read (browse / search)
# ---------------------------------------------------------------------------

@app.get("/api/marketplace/browse")
async def browse_marketplace():
    """All discoverable marketplace skills (fan-out + cache)."""
    try:
        skills = await mkt.browse_all_marketplace()
        return {"results": skills, "count": len(skills)}
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Marketplace unreachable: {exc}")


@app.get("/api/marketplace/search")
async def search_marketplace(q: str, limit: int = 10):
    """Search agentskill.sh (min 2 chars)."""
    q = q.strip()
    if len(q) < 2:
        raise HTTPException(status_code=400, detail="Query must be at least 2 characters")
    try:
        results = await mkt.search_marketplace(q, min(limit, 10))
        return {"results": results, "query": q, "count": len(results)}
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Marketplace unreachable: {exc}")

# ---------------------------------------------------------------------------
# Marketplace — write (install single)
# ---------------------------------------------------------------------------

@app.post("/api/marketplace/install", status_code=202)
async def install_skill(req: InstallRequest, background_tasks: BackgroundTasks):
    """
    Async single-skill install. Returns job_id immediately; downloads
    SKILL.md in the background and triggers skills-service reindex.
    """
    try:
        owner, repo = _parse_gh_url(req.owner) if "/" in req.owner or "github" in req.owner \
                      else (req.owner.strip(), req.repo.strip())
    except ValueError:
        owner, repo = req.owner.strip(), req.repo.strip()

    slug = req.slug.strip().lower().replace(" ", "-") or f"{owner}-{repo}".lower()
    job  = _new_job(total=1)

    async def _do_install():
        job.state = "running"
        try:
            content = await mkt.fetch_skill_md(owner, repo, slug)
            skill_dir = MARKETPLACE_DIR / slug
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
            job.installed.append(slug)
            logger.info("Installed skill %s from %s/%s", slug, owner, repo)
            await mkt.trigger_reindex(SKILLS_SERVICE_URL)
        except Exception as exc:
            job.failed.append({"slug": slug, "reason": str(exc)})
            logger.error("Failed to install %s: %s", slug, exc)
        finally:
            job.state = "done"

    background_tasks.add_task(_do_install)
    return {"job_id": job.id, "slug": slug, "status": "accepted"}

# ---------------------------------------------------------------------------
# GitHub repo scan
# ---------------------------------------------------------------------------

@app.get("/api/github/scan")
async def scan_github_repo(repo_url: str):
    """
    Scan a GitHub repo for all SKILL.md files.
    Supports full GitHub URLs and owner/repo shorthand.
    """
    try:
        owner, repo = _parse_gh_url(repo_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        skills = await mkt.scan_github_repo(owner, repo)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"GitHub unreachable: {exc}")

    installed_slugs = {d.name for d in MARKETPLACE_DIR.iterdir() if d.is_dir()}
    for s in skills:
        s["installed"] = s["slug"] in installed_slugs

    return {"owner": owner, "repo": repo, "skills": skills, "count": len(skills)}

# ---------------------------------------------------------------------------
# GitHub bulk install — fully async, concurrent downloads
# ---------------------------------------------------------------------------

@app.post("/api/github/install-bulk", status_code=202)
async def install_bulk(req: BulkInstallRequest, background_tasks: BackgroundTasks):
    """
    Kick off concurrent background installation of multiple skills.
    Returns job_id immediately; poll GET /api/jobs/{job_id} for progress.
    """
    job = _new_job(total=len(req.slugs))

    async def _do_bulk():
        job.state = "running"
        # Scan once to get raw_urls for all skills
        try:
            all_skills = await mkt.scan_github_repo(req.owner, req.repo)
        except Exception as exc:
            job.state = "failed"
            job.message = f"Scan failed: {exc}"
            return

        by_slug = {s["slug"]: s for s in all_skills}

        async def _install_one(slug: str):
            skill = by_slug.get(slug)
            if not skill:
                job.failed.append({"slug": slug, "reason": "not found in scan"})
                return
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
                job.installed.append(slug)
                logger.info("Bulk installed: %s", slug)
            except Exception as exc:
                job.failed.append({"slug": slug, "reason": str(exc)})
                logger.warning("Failed to install %s: %s", slug, exc)

        # Download all skills concurrently
        await asyncio.gather(*[_install_one(s) for s in req.slugs])

        if job.installed:
            await mkt.trigger_reindex(SKILLS_SERVICE_URL)

        job.state = "done"
        job.message = f"Installed {len(job.installed)}/{job.total}, failed {len(job.failed)}"
        logger.info("Bulk install job %s done: %s", job.id, job.message)

    background_tasks.add_task(_do_bulk)
    return {"job_id": job.id, "total": len(req.slugs), "status": "accepted"}

# ---------------------------------------------------------------------------
# Installed skills — list / delete
# ---------------------------------------------------------------------------

def _read_frontmatter(path: Path) -> tuple[str, str]:
    content = path.read_text(encoding="utf-8")
    name, description = path.parent.name, ""
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                fm = yaml.safe_load(parts[1]) or {}
                name        = fm.get("name", name)
                description = fm.get("description", "")
            except Exception:
                pass
    return name, description


@app.get("/api/installed")
async def list_installed():
    skills = []
    for entry in sorted(MARKETPLACE_DIR.iterdir()):
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.exists():
            continue
        name, description = _read_frontmatter(skill_md)
        skills.append({
            "slug": entry.name,
            "name": name,
            "description": description,
            "size": skill_md.stat().st_size,
        })
    return {"skills": skills, "count": len(skills)}


@app.delete("/api/installed/{slug}", status_code=204)
async def delete_skill(slug: str, background_tasks: BackgroundTasks):
    skill_dir = MARKETPLACE_DIR / slug
    if not skill_dir.exists():
        raise HTTPException(status_code=404, detail=f"Skill '{slug}' not found")
    skill_md = skill_dir / "SKILL.md"
    if skill_md.exists():
        skill_md.unlink()
    try:
        skill_dir.rmdir()
    except OSError:
        pass
    logger.info("Deleted marketplace skill: %s", slug)
    background_tasks.add_task(mkt.trigger_reindex, SKILLS_SERVICE_URL)

# ---------------------------------------------------------------------------
# Serve embedded UI
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    ui_path = Path(__file__).parent / "ui" / "index.html"
    return HTMLResponse(ui_path.read_text(encoding="utf-8"))
