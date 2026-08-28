"""Trusted client IP and CORS configuration helpers."""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass

from starlette.requests import Request


LOCAL_DEV_ORIGINS = ("http://localhost:5173", "http://localhost:3000")
SAFE_CORS_METHODS = ["GET", "POST", "PATCH", "DELETE", "OPTIONS"]
SAFE_CORS_HEADERS = ["Authorization", "Content-Type", "X-CSRF-Token"]
EXPOSED_CORS_HEADERS = ["X-Response-Time-Ms"]


@dataclass(frozen=True)
class CorsConfig:
    allow_origins: list[str]
    allow_methods: list[str]
    allow_headers: list[str]
    expose_headers: list[str]


def _is_production() -> bool:
    return os.getenv("APP_ENV", "local").strip().lower() in {"prod", "production"}


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def get_cors_config() -> CorsConfig:
    origins = _split_csv(os.getenv("ALLOWED_ORIGINS"))
    frontend_url = os.getenv("FRONTEND_URL", "").strip()
    if frontend_url:
        origins.append(frontend_url)

    origins = list(dict.fromkeys(origins))
    if _is_production():
        if not origins:
            raise RuntimeError(
                "ALLOWED_ORIGINS or FRONTEND_URL must be set in production."
            )
        if "*" in origins:
            raise RuntimeError("Wildcard CORS origins are not allowed in production.")
    elif not origins:
        origins = list(LOCAL_DEV_ORIGINS)

    return CorsConfig(
        allow_origins=origins,
        allow_methods=SAFE_CORS_METHODS,
        allow_headers=SAFE_CORS_HEADERS,
        expose_headers=EXPOSED_CORS_HEADERS,
    )


def _trusted_proxy_networks() -> list[ipaddress._BaseNetwork]:
    networks: list[ipaddress._BaseNetwork] = []
    for cidr in _split_csv(os.getenv("TRUSTED_PROXY_CIDRS")):
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue
    return networks


def _parse_ip(value: str) -> ipaddress._BaseAddress | None:
    candidate = value.strip().strip('"')
    if not candidate or candidate.lower() == "unknown":
        return None
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1 : candidate.index("]")]
    elif candidate.count(":") == 1 and "." in candidate:
        candidate = candidate.rsplit(":", 1)[0]

    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def _is_trusted_proxy(host: str, networks: list[ipaddress._BaseNetwork]) -> bool:
    peer_ip = _parse_ip(host)
    return bool(peer_ip and any(peer_ip in network for network in networks))


def _extract_forwarded_ips(header_value: str) -> list[ipaddress._BaseAddress] | None:
    ips: list[ipaddress._BaseAddress] = []
    for entry in header_value.split(","):
        match = re.search(r"(?:^|;)\s*for=([^;]+)", entry, flags=re.IGNORECASE)
        if not match:
            return None
        parsed = _parse_ip(match.group(1).strip())
        if parsed is None:
            return None
        ips.append(parsed)
    return ips or None


def _extract_x_forwarded_for_ips(header_value: str) -> list[ipaddress._BaseAddress] | None:
    ips: list[ipaddress._BaseAddress] = []
    for part in header_value.split(","):
        parsed = _parse_ip(part)
        if parsed is None:
            return None
        ips.append(parsed)
    return ips or None


def _derive_from_proxy_chain(
    forwarded_ips: list[ipaddress._BaseAddress],
    trusted_networks: list[ipaddress._BaseNetwork],
) -> str:
    for ip in reversed(forwarded_ips):
        if not any(ip in network for network in trusted_networks):
            return str(ip)
    return str(forwarded_ips[0])


def get_client_ip(request: Request) -> str:
    peer_host = request.client.host if request.client else "unknown"
    trusted_networks = _trusted_proxy_networks()

    if not _is_trusted_proxy(peer_host, trusted_networks):
        return peer_host

    forwarded = request.headers.get("Forwarded")
    if forwarded:
        forwarded_ips = _extract_forwarded_ips(forwarded)
        if forwarded_ips is None:
            return peer_host
        return _derive_from_proxy_chain(forwarded_ips, trusted_networks)

    x_forwarded_for = request.headers.get("X-Forwarded-For")
    if x_forwarded_for:
        forwarded_ips = _extract_x_forwarded_for_ips(x_forwarded_for)
        if forwarded_ips is None:
            return peer_host
        return _derive_from_proxy_chain(forwarded_ips, trusted_networks)

    return peer_host
