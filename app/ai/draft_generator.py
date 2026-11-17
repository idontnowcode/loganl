"""정상 학습 초안 (계획서 §3.1 8단계). 코드가 만든 특징·패턴 후보를 AI가 설명하고 빠진 정보를 짚는다.
초안의 관측은 PROPOSED 주장으로만 저장되며 사용자가 승인해야 확정된다."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.ai.context_builder import DATA_RULES, EvidencePackage
from app.ai.ollama_client import OllamaClient
from app.ai.response_validator import StructuredResult, ask_structured
from app.core.cancel import NEVER_CANCEL, CancelToken


class Observation(BaseModel):
    text: str
    kind: Literal["FACT", "HYPOTHESIS", "UNKNOWN"]
    evidence: list[str] = Field(default_factory=list)


TRIGGERS = Literal[
    "MODE_UNKNOWN", "NORMAL_RATIONALE_MISSING", "SESSION_RELATION_UNKNOWN", "TIMESTAMP_BASIS_UNKNOWN",
    "GUIDE_INFO_MISSING", "CAUSE_STATUS_UNKNOWN", "CRITERIA_DETAIL_UNKNOWN", "USER_LOG_CONFLICT",
    "CONFLICTING_KNOWLEDGE",
]


class MissingInfo(BaseModel):
    """AI가 짚은 빠진 정보. trigger는 계획서 §7.2의 9가지 질문 조건 중 하나."""

    text: str
    trigger: TRIGGERS = "NORMAL_RATIONALE_MISSING"

    @model_validator(mode="before")
    @classmethod
    def _from_text(cls, value):
        return {"text": value} if isinstance(value, str) else value


class NormalDraft(BaseModel):
    summary: str
    observations: list[Observation] = Field(default_factory=list)
    missing_info: list[MissingInfo] = Field(default_factory=list)


def generate_normal_draft(client: OllamaClient, model: str | None, package: EvidencePackage, context: str,
                          cancel: CancelToken = NEVER_CANCEL) -> StructuredResult[NormalDraft]:
    system = (
        "너는 정상 로그 학습 초안을 쓰는 보조자다. " + DATA_RULES +
        " 관측 사실(FACT)에는 반드시 근거 키를 붙여라. 추정은 HYPOTHESIS, 알 수 없으면 UNKNOWN으로 표시하라. "
        "패턴의 필수 여부를 확정하지 말고, 사용자에게 물어야 할 빠진 정보를 missing_info에 질문 문장(text)과 "
        "질문 조건(trigger)으로 적어라."
    )
    prompt = f"사례 정보:\n{context}\n\n근거 목록:\n{package.render()}"
    return ask_structured(client, model, system=system, prompt=prompt, schema=NormalDraft, package=package,
                          cancel=cancel)
