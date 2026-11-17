"""--check의 네트워크 안전 점검 줄 (사용자 질문: 로컬 LLM이 외부 인터넷과 연결되지 않게 쓰이는지)."""

from __future__ import annotations

import pytest

from app.ai.ollama_client import OllamaStatus
from main import network_safety_lines

BASE = "http://127.0.0.1:11434"


def status(remote=()) -> OllamaStatus:
    return OllamaStatus(base_url=BASE, reachable=True, version="t", models=["m:7b"], remote_models=list(remote))


def text(lines) -> str:
    return "\n".join(lines)


def test_safe_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OLLAMA_HOST", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OLLAMA_NO_CLOUD", "1")
    out = text(network_safety_lines(BASE, status()))
    assert "[주의]" not in out
    assert "OLLAMA_HOST 없음" in out and "모두 로컬 모델" in out


def test_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:11434")
    monkeypatch.delenv("OLLAMA_NO_CLOUD", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://192.0.2.1:3128")
    out = text(network_safety_lines(BASE, status(["big:120b-cloud"])))
    assert "[주의] OLLAMA_HOST=0.0.0.0:11434" in out
    assert "[주의] OLLAMA_NO_CLOUD=없음" in out
    assert "[주의] 클라우드·원격 모델" in out
    assert "무시합니다" in out  # 프록시는 이 앱이 무시


def test_loopback_host_variants_are_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("127.0.0.1:11434", "http://localhost:11434", "[::1]:11434"):
        monkeypatch.setenv("OLLAMA_HOST", value)
        assert f"[안전] OLLAMA_HOST={value}" in text(network_safety_lines(BASE, status()))
