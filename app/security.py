"""보안 공통 함수. 외부 네트워크 요청 금지(§9.2)를 지키기 위한 주소 판정을 한곳에 둔다."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

LOOPBACK_HOSTNAMES = frozenset({"localhost"})
ALLOWED_SCHEMES = frozenset({"http", "https"})


def is_loopback_host(host: str | None) -> bool:
    """호스트가 이 PC 자신(127.0.0.0/8, ::1, localhost)이면 True."""
    if not host:
        return False
    host = host.strip().strip("[]").lower()
    if host in LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_loopback_url(url: str) -> bool:
    """http(s) 주소이고 호스트가 loopback이며 사용자 정보가 없으면 True."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port  # 잘못된 포트면 ValueError
    except ValueError:
        return False
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return False
    if parts.username or parts.password:
        return False
    if port is not None and not (0 < port < 65536):
        return False
    return is_loopback_host(parts.hostname)


def require_loopback_url(url: str) -> str:
    """loopback 주소가 아니면 ValueError. 앞뒤 공백과 끝의 '/'를 정리해 돌려준다."""
    if not is_loopback_url(url):
        raise ValueError(
            f"이 PC 안의 주소(127.0.0.1, localhost, ::1)만 쓸 수 있습니다: {url!r}"
        )
    return url.strip().rstrip("/")
