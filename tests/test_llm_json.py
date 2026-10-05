"""The tolerant reader for a model's JSON, in the shapes that actually
arrive. Every case here is a real packaging failure that `json.loads`
rejects outright."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from common.llm_json import extract  # noqa: E402


class TestShapesThatArrive:
    def test_a_bare_object(self):
        assert extract('{"steps": []}') == {"steps": []}

    def test_a_json_fence(self):
        assert extract('```json\n{"steps": [], "rationale": "none"}\n```')["rationale"] == "none"

    def test_an_unlabelled_fence(self):
        assert extract('```\n{"a": 1}\n```') == {"a": 1}

    def test_preamble_before_the_object(self):
        assert extract('Here is the plan you asked for:\n{"a": 1}') == {"a": 1}

    def test_prose_after_the_object(self):
        assert extract('{"a": 1}\n\nI chose this because it is the shortest route.') == {"a": 1}

    def test_a_sentinel_when_one_is_used(self):
        text = 'thinking out loud\nPROPOSAL_JSON:\n{"id": "x"}'
        assert extract(text, "PROPOSAL_JSON:") == {"id": "x"}

    def test_the_sentinel_wins_over_an_earlier_object(self):
        text = '{"id": "wrong"}\nPROPOSAL_JSON:\n{"id": "right"}'
        assert extract(text, "PROPOSAL_JSON:")["id"] == "right"

    def test_the_sentinel_is_optional(self):
        # The planner prompts for a bare object and names no sentinel at
        # all; the same reader has to serve it.
        assert extract('{"steps": []}', None) == {"steps": []}


class TestItRefusesRatherThanGuessing:
    def test_prose_with_no_object_raises(self):
        with pytest.raises(RuntimeError, match="could not extract a JSON object"):
            extract("I am unable to produce a plan for this request.")

    def test_a_truncated_object_raises(self):
        with pytest.raises(RuntimeError):
            extract('{"steps": [{"capability": "resolve_target"')

    def test_a_json_array_is_not_an_object(self):
        # A list parses, but no caller can use one, so it must not pass.
        with pytest.raises(RuntimeError):
            extract("[1, 2, 3]")

    def test_the_message_carries_the_response_for_diagnosis(self):
        with pytest.raises(RuntimeError, match="the plan is: do nothing"):
            extract("the plan is: do nothing")
