"""
PromptWars — Unit Tests for the scoring API.
Run with: pytest tests/ -v
No real GCP credentials required — all external calls are mocked.
"""

from __future__ import annotations

import hashlib
import json
import sys
import types
from statistics import mean
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Stub out heavyweight GCP / vertexai imports BEFORE importing main
# so tests never need real credentials.
# ---------------------------------------------------------------------------

def _make_vertexai_stub() -> types.ModuleType:
    stub = types.ModuleType("vertexai")
    stub.init = MagicMock()

    gen_models = types.ModuleType("vertexai.generative_models")
    gen_models.GenerativeModel = MagicMock()
    gen_models.GenerationConfig = MagicMock()
    gen_models.Part = MagicMock()
    stub.generative_models = gen_models

    return stub


def _make_google_stubs() -> None:
    # google.cloud.aiplatform / google.api_core.exceptions
    google_mod = types.ModuleType("google")
    cloud_mod = types.ModuleType("google.cloud")
    aiplatform_mod = types.ModuleType("google.cloud.aiplatform")
    api_core_mod = types.ModuleType("google.api_core")
    exceptions_mod = types.ModuleType("google.api_core.exceptions")

    class GoogleAPIError(Exception):
        pass

    exceptions_mod.GoogleAPIError = GoogleAPIError

    google_mod.cloud = cloud_mod
    google_mod.api_core = api_core_mod
    api_core_mod.exceptions = exceptions_mod
    cloud_mod.aiplatform = aiplatform_mod

    sys.modules.setdefault("google", google_mod)
    sys.modules.setdefault("google.cloud", cloud_mod)
    sys.modules.setdefault("google.cloud.aiplatform", aiplatform_mod)
    sys.modules.setdefault("google.api_core", api_core_mod)
    sys.modules.setdefault("google.api_core.exceptions", exceptions_mod)


_make_google_stubs()
vertexai_stub = _make_vertexai_stub()
sys.modules.setdefault("vertexai", vertexai_stub)
sys.modules.setdefault("vertexai.generative_models", vertexai_stub.generative_models)

# Now it is safe to import from main
from main import (  # noqa: E402
    DEFAULT_DIMENSIONS,
    DimensionScore,
    ScoreRequest,
    ScoreResponse,
    _build_cache_key,
    _compute_response,
)


# ===========================================================================
# 1. TestHashFunction
# ===========================================================================


class TestHashFunction:
    """Tests for _build_cache_key — hash stability, uniqueness, format."""

    PROMPT = "Write a haiku about autumn."
    TASK = "creative writing"

    def _hash_only(self, prompt: str, task: str, rubric=None) -> str:
        """Return just the 24-char digest portion."""
        _, digest = _build_cache_key(prompt, task, rubric)
        return digest

    def _full_key(self, prompt: str, task: str, rubric=None) -> str:
        key, _ = _build_cache_key(prompt, task, rubric)
        return key

    # --- determinism ---

    def test_same_inputs_produce_same_hash(self):
        h1 = self._hash_only(self.PROMPT, self.TASK)
        h2 = self._hash_only(self.PROMPT, self.TASK)
        assert h1 == h2

    def test_same_inputs_produce_same_key(self):
        k1 = self._full_key(self.PROMPT, self.TASK)
        k2 = self._full_key(self.PROMPT, self.TASK)
        assert k1 == k2

    # --- uniqueness across inputs ---

    def test_different_prompt_produces_different_hash(self):
        h1 = self._hash_only(self.PROMPT, self.TASK)
        h2 = self._hash_only("A completely different prompt.", self.TASK)
        assert h1 != h2

    def test_different_task_produces_different_hash(self):
        h1 = self._hash_only(self.PROMPT, self.TASK)
        h2 = self._hash_only(self.PROMPT, "summarisation")
        assert h1 != h2

    def test_different_rubric_produces_different_hash(self):
        h1 = self._hash_only(self.PROMPT, self.TASK, rubric=["Clarity"])
        h2 = self._hash_only(self.PROMPT, self.TASK, rubric=["Specificity"])
        assert h1 != h2

    def test_none_rubric_uses_default_dimensions(self):
        h_none = self._hash_only(self.PROMPT, self.TASK, rubric=None)
        h_default = self._hash_only(self.PROMPT, self.TASK, rubric=DEFAULT_DIMENSIONS)
        assert h_none == h_default

    # --- format ---

    def test_hash_length_is_exactly_24_chars(self):
        digest = self._hash_only(self.PROMPT, self.TASK)
        assert len(digest) == 24

    def test_hash_is_hex_string(self):
        digest = self._hash_only(self.PROMPT, self.TASK)
        assert all(c in "0123456789abcdef" for c in digest)

    def test_full_key_prefix(self):
        key = self._full_key(self.PROMPT, self.TASK)
        assert key.startswith("score:")

    def test_full_key_total_length(self):
        # "score:" (6) + 24 hex chars = 30
        key = self._full_key(self.PROMPT, self.TASK)
        assert len(key) == 30

    # --- manual SHA-256 verification ---

    def test_hash_matches_manual_sha256(self):
        rubric_str = json.dumps(sorted(DEFAULT_DIMENSIONS))
        raw = f"{self.PROMPT}|{self.TASK}|{rubric_str}"
        expected = hashlib.sha256(raw.encode()).hexdigest()[:24]
        actual = self._hash_only(self.PROMPT, self.TASK)
        assert actual == expected


