import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ari_oncall_agent.schemas import RequestConfig  # noqa: E402


def test_request_config_parses_sample_payload():
    cfg = RequestConfig.model_validate(
        {
            "repo": {"url": "https://github.com/helloaaronli/ari-oncall-agent", "pat": "ghp_xxx"},
            "llm": {"provider": "openai", "api_key": "sk-xxx", "model": "gpt-5"},
            "options": {"skip_tests": False},
        }
    )
    assert cfg.repo.url.endswith("ari-oncall-agent")
    assert cfg.llm.model == "gpt-5"
    assert cfg.options == {"skip_tests": False}


def test_request_config_allows_unknown_sections():
    cfg = RequestConfig.model_validate(
        {
            "repo": {"url": "https://github.com/acme/svc"},
            "llm": {"provider": "openai", "api_key": "sk-x", "model": "gpt-5"},
            "future_section": {"anything": True},
        }
    )
    assert cfg.model_extra.get("future_section") == {"anything": True}
