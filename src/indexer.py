"""
Skill indexer — builds and caches a VectorStoreIndex from discovered skills.
"""
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from llama_index.core import Document, VectorStoreIndex, StorageContext, load_index_from_storage
from llama_index.core import Settings as LlamaSettings
from llama_index.embeddings.huggingface import HuggingFaceEmbedding

from discovery import discover_skills, compute_content_hash

logger = logging.getLogger(__name__)

# Below this cosine-similarity score, a "match" is considered noise rather than
# a genuine hit — e.g. querying for "Apache Camel" against an index that only
# has React/Frappe/Python skills should return NOTHING, not the closest-available
# irrelevant skill. Irrelevant skills injected as "ground truth" mislead agents
# into using the wrong framework's conventions.
#
# Calibrated empirically against BAAI/bge-small-en-v1.5 with this repo's skill
# set: unrelated queries (e.g. "Apache Camel route builder", "Java Spring Boot
# MVC") score ~0.47-0.63 against every indexed skill (there's an embedding
# "software text" baseline floor — it's never near 0), while genuine matches
# (e.g. a Frappe query against the frappe-api-patterns skill, or a pytest query
# against python-tdd) score ~0.70-0.82. 0.68 sits in the gap. Tune via
# SKILLS_MIN_SCORE if the skill set or embedding model changes.
DEFAULT_MIN_SCORE = float(os.environ.get("SKILLS_MIN_SCORE", "0.68"))


class SkillIndex:
    """Manages the vector index for skill documents."""

    def __init__(self, base_dirs, cache_dir: Path, min_score: float = DEFAULT_MIN_SCORE):
        if isinstance(base_dirs, Path):
            base_dirs = [base_dirs]
        self.base_dirs: List[Path] = list(base_dirs)
        self.cache_dir = cache_dir
        self.min_score = min_score
        self._index: Optional[VectorStoreIndex] = None
        self._skills: List[Dict[str, Any]] = []
        self._ready = False

        LlamaSettings.embed_model = HuggingFaceEmbedding(model_name="BAAI/bge-small-en-v1.5")
        LlamaSettings.llm = None

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def skills(self) -> List[Dict[str, Any]]:
        return self._skills

    def build(self):
        """Discover skills, check cache, build or load index."""
        self._ready = False
        self._skills = []
        for bd in self.base_dirs:
            self._skills.extend(discover_skills(bd))

        if not self._skills:
            logger.warning("No skills found in %s — index will be empty", self.base_dirs)
            self._index = VectorStoreIndex([])
            self._ready = True
            return

        combined_hashes = ":".join(compute_content_hash(bd) for bd in self.base_dirs)
        current_hash = hashlib.sha256(combined_hashes.encode()).hexdigest()
        meta_path = self.cache_dir / "meta.json"

        if self._cache_valid(meta_path, current_hash):
            logger.info("Cache is valid — loading index from disk")
            storage_context = StorageContext.from_defaults(persist_dir=str(self.cache_dir))
            self._index = load_index_from_storage(storage_context)
        else:
            logger.info("Cache stale or missing — building index from %d skills", len(self._skills))
            documents = self._build_documents()
            self._index = VectorStoreIndex.from_documents(documents)
            self._persist(current_hash)

        self._ready = True
        logger.info("Index ready: %d skills indexed", len(self._skills))

    def query(
        self,
        query_text: str,
        top_k: int = 3,
        tags: Optional[List[str]] = None,
        min_score: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Query the index and return matching skill content.

        Results below ``min_score`` (cosine similarity) are dropped rather than
        padded with the closest-available-but-irrelevant skill. When the index
        has no skill for the queried framework, this correctly returns an empty
        list instead of confidently-wrong content.
        """
        if not self._ready or self._index is None:
            return []

        threshold = self.min_score if min_score is None else min_score

        # Over-fetch when filtering by tags/score so post-filtering still has
        # enough candidates to reach top_k where genuinely relevant. Uses a
        # retriever (not as_query_engine) to avoid the LlamaIndex context-size
        # crash from synthesizing a response over all retrieved nodes.
        fetch_k = top_k * 3 if (tags or threshold > 0) else top_k
        retriever = self._index.as_retriever(similarity_top_k=fetch_k)
        nodes = retriever.retrieve(query_text)

        results: List[Dict[str, Any]] = []
        for node in nodes:
            score = getattr(node, "score", None)
            if score is not None and score < threshold:
                continue
            meta = node.metadata
            node_tags = meta.get("tags", [])
            if tags and node_tags and not any(t in node_tags for t in tags):
                continue
            results.append({
                "skill_name": meta.get("skill_name", "unknown"),
                "content": node.text,
                "tags": node_tags,
                "score": score,
            })
            if len(results) >= top_k:
                break

        return results

    def _build_documents(self) -> List[Document]:
        docs: List[Document] = []
        for skill in self._skills:
            tags_str = json.dumps(skill["tags"])
            for md_file in skill["files"]:
                content = md_file.read_text()
                doc = Document(
                    text=content,
                    metadata={
                        "skill_name": skill["name"],
                        "tags": skill["tags"],
                        "source_file": str(md_file.name),
                    },
                    excluded_llm_metadata_keys=["tags", "source_file"],
                )
                docs.append(doc)
        logger.info("Built %d documents from %d skills", len(docs), len(self._skills))
        return docs

    def _persist(self, content_hash: str):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._index.storage_context.persist(persist_dir=str(self.cache_dir))
        meta = {
            "content_hash": content_hash,
            "skill_count": len(self._skills),
            "built_at": datetime.now(timezone.utc).isoformat(),
        }
        (self.cache_dir / "meta.json").write_text(json.dumps(meta, indent=2))
        logger.info("Index persisted to %s", self.cache_dir)

    def _cache_valid(self, meta_path: Path, current_hash: str) -> bool:
        if not meta_path.exists():
            return False
        try:
            meta = json.loads(meta_path.read_text())
            return meta.get("content_hash") == current_hash
        except (json.JSONDecodeError, KeyError):
            return False
