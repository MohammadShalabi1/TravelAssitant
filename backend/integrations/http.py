"""Shared resilient HTTP integration client."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any

import requests

from backend.core.metrics import record_provider_call


class ProviderError(RuntimeError):
    provider = "unknown"


class ProviderUnavailableError(ProviderError):
    pass


@dataclass
class CircuitState:
    failures: int = 0
    opened_until: float = 0.0


_session = requests.Session()
_circuits: dict[str, CircuitState] = {}


def _circuit(provider: str) -> CircuitState:
    return _circuits.setdefault(provider, CircuitState())


def _retry_after_seconds(response: requests.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return min(float(value), 10.0)
    except ValueError:
        return None


def request_json(
    provider: str,
    method: str,
    url: str,
    *,
    timeout: float = 10.0,
    retries: int = 2,
    **kwargs: Any,
) -> Any:
    state = _circuit(provider)
    now = time.time()
    if state.opened_until > now:
        raise ProviderUnavailableError(f"{provider} circuit is open")

    delay = 0.4
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        started = time.time()
        try:
            response = _session.request(method, url, timeout=timeout, **kwargs)
            record_provider_call(provider, time.time() - started, response.status_code < 500)
            if response.status_code == 429:
                wait = _retry_after_seconds(response) or delay
                if attempt < retries:
                    time.sleep(wait)
                    continue
                raise ProviderUnavailableError(f"{provider} rate limited")
            if response.status_code >= 500:
                raise ProviderUnavailableError(f"{provider} returned {response.status_code}")
            response.raise_for_status()
            state.failures = 0
            return response.json()
        except (requests.Timeout, requests.ConnectionError, ProviderUnavailableError, ValueError) as exc:
            record_provider_call(provider, time.time() - started, False)
            last_error = exc
            if attempt < retries:
                time.sleep(delay + random.uniform(0, delay / 2))
                delay *= 2
                continue
            state.failures += 1
            if state.failures >= 3:
                state.opened_until = time.time() + 30
            raise ProviderUnavailableError(str(exc)) from exc
    raise ProviderUnavailableError(str(last_error))
