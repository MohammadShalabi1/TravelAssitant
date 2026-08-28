"""Deterministic model routing for travel-assistant turns."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum


DEFAULT_SIMPLE_MODEL = "gemini-2.5-flash-lite"
DEFAULT_FLASH_MODEL = "gemini-2.5-flash"


class RouteCategory(str, Enum):
    SIMPLE_CONVERSATION = "simple_conversation"
    TOOL_HEAVY_FACTUAL = "tool_heavy_factual"
    ITINERARY_PLANNING = "itinerary_planning"
    SAFETY_SENSITIVE = "safety_sensitive"


@dataclass(frozen=True)
class ModelRoute:
    category: RouteCategory
    model: str
    fallback_model: str
    reason: str


_SAFETY_PATTERNS = [
    r"\b(system|developer|hidden)\s+(prompt|instruction|message)s?\b",
    r"\b(api[_ -]?key|jwt[_ -]?secret|database[_ -]?url|credential|password|secret)s?\b",
    r"\b(ignore|override|bypass|disable)\b.*\b(policy|guard|safety|security|instruction|tool)s?\b",
    r"\breveal\b.*\b(prompt|secret|credential|config|environment)\b",
]

_ITINERARY_PATTERNS = [
    r"\b(itinerary|trip plan|travel plan|day[- ]?by[- ]?day|multi[- ]?day)\b",
    r"\b(plan|schedule|organize|build)\b.*\b(trip|route|vacation|journey|visit)\b",
    r"\b(budget|family|honeymoon)\b.*\b(trip|travel|vacation|itinerary)\b",
    r"\b\d+\s*(day|days|week|weeks)\b.*\b(trip|travel|itinerary|visit)\b",
]

_TOOL_HEAVY_PATTERNS = [
    r"\b(weather|forecast|temperature|rain|snow|current conditions)\b",
    r"\b(nearby|near me|around me|closest|open now)\b",
    r"\b(restaurant|cafe|coffee|hotel|museum|attraction|place)s?\b",
    r"\b(coordinate|latitude|longitude|geocode|directions?)\b",
    r"\b(today|tomorrow|tonight)\b.*\b(weather|open|nearby|restaurant|cafe|hotel|museum)\b",
]


def _configured_model(env_name: str, default: str) -> str:
    return os.getenv(env_name, default).strip() or default


def _matches_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _model_for(category: RouteCategory) -> str:
    if category == RouteCategory.SIMPLE_CONVERSATION:
        return _configured_model("RIHLA_MODEL_SIMPLE", DEFAULT_SIMPLE_MODEL)
    if category == RouteCategory.TOOL_HEAVY_FACTUAL:
        return _configured_model("RIHLA_MODEL_TOOL_HEAVY", DEFAULT_FLASH_MODEL)
    if category == RouteCategory.ITINERARY_PLANNING:
        return _configured_model("RIHLA_MODEL_ITINERARY", DEFAULT_FLASH_MODEL)
    return _configured_model("RIHLA_MODEL_SAFETY", DEFAULT_FLASH_MODEL)


def choose_model(user_text: str, security_restricted: bool = False) -> ModelRoute:
    """Classify a turn and select the primary/fallback Gemini model."""

    normalized = " ".join(user_text.lower().split())
    fallback_model = _configured_model("RIHLA_MODEL_FALLBACK", DEFAULT_FLASH_MODEL)

    if security_restricted or _matches_any(normalized, _SAFETY_PATTERNS):
        category = RouteCategory.SAFETY_SENSITIVE
        reason = "security_restricted" if security_restricted else "safety_signal"
    elif _matches_any(normalized, _ITINERARY_PATTERNS):
        category = RouteCategory.ITINERARY_PLANNING
        reason = "itinerary_signal"
    elif _matches_any(normalized, _TOOL_HEAVY_PATTERNS):
        category = RouteCategory.TOOL_HEAVY_FACTUAL
        reason = "tool_heavy_signal"
    else:
        category = RouteCategory.SIMPLE_CONVERSATION
        reason = "default"

    return ModelRoute(
        category=category,
        model=_model_for(category),
        fallback_model=fallback_model,
        reason=reason,
    )
