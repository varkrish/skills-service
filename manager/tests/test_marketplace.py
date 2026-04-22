"""Unit tests for marketplace.py — all HTTP calls are mocked."""
import asyncio
import pytest
import httpx
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import marketplace as mkt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SKILL_MD_FRAPPE = """---
name: frappe-containerfile-generator
description: Generate Containerfile for Frappe apps
---

# frappe-containerfile-generator
Some content here.
"""

SKILL_MD_NO_FM = "# Simple skill\nJust a markdown file."


def _mock_response(status_code: int = 200, json_data=None, text: str = ""):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = text
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    return resp


def _tree_entry(path: str, entry_type: str = "blob") -> dict:
    """Helper to build a Git tree API entry."""
    return {"path": path, "type": entry_type, "sha": "abc123"}


# ---------------------------------------------------------------------------
# _normalize
# ---------------------------------------------------------------------------

class TestNormalize:
    def test_full_data(self):
        raw = {
            "slug": "mcp-builder",
            "name": "MCP Builder",
            "description": "Build MCP servers",
            "githubOwner": "anthropics",
            "githubRepo": "skills",
            "installCount": 42,
            "contentQualityScore": 83,
            "securityScore": 100,
            "category": "development",
        }
        result = mkt._normalize(raw)
        assert result["slug"] == "mcp-builder"
        assert result["owner"] == "anthropics"
        assert result["repo"] == "skills"
        assert result["installCount"] == 42
        assert result["contentQualityScore"] == 83
        assert result["securityScore"] == 100
        assert result["sourceIdentifier"] == "anthropics/skills"

    def test_slug_owner_prefix_stripped(self):
        """agentskill.sh slugs are 'owner/skill-name' — owner prefix should be stripped."""
        raw = {
            "slug": "hawkli-1994/k8s-operator",
            "githubOwner": "hawkli-1994",
            "githubRepo": "k8s-operator-skills",
            "githubPath": "skill.md",
        }
        result = mkt._normalize(raw)
        assert result["slug"] == "k8s-operator"
        assert result["githubPath"] == "skill.md"

    def test_slug_without_slash_unchanged(self):
        raw = {"slug": "simple-skill", "githubOwner": "owner", "githubRepo": "repo"}
        result = mkt._normalize(raw)
        assert result["slug"] == "simple-skill"

    def test_missing_slug_uses_owner_repo(self):
        raw = {"githubOwner": "foo", "githubRepo": "bar"}
        result = mkt._normalize(raw)
        assert result["slug"] == "foo-bar"

    def test_missing_owner_repo(self):
        raw = {"slug": "test"}
        result = mkt._normalize(raw)
        assert result["owner"] == ""
        assert result["repo"] == ""
        assert result["sourceIdentifier"] == ""

    def test_defaults(self):
        raw = {}
        result = mkt._normalize(raw)
        assert result["installCount"] == 0
        assert result["contentQualityScore"] == 0
        assert result["category"] == ""
        assert result["platforms"] == []
        assert result["tags"] == []

    def test_github_path_preserved(self):
        raw = {
            "slug": "openclaw/acp-router",
            "githubOwner": "openclaw",
            "githubRepo": "openclaw",
            "githubPath": "extensions/acpx/skills/acp-router/SKILL.md",
        }
        result = mkt._normalize(raw)
        assert result["slug"] == "acp-router"
        assert result["githubPath"] == "extensions/acpx/skills/acp-router/SKILL.md"

    def test_marketplace_url(self):
        raw = {
            "slug": "hawkli-1994/k8s-operator",
            "githubOwner": "hawkli-1994",
            "githubRepo": "k8s-operator-skills",
        }
        result = mkt._normalize(raw)
        assert result["marketplaceUrl"] == "https://agentskill.sh/@hawkli-1994/k8s-operator"

    def test_marketplace_url_no_prefix(self):
        raw = {"slug": "simple-skill", "githubOwner": "owner", "githubRepo": "repo"}
        result = mkt._normalize(raw)
        assert result["marketplaceUrl"] == "https://agentskill.sh/@simple-skill"


# ---------------------------------------------------------------------------
# _parse_frontmatter
# ---------------------------------------------------------------------------

