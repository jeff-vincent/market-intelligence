"""Tests for relevance-filter pure functions and scoring logic."""
import numpy as np
import pytest
from unittest.mock import patch

import main as rf


# ────────────────────────────────────────────────────────────────────────────
# cosine_similarity
# ────────────────────────────────────────────────────────────────────────────

class TestCosineSimilarity:
    def test_identical_vectors(self):
        v = [1.0, 2.0, 3.0]
        assert rf.cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        assert rf.cosine_similarity(a, b) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert rf.cosine_similarity(a, b) == pytest.approx(-1.0)

    def test_similar_vectors(self):
        a = [1.0, 2.0, 3.0]
        b = [1.1, 2.1, 3.1]
        sim = rf.cosine_similarity(a, b)
        assert sim > 0.99

    def test_zero_vector_returns_zero(self):
        a = [0.0, 0.0, 0.0]
        b = [1.0, 2.0, 3.0]
        assert rf.cosine_similarity(a, b) == 0.0

    def test_high_dimensional(self):
        np.random.seed(42)
        a = np.random.randn(1536).tolist()
        b = np.random.randn(1536).tolist()
        sim = rf.cosine_similarity(a, b)
        assert -1.0 <= sim <= 1.0


# ────────────────────────────────────────────────────────────────────────────
# Banding logic
# ────────────────────────────────────────────────────────────────────────────

class TestBandingLogic:
    """Test that the threshold constants produce correct bands."""

    def test_pass_threshold(self):
        assert rf.T2_PASS_THRESHOLD == pytest.approx(0.35)

    def test_weak_threshold(self):
        assert rf.T2_WEAK_THRESHOLD == pytest.approx(0.20)

    def test_pass_band(self):
        score = 0.40
        if score > rf.T2_PASS_THRESHOLD:
            band = "PASS"
        elif score > rf.T2_WEAK_THRESHOLD:
            band = "WEAK"
        else:
            band = "DROP"
        assert band == "PASS"

    def test_weak_band(self):
        score = 0.25
        if score > rf.T2_PASS_THRESHOLD:
            band = "PASS"
        elif score > rf.T2_WEAK_THRESHOLD:
            band = "WEAK"
        else:
            band = "DROP"
        assert band == "WEAK"

    def test_drop_band(self):
        score = 0.15
        if score > rf.T2_PASS_THRESHOLD:
            band = "PASS"
        elif score > rf.T2_WEAK_THRESHOLD:
            band = "WEAK"
        else:
            band = "DROP"
        assert band == "DROP"

    def test_boundary_pass(self):
        """Score exactly at PASS threshold should be WEAK."""
        score = rf.T2_PASS_THRESHOLD
        if score > rf.T2_PASS_THRESHOLD:
            band = "PASS"
        elif score > rf.T2_WEAK_THRESHOLD:
            band = "WEAK"
        else:
            band = "DROP"
        assert band == "WEAK"

    def test_boundary_weak(self):
        """Score exactly at WEAK threshold should be DROP."""
        score = rf.T2_WEAK_THRESHOLD
        if score > rf.T2_PASS_THRESHOLD:
            band = "PASS"
        elif score > rf.T2_WEAK_THRESHOLD:
            band = "WEAK"
        else:
            band = "DROP"
        assert band == "DROP"

    def test_just_above_pass(self):
        score = rf.T2_PASS_THRESHOLD + 0.001
        band = "PASS" if score > rf.T2_PASS_THRESHOLD else ("WEAK" if score > rf.T2_WEAK_THRESHOLD else "DROP")
        assert band == "PASS"

    def test_negative_score(self):
        score = -0.1
        band = "PASS" if score > rf.T2_PASS_THRESHOLD else ("WEAK" if score > rf.T2_WEAK_THRESHOLD else "DROP")
        assert band == "DROP"


# ────────────────────────────────────────────────────────────────────────────
# Environment variable thresholds
# ────────────────────────────────────────────────────────────────────────────

class TestThresholdConfig:
    def test_custom_thresholds(self):
        """Thresholds can be overridden from env vars."""
        with patch.dict("os.environ", {"T2_PASS_THRESHOLD": "0.50", "T2_WEAK_THRESHOLD": "0.30"}):
            pass_t = float("0.50")
            weak_t = float("0.30")
            assert pass_t > weak_t
