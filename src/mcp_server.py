"""
MCP server for the Skills Service.

Exposes skills as MCP tools so any MCP-compatible agent (Claude, Cursor, etc.)
can discover and query project skills directly via the MCP protocol.

Tools exposed:
  - query_skills: Semantic search over indexed skill documents
  - list_skills:  List all available skills with metadata
  - reload_index: Trigger a re-index of skill documents
"""
import json
import logging
from pathlib import Path
from typing import List, Optional

from fastmcp import FastMCP

from discovery import discover_skills

logger = logging.getLogger(__name__)


def _list_skills_fn(base_dirs: List[Path]) -> str:
    """List all available skills with their names, descriptions, and tags."""
    skills = []
    for bd in base_dirs:
        skills.extend(discover_skills(bd))
    if not skills:
        return "No skills found."
    result = []
    for s in skills:
        result.append({
            "name": s["name"],
            "description": s["description"],
            "tags": s["tags"],
            "file_count": s["file_count"],
        })
    return json.dumps(result, indent=2)


def _make_query_fn(index):
    """Create a query function bound to a SkillIndex instance."""

    def query_skills(
        query: str,
        top_k: int = 3,
        tags: Optional[str] = None,
    ) -> str:
        """Search project skills and coding guidelines by meaning.

        Returns relevant skill content from indexed SKILL.md files.
        Optionally filter by tags (comma-separated, e.g. 'python,frappe').

        Args:
            query: Natural language question about coding patterns or guidelines.
            top_k: Maximum number of results to return (default: 3).
            tags: Comma-separated tag filter (e.g. 'python,testing'). Only skills
                  matching at least one tag are returned.
        """
        if not index.ready:
            return "Error: Skills index is not ready yet. Try again shortly."

        tag_list: Optional[List[str]] = None
        if tags:
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]

        results = index.query(query, top_k, tag_list)
        if not results:
            return "No matching skills found for your query."

        parts = []
        for r in results:
            parts.append(
                f"## {r['skill_name']} (tags: {', '.join(r['tags'])})\n\n{r['content']}"
            )
        return "\n\n---\n\n".join(parts)

    return query_skills


def _make_reload_fn(index):
    """Create a reload function bound to a SkillIndex instance."""

    def reload_index() -> str:
        """Trigger a re-index of all skill documents.

        Use this after adding, editing, or removing SKILL.md files
        to update the search index.
        """
        index.build()
        return f"Index rebuilt successfully. {len(index.skills)} skills indexed."

    return reload_index


def create_mcp_server(
    base_dirs,
    index=None,
) -> FastMCP:
    """Create a FastMCP server exposing skills as tools.

    Args:
        base_dirs: Path or list of Paths to skill directories.
        index: Optional SkillIndex instance. If None, query_skills and
               reload_index tools are not registered (list_skills still works).
    """
    if isinstance(base_dirs, Path):
        base_dirs = [base_dirs]
    base_dirs = list(base_dirs)

    mcp = FastMCP(
        "Skills Service",
        instructions=(
            "Search and browse project coding skills and guidelines. "
            "Use query_skills for semantic search or list_skills to see "
            "all available skills."
        ),
    )

    @mcp.tool(
        name="list_skills",
        description="List all available project skills with names, descriptions, and tags.",
    )
    def list_skills() -> str:
        return _list_skills_fn(base_dirs)

    if index is not None:
        query_fn = _make_query_fn(index)
        mcp.tool(
            name="query_skills",
            description=(
                "Search project skills and coding guidelines by meaning. "
                "Returns relevant skill content from indexed SKILL.md files. "
                "Optionally filter by comma-separated tags."
            ),
        )(query_fn)

        reload_fn = _make_reload_fn(index)
        mcp.tool(
            name="reload_index",
            description=(
                "Trigger a re-index of all skill documents. "
                "Use after adding, editing, or removing SKILL.md files."
            ),
        )(reload_fn)

    logger.info("MCP server created with tools for base_dirs=%s", base_dirs)
    return mcp
