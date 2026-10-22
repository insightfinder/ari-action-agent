import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from common import respond  # noqa: E402

SKILL_RESULT = {"skill": "aws-security-group-allowlist", "output": "added to sg-a", "commands": []}


def meta(direct=("run_skill",), confirm="publish_pr"):
    return SimpleNamespace(
        confirm_capability=confirm,
        artifacts={"pending_action": "propose_pr.pending_action"},
        direct_capabilities=direct,
    )


def test_a_skill_run_is_not_reported_as_nothing_executed():
    fact = respond.execution_fact(meta(), {"run_skill": SKILL_RESULT})
    assert "NOTHING WAS EXECUTED" not in fact
    assert "NOTHING WAS PROPOSED" not in fact
    assert "the `run_skill` step ran in this request" in fact
    assert "may have read from or changed external systems" in fact
    assert "the `publish_pr` step did not run, so no pull request was opened. There is no proposal" in fact


def test_a_skill_run_next_to_a_proposal_says_both():
    results = {"run_skill": SKILL_RESULT, "propose_pr": {"pending_action": {"id": "x"}}}
    fact = respond.execution_fact(meta(), results)
    assert "the `run_skill` step ran" in fact
    assert fact.endswith("There IS a proposal awaiting confirmation in this request.")
    assert "NOTHING WAS EXECUTED" not in fact


def test_without_a_skill_run_the_original_facts_are_unchanged():
    plain = SimpleNamespace(confirm_capability="publish_pr", artifacts={"pending_action": "propose_pr.pending_action"})
    for results in ({}, {"propose_pr": {"pending_action": {"id": "x"}}}, {"publish_pr": {"pr": {"url": "u"}}}):
        assert respond.execution_fact(meta(), results) == respond.execution_fact(plain, results)


def test_a_kind_without_a_confirm_step_still_reports_a_skill_run():
    assert "the `run_skill` step ran" in respond.execution_fact(meta(confirm=None), {"run_skill": SKILL_RESULT})
    assert respond.execution_fact(meta(confirm=None), {}) == ""


def test_the_oncall_kind_declares_run_skill_as_acting_directly():
    from ari_oncall_agent.meta import META

    assert META.direct_capabilities == ("run_skill",)
