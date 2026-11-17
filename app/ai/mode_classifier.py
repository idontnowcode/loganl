"""운전 모드 추정 (계획서 §3.1 4~5단계). AI는 **사용자가 등록한 모드 중에서만** 고르거나 UNKNOWN을 낸다
(REQUIREMENTS Q1 기본값). 결정은 사용자가 확인한다."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from pydantic import BaseModel, Field

from app.ai.context_builder import DATA_RULES, EvidencePackage
from app.ai.ollama_client import OllamaClient
from app.ai.response_validator import ask_structured
from app.core.cancel import NEVER_CANCEL, CancelToken
from app.models.records import ModeRecord

UNKNOWN = "UNKNOWN"


class ModeAnswer(BaseModel):
    mode: str = Field(description="등록된 모드 이름 중 하나 또는 UNKNOWN")
    reason: str
    evidence: list[str] = Field(default_factory=list)


@dataclass
class ModeGuess:
    status: str  # OK / UNKNOWN / NO_MODES / NO_MODEL / AI_UNAVAILABLE / VALIDATION_FAILED
    mode_id: int | None = None
    mode_name: str | None = None
    reason: str = ""
    evidence_keys: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def message(self) -> str:
        if self.status == "OK":
            return f"AI 추정: {self.mode_name} — {self.reason}"
        return {
            "UNKNOWN": f"AI가 모드를 판단하지 못했습니다 — {self.reason}",
            "NO_MODES": "등록된 운전 모드가 없습니다. '관리 → 운전 모드 관리'에서 먼저 등록하세요.",
            "NO_MODEL": "설정에서 사용할 Ollama 모델을 고르지 않아 추정을 건너뛰었습니다.",
        }.get(self.status, "AI 추정 실패: " + "; ".join(self.errors))


def classify_mode(client: OllamaClient, model: str | None, modes: Sequence[ModeRecord], package: EvidencePackage,
                  summary: str, cancel: CancelToken = NEVER_CANCEL) -> ModeGuess:
    active = [m for m in modes if m.status == "ACTIVE"]
    if not active:
        return ModeGuess("NO_MODES")
    names = {m.name: m for m in active}
    listing = "\n".join(f"- {m.name}: {m.description or ''} {('(조건: ' + m.conditions + ')') if m.conditions else ''}"
                        for m in active)
    system = ("너는 로그를 보고 사용자가 등록한 운전 모드 중 하나를 고르는 보조자다. " + DATA_RULES +
              " 목록에 없는 모드 이름을 만들지 마라. 확신할 근거가 없으면 mode에 UNKNOWN을 써라.")
    prompt = f"등록된 운전 모드:\n{listing}\n\n로그 요약:\n{summary}\n\n근거 목록:\n{package.render()}"

    def mode_is_registered(answer: ModeAnswer) -> list[str]:
        if answer.mode != UNKNOWN and answer.mode not in names:
            return [f"등록되지 않은 모드 이름: {answer.mode}"]
        if answer.mode != UNKNOWN and not answer.evidence:
            return ["모드를 고른 근거 키가 없습니다"]
        return []

    result = ask_structured(client, model, system=system, prompt=prompt, schema=ModeAnswer, package=package,
                            extra_checks=[mode_is_registered], cancel=cancel)
    if result.status == "NO_MODEL":
        return ModeGuess("NO_MODEL", errors=result.errors)
    if result.value is None:
        return ModeGuess(result.status, errors=result.errors)
    answer = result.value
    if answer.mode == UNKNOWN:
        return ModeGuess("UNKNOWN", reason=answer.reason, evidence_keys=answer.evidence)
    chosen = names[answer.mode]
    return ModeGuess("OK", chosen.id, chosen.name, answer.reason, answer.evidence)
