import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from temporalio.api.common.v1 import Payload  # noqa: E402

from common.orchestration.payload_codec import EncryptionCodec  # noqa: E402
from common.orchestration.temporal_client import _data_converter  # noqa: E402


def _key() -> bytes:
    return b"0" * 32


def _json_payload(obj) -> Payload:
    return Payload(metadata={"encoding": b"json/plain"}, data=json.dumps(obj).encode())


@pytest.mark.asyncio
async def test_round_trip_recovers_the_original_payload_exactly():
    codec = EncryptionCodec(_key())
    original = _json_payload({"config": {"llm": {"api_key": "sk-ant-secret", "model": "claude-sonnet-5"}}})

    [encoded] = await codec.encode([original])
    [decoded] = await codec.decode([encoded])

    assert json.loads(decoded.data.decode()) == json.loads(original.data.decode())


@pytest.mark.asyncio
async def test_only_sensitive_field_values_are_masked_everything_else_stays_plain():
    codec = EncryptionCodec(_key())
    original = _json_payload(
        {
            "capability": "generate_change",
            "prompt": "remove zone and subvenue fields",
            "config": {
                "llm": {"api_key": "sk-ant-secret", "provider": "anthropic", "model": "claude-sonnet-5"},
                "repo": {"pat": "ghp_supersecret", "url": "https://github.com/x/y"},
            },
            "results": {"generate_change": {"diff": "some diff text, not a secret"}},
        }
    )

    [encoded] = await codec.encode([original])

    # Still ordinary JSON — a viewer with no codec (e.g. the Temporal Web
    # UI) can read it directly, not just an opaque blob.
    assert encoded.metadata["encoding"] == b"json/plain"
    obj = json.loads(encoded.data.decode())

    # Non-sensitive fields untouched and fully readable.
    assert obj["capability"] == "generate_change"
    assert obj["prompt"] == "remove zone and subvenue fields"
    assert obj["config"]["llm"]["provider"] == "anthropic"
    assert obj["config"]["llm"]["model"] == "claude-sonnet-5"
    assert obj["config"]["repo"]["url"] == "https://github.com/x/y"
    assert obj["results"]["generate_change"]["diff"] == "some diff text, not a secret"

    # Only the sensitive values are masked, and the raw secret is nowhere
    # in the encoded bytes at all.
    assert obj["config"]["llm"]["api_key"] != "sk-ant-secret"
    assert obj["config"]["llm"]["api_key"].startswith("enc:v1:")
    assert obj["config"]["repo"]["pat"] != "ghp_supersecret"
    assert obj["config"]["repo"]["pat"].startswith("enc:v1:")
    assert b"sk-ant-secret" not in encoded.data
    assert b"ghp_supersecret" not in encoded.data


@pytest.mark.asyncio
async def test_decode_recovers_the_real_secret_values():
    codec = EncryptionCodec(_key())
    original = _json_payload({"config": {"llm": {"api_key": "sk-ant-secret"}, "repo": {"pat": "ghp_supersecret"}}})

    [encoded] = await codec.encode([original])
    [decoded] = await codec.decode([encoded])

    obj = json.loads(decoded.data.decode())
    assert obj["config"]["llm"]["api_key"] == "sk-ant-secret"
    assert obj["config"]["repo"]["pat"] == "ghp_supersecret"


@pytest.mark.asyncio
async def test_decode_passes_through_values_not_tagged_as_encrypted():
    # History written before this codec was enabled (or a field that was
    # never set) has plain values mixed in — decode must not choke on those.
    codec = EncryptionCodec(_key())
    plain = _json_payload({"config": {"llm": {"api_key": "sk-plain-not-encrypted"}}})

    [decoded] = await codec.decode([plain])

    assert json.loads(decoded.data.decode())["config"]["llm"]["api_key"] == "sk-plain-not-encrypted"


@pytest.mark.asyncio
async def test_non_json_payloads_are_left_untouched():
    codec = EncryptionCodec(_key())
    binary = Payload(metadata={"encoding": b"binary/null"}, data=b"\x00\x01\x02")

    encoded = await codec.encode([binary])
    decoded = await codec.decode([binary])

    assert encoded == [binary]
    assert decoded == [binary]


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


@pytest.mark.asyncio
async def test_the_trace_license_is_masked_and_the_rest_of_the_trace_block_is_not():
    # The caller's InsightFinder license rides in the request's `trace`
    # block, which travels in the workflow input like the LLM key does.
    codec = EncryptionCodec(_key())
    original = _json_payload(
        {
            "config": {
                "trace": {
                    "trace_project": "acme-MCP-Anthropic-x-x-LLM",
                    "trace_admin_user": "acme-admin",
                    "trace_admin_user_license": "if-license-secret",
                }
            }
        }
    )

    [encoded] = await codec.encode([original])
    trace = json.loads(encoded.data.decode())["config"]["trace"]

    assert "if-license-secret" not in encoded.data.decode()
    assert trace["trace_admin_user_license"].startswith("enc:v1:")
    assert trace["trace_project"] == "acme-MCP-Anthropic-x-x-LLM"
    assert trace["trace_admin_user"] == "acme-admin"