class TestParseFrontmatter:
    def test_valid_frontmatter(self):
        name, desc = mkt._parse_frontmatter(SKILL_MD_FRAPPE, "fallback")
        assert name == "frappe-containerfile-generator"
        assert "Containerfile" in desc

    def test_no_frontmatter(self):
        name, desc = mkt._parse_frontmatter(SKILL_MD_NO_FM, "fallback")
        assert name == "fallback"
        assert desc == ""

    def test_empty_string(self):
        name, desc = mkt._parse_frontmatter("", "fb")
        assert name == "fb"
        assert desc == ""

    def test_invalid_yaml(self):
        content = "---\n: invalid: yaml: {{{\n---\nbody"
        name, desc = mkt._parse_frontmatter(content, "fb")
        assert name == "fb"


# ---------------------------------------------------------------------------
# search_marketplace
# ---------------------------------------------------------------------------

class TestSearchMarketplace:
    @pytest.mark.asyncio
    async def test_returns_paginated_results(self):
        api_response = {
            "data": [
                {"slug": "mcp-builder", "name": "MCP", "description": "d",
                 "githubOwner": "anthropics", "githubRepo": "skills",
                 "installCount": 5, "contentQualityScore": 83}
            ],
            "total": 1,
            "page": 1,
            "totalPages": 1,
            "hasMore": False,
        }
        mock_resp = _mock_response(200, api_response)

        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get.return_value = mock_resp
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            result = await mkt.search_marketplace(q="mcp", limit=20)
            assert result["total"] == 1
            assert result["page"] == 1
            assert result["hasMore"] is False
            assert len(result["results"]) == 1
            assert result["results"][0]["slug"] == "mcp-builder"
            assert result["results"][0]["owner"] == "anthropics"

    @pytest.mark.asyncio
    async def test_empty_results(self):
        api_response = {"data": [], "total": 0, "page": 1, "totalPages": 0, "hasMore": False}
        mock_resp = _mock_response(200, api_response)

        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get.return_value = mock_resp
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            result = await mkt.search_marketplace(q="nonexistent")
            assert result["results"] == []
            assert result["total"] == 0

    @pytest.mark.asyncio
    async def test_pagination_params(self):
        api_response = {
            "data": [{"slug": f"s{i}", "githubOwner": f"o{i}", "githubRepo": f"r{i}"} for i in range(20)],
            "total": 500,
            "page": 3,
            "totalPages": 25,
            "hasMore": True,
        }
        mock_resp = _mock_response(200, api_response)

        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get.return_value = mock_resp
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            result = await mkt.search_marketplace(q="test", page=3, limit=20)
            assert result["page"] == 3
            assert result["totalPages"] == 25
            assert result["hasMore"] is True
            assert len(result["results"]) == 20

    @pytest.mark.asyncio
    async def test_limit_capped_at_max(self):
        api_response = {"data": [], "total": 0, "page": 1, "totalPages": 0, "hasMore": False}
        mock_resp = _mock_response(200, api_response)

        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get.return_value = mock_resp
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            await mkt.search_marketplace(q="test", limit=500)
            call_args = client_instance.get.call_args
            assert call_args[1]["params"]["limit"] == 100

    @pytest.mark.asyncio
    async def test_browse_no_query(self):
        """Empty q= param browses all skills."""
        api_response = {
            "data": [{"slug": "s1", "githubOwner": "o", "githubRepo": "r"}],
            "total": 107000,
            "page": 1,
            "totalPages": 1070,
            "hasMore": True,
        }
        mock_resp = _mock_response(200, api_response)

        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get.return_value = mock_resp
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            result = await mkt.search_marketplace(q="", page=1, limit=20)
            assert result["total"] == 107000
            assert result["hasMore"] is True

    @pytest.mark.asyncio
    async def test_sort_and_category(self):
        """Sort and category params are forwarded to the API."""
        api_response = {"data": [], "total": 0, "page": 1, "totalPages": 0, "hasMore": False}
        mock_resp = _mock_response(200, api_response)

        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get.return_value = mock_resp
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            await mkt.search_marketplace(q="test", sort="trending", category="development")
            call_args = client_instance.get.call_args
            params = call_args[1]["params"]
            assert params["sort"] == "trending"
            assert params["category"] == "development"


# ---------------------------------------------------------------------------
# scan_github_repo — finds every SKILL.md regardless of directory layout
# ---------------------------------------------------------------------------

