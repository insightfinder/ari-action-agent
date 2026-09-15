"""Single Temporal client connection to the externally hosted cluster,
reused by both the API process (to start/execute workflows) and the worker
process (indirectly, via the same settings)."""

from temporalio.client import Client, TLSConfig

from ..settings import get_settings

_client: Client | None = None


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
    )
    return _client