# ===========================================================================
# 2. TestScoreCalculation
# ===========================================================================


class TestScoreCalculation:
    """Tests for _compute_response — overall_score arithmetic."""

    HASH = "a" * 24

    def _dims(self, scores: list[int]) -> list[dict]:
        return [
            {"dimension": f"Dim{i}", "score": s, "reason": "test"}
            for i, s in enumerate(scores)
        ]

    def _overall(self, scores: list[int]) -> float:
        data = {
            "dimensions": self._dims(scores),
            "strengths": [],
            "improvements": [],
        }
        response = _compute_response(data, self.HASH, cache_hit=False)
        return response.overall_score

    def test_perfect_scores_give_100(self):
        assert self._overall([10, 10, 10, 10, 10]) == 100.0

    def test_zero_scores_give_0(self):
        assert self._overall([0, 0, 0, 0, 0]) == 0.0

    def test_mixed_scores_1(self):
        # mean([8,7,9,6,8]) = 7.6, *10 = 76.0
        assert self._overall([8, 7, 9, 6, 8]) == 76.0

    def test_mixed_scores_2(self):
        # mean([5,5,5,5,5]) = 5.0, *10 = 50.0
        assert self._overall([5, 5, 5, 5, 5]) == 50.0

    def test_mixed_scores_3(self):
        # mean([10,0]) = 5.0, *10 = 50.0
        assert self._overall([10, 0]) == 50.0

    def test_rounding_to_1_decimal(self):
        # mean([7,8,9]) = 8.0, *10 = 80.0 — exact
        result = self._overall([7, 8, 9])
        assert result == 80.0

    def test_non_round_average_rounded_correctly(self):
        # mean([1,2,3]) = 2.0, *10 = 20.0
        assert self._overall([1, 2, 3]) == 20.0

    def test_empty_dimensions_gives_0(self):
        data = {"dimensions": [], "strengths": [], "improvements": []}
        response = _compute_response(data, self.HASH, cache_hit=False)
        assert response.overall_score == 0.0

    def test_malformed_dimension_entry_is_skipped(self):
        data = {
            "dimensions": [
                {"dimension": "Good", "score": 8, "reason": "fine"},
                {"bad_key": "oops"},          # malformed — skipped
            ],
            "strengths": [],
            "improvements": [],
        }
        response = _compute_response(data, self.HASH, cache_hit=False)
        # Only one valid dimension: mean([8]) * 10 = 80.0
        assert response.overall_score == 80.0
        assert len(response.dimensions) == 1

    def test_cache_hit_flag_propagated(self):
        data = {"dimensions": self._dims([5, 5]), "strengths": [], "improvements": []}
        r = _compute_response(data, self.HASH, cache_hit=True)
        assert r.cache_hit is True

    def test_cache_miss_flag_propagated(self):
        data = {"dimensions": self._dims([5, 5]), "strengths": [], "improvements": []}
        r = _compute_response(data, self.HASH, cache_hit=False)
        assert r.cache_hit is False

    def test_prompt_hash_stored_in_response(self):
        data = {"dimensions": self._dims([5]), "strengths": [], "improvements": []}
        r = _compute_response(data, self.HASH, cache_hit=False)
        assert r.prompt_hash == self.HASH


# ===========================================================================
# 3. TestGeminiResponseParsing
# ===========================================================================


VALID_GEMINI_JSON = {
    "dimensions": [
        {"dimension": "Clarity", "score": 8, "reason": "Well stated."},
        {"dimension": "Specificity", "score": 7, "reason": "Could be narrower."},
        {"dimension": "Task alignment", "score": 9, "reason": "On target."},
        {"dimension": "Output format", "score": 6, "reason": "No format requested."},
        {"dimension": "Conciseness", "score": 8, "reason": "Appropriately brief."},
    ],
    "strengths": ["Clear intent", "Good vocabulary"],
    "improvements": ["Add output format", "Specify audience"],
}