class TestScanGithubRepo:
    @pytest.mark.asyncio
    async def test_cursor_skills_layout(self):
        """Repos with .cursor/skills/<slug>/SKILL.md layout."""
        tree_data = {
            "tree": [
                _tree_entry(".cursor/skills/frappe-scaffold/SKILL.md"),
                _tree_entry(".cursor/skills/frappe-builder/SKILL.md"),
                _tree_entry("README.md"),
            ]
        }

        async def mock_get(url, **kwargs):
            if "git/trees/main" in url:
                return _mock_response(200, tree_data)
            if "raw.githubusercontent" in url:
                return _mock_response(200, text=SKILL_MD_FRAPPE)
            return _mock_response(404)

        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get = mock_get
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            results = await mkt.scan_github_repo("vyogotech", "frappe-apps-manager")
            assert len(results) == 2
            slugs = {r["slug"] for r in results}
            assert "frappe-scaffold" in slugs
            assert "frappe-builder" in slugs

    @pytest.mark.asyncio
    async def test_skills_dir_layout(self):
        """Repos with skills/<slug>/SKILL.md layout."""
        tree_data = {
            "tree": [
                _tree_entry("skills/mcp-builder/SKILL.md"),
                _tree_entry("skills/frontend-design/SKILL.md"),
            ]
        }

        async def mock_get(url, **kwargs):
            if "git/trees/main" in url:
                return _mock_response(200, tree_data)
            if "raw.githubusercontent" in url:
                return _mock_response(200, text=SKILL_MD_NO_FM)
            return _mock_response(404)

        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get = mock_get
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            results = await mkt.scan_github_repo("anthropics", "skills")
            assert len(results) == 2

    @pytest.mark.asyncio
    async def test_agents_skills_layout(self):
        """Repos with .agents/skills/<slug>/SKILL.md (gapmiss/obsidian-plugin-skill)."""
        tree_data = {
            "tree": [
                _tree_entry(".agents/skills/obsidian/SKILL.md"),
                _tree_entry(".agents/skills/obsidian/references/eslint-rules.md"),
                _tree_entry("tools/install.sh"),
                _tree_entry("README.md"),
            ]
        }

        async def mock_get(url, **kwargs):
            if "git/trees/main" in url:
                return _mock_response(200, tree_data)
            if "raw.githubusercontent" in url and "SKILL.md" in url:
                return _mock_response(200, text="---\nname: obsidian\ndescription: Obsidian plugin dev\n---\n# Obsidian")
            return _mock_response(404)

        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get = mock_get
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            results = await mkt.scan_github_repo("gapmiss", "obsidian-plugin-skill")
            assert len(results) == 1
            assert results[0]["slug"] == "obsidian"
            assert results[0]["name"] == "obsidian"
            assert ".agents/skills/obsidian/SKILL.md" == results[0]["path"]

    @pytest.mark.asyncio
    async def test_flat_layout(self):
        """Flat monorepo: <slug>/SKILL.md at root level."""
        tree_data = {
            "tree": [
                _tree_entry("agent-browser/SKILL.md"),
                _tree_entry("brainstorming/SKILL.md"),
                _tree_entry("context7-cli/SKILL.md"),
            ]
        }

        async def mock_get(url, **kwargs):
            if "git/trees/main" in url:
                return _mock_response(200, tree_data)
            if "raw.githubusercontent" in url:
                return _mock_response(200, text=SKILL_MD_NO_FM)
            return _mock_response(404)

        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get = mock_get
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            results = await mkt.scan_github_repo("mxyhi", "ok-skills")
            assert len(results) == 3
            slugs = {r["slug"] for r in results}
            assert slugs == {"agent-browser", "brainstorming", "context7-cli"}

    @pytest.mark.asyncio
    async def test_nested_packs(self):
        """Nested vendored packs (hyperframes/gsap/SKILL.md)."""
        tree_data = {
            "tree": [
                _tree_entry("agent-browser/SKILL.md"),
                _tree_entry("hyperframes/gsap/SKILL.md"),
                _tree_entry("hyperframes/hyperframes-cli/SKILL.md"),
                _tree_entry("impeccable/adapt/SKILL.md"),
            ]
        }

        async def mock_get(url, **kwargs):
            if "git/trees/main" in url:
                return _mock_response(200, tree_data)
            if "raw.githubusercontent" in url:
                return _mock_response(200, text=SKILL_MD_NO_FM)
            return _mock_response(404)

        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get = mock_get
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            results = await mkt.scan_github_repo("mxyhi", "ok-skills")
            assert len(results) == 4
            slugs = {r["slug"] for r in results}
            assert "gsap" in slugs
            assert "adapt" in slugs

    @pytest.mark.asyncio
    async def test_all_layouts_returned(self):
        """When a repo has skills in multiple layouts, all are returned."""
        tree_data = {
            "tree": [
                _tree_entry("skills/mcp-builder/SKILL.md"),
                _tree_entry("agent-browser/SKILL.md"),
            ]
        }

        async def mock_get(url, **kwargs):
            if "git/trees/main" in url:
                return _mock_response(200, tree_data)
            if "raw.githubusercontent" in url:
                return _mock_response(200, text=SKILL_MD_NO_FM)
            return _mock_response(404)

        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get = mock_get
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            results = await mkt.scan_github_repo("org", "repo")
            assert len(results) == 2

    @pytest.mark.asyncio
    async def test_root_skill_md(self):
        """Single-skill repos with SKILL.md at root."""
        tree_data = {
            "tree": [
                _tree_entry("SKILL.md"),
                _tree_entry("README.md"),
            ]
        }

        async def mock_get(url, **kwargs):
            if "git/trees/main" in url:
                return _mock_response(200, tree_data)
            if "raw.githubusercontent" in url:
                return _mock_response(200, text=SKILL_MD_FRAPPE)
            return _mock_response(404)

        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get = mock_get
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            results = await mkt.scan_github_repo("owner", "single-skill")
            assert len(results) == 1
            assert results[0]["slug"] == "single-skill"
            assert results[0]["path"] == "SKILL.md"

    @pytest.mark.asyncio
    async def test_skill_singular_prefix(self):
        """Repos with skill/SKILL.md (blacktop/ipsw-skill)."""
        tree_data = {
            "tree": [
                _tree_entry("skill/SKILL.md"),
                _tree_entry("skill/references/dyld.md"),
            ]
        }

        async def mock_get(url, **kwargs):
            if "git/trees/main" in url:
                return _mock_response(200, tree_data)
            if "raw.githubusercontent" in url and "SKILL.md" in url:
                return _mock_response(200, text="---\nname: ipsw\ndescription: Apple RE\n---\n# ipsw")
            return _mock_response(404)

        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get = mock_get
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            results = await mkt.scan_github_repo("blacktop", "ipsw-skill")
            assert len(results) == 1
            assert results[0]["name"] == "ipsw"

    @pytest.mark.asyncio
    async def test_no_skills_found(self):
        """Repos with no SKILL.md at all."""
        tree_data = {
            "tree": [
                _tree_entry("README.md"),
                _tree_entry("docs/index.html"),
            ]
        }

        async def mock_get(url, **kwargs):
            if "git/trees/main" in url:
                return _mock_response(200, tree_data)
            return _mock_response(404)

        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get = mock_get
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            results = await mkt.scan_github_repo("microsoft", "skills")
            assert results == []

    @pytest.mark.asyncio
    async def test_falls_back_to_master_branch(self):
        """If main doesn't exist, try master."""
        tree_data = {"tree": [_tree_entry("SKILL.md")]}

        async def mock_get(url, **kwargs):
            if "git/trees/main" in url:
                return _mock_response(404)
            if "git/trees/master" in url:
                return _mock_response(200, tree_data)
            if "raw.githubusercontent" in url:
                return _mock_response(200, text=SKILL_MD_NO_FM)
            return _mock_response(404)

        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get = mock_get
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            results = await mkt.scan_github_repo("owner", "legacy-repo")
            assert len(results) == 1

    @pytest.mark.asyncio
    async def test_repo_not_found(self):
        """Non-existent repo returns empty list."""
        async def mock_get(url, **kwargs):
            return _mock_response(404)

        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get = mock_get
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            results = await mkt.scan_github_repo("nonexistent", "repo")
            assert results == []

    @pytest.mark.asyncio
    async def test_rate_limit_raw_fallback(self):
        """When tree API returns 403, probe raw URLs directly."""
        async def mock_get(url, **kwargs):
            if "git/trees/" in url:
                return _mock_response(403)
            if "raw.githubusercontent" in url and "README.md" in url:
                return _mock_response(200, text="# readme")
            if "raw.githubusercontent" in url and "skill/SKILL.md" in url:
                return _mock_response(200, text="---\nname: ipsw\ndescription: Apple RE\n---\n# ipsw")
            if "raw.githubusercontent" in url:
                return _mock_response(404)
            return _mock_response(404)

        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get = mock_get
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            results = await mkt.scan_github_repo("blacktop", "ipsw-skill")
            assert len(results) == 1
            assert results[0]["name"] == "ipsw"


