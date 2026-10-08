import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ari_oncall_agent.meta import _ensure_propose_pr_by_default  # noqa: E402
from common.models.plan import Plan, Step  # noqa: E402


def test_appends_propose_pr_when_generate_change_ran_and_planner_omitted_it():
    # Regression test for a live finding: a smaller/cheaper planner model
    # inconsistently included this step for the same kind of request
    # across separate runs -- this makes "propose a PR by default" not
    # dependent on the LLM remembering every time.
    plan = Plan(steps=[Step(capability="clone_repo"), Step(capability="generate_change")], rationale="r")

    steps = _ensure_propose_pr_by_default(plan, {})

    assert [s.capability for s in steps] == ["clone_repo", "generate_change", "propose_pr"]


def test_does_not_duplicate_propose_pr_when_planner_already_included_it():
    plan = Plan(
        steps=[Step(capability="clone_repo"), Step(capability="generate_change"), Step(capability="propose_pr")],
        rationale="r",
    )

    steps = _ensure_propose_pr_by_default(plan, {})

    assert [s.capability for s in steps].count("propose_pr") == 1


def test_does_nothing_when_generate_change_never_ran():
    # A plain question, or a plan that never edits anything -- nothing to
    # publish, so no propose_pr step should be conjured up.
    plan = Plan(steps=[], rationale="just a question")

    assert _ensure_propose_pr_by_default(plan, {}) == []


def test_respects_options_skip_pr():
    plan = Plan(steps=[Step(capability="clone_repo"), Step(capability="generate_change")], rationale="r")

    steps = _ensure_propose_pr_by_default(plan, {"skip_pr": True})

    assert [s.capability for s in steps] == ["clone_repo", "generate_change"]


def test_respects_plans_own_skip_publish_pr_flag():
    # The planner's own explicit signal that the *request* said not to
    # open a PR -- distinct from options (a caller-supplied override) and
    # from simply omitting propose_pr (which is NOT enough by itself,
    # since that's indistinguishable from the model just forgetting).
    plan = Plan(
        steps=[Step(capability="clone_repo"), Step(capability="generate_change")],
        rationale="r",
        skip_publish_pr=True,
    )

    steps = _ensure_propose_pr_by_default(plan, {})

    assert [s.capability for s in steps] == ["clone_repo", "generate_change"]


def test_never_appends_publish_pr_which_is_reachable_only_through_a_confirmation():
    # publish_pr refuses any step that was not confirmed, and confirmation
    # comes from plan_task's bypass, not from a plan. A policy that
    # appended it would hand every run a step guaranteed to fail.
    plan = Plan(steps=[Step(capability="clone_repo"), Step(capability="generate_change")], rationale="r")

    steps = _ensure_propose_pr_by_default(plan, {})

    assert "publish_pr" not in [s.capability for s in steps]
