import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jenkins_agent.timefmt import TIME_RULE, format_duration, humanize, with_time_rule  # noqa: E402


def test_a_time_marker_becomes_a_utc_date():
    assert humanize("Build #9 started [ts:1791454828356].") == "Build #9 started 2026-10-08 10:20:28 UTC."


def test_a_duration_marker_becomes_a_readable_duration():
    assert humanize("It ran for [dur:484357].") == "It ran for 8m 4s."


@pytest.mark.parametrize(
    "ms, shown",
    [(0, "0ms"), (356, "356ms"), (1499, "1s"), (59_600, "1m 0s"), (484_357, "8m 4s"), (3_723_000, "1h 2m 3s")],
)
def test_durations_are_rounded_to_whole_seconds(ms, shown):
    assert format_duration(ms) == shown


def test_markers_tolerate_spaces_inside_the_brackets():
    assert humanize("[ts: 1791454828356 ] / [dur: 2000]") == "2026-10-08 10:20:28 UTC / 2s"


def test_a_bare_epoch_millisecond_number_gets_its_date_alongside():
    assert humanize("timestamp 1791454828356") == "timestamp 1791454828356 (2026-10-08 10:20:28 UTC)"


def test_a_number_already_followed_by_an_explanation_is_left_alone():
    text = "timestamp 1791454828356 (Oct 8)"
    assert humanize(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "Build #9 of job 42 failed at line 1234.",
        "queue id 1234567890123456",
        "version 1.1791454828356",
        "[ts:12]",
        "[ts:abc] and [dur:]",
        "9999999999999 is past 2100",
    ],
)
def test_other_numbers_and_malformed_markers_are_not_touched(text):
    assert humanize(text) == text


def test_empty_text_stays_empty():
    assert humanize("") == ""


def test_the_rule_is_appended_to_any_instruction():
    assert with_time_rule("Do the thing.") == f"Do the thing.\n\n{TIME_RULE}"