# ---------------------------------------------------------------------------
# fetch_skill_md
# ---------------------------------------------------------------------------

class TestFetchSkillMd:
    @pytest.mark.asyncio
    async def test_github_path_used_first(self):
        """When github_path is provided (from agentskill.sh), use it directly."""
        async def mock_get(url, **kwargs):
            if "extensions/acpx/skills/acp-router/SKILL.md" in url and "/main/" in url:
                return _mock_response(200, text=SKILL_MD_FRAPPE)
            return _mock_response(404)

        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get = mock_get
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            content = await mkt.fetch_skill_md(
                "openclaw", "openclaw", "acp-router",
                github_path="extensions/acpx/skills/acp-router/SKILL.md",
            )
            assert "frappe-containerfile-generator" in content

    @pytest.mark.asyncio
    async def test_case_insensitive_github_path(self):
        """skill.md (lowercase) from agentskill.sh should work."""
        async def mock_get(url, **kwargs):
            if "skill.md" in url and "/main/" in url:
                return _mock_response(200, text=SKILL_MD_NO_FM)
            return _mock_response(404)

        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get = mock_get
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            content = await mkt.fetch_skill_md(
                "hawkli-1994", "k8s-operator-skills", "k8s-operator",
                github_path="skill.md",
            )
            assert "Simple skill" in content

    @pytest.mark.asyncio
    async def test_monorepo_skills_prefix(self):
        """Should find skills/<slug>/SKILL.md on main branch."""
        async def mock_get(url, **kwargs):
            if "skills/mcp-builder/SKILL.md" in url and "/main/" in url:
                return _mock_response(200, text=SKILL_MD_FRAPPE)
            return _mock_response(404)

        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get = mock_get
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            content = await mkt.fetch_skill_md("anthropics", "skills", "mcp-builder")
            assert "frappe-containerfile-generator" in content

    @pytest.mark.asyncio
    async def test_root_skill_md(self):
        """Should find SKILL.md at root."""
        async def mock_get(url, **kwargs):
            if url.endswith("/main/SKILL.md"):
                return _mock_response(200, text=SKILL_MD_NO_FM)
            return _mock_response(404)

        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get = mock_get
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            content = await mkt.fetch_skill_md("owner", "repo", "")
            assert "Simple skill" in content

    @pytest.mark.asyncio
    async def test_not_found_raises(self):
        """Should raise ValueError if no SKILL.md found anywhere."""
        async def mock_get(url, **kwargs):
            return _mock_response(404)

        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get = mock_get
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            with pytest.raises(ValueError, match="SKILL.md not found"):
                await mkt.fetch_skill_md("owner", "repo", "slug")

    @pytest.mark.asyncio
    async def test_cursor_skills_prefix(self):
        """Should find .cursor/skills/<slug>/SKILL.md."""
        async def mock_get(url, **kwargs):
            if ".cursor/skills/my-skill/SKILL.md" in url:
                return _mock_response(200, text=SKILL_MD_NO_FM)
            return _mock_response(404)

        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get = mock_get
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            content = await mkt.fetch_skill_md("owner", "repo", "my-skill")
            assert "Simple skill" in content

    @pytest.mark.asyncio
    async def test_skill_singular_prefix(self):
        """Should find skill/<slug>/SKILL.md (singular 'skill' dir)."""
        async def mock_get(url, **kwargs):
            if "skill/ipsw/SKILL.md" in url and "/main/" in url:
                return _mock_response(200, text=SKILL_MD_NO_FM)
            return _mock_response(404)

        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get = mock_get
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            content = await mkt.fetch_skill_md("blacktop", "ipsw-skill", "ipsw")
            assert "Simple skill" in content

    @pytest.mark.asyncio
    async def test_skill_root_fallback_no_slug(self):
        """Should find skill/SKILL.md when no slug is given."""
        async def mock_get(url, **kwargs):
            if url.endswith("/main/skill/SKILL.md"):
                return _mock_response(200, text=SKILL_MD_FRAPPE)
            return _mock_response(404)

        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get = mock_get
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            content = await mkt.fetch_skill_md("blacktop", "ipsw-skill", "")
            assert "frappe-containerfile-generator" in content


# ---------------------------------------------------------------------------
# trigger_reindex
# ---------------------------------------------------------------------------

class TestTriggerReindex:
    @pytest.mark.asyncio
    async def test_success(self):
        mock_resp = _mock_response(202)

        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.post.return_value = mock_resp
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            await mkt.trigger_reindex("http://skills:8090")

    @pytest.mark.asyncio
    async def test_failure_is_non_fatal(self):
        with patch("marketplace.httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.post.side_effect = httpx.RequestError("conn refused")
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            await mkt.trigger_reindex("http://skills:8090")