class TestGeminiResponseParsing:
    """Tests that _compute_response correctly processes Gemini's JSON payload."""

    HASH = "b" * 24

    def test_valid_json_parses_without_error(self):
        result = _compute_response(VALID_GEMINI_JSON, self.HASH, cache_hit=False)
        assert isinstance(result, ScoreResponse)

    def test_all_five_dimensions_parsed(self):
        result = _compute_response(VALID_GEMINI_JSON, self.HASH, cache_hit=False)
        assert len(result.dimensions) == 5

    def test_dimension_scores_are_within_0_to_10(self):
        result = _compute_response(VALID_GEMINI_JSON, self.HASH, cache_hit=False)
        for d in result.dimensions:
            assert 0 <= d.score <= 10

    def test_dimension_names_match_input(self):
        result = _compute_response(VALID_GEMINI_JSON, self.HASH, cache_hit=False)
        names = [d.dimension for d in result.dimensions]
        assert names == [d["dimension"] for d in VALID_GEMINI_JSON["dimensions"]]

    def test_dimension_reasons_are_strings(self):
        result = _compute_response(VALID_GEMINI_JSON, self.HASH, cache_hit=False)
        for d in result.dimensions:
            assert isinstance(d.reason, str) and len(d.reason) > 0

    def test_strengths_is_a_list_of_strings(self):
        result = _compute_response(VALID_GEMINI_JSON, self.HASH, cache_hit=False)
        assert isinstance(result.strengths, list)
        assert all(isinstance(s, str) for s in result.strengths)

    def test_improvements_is_a_list_of_strings(self):
        result = _compute_response(VALID_GEMINI_JSON, self.HASH, cache_hit=False)
        assert isinstance(result.improvements, list)
        assert all(isinstance(s, str) for s in result.improvements)

    def test_strengths_content_matches(self):
        result = _compute_response(VALID_GEMINI_JSON, self.HASH, cache_hit=False)
        assert result.strengths == VALID_GEMINI_JSON["strengths"]

    def test_improvements_content_matches(self):
        result = _compute_response(VALID_GEMINI_JSON, self.HASH, cache_hit=False)
        assert result.improvements == VALID_GEMINI_JSON["improvements"]

    def test_invalid_json_string_raises_json_decode_error(self):
        with pytest.raises(json.JSONDecodeError):
            json.loads("not valid JSON {{{")

    def test_missing_strengths_defaults_to_empty_list(self):
        data = {**VALID_GEMINI_JSON}
        del data["strengths"]  # type: ignore[misc]
        result = _compute_response(data, self.HASH, cache_hit=False)
        assert result.strengths == []

    def test_missing_improvements_defaults_to_empty_list(self):
        data = {**VALID_GEMINI_JSON}
        del data["improvements"]  # type: ignore[misc]
        result = _compute_response(data, self.HASH, cache_hit=False)
        assert result.improvements == []

    def test_score_cast_from_string_int(self):
        data = {
            "dimensions": [{"dimension": "Clarity", "score": "8", "reason": "fine"}],
            "strengths": [],
            "improvements": [],
        }
        result = _compute_response(data, self.HASH, cache_hit=False)
        assert result.dimensions[0].score == 8


# ===========================================================================
# 4. TestCacheKeyFormat
# ===========================================================================


class TestCacheKeyFormat:
    """Tests that the Redis key follows the expected format and is sensitive to input."""

    def _key(self, prompt: str, task: str, rubric=None) -> str:
        key, _ = _build_cache_key(prompt, task, rubric)
        return key

    def _digest(self, prompt: str, task: str, rubric=None) -> str:
        _, digest = _build_cache_key(prompt, task, rubric)
        return digest

    def test_key_follows_score_colon_24char_format(self):
        key = self._key("hello", "world")
        parts = key.split(":")
        assert parts[0] == "score"
        assert len(parts[1]) == 24

    def test_whitespace_in_prompt_matters(self):
        h1 = self._digest("hello world", "task")
        h2 = self._digest("helloworld", "task")
        assert h1 != h2

    def test_leading_whitespace_matters(self):
        h1 = self._digest("  hello", "task")
        h2 = self._digest("hello", "task")
        assert h1 != h2

    def test_trailing_whitespace_matters(self):
        h1 = self._digest("hello  ", "task")
        h2 = self._digest("hello", "task")
        assert h1 != h2

    def test_case_sensitive_prompt(self):
        h1 = self._digest("Hello", "task")
        h2 = self._digest("hello", "task")
        assert h1 != h2

    def test_case_sensitive_task(self):
        h1 = self._digest("prompt", "Task")
        h2 = self._digest("prompt", "task")
        assert h1 != h2

    def test_rubric_order_is_normalised(self):
        """Rubric items are sorted before hashing so order doesn't matter."""
        h1 = self._digest("p", "t", rubric=["Clarity", "Specificity"])
        h2 = self._digest("p", "t", rubric=["Specificity", "Clarity"])
        assert h1 == h2

    def test_key_is_string(self):
        key = self._key("prompt", "task")
        assert isinstance(key, str)

    def test_digest_is_string(self):
        digest = self._digest("prompt", "task")
        assert isinstance(digest, str)


