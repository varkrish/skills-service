"""
Skills Service — FastAPI application.

Indexes Cursor-style skill folders and provides semantic search over them.
Also exposes an MCP endpoint at /mcp for direct agent integration.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.routing import Mount

from discovery import discover_skills
from indexer import SkillIndex
from mcp_server import create_mcp_server

import json as _json
import traceback as _tb


class _StructuredFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = "".join(_tb.format_exception(*record.exc_info))
        return _json.dumps(entry, default=str)


_handler = logging.StreamHandler()
_handler.setFormatter(_StructuredFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler])
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class QueryRequest(BaseModel):
    query: str
    top_k: int = 3
    tags: Optional[List[str]] = None


class SkillResult(BaseModel):
    skill_name: str
    content: str
    tags: List[str]


class QueryResponse(BaseModel):
    results: List[SkillResult]


class SkillInfo(BaseModel):
    name: str
    description: str
    tags: List[str]
    file_count: int


class SkillsListResponse(BaseModel):
    skills: List[SkillInfo]


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _resolve_base_dirs() -> List[Path]:
    """Parse SKILLS_BASE_DIRS (colon-separated) or fall back to SKILLS_BASE_DIR."""
    multi = os.environ.get("SKILLS_BASE_DIRS")
    if multi:
        sep = "," if "," in multi else ":"
        return [Path(p.strip()) for p in multi.split(sep) if p.strip()]
    return [Path(os.environ.get("SKILLS_BASE_DIR", "/app/skills"))]


def create_app() -> Starlette:
    base_dirs = _resolve_base_dirs()
    cache_dir = Path(os.environ.get("SKILLS_INDEX_CACHE_DIR", str(Path.home() / ".crew-ai" / "skill_index_cache")))

    _state: dict = {"index": None}

    # Use a proxy index that delegates to _state["index"] once ready,
    # so MCP tools can be registered upfront and work as soon as index builds.
    class _IndexProxy:
        @property
        def ready(self):
            idx = _state["index"]
            return idx is not None and idx.ready
        @property
        def skills(self):
            idx = _state["index"]
            return idx.skills if idx else []
        def query(self, *a, **kw):
            return _state["index"].query(*a, **kw)
        def build(self):
            return _state["index"].build()

    mcp = create_mcp_server(base_dirs, index=_IndexProxy())
    mcp_asgi = mcp.http_app(path="/")
    mcp_lifespan = mcp_asgi.lifespan

    @asynccontextmanager
    async def _combined_lifespan(the_app):
        async with mcp_lifespan(the_app):
            asyncio.create_task(_build_index())
            yield

    api = FastAPI(title="Skills Service", version="0.1.0", lifespan=_combined_lifespan)

    async def _build_index():
        logger.info("Starting index build (base_dirs=%s)", base_dirs)
        loop = asyncio.get_event_loop()
        idx = await loop.run_in_executor(None, lambda: SkillIndex(base_dirs, cache_dir))
        await loop.run_in_executor(None, idx.build)
        _state["index"] = idx
        logger.info("Index ready — MCP tools (query_skills, reload_index) now functional")

    @api.get("/health")
    async def health():
        return {"status": "ok"}

    @api.get("/health/ready")
    async def health_ready():
        idx = _state["index"]
        if idx is None or not idx.ready:
            raise HTTPException(status_code=503, detail="Index not ready")
        return {"status": "ready"}

    @api.get("/skills", response_model=SkillsListResponse)
    async def list_skills():
        skills = []
        for bd in base_dirs:
            skills.extend(discover_skills(bd))
        return SkillsListResponse(
            skills=[
                SkillInfo(
                    name=s["name"],
                    description=s["description"],
                    tags=s["tags"],
                    file_count=s["file_count"],
                )
                for s in skills
            ]
        )

    @api.post("/query", response_model=QueryResponse)
    async def query(req: QueryRequest):
        idx = _state["index"]
        if idx is None or not idx.ready:
            raise HTTPException(status_code=503, detail="Index not ready")

        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None, lambda: idx.query(req.query, req.top_k, req.tags)
        )
        return QueryResponse(results=[SkillResult(**r) for r in results])

    @api.post("/reload", status_code=202)
    async def reload(background_tasks: BackgroundTasks):
        idx = _state["index"]
        if idx is None:
            raise HTTPException(status_code=503, detail="Index not initialized yet")
        logger.info("Reload requested — rebuilding index in background")
        background_tasks.add_task(idx.build)
        return {"status": "rebuilding"}

    api.mount("/mcp", mcp_asgi)

    logger.info("MCP endpoint mounted at /mcp (list_skills only until index ready)")
    return api


app = create_app()
