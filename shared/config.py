"""Shared configuration and MongoDB/Redis clients for all services."""
import os
from motor.motor_asyncio import AsyncIOMotorClient
import redis.asyncio as aioredis


MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017/mc")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
LLM_GATEWAY_URL = os.environ.get("LLM_GATEWAY_URL", "http://localhost:8082")

_mongo_client = None
_db = None
_redis = None


async def get_db():
    global _mongo_client, _db
    if _db is None:
        _mongo_client = AsyncIOMotorClient(MONGO_URL)
        db_name = MONGO_URL.rsplit("/", 1)[-1].split("?")[0] or "mc"
        _db = _mongo_client[db_name]
    return _db


async def get_redis():
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis
