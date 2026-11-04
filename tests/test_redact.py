import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from common.redact import without_fields  # noqa: E402


def test_it_strips_the_named_key_at_any_depth():
    value = {
        "a": {"url": "x", "keep": 1},
        "b": [{"url": "y"}, {"keep": 2}],
    }
    assert without_fields(value, ("url",)) == {"a": {"keep": 1}, "b": [{}, {"keep": 2}]}


def test_an_empty_field_list_returns_the_value_unchanged():
    value = {"url": "x"}
    assert without_fields(value, ()) is value


def test_it_leaves_scalars_and_unlisted_keys_alone():
    assert without_fields("url", ("url",)) == "url"
    assert without_fields({"other": "url"}, ("url",)) == {"other": "url"}


def test_it_does_not_mutate_its_input():
    value = {"a": {"url": "x"}}
    without_fields(value, ("url",))
    assert value == {"a": {"url": "x"}}
