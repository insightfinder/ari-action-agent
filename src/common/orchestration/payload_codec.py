"""Masks only the handful of genuinely sensitive fields in a Temporal
payload (a request's LLM `api_key`, repo `pat`/`ssh_key`, and the
InsightFinder `trace_admin_user_license` of its `trace` block) before it's
sent to the Temporal server for persistence — everything else in the payload
(prompt, capability, params, step results, diffs, PR links, ...) stays
fully readable plain JSON in Temporal's Web UI / `temporal workflow show`.

Earlier version of this codec encrypted the *entire* payload as one opaque
blob. Confirmed live that this was the wrong trade-off: it also hid every
non-sensitive field, making the Temporal UI useless for actually observing
what a request is doing — exactly the kind of visibility this UI exists
for. Field-level masking (this version) keeps that visibility while still
closing the real gap: request secrets no longer sit in plaintext in
Temporal's event history (see docs/MEMORY.md for the original finding —
`temporal workflow show --output json` showed a real GitHub PAT and
Anthropic API key in cleartext).

How it works: the default Temporal payload converter serializes a Python
dict/dataclass as JSON (`encoding: json/plain`). This codec parses that
JSON, walks it recursively, and replaces the *value* of any dict key named
`api_key`, `pat`, `ssh_key` or `trace_admin_user_license` (wherever it
appears, at any nesting depth — robust to whichever capability's payload
shape carries it) with an AES-256-GCM-encrypted, base64-armored string
tagged with a fixed prefix.
The payload's `encoding` metadata is left as `json/plain` — unchanged — so
it still renders as ordinary JSON to any viewer; only those specific field
values are unreadable. `decode()` reverses exactly that transformation,
so activities/workflows see the real plaintext secret when they actually
need it (e.g. `git clone` with the PAT, calling the LLM with the API key).
"""

import base64
import json
import os
from collections.abc import Sequence
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from temporalio.api.common.v1 import Payload
from temporalio.converter import PayloadCodec

_JSON_ENCODING = b"json/plain"
# `trace_admin_user_license` is the caller's InsightFinder license, carried in
# a request's `trace` block (common/tracing.py) — the same name mcp-service's
# own codec masks.
_SENSITIVE_KEYS = {"api_key", "pat", "ssh_key", "trace_admin_user_license"}
_ENCRYPTED_PREFIX = "enc:v1:"
_NONCE_SIZE = 12  # standard for AES-GCM


class EncryptionCodec(PayloadCodec):
    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError(f"Payload encryption key must be 32 bytes (AES-256), got {len(key)}")
        self._aesgcm = AESGCM(key)

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return [self._transform(p, self._encrypt_value) for p in payloads]

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return [self._transform(p, self._decrypt_value) for p in payloads]

    def _transform(self, payload: Payload, value_fn) -> Payload:
        if payload.metadata.get("encoding") != _JSON_ENCODING:
            # Not JSON (e.g. a bare binary/null payload) — nothing to walk,
            # leave untouched rather than guessing at its shape.
            return payload
        try:
            obj = json.loads(payload.data.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            return payload

        new_payload = Payload()
        new_payload.CopyFrom(payload)
        new_payload.data = json.dumps(_walk(obj, value_fn)).encode()
        return new_payload

    def _encrypt_value(self, value: str) -> str:
        if value.startswith(_ENCRYPTED_PREFIX):
            return value  # already encrypted — never double-encrypt
        nonce = os.urandom(_NONCE_SIZE)
        ciphertext = self._aesgcm.encrypt(nonce, value.encode(), None)
        return _ENCRYPTED_PREFIX + base64.b64encode(nonce + ciphertext).decode()

    def _decrypt_value(self, value: str) -> str:
        if not value.startswith(_ENCRYPTED_PREFIX):
            # Not one of ours (e.g. history from before this codec, or a
            # field that was empty/never set) — pass through unchanged.
            return value
        raw = base64.b64decode(value[len(_ENCRYPTED_PREFIX) :])
        nonce, ciphertext = raw[:_NONCE_SIZE], raw[_NONCE_SIZE:]
        return self._aesgcm.decrypt(nonce, ciphertext, None).decode()


def _walk(obj: Any, value_fn) -> Any:
    if isinstance(obj, dict):
        return {
            k: (value_fn(v) if k in _SENSITIVE_KEYS and isinstance(v, str) else _walk(v, value_fn))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_walk(v, value_fn) for v in obj]
    return obj
