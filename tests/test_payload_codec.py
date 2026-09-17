import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from temporalio.api.common.v1 import Payload  # noqa: E402

from ari_agent.orchestration.payload_codec import EncryptionCodec  # noqa: E402
from ari_agent.orchestration.temporal_client import _data_converter  # noqa: E402


def _key() -> bytes:
    return b"0" * 32


@pytest.mark.asyncio
async def test_round_trip_recovers_the_original_payload_exactly():
    codec = EncryptionCodec(_key())
    original = Payload(metadata={"encoding": b"json/plain"}, data=b'{"api_key": "sk-secret"}')

    encoded = await codec.encode([original])
    decoded = await codec.decode(encoded)

    assert decoded == [original]


@pytest.mark.asyncio
async def test_encoded_payload_does_not_contain_the_plaintext_secret():
    codec = EncryptionCodec(_key())
    secret = b"ghp_super_secret_token_value"
    original = Payload(metadata={"encoding": b"json/plain"}, data=secret)

    [encoded] = await codec.encode([original])

    assert secret not in encoded.data
    assert encoded.metadata["encoding"] == b"binary/encrypted"


@pytest.mark.asyncio
async def test_decode_passes_through_payloads_not_tagged_as_encrypted():
    # History written before this codec was enabled has plain payloads
    # mixed into the same workflow history as new encrypted ones — decode
    # must not choke on those.
    codec = EncryptionCodec(_key())
    plain = Payload(metadata={"encoding": b"json/plain"}, data=b'{"x": 1}')

    decoded = await codec.decode([plain])

    assert decoded == [plain]


def test_rejects_a_key_that_is_not_32_bytes():
    with pytest.raises(ValueError, match="32 bytes"):
        EncryptionCodec(b"too-short")


def test_data_converter_uses_default_when_no_key_configured():
    from temporalio.converter import DataConverter

    assert _data_converter(None) is DataConverter.default
    assert _data_converter("") is DataConverter.default


def test_data_converter_builds_a_codec_when_key_configured():
    converter = _data_converter(base64.b64encode(_key()).decode())

    assert converter.payload_codec is not None
    assert isinstance(converter.payload_codec, EncryptionCodec)
