"""Ollama 연결 상태 확인 (PRN-01, SEC-01·03, T-NA-08·09, T-SEC-02). 가짜 서버(MockTransport)만 쓴다."""

from __future__ import annotations

import httpx
import pytest

from app.ai.ollama_client import OllamaClient

BASE = "http://127.0.0.1:11434"


def make_client(handler, **kwargs) -> OllamaClient:
    return OllamaClient(BASE, transport=httpx.MockTransport(handler), **kwargs)


def healthy_handler(models=("test-model:7b", "other:latest")):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.0.1-test"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in models]})
        return httpx.Response(404)

    return handler


def test_reachable_server_lists_models() -> None:
    with make_client(healthy_handler()) as client:
        status = client.check_status()
    assert status.reachable
    assert status.version == "0.0.1-test"
    assert status.models == ["test-model:7b", "other:latest"]
    assert status.model_available is None
    assert "연결됨" in status.summary() and "미지정" in status.summary()


@pytest.mark.parametrize("wanted, available", [("test-model:7b", True), ("other", True), ("missing:8b", False)])
def test_configured_model_availability(wanted: str, available: bool) -> None:
    with make_client(healthy_handler()) as client:
        status = client.check_status(wanted)
    assert status.model_available is available
    assert status.configured_model == wanted


def test_server_not_running() -> None:  # T-NA-08
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with make_client(handler) as client:
        status = client.check_status("test-model:7b")
    assert not status.reachable
    assert "실행 중인지" in (status.error or "")
    assert status.summary() == "Ollama: 연결 안 됨"


def test_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with make_client(handler) as client:
        status = client.check_status()
    assert not status.reachable and "시간이 초과" in (status.error or "")


def test_http_error_status() -> None:
    with make_client(lambda request: httpx.Response(500)) as client:
        status = client.check_status()
    assert not status.reachable and "HTTP 500" in (status.error or "")


@pytest.mark.parametrize(
    "tags_body",
    [b"not json", b"[]", b'{"models": "x"}', b'{"models": [{"size": 1}]}'],
)
def test_malformed_response(tags_body: bytes) -> None:  # T-NA-09 (Phase 1 범위)
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.0.1-test"})
        return httpx.Response(200, content=tags_body)

    with make_client(handler) as client:
        status = client.check_status()
    assert not status.reachable and "형식" in (status.error or "")


@pytest.mark.parametrize("url", ["http://10.1.2.3:11434", "http://ollama.example.com", "https://api.example.com"])
def test_non_loopback_url_rejected(url: str) -> None:
    with pytest.raises(ValueError, match="이 PC 안의 주소"):
        OllamaClient(url)


def test_proxy_environment_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """사내 PC에 프록시 환경 변수가 있어도 요청은 loopback 서버로 바로 간다 (trust_env=False)."""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://192.0.2.1:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return healthy_handler()(request)

    with make_client(handler) as client:
        status = client.check_status()
    assert status.reachable
    assert seen and all(u.startswith(BASE) for u in seen)


def test_redirects_are_not_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://203.0.113.9/api/version"})

    with make_client(handler) as client:
        status = client.check_status()
    assert not status.reachable


# ── 클라우드·원격 모델 차단 (§9.2 외부 LLM 금지) ──
def tags_with_remote(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/version":
        return httpx.Response(200, json={"version": "0.0.1-test"})
    if request.url.path == "/api/tags":
        return httpx.Response(200, json={"models": [
            {"name": "local-model:7b"},
            {"name": "big-model:120b-cloud"},
            {"name": "proxy-model:1b", "remote_host": "https://ollama.com:443", "remote_model": "x"},
        ]})
    if request.url.path == "/api/chat":
        raise AssertionError("원격 모델 요청이 Ollama로 보내지면 안 된다")
    return httpx.Response(404)


def test_remote_models_are_listed_separately() -> None:
    with make_client(tags_with_remote) as client:
        status = client.check_status("big-model:120b-cloud")
    assert status.models == ["local-model:7b"]
    assert status.remote_models == ["big-model:120b-cloud", "proxy-model:1b"]
    assert "클라우드 모델이라 쓸 수 없음" in status.summary()
    assert "이 앱은 쓰지 않음" in status.details()


@pytest.mark.parametrize("model", ["big-model:120b-cloud", "proxy-model:1b", "anything:cloud"])
def test_remote_model_requests_are_never_sent(model: str) -> None:
    from app.ai.ollama_client import OllamaError

    with make_client(tags_with_remote) as client, pytest.raises(OllamaError, match="클라우드"):
        client.generate_json(model=model, system="s", prompt="p", schema={})
