"""The last line of defence against leaking credentials.

The first line is structural: every read in `views.py` keeps only
whitelisted fields, so container `env`/`args`/`command`, annotations such
as `kubectl.kubernetes.io/last-applied-configuration`, and HelmRelease
inline values are never even carried into a result. What is left that can
still hold a secret is free text the cluster hands back verbatim — a
container's log tail, an event message, an API error body, a source URL —
and that is what this module scrubs.

Every capability's return value passes through `redact_obj` before it
becomes a step result, so the shared `respond` step, the published A2A
artifacts and Temporal's history only ever see the scrubbed form.

The patterns are deliberately greedy: a false positive costs a reader one
"[REDACTED]" in a log line, a false negative leaks a credential to a chat
transcript, an LLM provider and a workflow history at once.
"""

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

REDACTED = "[REDACTED]"

_SECRET_WORD = (
    r"(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|"
    r"credentials?|authorization|cookie|session[_-]?id|client[_-]?secret)"
)

_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL)
# A log tail is cut by line count and size, so it can hold half a key: a BEGIN with the rest cut off
# (everything after it goes), or an END whose BEGIN was cut (the base64 lines before it go).
_PEM_NO_END = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*\Z", re.DOTALL)
_PEM_NO_BEGIN = re.compile(r"(?:^[ \t]*[A-Za-z0-9+/=]{16,}[ \t]*\r?\n)*[ \t]*-----END [A-Z0-9 ]*PRIVATE KEY-----", re.MULTILINE)
_AUTH_HEADER = re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}")
_URL_USERINFO = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@")
_URL_SECRET_PARAM = re.compile(
    r"(?i)([?&](?:token|access_token|id_token|api[_-]?key|key|sig|signature|password|secret|"
    r"x-amz-signature|x-amz-credential|x-amz-security-token)=)[^&\s\"']+"
)
_PREFIXED_TOKENS = [
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\bdckr_pat_[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"),
]
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")
# `password=x`, `"apiKey": "x"`, `DB_PASSWORD: x`, `aws_secret_access_key = x`
_KEY_VALUE = re.compile(
    rf"(?i)([\"']?[\w.-]*{_SECRET_WORD}[\w.-]*[\"']?\s*[:=]\s*[\"']?)(?!\[REDACTED\])([^\s\"',;&}}\]]+)"
)
# `"password": "correct horse battery"` — a quoted value is taken whole, spaces included.
_KEY_QUOTED_VALUE = re.compile(
    rf"(?i)([\"']?[\w.-]*{_SECRET_WORD}[\w.-]*[\"']?\s*[:=]\s*)([\"'])(?!\[REDACTED\])([^\"'\n]*)\2"
)
_SECRET_KEY_NAME = re.compile(rf"(?i)^[\w.-]*{_SECRET_WORD}[\w.-]*$")


def redact_text(text: str) -> str:
    """Idempotent: scrubbing already-scrubbed text changes nothing (results
    are redacted where they are read and again on the way out)."""
    if not text:
        return text
    out = _PEM.sub(REDACTED, text)
    out = _PEM_NO_END.sub(REDACTED, out)
    out = _PEM_NO_BEGIN.sub(REDACTED, out)
    out = _AUTH_HEADER.sub(lambda m: f"{m.group(1)} {REDACTED}", out)
    out = _URL_USERINFO.sub(lambda m: f"{m.group(1)}{REDACTED}@", out)
    out = _URL_SECRET_PARAM.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
    for pattern in _PREFIXED_TOKENS:
        out = pattern.sub(REDACTED, out)
    out = _JWT.sub(REDACTED, out)
    out = _KEY_QUOTED_VALUE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}{m.group(2)}", out)
    out = _KEY_VALUE.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
    return out


def redact_obj(obj: Any) -> Any:
    """`obj` with every string scrubbed, and the value of any dict key that
    itself names a secret replaced outright."""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        cleaned = {}
        for key, value in obj.items():
            if isinstance(key, str) and _SECRET_KEY_NAME.match(key) and isinstance(value, (str, int, float)):
                cleaned[key] = REDACTED
            else:
                cleaned[key] = redact_obj(value)
        return cleaned
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj


def strip_url_credentials(url: str | None) -> str | None:
    """A source URL with any userinfo, query string and fragment removed.
    Git URLs in Flux objects can carry a token (`https://x:TOKEN@host/...`);
    nothing a reader needs lives in the query or fragment."""
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return redact_text(url)
    if not parts.scheme or not parts.netloc:
        return redact_text(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    if parts.username and parts.scheme == "ssh" and parts.password is None:
        # `ssh://git@github.com/...` — "git" is a fixed login name, not a secret.
        host = f"{parts.username}@{host}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def bounded(text: str | None, max_chars: int) -> str | None:
    if text is None or len(text) <= max_chars:
        return text
    return text[: max_chars - 15] + "...[truncated]"
