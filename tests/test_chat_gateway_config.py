import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chat_gateway.config import ConfigError, GatewaySettings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in GatewaySettings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.chdir(tmp_path)


def test_llm_config_needs_a_key_and_knows_default_models():
    with pytest.raises(ConfigError, match="LLM_API_KEY"):
        GatewaySettings().llm_config()
    assert GatewaySettings(llm_api_key="k").llm_config() == {"provider": "openai", "api_key": "k", "model": "gpt-4o-mini"}
    assert GatewaySettings(llm_api_key="k", llm_provider="anthropic").llm_config()["model"] == "claude-haiku-4-5"


def test_llm_config_for_an_unknown_provider_requires_an_explicit_model():
    with pytest.raises(ConfigError, match="LLM_MODEL"):
        GatewaySettings(llm_api_key="k", llm_provider="mistral").llm_config()
    llm = GatewaySettings(llm_api_key="k", llm_provider="mistral", llm_model="m", llm_base_url="https://llm.test/v1").llm_config()
    assert llm == {"provider": "mistral", "api_key": "k", "model": "m", "base_url": "https://llm.test/v1"}


def test_repo_config_uses_defaults_ref_and_pat():
    settings = GatewaySettings(default_repo_url="https://github.com/org/repo", default_repo_ref="dev", github_pat="p")
    assert settings.repo_config() == {"url": "https://github.com/org/repo", "ref": "dev", "pat": "p"}
    assert settings.repo_config("https://github.com/org/other", "main")["ref"] == "main"


def test_repo_config_without_any_repository_is_a_config_error():
    with pytest.raises(ConfigError, match="DEFAULT_REPO_URL"):
        GatewaySettings().repo_config()


def test_repo_host_allowlist_rejects_foreign_hosts_plain_http_and_embedded_credentials():
    settings = GatewaySettings(github_pat="p")
    with pytest.raises(ConfigError, match="not allowed"):
        settings.repo_config("https://evil.example/x")
    with pytest.raises(ConfigError, match="https"):
        settings.repo_config("http://github.com/org/x")
    with pytest.raises(ConfigError, match="credentials"):
        settings.repo_config("https://ghp_x@github.com/org/x")
    assert settings.repo_config("https://GitHub.com/org/x")["url"] == "https://GitHub.com/org/x"


def test_allowed_hosts_include_the_configured_repositories_and_the_explicit_list():
    settings = GatewaySettings(
        default_repo_url="https://gitlab.example/org/repo",
        repo_map="C1=https://git.corp.example/a/b",
        allowed_repo_hosts="github.com, Codeberg.org",
    )
    assert settings.allowed_hosts() == {"github.com", "codeberg.org", "gitlab.example", "git.corp.example"}
    assert settings.repo_config("https://git.corp.example/a/b")["url"] == "https://git.corp.example/a/b"


def test_require_pat_refuses_to_build_a_config_that_cannot_push():
    settings = GatewaySettings(default_repo_url="https://github.com/org/repo")
    with pytest.raises(ConfigError, match="GITHUB_PAT"):
        settings.repo_config(require_pat=True)
    assert "pat" not in settings.repo_config()


def test_repo_for_prefers_the_full_thread_key_over_the_channel():
    settings = GatewaySettings(repo_map="C1=https://github.com/org/chan, C1:9.9=https://github.com/org/thread, bad, =x")
    assert settings.repo_for("C1:9.9") == "https://github.com/org/thread"
    assert settings.repo_for("C1:1.1") == "https://github.com/org/chan"
    assert settings.repo_for("C2:1.1") == ""
    assert settings.repo_map_entries() == {"C1": "https://github.com/org/chan", "C1:9.9": "https://github.com/org/thread"}


def test_options_json_must_be_an_object():
    assert GatewaySettings().options() == {"lane": "interactive"}
    assert GatewaySettings(options_json='{"skip_tests": true}').options() == {"lane": "interactive", "skip_tests": True}
    assert GatewaySettings(options_json='{"lane": "batch"}').options() == {"lane": "batch"}
    with pytest.raises(ConfigError, match="valid JSON"):
        GatewaySettings(options_json="{nope").options()
    with pytest.raises(ConfigError, match="JSON object"):
        GatewaySettings(options_json="[1]").options()


def test_approvers_and_masking():
    settings = GatewaySettings(approvers=" boss , ops ,", ari_api_key="abcdefgh", llm_api_key="ab", github_pat="")
    assert settings.approver_set() == {"boss", "ops"}
    masked = settings.masked()
    assert masked["ari_api_key"] == "abcd…"
    assert masked["llm_api_key"] == "…"
    assert masked["github_pat"] == ""
    assert masked["approvers"] == " boss , ops ,"


def test_settings_do_not_read_a_dotenv_file_from_the_working_directory(tmp_path):
    (tmp_path / ".env.chat-gateway").write_text("LLM_API_KEY=from-file\n", encoding="utf-8")
    (tmp_path / ".env").write_text("LLM_API_KEY=from-env-file\n", encoding="utf-8")
    assert GatewaySettings().llm_api_key == ""
