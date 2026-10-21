from common.redaction import redact_secrets


def test_keys_tokens_and_url_credentials_are_masked():
    text = ("Incorrect API key provided: sk-proj-****abcd. "
            "token ghp_abcdefghijklmnopqrstuvwxyz0123 and github_pat_11ABCDEFG0123456789_abcdefghij "
            "cloning https://x-access-token:secret-pat@github.com/acme/app.git")
    redacted = redact_secrets(text)
    for secret in ("sk-proj", "ghp_", "github_pat_", "secret-pat", "x-access-token"):
        assert secret not in redacted
    assert "https://[REDACTED]@github.com/acme/app.git" in redacted


def test_plain_text_is_left_alone():
    text = "exit 1: tests failed in https://github.com/acme/app (3 of 120)"
    assert redact_secrets(text) == text
