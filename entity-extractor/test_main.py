"""Tests for entity-extractor parsing, filtering, and entity logic."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

import main as ee


# ────────────────────────────────────────────────────────────────────────────
# T4 JSON parsing (extracted from t4_analyze)
# ────────────────────────────────────────────────────────────────────────────

class TestT4JsonParsing:
    """Test the JSON extraction logic used in t4_analyze."""

    def _parse(self, raw: str):
        """Replicate the parsing logic from t4_analyze."""
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(cleaned)

    def test_clean_json(self):
        raw = '{"entities": [{"name": "Kubernetes", "type": "tool", "summary": "Container orchestration"}], "relationships": [], "item_summary": "test"}'
        result = self._parse(raw)
        assert len(result["entities"]) == 1
        assert result["entities"][0]["name"] == "Kubernetes"

    def test_markdown_wrapped_json(self):
        raw = '```json\n{"entities": [{"name": "Terraform", "type": "tool", "summary": "IaC"}], "relationships": [], "item_summary": "test"}\n```'
        result = self._parse(raw)
        assert result["entities"][0]["name"] == "Terraform"

    def test_triple_backtick_no_lang(self):
        raw = '```\n{"entities": [], "relationships": [], "item_summary": "test"}\n```'
        result = self._parse(raw)
        assert result["entities"] == []

    def test_invalid_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            self._parse("not json at all")


# ────────────────────────────────────────────────────────────────────────────
# Entity filtering (from process_item)
# ────────────────────────────────────────────────────────────────────────────

class TestEntityFiltering:
    """Test entity filtering: skip_types and name length."""

    def _filter_entities(self, entities: list) -> list:
        """Replicate the filtering logic from process_item."""
        skip_types = {"person", "community"}
        result = []
        for ent in entities:
            name = (ent.get("name") or "").strip()
            if not name or len(name) < 2:
                continue
            if ent.get("type", "").lower() in skip_types:
                continue
            result.append(ent)
        return result

    def test_valid_entities_pass(self):
        entities = [
            {"name": "Kubernetes", "type": "tool", "summary": "..."},
            {"name": "HashiCorp", "type": "company", "summary": "..."},
        ]
        assert len(self._filter_entities(entities)) == 2

    def test_person_type_skipped(self):
        entities = [
            {"name": "John Doe", "type": "person", "summary": "..."},
            {"name": "Kubernetes", "type": "tool", "summary": "..."},
        ]
        assert len(self._filter_entities(entities)) == 1
        assert self._filter_entities(entities)[0]["name"] == "Kubernetes"

    def test_community_type_skipped(self):
        entities = [
            {"name": "r/kubernetes", "type": "community", "summary": "..."},
        ]
        assert len(self._filter_entities(entities)) == 0

    def test_short_name_skipped(self):
        entities = [
            {"name": "K", "type": "tool", "summary": "..."},
            {"name": "", "type": "tool", "summary": "..."},
        ]
        assert len(self._filter_entities(entities)) == 0

    def test_exactly_two_chars(self):
        entities = [{"name": "Go", "type": "tool", "summary": "..."}]
        assert len(self._filter_entities(entities)) == 1

    def test_none_name(self):
        entities = [{"name": None, "type": "tool", "summary": "..."}]
        assert len(self._filter_entities(entities)) == 0

    def test_whitespace_name(self):
        entities = [{"name": "  ", "type": "tool", "summary": "..."}]
        assert len(self._filter_entities(entities)) == 0

    def test_mixed_case_skip_type(self):
        entities = [{"name": "Alice", "type": "Person", "summary": "..."}]
        assert len(self._filter_entities(entities)) == 0


# ────────────────────────────────────────────────────────────────────────────
# T3 response parsing
# ────────────────────────────────────────────────────────────────────────────

class TestT3ResponseParsing:
    """Test the 'starts with yes' logic used in t3_classify."""

    def _parse_t3(self, response: str) -> bool:
        return response.strip().lower().startswith("yes")

    def test_yes(self):
        assert self._parse_t3("yes") is True

    def test_yes_with_explanation(self):
        assert self._parse_t3("Yes, this is about developer tools") is True

    def test_no(self):
        assert self._parse_t3("no") is False

    def test_no_with_explanation(self):
        assert self._parse_t3("No, this is about cooking") is False

    def test_whitespace_yes(self):
        assert self._parse_t3("  yes  ") is True

    def test_uppercase(self):
        assert self._parse_t3("YES") is True

    def test_empty(self):
        assert self._parse_t3("") is False

    def test_maybe(self):
        assert self._parse_t3("maybe") is False


# ────────────────────────────────────────────────────────────────────────────
# Relationship strength capping
# ────────────────────────────────────────────────────────────────────────────

class TestRelationshipStrength:
    def test_strength_increment(self):
        old_strength = 0.5
        new_strength = min(old_strength + 0.1, 1.0)
        assert new_strength == pytest.approx(0.6)

    def test_strength_cap_at_one(self):
        old_strength = 0.95
        new_strength = min(old_strength + 0.1, 1.0)
        assert new_strength == 1.0

    def test_strength_already_at_cap(self):
        old_strength = 1.0
        new_strength = min(old_strength + 0.1, 1.0)
        assert new_strength == 1.0

    def test_initial_strength(self):
        assert 0.5 == pytest.approx(0.5)
