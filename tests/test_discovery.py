"""
Tests for skill discovery — scanning skill folders and parsing SKILL.md frontmatter.
TDD: Written before implementation.
"""
import pytest
import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _write_skill(base: Path, name: str, content: str):
    """Helper: create a skill folder with SKILL.md."""
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content)
    return skill_dir


class TestDiscoverSkills:

    def test_discovers_skill_folders(self, tmp_path):
        from discovery import discover_skills
        _write_skill(tmp_path, "react-patterns", "---\nname: react-patterns\ndescription: React\n---\n# React")
        _write_skill(tmp_path, "python-tdd", "---\nname: python-tdd\ndescription: TDD\n---\n# TDD")

        skills = discover_skills(tmp_path)
        names = {s["name"] for s in skills}
        assert names == {"react-patterns", "python-tdd"}

    def test_parses_frontmatter_tags(self, tmp_path):
        from discovery import discover_skills
        _write_skill(tmp_path, "frappe-api", (
            "---\nname: frappe-api\ndescription: Frappe API\ntags: [python, frappe]\n---\n# Frappe"
        ))
        skills = discover_skills(tmp_path)
        assert skills[0]["tags"] == ["python", "frappe"]

    def test_missing_tags_defaults_to_empty(self, tmp_path):
        from discovery import discover_skills
        _write_skill(tmp_path, "no-tags", "---\nname: no-tags\ndescription: No tags\n---\n# Content")
        skills = discover_skills(tmp_path)
        assert skills[0]["tags"] == []

    def test_missing_name_uses_directory_name(self, tmp_path):
        from discovery import discover_skills
        _write_skill(tmp_path, "my-skill", "---\ndescription: Test skill\n---\n# Content")
        skills = discover_skills(tmp_path)
        assert skills[0]["name"] == "my-skill"

    def test_ignores_dirs_without_skill_md(self, tmp_path):
        from discovery import discover_skills
        _write_skill(tmp_path, "valid-skill", "---\nname: valid\ndescription: V\n---\n# V")
        (tmp_path / "not-a-skill").mkdir()
        (tmp_path / "not-a-skill" / "README.md").write_text("# Readme")

        skills = discover_skills(tmp_path)
        assert len(skills) == 1

    def test_collects_supporting_md_files(self, tmp_path):
        from discovery import discover_skills
        skill_dir = _write_skill(tmp_path, "multi", "---\nname: multi\ndescription: M\n---\n# Main")
        (skill_dir / "reference.md").write_text("# Detailed reference")
        (skill_dir / "examples.md").write_text("# Examples")

        skills = discover_skills(tmp_path)
        assert skills[0]["file_count"] == 3  # SKILL.md + reference.md + examples.md

    def test_empty_base_dir(self, tmp_path):
        from discovery import discover_skills
        skills = discover_skills(tmp_path)
        assert skills == []


class TestContentHash:

    def test_same_content_same_hash(self, tmp_path):
        from discovery import compute_content_hash
        _write_skill(tmp_path, "s1", "---\nname: s1\ndescription: D\n---\n# Body")
        h1 = compute_content_hash(tmp_path)
        h2 = compute_content_hash(tmp_path)
        assert h1 == h2

    def test_different_content_different_hash(self, tmp_path):
        from discovery import compute_content_hash
        _write_skill(tmp_path, "s1", "---\nname: s1\ndescription: D\n---\n# V1")
        h1 = compute_content_hash(tmp_path)
        (tmp_path / "s1" / "SKILL.md").write_text("---\nname: s1\ndescription: D\n---\n# V2")
        h2 = compute_content_hash(tmp_path)
        assert h1 != h2

    def test_hash_is_deterministic_across_calls(self, tmp_path):
        from discovery import compute_content_hash
        _write_skill(tmp_path, "a", "---\nname: a\ndescription: D\n---\n# A")
        _write_skill(tmp_path, "b", "---\nname: b\ndescription: D\n---\n# B")
        h1 = compute_content_hash(tmp_path)
        h2 = compute_content_hash(tmp_path)
        assert h1 == h2
