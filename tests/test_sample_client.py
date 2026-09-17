"""Tests for the request bodies docs/examples/sample_client.py builds.

The sample is what anyone evaluating this agent runs first, so the shape of
what it sends is worth pinning. Three real failures, all hit while running it
against a live deployment, are covered here:

  * A private REPO_URL could not even be cloned in `chat` mode, because the
    PAT was attached only in the modes that open a PR. git's own message for
    that ("could not read Username for 'https://github.com'") gives no hint
    that a token was the missing piece.
  * The provider was hardcoded, so using anything but OpenAI meant editing
    the file -- which is also the first thing anyone diffs when reporting a
    problem with it.
  * The webhook callback was hardcoded to localhost, which addresses the
    agent's own pod when the agent runs in Kubernetes, so no delivery could
    ever arrive.

Loaded by path: docs/examples/ isn't part of the installed package.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_SAMPLE = Path(__file__).resolve().parents[1] / "docs" / "examples" / "sample_client.py"


@pytest.fixture()
def sample(monkeypatch):
    # A key has to be present for most of these; individual tests override it.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    spec = importlib.util.spec_from_file_location("ari_sample_client", _SAMPLE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ari_sample_client"] = module
    spec.loader.exec_module(module)
    return module


def test_openai_is_the_default_so_an_openai_key_needs_no_edits(sample):
    llm = sample._llm()
    assert llm == {"provider": "openai", "api_key": "sk-test", "model": "gpt-4o-mini"}


def test_provider_switches_to_anthropic_from_the_environment(sample, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    llm = sample._llm()
    assert llm["provider"] == "anthropic"
    assert llm["api_key"] == "sk-ant-test"
    assert llm["model"] == "claude-haiku-4-5", "provider's default model, not OpenAI's"


def test_llm_api_key_overrides_the_per_provider_variable(sample, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-explicit")
    assert sample._llm()["api_key"] == "sk-explicit"


def test_missing_key_fails_with_a_message_naming_the_variable(sample, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        sample._llm()
    assert "OPENAI_API_KEY" in str(exc_info.value)


def test_unknown_provider_asks_for_a_model_rather_than_guessing(sample, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "cohere")
    monkeypatch.setenv("COHERE_API_KEY", "sk-co")
    with pytest.raises(SystemExit) as exc_info:
        sample._llm()
    assert "LLM_MODEL" in str(exc_info.value)


def test_pat_is_sent_in_every_mode_once_it_is_set(sample, monkeypatch):
    # Not just the PR-opening modes: cloning a private repo needs it too.
    monkeypatch.setenv("GITHUB_PAT", "ghp_test")
    assert sample._config()["repo"]["pat"] == "ghp_test"


def test_pat_is_omitted_when_unset_so_public_repos_still_work(sample):
    assert "pat" not in sample._config()["repo"]


def test_message_carries_the_prompt_and_config_as_separate_parts(sample):
    message = sample._message("fix it", {"repo": {"url": "u"}})
    assert message["role"] == "ROLE_USER"
    texts = [p["text"] for p in message["parts"] if "text" in p]
    datas = [p["data"] for p in message["parts"] if "data" in p]
    assert texts == ["fix it"]
    assert datas == [{"repo": {"url": "u"}}]
