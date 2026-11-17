"""SEC-01·03: loopback 판정과 테스트 중 외부 네트워크 차단 장치."""

import socket

import pytest

from app.security import is_loopback_host, is_loopback_url, require_loopback_url


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "127.0.0.1", "127.8.9.10", "::1", "[::1]"])
def test_loopback_hosts(host: str) -> None:
    assert is_loopback_host(host)


@pytest.mark.parametrize("host", ["", None, "10.0.0.1", "192.0.2.1", "example.com", "localhost.example.com", "0.0.0.0"])
def test_non_loopback_hosts(host) -> None:
    assert not is_loopback_host(host)


@pytest.mark.parametrize(
    "url, ok",
    [
        ("http://127.0.0.1:11434", True),
        ("https://localhost", True),
        ("http://[::1]:11434/api", True),
        ("http://127.0.0.1:99999", False),
        ("file:///C:/x", False),
        ("http://127.0.0.1.example.com", False),
        ("http://admin@localhost:11434", False),
        ("not a url", False),
    ],
)
def test_loopback_urls(url: str, ok: bool) -> None:
    assert is_loopback_url(url) is ok


def test_require_loopback_url_strips_trailing_slash() -> None:
    assert require_loopback_url(" http://127.0.0.1:11434/ ") == "http://127.0.0.1:11434"
    with pytest.raises(ValueError):
        require_loopback_url("http://203.0.113.5:11434")


def test_guard_blocks_external_connection() -> None:
    """conftest 장치가 실제로 막는지 확인한다. 192.0.2.1은 문서용 주소라 가려 해도 갈 곳이 없다."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        with pytest.raises(RuntimeError, match="외부 네트워크"):
            sock.connect(("192.0.2.1", 80))
    finally:
        sock.close()


def test_guard_blocks_external_name_lookup() -> None:
    with pytest.raises(RuntimeError, match="외부 이름 조회"):
        socket.getaddrinfo("example.com", 80)


def test_guard_allows_loopback() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect(server.getsockname())
    finally:
        client.close()
        server.close()
