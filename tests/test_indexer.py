"""
Unit tests for SkillIndex.query() relevance filtering.
TDD: Uses a mocked llama_index engine to avoid the embedding model dependency.
"""
from pathlib import Path
from unittest.mock import MagicMock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from indexer import SkillIndex


def _make_node(skill_name: str, text: str, score: float, tags=None):
    node = MagicMock()
    node.text = text
    node.score = score
    node.metadata = {"skill_name": skill_name, "tags": tags or []}
    return node


def _index_with_nodes(nodes, min_score=0.68):
    index = SkillIndex.__new__(SkillIndex)
    index.min_score = min_score
    index._ready = True
    mock_retriever = MagicMock()
    mock_retriever.retrieve.return_value = nodes
    mock_vector_index = MagicMock()
    mock_vector_index.as_retriever.return_value = mock_retriever
    index._index = mock_vector_index
    return index


class TestSkillIndexRelevanceFiltering:

    def test_drops_results_below_min_score(self):
        nodes = [
            _make_node("react-component-style", "Use hooks", 0.55),
            _make_node("frappe-api-patterns", "Use whitelist", 0.51),
        ]
        index = _index_with_nodes(nodes)

        results = index.query("Apache Camel route builder", top_k=3)

        assert results == []

    def test_keeps_results_at_or_above_min_score(self):
        nodes = [
            _make_node("frappe-api-patterns", "Use @whitelist", 0.82),
            _make_node("react-component-style", "Use hooks", 0.55),
        ]
        index = _index_with_nodes(nodes)

        results = index.query("Frappe whitelist decorator", top_k=3)

        assert len(results) == 1
        assert results[0]["skill_name"] == "frappe-api-patterns"
        assert results[0]["score"] == 0.82

    def test_min_score_override_widens_results(self):
        nodes = [_make_node("react-component-style", "Use hooks", 0.55)]
        index = _index_with_nodes(nodes)

        results = index.query("anything", top_k=3, min_score=0.0)

        assert len(results) == 1

    def test_not_ready_returns_empty(self):
        index = SkillIndex.__new__(SkillIndex)
        index._ready = False
        index._index = None

        assert index.query("anything") == []

    def test_missing_score_attribute_is_not_filtered(self):
        node = MagicMock(spec=["text", "metadata"])
        node.text = "content"
        node.metadata = {"skill_name": "legacy-skill", "tags": []}
        index = _index_with_nodes([node])

        results = index.query("anything", top_k=3)

        assert len(results) == 1
        assert results[0]["skill_name"] == "legacy-skill"
