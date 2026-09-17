"""Encrypts Temporal payloads before they're sent to the Temporal server,
so request secrets (LLM `api_key`, repo `pat`/`ssh_key`) never sit as
plaintext-in-base64 in Temporal's persisted event history — confirmed as a
real gap live: `temporal workflow show --output json` on a real workflow
here showed the exact GitHub PAT and Anthropic key from a request's
`ActivityTaskScheduled` input, readable by anyone with Temporal UI/API
access to the namespace (see docs/MEMORY.md, 2026-09-15 and 2026-09-17
entries). This has nothing to do with this agent's own `x-api-key` inbound
auth or TLS in transit — it protects data *at rest* inside Temporal itself.

AES-256-GCM, one random 12-byte nonce per payload (prefixed to the
ciphertext, standard construction — a nonce is not a secret). The whole
`Payload` protobuf message (metadata + data) is encrypted as one blob and
tagged with a distinct `encoding` so `decode` can tell an encrypted payload
apart from a plain one — required for a rolling upgrade: workflow history
written before this codec was enabled has `binary/encrypted`-tagged
payloads mixed with old plain ones in the same history, and both must
decode correctly.

The key is a deployment-level secret (`TEMPORAL_PAYLOAD_ENCRYPTION_KEY`),
generated once per environment — not a per-request one. Everything else in
this agent's request payload stays fully self-contained/per-request per
this project's own design; this key only ever protects Temporal's own
storage, the same role TLS/API keys play for transport/inbound auth.
Without a Temporal *codec server* configured (deliberately not set up
here — see settings.py), the Temporal Web UI simply cannot decrypt these
payloads at all: they show as opaque ciphertext, which is exactly the
point — nobody browsing the UI can read a request's secrets, whether or
not they're otherwise authorized to view the namespace.
"""

import os
from collections.abc import Sequence

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from temporalio.api.common.v1 import Payload
from temporalio.converter import PayloadCodec

_ENCODING = b"binary/encrypted"
_NONCE_SIZE = 12  # standard for AES-GCM


class EncryptionCodec(PayloadCodec):
    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError(f"Payload encryption key must be 32 bytes (AES-256), got {len(key)}")
        self._aesgcm = AESGCM(key)

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return [self._encode_one(p) for p in payloads]

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return [self._decode_one(p) for p in payloads]

    def _encode_one(self, payload: Payload) -> Payload:
        nonce = os.urandom(_NONCE_SIZE)
        ciphertext = self._aesgcm.encrypt(nonce, payload.SerializeToString(), None)
        return Payload(metadata={"encoding": _ENCODING}, data=nonce + ciphertext)

    def _decode_one(self, payload: Payload) -> Payload:
        if payload.metadata.get("encoding") != _ENCODING:
            # Not one of ours (e.g. history written before this codec was
            # enabled) — pass through unchanged rather than erroring, so a
            # rolling upgrade doesn't break replay of older workflows.
            return payload
        nonce, ciphertext = payload.data[:_NONCE_SIZE], payload.data[_NONCE_SIZE:]
        plaintext = self._aesgcm.decrypt(nonce, ciphertext, None)
        decoded = Payload()
        decoded.ParseFromString(plaintext)
        return decoded
