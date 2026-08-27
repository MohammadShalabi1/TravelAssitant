"""
In-process observability metrics.

Tracks:
  - requests per endpoint
  - avg / p95 response time
  - cache hit rate
  - tool usage frequency + failure rate
  - active sessions

Exposes:  GET /metrics  (plain text Prometheus format)
          GET /api/metrics  (JSON, human-readable)
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock
from typing import Deque

_lock = Lock()

# ── Counters ──────────────────────────────────────────────────────────────────
_request_counts:    dict[str, int]   = defaultdict(int)   # endpoint → count
_error_counts:      dict[str, int]   = defaultdict(int)   # endpoint → errors
_tool_calls:        dict[str, int]   = defaultdict(int)   # tool_name → calls
_tool_failures:     dict[str, int]   = defaultdict(int)   # tool_name → failures
_cache_hits:        int = 0
_cache_misses:      int = 0
_prompt_guard_decisions: dict[str, int] = defaultdict(int)
_prompt_guard_latencies: Deque[float] = deque(maxlen=500)
_blocked_tool_calls: dict[str, int] = defaultdict(int)

# ── Latency sliding window (last 500 requests per endpoint) ──────────────────
_latencies: dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=500))


# ── Public recording API ──────────────────────────────────────────────────────

def record_request(endpoint: str, duration_s: float, success: bool = True):
    with _lock:
        _request_counts[endpoint] += 1
        _latencies[endpoint].append(duration_s)
        if not success:
            _error_counts[endpoint] += 1


def record_tool_call(tool_name: str, success: bool = True):
    with _lock:
        _tool_calls[tool_name] += 1
        if not success:
            _tool_failures[tool_name] += 1


def record_cache(hit: bool):
    global _cache_hits, _cache_misses
    with _lock:
        if hit:
            _cache_hits += 1
        else:
            _cache_misses += 1


def record_prompt_guard(action: str, risk_level: str, latency_ms: float):
    with _lock:
        _prompt_guard_decisions[f"{action}:{risk_level}"] += 1
        _prompt_guard_latencies.append(latency_ms / 1000)


def record_blocked_tool_call(tool_name: str, reason: str):
    with _lock:
        _blocked_tool_calls[f"{tool_name}:{reason}"] += 1


# ── Snapshot ──────────────────────────────────────────────────────────────────

def _percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    idx = int(len(sorted_data) * p / 100)
    return round(sorted_data[min(idx, len(sorted_data) - 1)], 4)


def get_metrics() -> dict:
    with _lock:
        total_requests = sum(_request_counts.values())
        total_errors   = sum(_error_counts.values())
        total_cache    = _cache_hits + _cache_misses
        cache_hit_rate = round(_cache_hits / total_cache, 4) if total_cache else 0.0

        endpoint_stats = {}
        for ep, count in _request_counts.items():
            lats = list(_latencies[ep])
            endpoint_stats[ep] = {
                "requests":   count,
                "errors":     _error_counts.get(ep, 0),
                "avg_ms":     round(sum(lats) / len(lats) * 1000, 1) if lats else 0,
                "p95_ms":     round(_percentile(lats, 95) * 1000, 1),
            }

        tool_stats = {}
        for tool, calls in _tool_calls.items():
            tool_stats[tool] = {
                "calls":        calls,
                "failures":     _tool_failures.get(tool, 0),
                "failure_rate": round(_tool_failures.get(tool, 0) / calls, 4) if calls else 0,
            }

        return {
            "summary": {
                "total_requests":  total_requests,
                "total_errors":    total_errors,
                "error_rate":      round(total_errors / total_requests, 4) if total_requests else 0,
                "cache_hits":      _cache_hits,
                "cache_misses":    _cache_misses,
                "cache_hit_rate":  cache_hit_rate,
            },
            "endpoints": endpoint_stats,
            "tools":     tool_stats,
            "security": {
                "prompt_guard_decisions": dict(_prompt_guard_decisions),
                "prompt_guard_p95_ms": round(
                    _percentile(list(_prompt_guard_latencies), 95) * 1000, 1
                ),
                "blocked_tool_calls": dict(_blocked_tool_calls),
            },
        }


def prometheus_export() -> str:
    """Minimal Prometheus text format."""
    m = get_metrics()
    lines = [
        "# HELP travel_agent_requests_total Total HTTP requests",
        "# TYPE travel_agent_requests_total counter",
    ]
    for ep, stats in m["endpoints"].items():
        safe = ep.replace("/", "_").replace("-", "_").lstrip("_")
        lines.append(f'travel_agent_requests_total{{endpoint="{ep}"}} {stats["requests"]}')
        lines.append(f'travel_agent_errors_total{{endpoint="{ep}"}} {stats["errors"]}')
        lines.append(f'travel_agent_p95_ms{{endpoint="{ep}"}} {stats["p95_ms"]}')

    lines += [
        f'travel_agent_cache_hits_total {m["summary"]["cache_hits"]}',
        f'travel_agent_cache_misses_total {m["summary"]["cache_misses"]}',
    ]
    for tool, stats in m["tools"].items():
        lines.append(f'travel_agent_tool_calls_total{{tool="{tool}"}} {stats["calls"]}')
        lines.append(f'travel_agent_tool_failures_total{{tool="{tool}"}} {stats["failures"]}')
    for decision, count in m["security"]["prompt_guard_decisions"].items():
        action, risk_level = decision.split(":", 1)
        lines.append(
            f'travel_agent_prompt_guard_decisions_total{{action="{action}",risk_level="{risk_level}"}} {count}'
        )
    for blocked, count in m["security"]["blocked_tool_calls"].items():
        tool, reason = blocked.split(":", 1)
        lines.append(
            f'travel_agent_blocked_tool_calls_total{{tool="{tool}",reason="{reason}"}} {count}'
        )

    return "\n".join(lines) + "\n"
