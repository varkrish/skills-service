"""
Unit tests for skill-manager FastAPI endpoints (main.py).

All external I/O (marketplace API, GitHub API, filesystem writes) is mocked.
Tests use httpx.AsyncClient + ASGI transport against the live FastAPI app.
"""
import asyncio
import os
import sys
import pytest
import httpx
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

# Patch MARKETPLACE_DIR before importing main so it uses a temp directory
import tempfile
_tmp_marketplace = tempfile.mkdtemp(prefix="skills-marketplace-test-")
os.environ["SKILLS_MARKETPLACE_DIR"] = _tmp_marketplace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main
from main import app, _parse_gh_url, _new_job, _jobs


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_jobs():
    """Clear job store between tests."""
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture(autouse=True)
def reset_marketplace_dir():
    """Ensure a clean marketplace directory for each test."""
    import shutil
    mp = Path(_tmp_marketplace)
    if mp.exists():
        shutil.rmtree(mp)
    mp.mkdir(parents=True, exist_ok=True)
    main.MARKETPLACE_DIR = mp
    yield
    if mp.exists():
        shutil.rmtree(mp)
    mp.mkdir(parents=True, exist_ok=True)


@pytest.fixture
def async_client():
    """Async test client using ASGI transport."""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# _parse_gh_url
# ---------------------------------------------------------------------------

class TestParseGhUrl:
    def test_full_url(self):
        owner, repo = _parse_gh_url("https://github.com/anthropics/skills")
        assert owner == "anthropics"
        assert repo == "skills"

    def test_full_url_trailing_slash(self):
        owner, repo = _parse_gh_url("https://github.com/anthropics/skills/")
        assert owner == "anthropics"
        assert repo == "skills"

    def test_full_url_dotgit(self):
        owner, repo = _parse_gh_url("https://github.com/foo/bar.git")
        assert owner == "foo"
        assert repo == "bar"

    def test_owner_repo(self):
        owner, repo = _parse_gh_url("anthropics/skills")
        assert owner == "anthropics"
        assert repo == "skills"

    def test_owner_repo_with_spaces(self):
        owner, repo = _parse_gh_url("  anthropics / skills  ")
        # After stripping, becomes "anthropics / skills" — the regex won't match,
        # so it falls back to splitting
        assert owner.strip() == "anthropics"

    def test_invalid(self):
        with pytest.raises(ValueError, match="Cannot parse"):
            _parse_gh_url("just-one-word")


# ---------------------------------------------------------------------------
# GET /api/health
# ---------------------------------------------------------------------------

class TestHealth:
    @pytest.mark.asyncio
    async def test_health(self, async_client):
        resp = await async_client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "skill-manager"


# ---------------------------------------------------------------------------
# GET /api/marketplace/search
# ---------------------------------------------------------------------------

