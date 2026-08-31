"""
Semantic cache backed by Redis.

Final assistant messages are cached only behind an explicit CacheIdentity:
normalized query, cache kind, model version, prompt version, conversation-context
hash, and user/session scope. This prevents semantically similar personalized
prompts from sharing answers across users or conversations.

Cache key shape:
    scache:entry:<sha256(CacheIdentity JSON)>

A sorted-set "scache:index" maps key to insert_time for LRU eviction.
Falls back gracefully to in-memory if Redis is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Optional

import numpy as np
from dotenv import load_dotenv

from backend.core.logger import get_logger

load_dotenv()
log = get_logger(__name__)

SIMILARITY_THRESHOLD = 0.85
MAX_CACHE_SIZE = 500
DEFAULT_TTL = 3600
PROMPT_VERSION = "rihla-system-v1"
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
TOOL_RESULT_TTLS = {
    "get_coordinates": 604_800,
    "get_current_weather": 600,
    "get_nearby_places": 86_400,
}

_model: Optional[Any] = None
_redis = None
_use_memory_fallback = False
_MEMORY_CACHE: list[dict] = []


class CacheKind(str, Enum):
    GLOBAL_STATELESS = "global_stateless"
    USER_SCOPED = "user_scoped"
    NON_CACHEABLE = "non_cacheable"
    TOOL_RESULT = "tool_result"


class CacheAction(str, Enum):
    ALLOW = "allow"
    BYPASS = "bypass"


@dataclass(frozen=True)
class CachePolicy:
    kind: CacheKind
    action: CacheAction
    ttl_seconds: int
    reason: str


@dataclass(frozen=True)
class CacheIdentity:
    normalized_query: str
    cache_kind: str
    model_version: str
    prompt_version: str
    context_hash: str
    user_scope: str
    session_scope: str


def normalize_query(query: str) -> str:
    return " ".join(query.lower().strip().split())


def hash_context(messages: list[tuple[str, str]] | None) -> str:
    if not messages:
        return "empty"

    hashed_messages = [
        {"role": role, "content_hash": hashlib.sha256(content.encode()).hexdigest()}
        for role, content in messages
        if role != "tool"
    ]
    payload = json.dumps(hashed_messages, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def classify_cache_request(query: str, has_context: bool = False) -> CachePolicy:
    normalized = normalize_query(query)
    if not normalized:
        return CachePolicy(CacheKind.NON_CACHEABLE, CacheAction.BYPASS, 0, "empty_query")

    sensitive_terms = (
        "password",
        "token",
        "jwt",
        "secret",
        "api key",
        "credential",
        "system prompt",
        "hidden instruction",
    )
    if any(term in normalized for term in sensitive_terms):
        return CachePolicy(CacheKind.NON_CACHEABLE, CacheAction.BYPASS, 0, "sensitive_prompt")

    contextual_terms = (
        "my ",
        "i ",
        "i'm",
        "ive",
        "i've",
        "we ",
        "our ",
        "remember",
        "previous",
        "earlier",
        "again",
        "prefer",
        "preference",
        "budget",
    )
    padded = f" {normalized} "
    if has_context or any(term in padded for term in contextual_terms):
        return CachePolicy(
            CacheKind.USER_SCOPED,
            CacheAction.ALLOW,
            DEFAULT_TTL,
            "personalized_or_contextual",
        )

    if "weather" in normalized:
        return CachePolicy(CacheKind.GLOBAL_STATELESS, CacheAction.ALLOW, 600, "dynamic_weather")

    place_terms = ("nearby", "restaurant", "cafe", "museum", "hotel", "attraction")
    if any(term in normalized for term in place_terms):
        return CachePolicy(CacheKind.GLOBAL_STATELESS, CacheAction.ALLOW, 86_400, "dynamic_places")

    return CachePolicy(CacheKind.NON_CACHEABLE, CacheAction.BYPASS, 0, "general_final_answer")


def build_cache_identity(
    query: str,
    *,
    cache_kind: CacheKind,
    model_version: str,
    prompt_version: str = PROMPT_VERSION,
    context_hash: str = "empty",
    user_id: str | None = None,
    session_id: str | None = None,
) -> CacheIdentity:
    is_user_scoped = cache_kind == CacheKind.USER_SCOPED
    return CacheIdentity(
        normalized_query=normalize_query(query),
        cache_kind=cache_kind.value,
        model_version=model_version,
        prompt_version=prompt_version,
        context_hash=context_hash,
        user_scope=user_id if is_user_scoped and user_id else "global",
        session_scope=session_id if is_user_scoped and session_id else "global",
    )


def cache_key(identity: CacheIdentity) -> str:
    payload = json.dumps(asdict(identity), sort_keys=True, separators=(",", ":"))
    return f"scache:entry:{hashlib.sha256(payload.encode()).hexdigest()}"


def get_ttl(query: str) -> int:
    return classify_cache_request(query).ttl_seconds or DEFAULT_TTL


def build_tool_cache_identity(tool_name: str, args: dict) -> CacheIdentity:
    canonical_args = json.dumps(args, sort_keys=True, separators=(",", ":"))
    return build_cache_identity(
        f"{tool_name}:{canonical_args}",
        cache_kind=CacheKind.TOOL_RESULT,
        model_version="tool-result-v1",
        context_hash="tool",
    )


def get_tool_cache(tool_name: str, args: dict) -> Optional[str]:
    identity = build_tool_cache_identity(tool_name, args)
    query = identity.normalized_query
    return get_cache(query, identity, threshold=0.999)


def set_tool_cache(tool_name: str, args: dict, result: str) -> None:
    ttl = TOOL_RESULT_TTLS.get(tool_name, DEFAULT_TTL)
    identity = build_tool_cache_identity(tool_name, args)
    set_cache(identity.normalized_query, result, identity, ttl)


def get_cache(
    query: str,
    identity: CacheIdentity,
    threshold: float = SIMILARITY_THRESHOLD,
) -> Optional[str]:
    r = _get_redis()

    if r is None:
        if not _MEMORY_CACHE:
            return None
        query_vec = _encode_query(query)
        return _memory_get(query_vec, identity, threshold)

    key = cache_key(identity)
    raw = r.hgetall(key)
    if not raw:
        log.debug("[CACHE MISS] reason=no_identity_match")
        return None

    now = time.time()
    expiry = float(raw.get("expiry", 0))
    if expiry and expiry < now:
        r.delete(key)
        r.zrem("scache:index", key)
        log.debug("[CACHE MISS] reason=expired")
        return None

    expected_identity = json.dumps(asdict(identity), sort_keys=True)
    if raw.get("identity") != expected_identity:
        log.debug("[CACHE MISS] reason=identity_mismatch")
        return None

    query_vec = _encode_query(query)
    score = _cosine(query_vec, json.loads(raw["embedding"]))
    if score >= threshold:
        log.info(
            f"[CACHE HIT] kind={identity.cache_kind} scope={identity.user_scope} "
            f"key={key[-8:]} similarity={score:.3f}"
        )
        return raw["answer"]

    log.debug("[CACHE MISS] reason=similarity_below_threshold")
    return None


def set_cache(
    query: str,
    answer: str,
    identity: CacheIdentity,
    ttl: int = DEFAULT_TTL,
) -> None:
    if ttl <= 0:
        log.debug("[CACHE BYPASS] reason=non_positive_ttl")
        return

    embedding = _encode_query(query)
    expiry = time.time() + ttl
    r = _get_redis()

    if r is None:
        _memory_set(query, embedding, answer, expiry, identity)
        return

    key = cache_key(identity)
    r.hset(
        key,
        mapping={
            "query": query,
            "embedding": json.dumps(embedding),
            "answer": answer,
            "expiry": str(expiry),
            "identity": json.dumps(asdict(identity), sort_keys=True),
        },
    )
    r.expire(key, ttl + 60)
    r.zadd("scache:index", {key: time.time()})
    _evict_if_needed(r)
    log.debug(f"[CACHE SET] kind={identity.cache_kind} ttl={ttl}s key={key[-8:]}")


def _get_model() -> Any:
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer

            _model = SentenceTransformer("all-MiniLM-L6-v2")
        except Exception as exc:
            log.warning(f"Sentence transformer unavailable, using hash embeddings: {exc}")
            _model = False
    return _model


def _encode_query(query: str) -> list:
    model = _get_model()
    if model is False:
        digest = hashlib.sha256(normalize_query(query).encode()).digest()
        return [byte / 255 for byte in digest]
    embedding = model.encode(query)
    return embedding.tolist() if hasattr(embedding, "tolist") else list(embedding)


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


def _evict_if_needed(r) -> None:
    size = r.zcard("scache:index")
    if size > MAX_CACHE_SIZE:
        oldest = r.zrange("scache:index", 0, size - MAX_CACHE_SIZE - 1)
        if oldest:
            r.delete(*oldest)
            r.zrem("scache:index", *oldest)
            log.info(f"[CACHE EVICT] removed {len(oldest)} old entries")


def _memory_get(
    query_vec: list,
    identity: CacheIdentity,
    threshold: float,
) -> Optional[str]:
    now = time.time()
    best_score, best_answer = 0.0, None
    for item in _MEMORY_CACHE:
        if item["expiry"] and item["expiry"] < now:
            continue
        if item["identity"] != identity:
            continue
        score = _cosine(query_vec, item["embedding"])
        if score > best_score:
            best_score, best_answer = score, item["answer"]
    if best_score >= threshold:
        log.info(
            f"[MEM CACHE HIT] kind={identity.cache_kind} scope={identity.user_scope} "
            f"similarity={best_score:.3f}"
        )
        return best_answer
    return None


def _memory_set(
    query: str,
    embedding: list,
    answer: str,
    expiry: float,
    identity: CacheIdentity,
) -> None:
    global _MEMORY_CACHE
    _MEMORY_CACHE.append(
        {
            "query": query,
            "embedding": embedding,
            "answer": answer,
            "expiry": expiry,
            "identity": identity,
        }
    )
    if len(_MEMORY_CACHE) > MAX_CACHE_SIZE:
        _MEMORY_CACHE = _MEMORY_CACHE[-MAX_CACHE_SIZE:]
    log.debug(f"[MEM CACHE SET] kind={identity.cache_kind} scope={identity.user_scope}")
