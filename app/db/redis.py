from redis.asyncio import Redis, from_url

from app.core.config import settings

_redis_client: Redis | None = None


async def get_redis() -> Redis:
    """Return the shared Redis async client, initialising it on first call."""
    global _redis_client
    if _redis_client is None:
        _redis_client = from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
        )
    return _redis_client


async def close_redis() -> None:
    """Close the Redis connection — call on application shutdown."""
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None
