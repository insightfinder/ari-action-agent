import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from a2a.utils import TransportProtocol  # noqa: E402

from common.a2a_server.agent_card import build_agent_card  # noqa: E402
from common.settings import Settings  # noqa: E402


def test_agent_card_declares_both_jsonrpc_and_rest_at_the_public_url():
    settings = Settings(agent_public_url="https://ari.example.com")
    card = build_agent_card(settings)

    bindings = {i.protocol_binding for i in card.supported_interfaces}
    assert bindings == {TransportProtocol.JSONRPC, TransportProtocol.HTTP_JSON}
    assert all(i.url == "https://ari.example.com" for i in card.supported_interfaces)


def test_agent_card_declares_api_key_security_scheme():
    card = build_agent_card(Settings())

    schemes = card.security_schemes
    assert "apiKeyAuth" in schemes
    scheme = schemes["apiKeyAuth"].api_key_security_scheme
    assert scheme.location == "header"
    assert scheme.name == "x-api-key"
    assert len(card.security_requirements) == 1
    assert "apiKeyAuth" in card.security_requirements[0].schemes


def test_agent_card_advertises_push_notifications_not_streaming():
    card = build_agent_card(Settings())

    assert card.capabilities.push_notifications is True
    assert card.capabilities.streaming is False


def test_agent_card_has_the_code_analysis_fix_skill():
    card = build_agent_card(Settings())

    ids = {s.id for s in card.skills}
    assert "code_analysis_fix" in ids


def test_agent_card_switches_content_by_agent_kind():
    oncall_card = build_agent_card(Settings(agent_kind="oncall"))
    jenkins_card = build_agent_card(Settings(agent_kind="jenkins"))

    assert {s.id for s in oncall_card.skills} == {"code_analysis_fix"}
    assert {s.id for s in jenkins_card.skills} == {"jenkins_read_diagnose_act"}
    assert oncall_card.description != jenkins_card.description
    assert "jenkins" in jenkins_card.description.lower()


def test_agent_card_defaults_to_oncall_for_an_unknown_agent_kind():
    # Fail safe, not fail closed — an unrecognized kind (e.g. a typo in a
    # deployment's env var) still serves a real, working card.
    card = build_agent_card(Settings(agent_kind="something-typoed"))

    assert {s.id for s in card.skills} == {"code_analysis_fix"}
