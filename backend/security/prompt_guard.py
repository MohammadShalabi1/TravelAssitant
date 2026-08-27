from __future__ import annotations

import base64
import binascii
import re
import time
from typing import Literal

from pydantic import BaseModel, Field

from backend.core.logger import get_logger

log = get_logger(__name__)

PromptAction = Literal["allow", "allow_with_restrictions", "block"]
RiskLevel = Literal["low", "medium", "high"]


class PromptRiskResult(BaseModel):
    risk_score: int = Field(ge=0, le=100)
    risk_level: RiskLevel
    signals: list[str]
    action: PromptAction
    latency_ms: float


_OVERRIDE_PATTERNS = (
    r"\bignore\b.{0,80}\b(previous|prior|above|system|developer|instruction|rules)\b",
    r"\boverride\b.{0,80}\b(system|developer|instruction|rules|policy)\b",
    r"\byou are now\b",
    r"\bact as\b.{0,40}\b(system|developer|admin)\b",
)
_SECRET_PATTERNS = (
    r"\b(show|reveal|print|dump|exfiltrate|leak)\b.{0,80}\b(prompt|secret|token|key|credential|env)\b",
    r"\b(show|reveal|print|dump|exfiltrate|leak)\b.{0,80}\b(system prompt|developer message|hidden instruction|internal prompt)\b",
    r"\b(jwt secret|database url)\b",
)
_DISABLE_PATTERNS = (
    r"\b(disable|turn off|bypass|skip)\b.{0,80}\b(safety|guard|policy|filter|tool)\b",
    r"\bwithout\b.{0,40}\b(rate limit|validation|authorization|policy)\b",
)
_TOOL_ABUSE_PATTERNS = (
    r"\b(call|execute|run|use)\b.{0,40}\b(command|shell|python|file|url|http|host)\b",
    r"\bnon[- ]?allowlisted\b",
)


def _normalized_variants(prompt: str) -> list[str]:
    lowered = " ".join(prompt.lower().split())
    variants = [lowered]

    compact = re.sub(r"[^A-Za-z0-9+/=]", "", prompt)
    if len(compact) >= 16 and len(compact) % 4 == 0:
        try:
            decoded = base64.b64decode(compact, validate=True).decode("utf-8", errors="ignore")
            if decoded and decoded != prompt:
                variants.append(" ".join(decoded.lower().split()))
        except (binascii.Error, ValueError):
            pass

    deobfuscated = lowered.translate(str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "@": "a", "$": "s"}))
    if deobfuscated != lowered:
        variants.append(deobfuscated)

    return variants


def _matches(patterns: tuple[str, ...], variants: list[str]) -> bool:
    return any(re.search(pattern, variant, re.IGNORECASE) for pattern in patterns for variant in variants)


def analyze_prompt(prompt: str) -> PromptRiskResult:
    start = time.perf_counter()
    variants = _normalized_variants(prompt)
    signals: list[str] = []
    score = 0

    if _matches(_OVERRIDE_PATTERNS, variants):
        signals.append("instruction_override")
        score += 35
    if _matches(_SECRET_PATTERNS, variants):
        signals.append("secret_or_prompt_exfiltration")
        score += 55
    if _matches(_DISABLE_PATTERNS, variants):
        signals.append("policy_disable_attempt")
        score += 35
    if _matches(_TOOL_ABUSE_PATTERNS, variants):
        signals.append("tool_abuse_attempt")
        score += 30
    if len(variants) > 1 and any(signal in signals for signal in ("instruction_override", "secret_or_prompt_exfiltration")):
        signals.append("encoded_or_obfuscated_attack")
        score += 25

    score = min(score, 100)
    if score >= 70 or "secret_or_prompt_exfiltration" in signals:
        action: PromptAction = "block"
        level: RiskLevel = "high"
    elif score >= 35:
        action = "allow_with_restrictions"
        level = "medium"
    else:
        action = "allow"
        level = "low"

    result = PromptRiskResult(
        risk_score=score,
        risk_level=level,
        signals=signals,
        action=action,
        latency_ms=round((time.perf_counter() - start) * 1000, 3),
    )
    log.info(
        f"prompt_guard decision action={result.action} level={result.risk_level} "
        f"score={result.risk_score} signals={result.signals}"
    )
    return result


def sanitize_model_text(text: str) -> str:
    normalized = text.lower()
    risky_terms = (
        "jwt_secret",
        "gemini_api_key",
        "database_url",
        "system prompt:",
        "developer message:",
        "hidden instruction",
        "api key is",
        "token is",
    )
    if any(term in normalized for term in risky_terms):
        return "I cannot reveal hidden instructions, credentials, or internal configuration."
    return text


def safe_error_message() -> str:
    return "The request could not be completed safely. Please try a normal travel request."
