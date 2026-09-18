"""Single Temporal client connection to the externally hosted cluster,
reused by both the API process (to start/execute workflows) and the worker
process (indirectly, via the same settings)."""

import base64

from temporalio.client import Client, TLSConfig
from temporalio.converter import DataConverter

from ..settings import get_settings
from .payload_codec import EncryptionCodec

_client: Client | None = None


def _data_converter(payload_encryption_key: str | None) -> DataConverter:
    if not payload_encryption_key:
        return DataConverter.default
    key = base64.b64decode(payload_encryption_key)
    return DataConverter(payload_codec=EncryptionCodec(key))


async def get_temporal_client() -> Client:
    global _client
    if _client is not None:
        return _client

    settings = get_settings()
    _client = await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace,
        tls=TLSConfig() if settings.temporal_tls_enabled else False,
        api_key=settings.temporal_api_key or None,
        data_converter=_data_converter(settings.temporal_payload_encryption_key),
    )
    return _client
