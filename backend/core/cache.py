"""
Semantic cache backed by Redis.

Each entry is stored as a Redis hash under the key:
    scache:<sha256(query)>

A sorted-set  "scache:index"  maps  key → insert_time  for LRU eviction.

Falls back gracefully to in-memory if Redis is unavailable (dev mode).
"""

from __future__ import annotations

import json
import time
import hashlib
import os
from typing import Optional

import numpy as np
from sentence_transformers import SentenceTransformer

from core.logger import get_logger

log = get_logger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
SIMILARITY_THRESHOLD = 0.85
MAX_CACHE_SIZE = 500          # evict oldest when exceeded
DEFAULT_TTL = 3600            # seconds

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# ── Model (loaded once) ───────────────────────────────────────────────────────
_model: Optional[SentenceTransformer] = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


# ── Redis connection (lazy, with fallback) ────────────────────────────────────
_redis = None
_use_memory_fallback = False
_MEMORY_CACHE: list[dict] = []   # fallback


def _get_redis():
    global _redis, _use_memory_fallback
    if _use_memory_fallback:
        return None
    if _redis is not None:
        return _redis
    try:
        import redis as redis_lib
        client = redis_lib.from_url(REDIS_URL, decode_responses=True, socket_timeout=2)
        client.ping()
        _redis = client
        log.info("Redis cache connected")
        return _redis
    except Exception as e:
        log.warning(f"Redis unavailable ({e}), using in-memory fallback")
        _use_memory_fallback = True
        return None


def _cosine(a, b):
    a, b = np.array(a), np.array(b)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


# ── TTL helper ────────────────────────────────────────────────────────────────

def get_ttl(query: str) -> int:
    q = query.lower()
    if "weather" in q:
        return 600          # 10 min
    if "nearby" in q or "restaurant" in q:
        return 86_400       # 1 day
    return DEFAULT_TTL      # 1 hour


# ── Public API ────────────────────────────────────────────────────────────────

def get_cache(query: str, threshold: float = SIMILARITY_THRESHOLD) -> Optional[str]:
    query_vec = _get_model().encode(query).tolist()
    r = _get_redis()

    if r is None:
        return _memory_get(query_vec, threshold)

    # Scan all scache: keys
    best_score, best_answer = 0.0, None
    now = time.time()

    for key in r.scan_iter("scache:entry:*"):
        try:
            raw = r.hgetall(key)
            if not raw:
                continue
            expiry = float(raw.get("expiry", 0))
            if expiry and expiry < now:
                r.delete(key)
                r.zrem("scache:index", key)
                continue
            stored_vec = json.loads(raw["embedding"])
            score = _cosine(query_vec, stored_vec)
            if score > best_score:
                best_score = score
                best_answer = raw["answer"]
        except Exception as e:
            log.debug(f"Cache read error on {key}: {e}")

    if best_score >= threshold:
        log.info(f"[CACHE HIT] similarity={best_score:.3f}")
        return best_answer

    log.debug("[CACHE MISS]")
    return None


def set_cache(query: str, answer: str, ttl: int = DEFAULT_TTL):
    embedding = _get_model().encode(query).tolist()
    expiry = time.time() + ttl if ttl else 0
    r = _get_redis()

    if r is None:
        _memory_set(query, embedding, answer, expiry)
        return

    key = f"scache:entry:{hashlib.sha256(query.encode()).hexdigest()}"
    r.hset(key, mapping={
        "query":     query,
        "embedding": json.dumps(embedding),
        "answer":    answer,
        "expiry":    str(expiry),
    })
    if ttl:
        r.expire(key, ttl + 60)   # let Redis also hard-expire it

    # Update sorted set for LRU eviction
    r.zadd("scache:index", {key: time.time()})
    _evict_if_needed(r)
    log.debug(f"[CACHE SET] ttl={ttl}s key={key[-8:]}")


def _evict_if_needed(r):
    size = r.zcard("scache:index")
    if size > MAX_CACHE_SIZE:
        oldest = r.zrange("scache:index", 0, size - MAX_CACHE_SIZE - 1)
        if oldest:
            r.delete(*oldest)
            r.zrem("scache:index", *oldest)
            log.info(f"[CACHE EVICT] removed {len(oldest)} old entries")


# ── In-memory fallback ────────────────────────────────────────────────────────

def _memory_get(query_vec: list, threshold: float) -> Optional[str]:
    now = time.time()
    best_score, best_answer = 0.0, None
    for item in _MEMORY_CACHE:
        if item["expiry"] and item["expiry"] < now:
            continue
        score = _cosine(query_vec, item["embedding"])
        if score > best_score:
            best_score, best_answer = score, item["answer"]
    if best_score >= threshold:
        log.info(f"[MEM CACHE HIT] similarity={best_score:.3f}")
        return best_answer
    return None


def _memory_set(query: str, embedding: list, answer: str, expiry: float):
    global _MEMORY_CACHE
    _MEMORY_CACHE.append({"query": query, "embedding": embedding,
                           "answer": answer, "expiry": expiry})
    if len(_MEMORY_CACHE) > MAX_CACHE_SIZE:
        _MEMORY_CACHE = _MEMORY_CACHE[-MAX_CACHE_SIZE:]
    log.debug("[MEM CACHE SET]")