"""Shared async Redis client for the mitmproxy addons.

One connection pool per process, created lazily on first use. Using one
pool avoids three separate pools (one per addon) competing for connections
under concurrent traffic.
"""
from __future__ import annotations

import os

import redis.asyncio as redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379")

_client: redis.Redis | None = None


async def client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(REDIS_URL, decode_responses=True)
    return _client