class TestMarketplaceSearch:
    @pytest.mark.asyncio
    async def test_search_returns_results(self, async_client):
        with patch("main.mkt.search_marketplace", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = {
                "results": [
                    {"slug": "mcp-builder", "name": "MCP Builder",
                     "description": "Build MCP", "owner": "anthropics",
                     "repo": "skills", "sourceIdentifier": "anthropics/skills",
                     "installCount": 5}
                ],
                "total": 1, "page": 1, "totalPages": 1, "hasMore": False,
            }
            resp = await async_client.get("/api/marketplace/search?q=mcp&limit=20")
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] == 1
            assert data["results"][0]["slug"] == "mcp-builder"

    @pytest.mark.asyncio
    async def test_browse_no_query(self, async_client):
        """GET /api/marketplace/search without q= browses all."""
        with patch("main.mkt.search_marketplace", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = {
                "results": [
                    {"slug": "s1", "name": "S1", "description": "d1",
                     "owner": "o", "repo": "r", "installCount": 10},
                ],
                "total": 107000, "page": 1, "totalPages": 1070, "hasMore": True,
            }
            resp = await async_client.get("/api/marketplace/search?page=1&limit=20")
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] == 107000
            assert data["hasMore"] is True

    @pytest.mark.asyncio
    async def test_search_with_sort_and_category(self, async_client):
        with patch("main.mkt.search_marketplace", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = {
                "results": [], "total": 0, "page": 1, "totalPages": 0, "hasMore": False,
            }
            resp = await async_client.get("/api/marketplace/search?q=test&sort=trending&category=development")
            assert resp.status_code == 200
            mock_search.assert_called_once_with(
                q="test", page=1, limit=20, sort="trending", category="development",
            )

    @pytest.mark.asyncio
    async def test_search_marketplace_error(self, async_client):
        with patch("main.mkt.search_marketplace", new_callable=AsyncMock) as mock_search:
            mock_search.side_effect = httpx.HTTPStatusError(
                "err", request=MagicMock(), response=MagicMock()
            )
            resp = await async_client.get("/api/marketplace/search?q=test")
            assert resp.status_code == 502


# ---------------------------------------------------------------------------
# GET /api/github/scan
# ---------------------------------------------------------------------------

class TestGithubScan:
    @pytest.mark.asyncio
    async def test_scan_monorepo(self, async_client):
        with patch("main.mkt.scan_github_repo", new_callable=AsyncMock) as mock_scan:
            mock_scan.return_value = [
                {"slug": "frappe-scaffold", "name": "Frappe Scaffold",
                 "description": "Scaffold", "path": ".cursor/skills/frappe-scaffold/SKILL.md",
                 "raw_url": "https://raw.githubusercontent.com/o/r/main/.cursor/skills/frappe-scaffold/SKILL.md"},
            ]
            resp = await async_client.get("/api/github/scan?repo_url=vyogotech/frappe-apps-manager")
            assert resp.status_code == 200
            data = resp.json()
            assert data["owner"] == "vyogotech"
            assert data["repo"] == "frappe-apps-manager"
            assert data["count"] == 1
            assert data["skills"][0]["installed"] is False

    @pytest.mark.asyncio
    async def test_scan_with_installed(self, async_client):
        # Pre-create an installed skill directory
        (main.MARKETPLACE_DIR / "my-skill").mkdir(parents=True, exist_ok=True)
        (main.MARKETPLACE_DIR / "my-skill" / "SKILL.md").write_text("# test")

        with patch("main.mkt.scan_github_repo", new_callable=AsyncMock) as mock_scan:
            mock_scan.return_value = [
                {"slug": "my-skill", "name": "My Skill", "description": "...",
                 "path": "skills/my-skill/SKILL.md",
                 "raw_url": "https://example.com/SKILL.md"},
            ]
            resp = await async_client.get("/api/github/scan?repo_url=owner/repo")
            assert resp.status_code == 200
            data = resp.json()
            assert data["skills"][0]["installed"] is True

    @pytest.mark.asyncio
    async def test_scan_no_skills(self, async_client):
        with patch("main.mkt.scan_github_repo", new_callable=AsyncMock) as mock_scan:
            mock_scan.return_value = []
            resp = await async_client.get("/api/github/scan?repo_url=microsoft/skills")
            assert resp.status_code == 200
            data = resp.json()
            assert data["count"] == 0
            assert data["skills"] == []

    @pytest.mark.asyncio
    async def test_scan_invalid_url(self, async_client):
        resp = await async_client.get("/api/github/scan?repo_url=invalid")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/marketplace/install  (async job)
# ---------------------------------------------------------------------------

class TestMarketplaceInstall:
    @pytest.mark.asyncio
    async def test_install_returns_job_id(self, async_client):
        with patch("main.mkt.fetch_skill_md", new_callable=AsyncMock) as mock_fetch, \
             patch("main.mkt.trigger_reindex", new_callable=AsyncMock):
            mock_fetch.return_value = "# Test skill"
            resp = await async_client.post("/api/marketplace/install", json={
                "owner": "anthropics",
                "repo": "skills",
                "slug": "test-skill"
            })
            assert resp.status_code == 202
            data = resp.json()
            assert "job_id" in data
            assert data["slug"] == "test-skill"

    @pytest.mark.asyncio
    async def test_install_creates_skill_file(self, async_client):
        with patch("main.mkt.fetch_skill_md", new_callable=AsyncMock) as mock_fetch, \
             patch("main.mkt.trigger_reindex", new_callable=AsyncMock):
            mock_fetch.return_value = "# Installed skill content"
            resp = await async_client.post("/api/marketplace/install", json={
                "owner": "anthropics",
                "repo": "skills",
                "slug": "installed-skill"
            })
            assert resp.status_code == 202
            job_id = resp.json()["job_id"]

            # Background task runs in-process; give it a moment
            await asyncio.sleep(0.2)

            skill_path = main.MARKETPLACE_DIR / "installed-skill" / "SKILL.md"
            assert skill_path.exists()
            assert skill_path.read_text() == "# Installed skill content"

            job = _jobs[job_id]
            assert job.state == "done"
            assert "installed-skill" in job.installed


# ---------------------------------------------------------------------------
# POST /api/github/install-bulk  (async job, concurrent)
# ---------------------------------------------------------------------------

class TestBulkInstall:
    @pytest.mark.asyncio
    async def test_bulk_install_with_raw_urls(self, async_client):
        """When raw_url is provided, the service downloads directly (no GitHub API scan)."""
        call_count = {"n": 0}

        original_client_init = httpx.AsyncClient.__init__

        with patch("main.mkt.trigger_reindex", new_callable=AsyncMock), \
             patch("main.httpx.AsyncClient") as MockClient:

            # Mock the httpx client used inside _install_one
            client_instance = AsyncMock()
            resp = MagicMock()
            resp.status_code = 200
            resp.text = "# Skill content from raw_url"
            resp.raise_for_status = MagicMock()
            client_instance.get.return_value = resp
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            api_resp = await async_client.post("/api/github/install-bulk", json={
                "owner": "org",
                "repo": "repo",
                "skills": [
                    {"slug": "skill-a", "raw_url": "https://raw.githubusercontent.com/org/repo/main/skills/skill-a/SKILL.md"},
                    {"slug": "skill-b", "raw_url": "https://raw.githubusercontent.com/org/repo/main/skills/skill-b/SKILL.md"},
                ]
            })
            assert api_resp.status_code == 202
            data = api_resp.json()
            assert "job_id" in data
            assert data["total"] == 2

            await asyncio.sleep(0.3)

            job = _jobs[data["job_id"]]
            assert job.state == "done"
            assert len(job.installed) == 2
            assert "skill-a" in job.installed
            assert "skill-b" in job.installed

    @pytest.mark.asyncio
    async def test_bulk_install_without_raw_url_uses_fetch(self, async_client):
        """When raw_url is empty, falls back to fetch_skill_md."""
        with patch("main.mkt.fetch_skill_md", new_callable=AsyncMock) as mock_fetch, \
             patch("main.mkt.trigger_reindex", new_callable=AsyncMock):
            mock_fetch.return_value = "# Fetched via API"

            api_resp = await async_client.post("/api/github/install-bulk", json={
                "owner": "org",
                "repo": "repo",
                "skills": [
                    {"slug": "fallback-skill", "raw_url": ""},
                ]
            })
            assert api_resp.status_code == 202
            job_id = api_resp.json()["job_id"]

            await asyncio.sleep(0.2)

            job = _jobs[job_id]
            assert job.state == "done"
            assert "fallback-skill" in job.installed
            mock_fetch.assert_called_once_with("org", "repo", "fallback-skill")

            skill_path = main.MARKETPLACE_DIR / "fallback-skill" / "SKILL.md"
            assert skill_path.exists()
            assert skill_path.read_text() == "# Fetched via API"


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}
# ---------------------------------------------------------------------------