# ===========================================================================
# 5. TestAPIContract
# ===========================================================================


class TestAPIContract:
    """Tests for Pydantic request/response model validation (no HTTP required)."""

    # --- ScoreRequest ---

    def test_score_request_valid_minimal(self):
        req = ScoreRequest(prompt="My prompt", task="My task")
        assert req.prompt == "My prompt"
        assert req.task == "My task"
        assert req.rubric is None

    def test_score_request_rubric_defaults_to_none(self):
        req = ScoreRequest(prompt="x", task="y")
        assert req.rubric is None

    def test_score_request_accepts_custom_rubric(self):
        req = ScoreRequest(prompt="x", task="y", rubric=["Clarity", "Brevity"])
        assert req.rubric == ["Clarity", "Brevity"]

    def test_score_request_missing_prompt_raises_validation_error(self):
        with pytest.raises(ValidationError) as exc_info:
            ScoreRequest(task="My task")  # type: ignore[call-arg]
        errors = exc_info.value.errors()
        fields = [e["loc"][0] for e in errors]
        assert "prompt" in fields

    def test_score_request_missing_task_raises_validation_error(self):
        with pytest.raises(ValidationError) as exc_info:
            ScoreRequest(prompt="My prompt")  # type: ignore[call-arg]
        errors = exc_info.value.errors()
        fields = [e["loc"][0] for e in errors]
        assert "task" in fields

    def test_score_request_missing_both_raises_validation_error(self):
        with pytest.raises(ValidationError) as exc_info:
            ScoreRequest()  # type: ignore[call-arg]
        errors = exc_info.value.errors()
        fields = [e["loc"][0] for e in errors]
        assert "prompt" in fields
        assert "task" in fields

    def test_score_request_empty_prompt_raises_validation_error(self):
        with pytest.raises(ValidationError):
            ScoreRequest(prompt="", task="task")

    def test_score_request_empty_task_raises_validation_error(self):
        with pytest.raises(ValidationError):
            ScoreRequest(prompt="prompt", task="")

    # --- DimensionScore ---

    def test_dimension_score_valid(self):
        d = DimensionScore(dimension="Clarity", score=7, reason="Clear.")
        assert d.score == 7

    def test_dimension_score_rejects_score_above_10(self):
        with pytest.raises(ValidationError):
            DimensionScore(dimension="Clarity", score=11, reason="Too high.")

    def test_dimension_score_rejects_negative_score(self):
        with pytest.raises(ValidationError):
            DimensionScore(dimension="Clarity", score=-1, reason="Negative.")

    def test_dimension_score_accepts_boundary_0(self):
        d = DimensionScore(dimension="Clarity", score=0, reason="Very poor.")
        assert d.score == 0

    def test_dimension_score_accepts_boundary_10(self):
        d = DimensionScore(dimension="Clarity", score=10, reason="Perfect.")
        assert d.score == 10

    # --- ScoreResponse ---

    def test_score_response_valid(self):
        resp = ScoreResponse(
            overall_score=76.0,
            dimensions=[DimensionScore(dimension="Clarity", score=8, reason="Good.")],
            strengths=["Concise"],
            improvements=["Add examples"],
            cache_hit=False,
            prompt_hash="a" * 24,
        )
        assert resp.overall_score == 76.0
        assert resp.cache_hit is False

    def test_score_response_serialises_to_dict(self):
        resp = ScoreResponse(
            overall_score=50.0,
            dimensions=[],
            strengths=[],
            improvements=[],
            cache_hit=True,
            prompt_hash="b" * 24,
        )
        d = resp.model_dump()
        assert d["cache_hit"] is True
        assert d["overall_score"] == 50.0
