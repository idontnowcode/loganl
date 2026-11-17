"""AI 출력 검증 (docs/AI_POLICY.md §3). 검증을 통과한 응답만 저장한다.

- V-1 JSON 파싱 + Pydantic 스키마
- V-2 FACT 항목은 근거 키가 하나 이상
- V-3 모든 근거 키가 이번 근거 묶음에 있음
- 추가 규칙(V-4~V-9 등)은 extra_checks로 넣는다.
실패하면 한 번 다시 묻고, 그래도 실패하면 VALIDATION_FAILED.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from app.ai.context_builder import EvidencePackage
from app.ai.ollama_client import OllamaClient, OllamaError
from app.core.cancel import NEVER_CANCEL, CancelToken
from app.core.evidence_validator import unknown_evidence_keys

T = TypeVar("T", bound=BaseModel)
EVIDENCE_FIELDS = ("evidence", "counter_evidence")
Check = Callable[[Any], list[str]]


@dataclass
class ValidationResult(Generic[T]):
    ok: bool
    value: T | None = None
    errors: list[str] = field(default_factory=list)


def iter_items(value: Any) -> Iterator[BaseModel]:
    """모델 안의 모든 하위 모델을 돈다."""
    if isinstance(value, BaseModel):
        yield value
        for name in type(value).model_fields:
            yield from iter_items(getattr(value, name))
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_items(item)


def evidence_keys_of(item: BaseModel) -> list[str]:
    keys: list[str] = []
    for name in EVIDENCE_FIELDS:
        found = getattr(item, name, None)
        if isinstance(found, list):
            keys.extend(str(k) for k in found)
    return keys


def validate_response(raw: str, schema: type[T], package: EvidencePackage | None = None,
                      extra_checks: Sequence[Check] = ()) -> ValidationResult[T]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ValidationResult(False, None, ["V-1: 응답이 JSON이 아닙니다."])
    try:
        value = schema.model_validate(data)
    except ValidationError as exc:
        return ValidationResult(False, None, [f"V-1: 형식 오류 {e['loc']}: {e['msg']}" for e in exc.errors()[:5]])

    errors: list[str] = []
    for item in iter_items(value):
        keys = evidence_keys_of(item)
        if getattr(item, "kind", None) == "FACT" and not getattr(item, "evidence", None):
            errors.append(f"V-2: 근거 없는 사실: {getattr(item, 'text', '')[:60]}")
        if package is not None:
            unknown = unknown_evidence_keys(keys, package.keys())
            if unknown:
                errors.append(f"V-3: 없는 근거 키 {sorted(unknown)}")
    for check in extra_checks:
        errors.extend(check(value))
    return ValidationResult(not errors, value if not errors else None, errors)


@dataclass
class StructuredResult(Generic[T]):
    status: str  # OK / AI_UNAVAILABLE / VALIDATION_FAILED / NO_MODEL
    value: T | None = None
    errors: list[str] = field(default_factory=list)
    raw: list[str] = field(default_factory=list)


def ask_structured(
    client: OllamaClient,
    model: str | None,
    *,
    system: str,
    prompt: str,
    schema: type[T],
    package: EvidencePackage | None,
    extra_checks: Sequence[Check] = (),
    cancel: CancelToken = NEVER_CANCEL,
    retries: int = 1,
) -> StructuredResult[T]:
    if not model:
        return StructuredResult("NO_MODEL", errors=["설정에서 사용할 Ollama 모델을 고르지 않았습니다."])
    json_schema = schema.model_json_schema()
    attempt_prompt = prompt
    raws: list[str] = []
    errors: list[str] = []
    for _ in range(retries + 1):
        try:
            raw = client.generate_json(model=model, system=system, prompt=attempt_prompt, schema=json_schema,
                                       cancel=cancel)
        except OllamaError as exc:
            return StructuredResult("AI_UNAVAILABLE", errors=[str(exc)], raw=raws)
        raws.append(raw)
        result = validate_response(raw, schema, package, extra_checks)
        if result.ok:
            return StructuredResult("OK", result.value, [], raws)
        errors = result.errors
        attempt_prompt = (prompt + "\n\n이전 응답이 다음 이유로 거부됐다. 규칙을 지켜 다시 답하라:\n- "
                          + "\n- ".join(errors[:8]))
    return StructuredResult("VALIDATION_FAILED", errors=errors, raw=raws)
