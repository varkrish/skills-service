"""
Skill discovery — scans a base directory for Cursor-style skill folders.

Each skill is a subdirectory containing a SKILL.md with YAML frontmatter:
  ---
  name: skill-name
  description: What this skill does
  tags: [python, frappe]
  ---
"""
import hashlib
import logging
from pathlib import Path
from typing import Any, Dict, List

import yaml

logger = logging.getLogger(__name__)


def discover_skills(base_dir: Path) -> List[Dict[str, Any]]:
    """Scan base_dir for skill folders and return parsed metadata."""
    skills: List[Dict[str, Any]] = []
    if not base_dir.exists():
        logger.warning("Skills base dir does not exist: %s", base_dir)
        return skills

    for entry in sorted(base_dir.iterdir()):
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.exists():
            continue

        frontmatter = _parse_frontmatter(skill_md.read_text())
        md_files = sorted(entry.glob("*.md"))
        skills.append({
            "name": frontmatter.get("name", entry.name),
            "description": frontmatter.get("description", ""),
            "tags": frontmatter.get("tags", []),
            "dir": entry,
            "files": md_files,
            "file_count": len(md_files),
        })

    logger.info("Discovered %d skill(s) in %s", len(skills), base_dir)
    return skills


def compute_content_hash(base_dir: Path) -> str:
    """Compute a deterministic SHA-256 hash of all skill file contents.

    Hash = SHA256(sorted(relative_path + SHA256(file_content)) for each .md file).
    """
    hasher = hashlib.sha256()
    file_hashes: List[str] = []

    for entry in sorted(base_dir.iterdir()):
        if not entry.is_dir():
            continue
        if not (entry / "SKILL.md").exists():
            continue
        for md_file in sorted(entry.glob("*.md")):
            rel = md_file.relative_to(base_dir)
            content_hash = hashlib.sha256(md_file.read_bytes()).hexdigest()
            file_hashes.append(f"{rel}:{content_hash}")

    for fh in file_hashes:
        hasher.update(fh.encode())

    return hasher.hexdigest()


def _parse_frontmatter(text: str) -> Dict[str, Any]:
    """Extract YAML frontmatter from a markdown file."""
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        return yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        logger.warning("Failed to parse YAML frontmatter", exc_info=True)
        return {}
