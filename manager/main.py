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


@app.get("/api/marketplace/search")
async def search_marketplace(q: str, limit: int = 10):
    """Search agentskill.sh for skills matching the query."""
    if not q or len(q.strip()) < 2:
        raise HTTPException(status_code=400, detail="Query must be at least 2 characters")
    try:
        results = await mkt.search_marketplace(q.strip(), min(limit, 20))
        return {"results": results, "query": q}
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Marketplace unreachable: {exc}")


@app.post("/api/marketplace/install", status_code=202)
async def install_skill(req: InstallRequest, background_tasks: BackgroundTasks):
    """
    Download SKILL.md from GitHub and save to the marketplace volume.
    Triggers skills-service reindex in the background.
    """
    slug = req.slug.strip().lower().replace(" ", "-") or f"{req.owner}-{req.repo}"
    skill_dir = MARKETPLACE_DIR / slug

    try:
        content = await mkt.fetch_skill_md(req.owner, req.repo)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"GitHub unreachable: {exc}")

    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    logger.info("Installed skill %s from %s/%s", slug, req.owner, req.repo)

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
