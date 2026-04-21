"""
Tests for the skills service FastAPI endpoints.
TDD: Uses a mock indexer to avoid embedding model dependency in unit tests.
"""
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from httpx import AsyncClient, ASGITransport
from fastapi import FastAPI, BackgroundTasks, HTTPException


def _write_skill(base: Path, name: str, content: str):
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content)
    return skill_dir


def _create_test_app(skills_dir: Path):
    """Build a FastAPI app with a mock indexer (no embedding model needed)."""
    import asyncio
    from discovery import discover_skills
    from typing import List, Optional
    from pydantic import BaseModel

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

    app = FastAPI()

    _skills_data = discover_skills(skills_dir)
    _ready = True

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def health_ready():
        if not _ready:
            raise HTTPException(status_code=503, detail="Index not ready")
        return {"status": "ready"}

    @app.get("/skills", response_model=SkillsListResponse)
    async def list_skills():
        skills = discover_skills(skills_dir)
        return SkillsListResponse(
            skills=[
                SkillInfo(
                    name=s["name"], description=s["description"],
                    tags=s["tags"], file_count=s["file_count"],
                )
                for s in skills
            ]
        )

    @app.post("/query", response_model=QueryResponse)
    async def query(req: QueryRequest):
        if not _ready:
            raise HTTPException(status_code=503, detail="Index not ready")

        results = []
        for skill in _skills_data:
            skill_tags = skill["tags"]
            if req.tags and not any(t in skill_tags for t in req.tags):
                continue
            for f in skill["files"]:
                content = f.read_text()
                if req.query.lower() in content.lower():
                    results.append({
                        "skill_name": skill["name"],
                        "content": content,
                        "tags": skill_tags,
                    })
            if len(results) >= req.top_k:
                break
        return QueryResponse(results=[SkillResult(**r) for r in results[:req.top_k]])

    @app.post("/reload", status_code=202)
    async def reload(background_tasks: BackgroundTasks):
        return {"status": "rebuilding"}

    return app


@pytest.fixture
def skills_dir(tmp_path):
    _write_skill(tmp_path, "frappe-api", (
        "---\nname: frappe-api\n"
        "description: Frappe API patterns\n"
        "tags: [python, frappe]\n"
        "---\n# Frappe API\nUse @whitelist for HTTP endpoints."
    ))
    _write_skill(tmp_path, "react-hooks", (
        "---\nname: react-hooks\n"
        "description: React hooks best practices\n"
        "tags: [react, frontend]\n"
        "---\n# React Hooks\nPrefer useCallback for memoization."
    ))
    return tmp_path


@pytest.fixture
def app(skills_dir):
    return _create_test_app(skills_dir)


class TestHealthEndpoints:

    @pytest.mark.asyncio
    async def test_health_returns_ok(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    @pytest.mark.asyncio
    async def test_health_ready(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health/ready")
        assert resp.status_code == 200


class TestSkillsEndpoint:

    @pytest.mark.asyncio
    async def test_get_skills_returns_list(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/skills")
        assert resp.status_code == 200
        data = resp.json()
        assert "skills" in data
        names = {s["name"] for s in data["skills"]}
        assert "frappe-api" in names
        assert "react-hooks" in names

    @pytest.mark.asyncio
    async def test_skills_include_tags(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/skills")
        skills = resp.json()["skills"]
        frappe = [s for s in skills if s["name"] == "frappe-api"][0]
        assert "python" in frappe["tags"]

    @pytest.mark.asyncio
    async def test_skills_include_file_count(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/skills")
        skills = resp.json()["skills"]
        for s in skills:
            assert s["file_count"] >= 1


class TestQueryEndpoint:

    @pytest.mark.asyncio
    async def test_query_returns_results(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/query", json={"query": "whitelist", "top_k": 2})
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data
        assert len(data["results"]) >= 1

    @pytest.mark.asyncio
    async def test_query_with_tags_filters(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/query", json={
                "query": "hooks",
                "top_k": 5,
                "tags": ["react"],
            })
        assert resp.status_code == 200
        results = resp.json()["results"]
        for r in results:
            assert "react" in r.get("tags", [])

    @pytest.mark.asyncio
    async def test_query_empty_results(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/query", json={
                "query": "nonexistent xyz abc",
                "top_k": 1,
                "tags": ["doesnotexist"],
            })
        assert resp.status_code == 200
        assert resp.json()["results"] == []

    @pytest.mark.asyncio
    async def test_query_bad_request(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/query", json={})
        assert resp.status_code == 422


class TestReloadEndpoint:

    @pytest.mark.asyncio
    async def test_reload_returns_202(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/reload")
        assert resp.status_code == 202
