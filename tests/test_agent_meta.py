import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from common.agent_meta import get_agent_meta  # noqa: E402


def test_oncall_and_jenkins_resolve_to_different_real_metadata():
    oncall = get_agent_meta("oncall")
    jenkins = get_agent_meta("jenkins")

    assert oncall.skill.id == "code_analysis_fix"
    assert jenkins.skill.id == "jenkins_read_diagnose_act"
    assert oncall.description != jenkins.description
    assert oncall.planner_fallback_steps != jenkins.planner_fallback_steps
    assert oncall.artifacts != jenkins.artifacts


def test_unknown_agent_kind_falls_back_to_oncall():
    assert get_agent_meta("something-typoed").skill.id == get_agent_meta("oncall").skill.id


def test_fallback_steps_only_name_capabilities_that_kind_actually_has():
    # The real bug this guards: reusing one kind's fallback plan for
    # another would reference capabilities that don't exist in that
    # kind's own registry at all.
    import os

    import common.capabilities_registry as capabilities_registry
    from common.settings import get_settings

    for kind in ("oncall", "jenkins"):
        os.environ["AGENT_KIND"] = kind
        get_settings.cache_clear()
        capabilities_registry._load_specs.cache_clear()
        try:
            available = {c["name"] for c in capabilities_registry.catalog()}
            for step in get_agent_meta(kind).planner_fallback_steps:
                assert step["capability"] in available, f"{kind}'s fallback references a capability it doesn't have"
        finally:
            del os.environ["AGENT_KIND"]
            get_settings.cache_clear()
            capabilities_registry._load_specs.cache_clear()