class TestJobPolling:
    @pytest.mark.asyncio
    async def test_get_job(self, async_client):
        job = _new_job(total=3)
        job.state = "running"
        job.installed = ["a", "b"]

        resp = await async_client.get(f"/api/jobs/{job.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "running"
        assert data["total"] == 3
        assert len(data["installed"]) == 2

    @pytest.mark.asyncio
    async def test_get_job_not_found(self, async_client):
        resp = await async_client.get("/api/jobs/nonexistent-uuid")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/installed
# ---------------------------------------------------------------------------

class TestInstalledSkills:
    @pytest.mark.asyncio
    async def test_list_installed_empty(self, async_client):
        resp = await async_client.get("/api/installed")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["skills"] == []

    @pytest.mark.asyncio
    async def test_list_installed_with_skills(self, async_client):
        skill_dir = main.MARKETPLACE_DIR / "my-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("---\nname: My Skill\ndescription: A test\n---\n# Content")

        resp = await async_client.get("/api/installed")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["skills"][0]["slug"] == "my-skill"
        assert data["skills"][0]["name"] == "My Skill"
        assert data["skills"][0]["description"] == "A test"


# ---------------------------------------------------------------------------
# DELETE /api/installed/{slug}
# ---------------------------------------------------------------------------

class TestDeleteSkill:
    @pytest.mark.asyncio
    async def test_delete_existing(self, async_client):
        skill_dir = main.MARKETPLACE_DIR / "del-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("# test")

        with patch("main.mkt.trigger_reindex", new_callable=AsyncMock):
            resp = await async_client.delete("/api/installed/del-skill")
            assert resp.status_code == 204

        assert not (skill_dir / "SKILL.md").exists()

    @pytest.mark.asyncio
    async def test_delete_not_found(self, async_client):
        resp = await async_client.delete("/api/installed/nonexistent")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /  (UI)
# ---------------------------------------------------------------------------

class TestUI:
    @pytest.mark.asyncio
    async def test_serve_ui(self, async_client):
        resp = await async_client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
