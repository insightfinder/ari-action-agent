"""Masks credentials in free text that leaves this agent: a provider's error
message, a failed command's stderr, a step's failure reason on a trace span.
"""

import re

# A provider's error text can quote the key it was sent: OpenAI's 401 says "Incorrect API key
# provided: sk-proj-****abcd", first and last characters intact. Masked or not, it has no business in
# a failure reason, which reaches the `respond` step's prompt, the caller's reply and Temporal's history.
# "api key provided: X" / "api_key=X", where X looks like a key (has a digit or a mask), not a word.
_KEY_ECHO = re.compile(r"(?i)(api[ _-]?key\b[^:=\n]{0,30}[:=]\s*['\"]?)(?=[^\s'\",]*[0-9*])[^\s'\",]{6,}")
_KEY_LIKE = re.compile(
    r"\b(?:sk|pk|rk)-[A-Za-z0-9_*.\-]{4,}"  # OpenAI, Anthropic and look-alikes
    r"|\bAIza[A-Za-z0-9_*\-]{8,}"  # Google
    r"|\bgsk_[A-Za-z0-9_*]{8,}"  # Groq
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"  # GitHub tokens
    r"|(?i:\bbearer\s+)[A-Za-z0-9._~+/=*\-]{8,}"
    r"|\b[0-9a-fA-F]{32,}\b"  # Azure OpenAI and other bare hex keys
)
# Credentials in a URL, as in a failed `git clone https://x-access-token:<pat>@github.com/...`.
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s@]+@")


def redact_secrets(text: str) -> str:
    text = _URL_USERINFO.sub(r"\1[REDACTED]@", text)
    return _KEY_LIKE.sub("[REDACTED]", _KEY_ECHO.sub(r"\1[REDACTED]", text))
