"""가짜 Ollama. /api/chat에 미리 정한 응답을 스트리밍(NDJSON)으로 돌려주고 요청 본문을 기록한다."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from app.ai.ollama_client import OllamaClient

BASE = "http://127.0.0.1:11434"


class FakeOllama:
    def __init__(self, responses: list[Any] | None = None, models: tuple[str, ...] = ("test-model:7b",)) -> None:
        self.responses = list(responses or [])
        self.requests: list[dict[str, Any]] = []
        self.models = models
        self.transport = httpx.MockTransport(self._handle)

    def push(self, *responses: Any) -> None:
        self.responses.extend(responses)

    def client(self) -> OllamaClient:
        return OllamaClient(BASE, transport=self.transport)

    @property
    def factory(self) -> Callable[[], OllamaClient]:
        return self.client

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.0.1-test"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in self.models]})
        if request.url.path != "/api/chat":
            return httpx.Response(404)
        self.requests.append(json.loads(request.content))
        if not self.responses:
            return httpx.Response(500, text="가짜 Ollama: 준비된 응답 없음")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        content = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        chunks = [json.dumps({"message": {"content": content[i:i + 17]}, "done": False}, ensure_ascii=False)
                  for i in range(0, len(content), 17)]
        chunks.append(json.dumps({"message": {"content": ""}, "done": True}))
        return httpx.Response(200, content=("\n".join(chunks) + "\n").encode("utf-8"))

    def last_prompt(self) -> str:
        return self.requests[-1]["messages"][-1]["content"]

    def last_system(self) -> str:
        return self.requests[-1]["messages"][0]["content"]
