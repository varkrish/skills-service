"""
Tests for the MCP server module.
TDD: Tests written before implementation.
Validates that skills are exposed as MCP tools (query_skills, list_skills, reload_index).
"""
import asyncio
import json
import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _write_skill(base: Path, name: str, content: str):
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content)


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
def mock_index():
    idx = MagicMock()
    idx.ready = True
    idx.skills = [{"name": "frappe-api"}, {"name": "react-hooks"}]
    idx.query.return_value = [
        {"skill_name": "frappe-api", "content": "Use @whitelist", "tags": ["python"]},
    ]
    return idx


def _tool_names(mcp) -> list:
    return [t.name for t in asyncio.get_event_loop().run_until_complete(mcp.list_tools())]


class TestCreateMcpServer:

    def test_returns_fastmcp_instance(self, skills_dir):
        from mcp_server import create_mcp_server
        from fastmcp import FastMCP
        mcp = create_mcp_server(skills_dir)
        assert isinstance(mcp, FastMCP)

    def test_has_list_skills_tool(self, skills_dir, mock_index):
        from mcp_server import create_mcp_server
        mcp = create_mcp_server(skills_dir, index=mock_index)
        assert "list_skills" in _tool_names(mcp)

    def test_has_query_skills_tool(self, skills_dir, mock_index):
        from mcp_server import create_mcp_server
        mcp = create_mcp_server(skills_dir, index=mock_index)
        assert "query_skills" in _tool_names(mcp)

    def test_has_reload_index_tool(self, skills_dir, mock_index):
        from mcp_server import create_mcp_server
        mcp = create_mcp_server(skills_dir, index=mock_index)
        assert "reload_index" in _tool_names(mcp)

    def test_without_index_only_list_skills(self, skills_dir):
        from mcp_server import create_mcp_server
        mcp = create_mcp_server(skills_dir, index=None)
        names = _tool_names(mcp)
        assert "list_skills" in names
        assert "query_skills" not in names
        assert "reload_index" not in names


class TestMcpToolFunctions:

    def test_list_skills_returns_skill_names(self, skills_dir):
        from mcp_server import _list_skills_fn
        result = _list_skills_fn(skills_dir)
        assert "frappe-api" in result
        assert "react-hooks" in result
        parsed = json.loads(result)
        assert len(parsed) == 2

    def test_query_skills_returns_matching_content(self):
        from mcp_server import _make_query_fn

        mock_index = MagicMock()
        mock_index.ready = True
        mock_index.query.return_value = [
            {"skill_name": "frappe-api", "content": "Use @whitelist", "tags": ["python"]},
        ]

        query_fn = _make_query_fn(mock_index)
        result = query_fn("How do I expose an API?", top_k=2, tags=None)
        assert "frappe-api" in result
        assert "@whitelist" in result
        mock_index.query.assert_called_once_with("How do I expose an API?", 2, None)

    def test_query_skills_with_tag_filter(self):
        from mcp_server import _make_query_fn

        mock_index = MagicMock()
        mock_index.ready = True
        mock_index.query.return_value = [
            {"skill_name": "react-hooks", "content": "useCallback", "tags": ["react"]},
        ]

        query_fn = _make_query_fn(mock_index)
        result = query_fn("memoization", top_k=3, tags="react")
        assert "react-hooks" in result
        mock_index.query.assert_called_once_with("memoization", 3, ["react"])

    def test_query_skills_index_not_ready(self):
        from mcp_server import _make_query_fn

        mock_index = MagicMock()
        mock_index.ready = False

        query_fn = _make_query_fn(mock_index)
        result = query_fn("anything")
        assert "not ready" in result.lower()

    def test_query_skills_no_results(self):
        from mcp_server import _make_query_fn

        mock_index = MagicMock()
        mock_index.ready = True
        mock_index.query.return_value = []

        query_fn = _make_query_fn(mock_index)
        result = query_fn("nonexistent topic")
        assert "no matching" in result.lower()

    def test_reload_triggers_rebuild(self):
        from mcp_server import _make_reload_fn

        mock_index = MagicMock()
        mock_index.skills = [{"name": "a"}, {"name": "b"}]
        reload_fn = _make_reload_fn(mock_index)
        result = reload_fn()
        mock_index.build.assert_called_once()
        assert "rebuilt" in result.lower()
        assert "2" in result
