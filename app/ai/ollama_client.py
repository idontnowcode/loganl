"""로컬 Ollama HTTP 클라이언트 (계획서 §2.2, §9.2, docs/AI_POLICY.md §2).

Phase 1은 연결 상태 확인만 한다. JSON 생성(generate_json)은 Phase 3에서 더한다.

- 주소는 loopback만 허용한다.
- `trust_env=False`: 사내 PC의 HTTP_PROXY 같은 환경 변수가 있어도 요청을 프록시로 보내지 않는다.
- `follow_redirects=False`: 리디렉션으로 다른 호스트에 가지 않는다.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import BaseModel, Field

from app import __version__
from app.core.cancel import NEVER_CANCEL, CancelToken
from app.security import require_loopback_url


class OllamaError(RuntimeError):
    """Ollama 호출 실패. 메시지는 화면에 그대로 보여 줄 수 있는 한국어다."""


# 문맥 길이(num_ctx). 주지 않으면 Ollama 기본값(0.40 기준 약 2,000토큰)만 쓰고, 넘는 입력은 앞에서부터 잘라 버린다.
# 그러면 시스템 지시(규칙·한국어·근거 키 형식)가 사라져 영어 응답·없는 근거 키로 검증에 실패한다(2026-11-19 실사용).
MIN_CONTEXT = 4096
MAX_CONTEXT = 16384
OUTPUT_RESERVE = 2048  # 응답 JSON 몫
CHARS_PER_TOKEN = 1.5  # 한국어가 섞인 글은 gemma3에서 1토큰에 약 1.9자(실측). 모자라지 않게 작게 잡는다


def context_size(*texts: str) -> int:
    """보낼 글에 맞춘 num_ctx. 1024 단위로 올리고 MIN~MAX 사이로 둔다. MAX를 넘으면 잘리므로 OllamaError."""
    need = int(sum(len(t) for t in texts) / CHARS_PER_TOKEN) + OUTPUT_RESERVE
    if need > MAX_CONTEXT:
        raise OllamaError(f"AI에 보낼 내용이 너무 깁니다(약 {need}토큰, 최대 {MAX_CONTEXT}). 근거를 줄여 다시 시도하세요.")
    return max(MIN_CONTEXT, -(-need // 1024) * 1024)


class OllamaStatus(BaseModel):
    base_url: str
    reachable: bool
    version: str | None = None
    models: list[str] = Field(default_factory=list)  # 이 PC에서 도는 로컬 모델만
    remote_models: list[str] = Field(default_factory=list)  # 클라우드·원격 모델(쓰지 않음)
    configured_model: str | None = None
    model_available: bool | None = None  # 모델을 지정하지 않았으면 None
    error: str | None = None

    def summary(self) -> str:
        """상태 표시줄에 쓸 한 줄."""
        if not self.reachable:
            return "Ollama: 연결 안 됨"
        text = f"Ollama: 연결됨 (v{self.version or '?'}, 로컬 모델 {len(self.models)}개)"
        if self.configured_model is None:
            return text + " · 사용할 모델 미지정"
        if self.configured_model in self.remote_models or is_remote_name(self.configured_model):
            return text + f" · 지정한 모델은 클라우드 모델이라 쓸 수 없음: {self.configured_model}"
        if self.model_available:
            return text + f" · {self.configured_model}"
        return text + f" · 지정한 모델 없음: {self.configured_model}"

    def details(self) -> str:
        """툴팁·점검 출력에 쓸 여러 줄."""
        lines = [f"주소: {self.base_url}"]
        if self.reachable:
            lines.append(f"버전: {self.version or '알 수 없음'}")
            lines.append("로컬 모델: " + (", ".join(self.models) if self.models else "없음"))
            if self.remote_models:
                lines.append("클라우드·원격 모델(이 앱은 쓰지 않음): " + ", ".join(self.remote_models))
        if self.error:
            lines.append(f"오류: {self.error}")
        return "\n".join(lines)


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        *,
        connect_timeout_s: float = 3.0,
        request_timeout_s: float = 300.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = require_loopback_url(base_url)
        self._status_timeout = httpx.Timeout(max(5.0, connect_timeout_s), connect=connect_timeout_s)
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(request_timeout_s, connect=connect_timeout_s),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": f"LocalLogAnalyzer/{__version__}"},
        )

    def check_status(self, model: str | None = None) -> OllamaStatus:
        """서버 버전과 설치된 모델 목록을 읽는다. 실패해도 예외를 던지지 않고 상태로 돌려준다."""
        try:
            version_data = self._get_json("/api/version")
            version = version_data.get("version")
            if version is not None and not isinstance(version, str):
                raise ValueError("version 형식 오류")
            models, remote = _split_models(self._get_json("/api/tags"))
        except httpx.ConnectError:
            return self._failed(model, "Ollama에 연결할 수 없습니다. Ollama가 실행 중인지 확인하세요.")
        except httpx.TimeoutException:
            return self._failed(model, "Ollama 응답 시간이 초과됐습니다.")
        except httpx.HTTPStatusError as exc:
            return self._failed(model, f"Ollama가 오류 응답을 보냈습니다 (HTTP {exc.response.status_code}).")
        except httpx.HTTPError as exc:
            return self._failed(model, f"Ollama 요청에 실패했습니다 ({exc.__class__.__name__}).")
        except (ValueError, KeyError, TypeError):
            return self._failed(model, "Ollama 응답 형식을 이해할 수 없습니다.")

        return OllamaStatus(
            base_url=self.base_url,
            reachable=True,
            version=version,
            models=models,
            remote_models=remote,
            configured_model=model,
            model_available=None if model is None else _has_model(models, model),
        )

    def ensure_local_model(self, model: str) -> None:
        """클라우드·원격 모델이면 OllamaError. 원격 모델은 Ollama가 요청을 ollama.com으로 넘기므로 쓰지 않는다(§9.2)."""
        if is_remote_name(model):
            raise OllamaError(f"'{model}'은(는) 클라우드 모델이라 쓸 수 없습니다. 이 PC의 로컬 모델만 씁니다.")
        try:
            _, remote = _split_models(self._get_json("/api/tags"))
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise OllamaError("Ollama 모델 목록을 확인하지 못해 요청을 보내지 않았습니다.") from exc
        if model in remote or (":" not in model and f"{model}:latest" in remote):
            raise OllamaError(f"'{model}'은(는) 원격(클라우드) 모델이라 쓸 수 없습니다. 이 PC의 로컬 모델만 씁니다.")

    def generate_json(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        cancel: CancelToken = NEVER_CANCEL,
    ) -> str:
        """/api/chat에 JSON 스키마를 지정해 요청하고 응답 문자열을 돌려준다(검증은 response_validator가 한다).
        스트리밍으로 받으며 조각마다 취소를 확인한다. 실패하면 OllamaError.
        보내기 전에 모델이 로컬 모델인지 확인한다(클라우드 모델이면 보내지 않음).
        문맥 길이는 글 길이에 맞춰 정한다(context_size) — 시스템 지시가 잘리지 않게."""
        num_ctx = context_size(system, prompt)
        self.ensure_local_model(model)
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "format": schema,
            "stream": True,
            "options": {"temperature": 0, "num_ctx": num_ctx},
        }
        chunks: list[str] = []
        try:
            with self._client.stream("POST", "/api/chat", json=body) as response:
                if response.status_code >= 400:
                    response.read()
                    raise OllamaError(f"Ollama가 오류 응답을 보냈습니다 (HTTP {response.status_code}): "
                                      f"{response.text[:200]}")
                for line in response.iter_lines():
                    cancel.raise_if_cancelled()
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if data.get("error"):
                        raise OllamaError(f"Ollama 오류: {data['error']}")
                    chunks.append((data.get("message") or {}).get("content", ""))
                    if data.get("done"):
                        break
        except httpx.ConnectError as exc:
            raise OllamaError("Ollama에 연결할 수 없습니다. Ollama가 실행 중인지 확인하세요.") from exc
        except httpx.TimeoutException as exc:
            raise OllamaError("Ollama 응답 시간이 초과됐습니다.") from exc
        except httpx.HTTPError as exc:
            raise OllamaError(f"Ollama 요청에 실패했습니다 ({exc.__class__.__name__}).") from exc
        except json.JSONDecodeError as exc:
            raise OllamaError("Ollama 스트림 형식을 이해할 수 없습니다.") from exc
        return "".join(chunks)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OllamaClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _get_json(self, path: str) -> dict[str, Any]:
        response = self._client.get(path, timeout=self._status_timeout)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("JSON 객체가 아님")
        return data

    def _failed(self, model: str | None, message: str) -> OllamaStatus:
        return OllamaStatus(base_url=self.base_url, reachable=False, configured_model=model, error=message)


def is_remote_name(model: str) -> bool:
    """Ollama 클라우드 모델 이름 관례(…-cloud, …:cloud)."""
    lowered = model.lower()
    return lowered.endswith("-cloud") or lowered.endswith(":cloud") or "-cloud:" in lowered


def _split_models(data: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(로컬 모델, 원격 모델). 원격: remote_host·remote_model 항목이 있거나 이름이 클라우드 관례인 것."""
    items = data.get("models")
    if not isinstance(items, list):
        raise ValueError("models 목록이 없음")
    local: list[str] = []
    remote: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("models 항목 형식 오류")
        name = item.get("name") or item.get("model")
        if not isinstance(name, str) or not name:
            raise ValueError("모델 이름 없음")
        if item.get("remote_host") or item.get("remote_model") or is_remote_name(name):
            remote.append(name)
        else:
            local.append(name)
    return local, remote


def _has_model(installed: list[str], wanted: str) -> bool:
    """`llama3`처럼 태그 없이 지정하면 `llama3:latest`와 같다고 본다."""
    candidates = {wanted} if ":" in wanted else {wanted, f"{wanted}:latest"}
    return any(name in candidates for name in installed)
