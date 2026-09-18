import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from a2a.utils import TransportProtocol  # noqa: E402

from ari_agent.a2a_server.agent_card import build_agent_card  # noqa: E402
from ari_agent.settings import Settings  # noqa: E402


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
