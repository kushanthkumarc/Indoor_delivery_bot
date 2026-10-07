from redis.asyncio import Redis, from_url

from app.core.config import settings

_redis_client: Redis | None = None


async def get_redis() -> Redis:
    """
    Return the shared Redis async client, initialising it on first call.

    Resilience kwargs (added for cloud Redis providers that close sockets
    after a few minutes of inactivity, or transient network drops):

      * `socket_keepalive=True`     — TCP keepalive detects dead peers
      * `health_check_interval=30`  — PING every 30s while idle
      * `retry_on_timeout=True`     — refresh failed commands
      * `retry_on_error=[ConnectionError, TimeoutError]` — auto-retry transient errors

    Note: the URL must use `rediss://` (TLS) for cloud providers like
    Upstash, Render KV, Redis Cloud. Plain `redis://` will fail with
    "Connection closed by server" because the server requires TLS.
    """
    global _redis_client
    if _redis_client is None:
        _redis_client = from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_keepalive=True,
            health_check_interval=30,
            retry_on_timeout=True,
            retry_on_error=[ConnectionError, TimeoutError],
        )
    return _redis_client


async def close_redis() -> None:
    """Close the Redis connection — call on application shutdown."""
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None