"""계층 3 — Ollama 진단 (계획서 §6.3 계층 3, §7.4, docs/AI_POLICY.md §3 V-1~V-9, CR-01).

AI는 계층 1·2가 만든 근거 묶음만 받는다. 응답은 다섯 부분이다:
관측 사실 / 원인 가설(+반대 근거) / 추가 확인 절차(AI 제안) / 대응 순서(승인된 매뉴얼·가이드 근거만) / 판단 불가 항목.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from app.ai.context_builder import DATA_RULES, KNOWLEDGE_KINDS, OBSERVATION_KINDS, EvidencePackage
from app.ai.ollama_client import OllamaClient
from app.ai.response_validator import StructuredResult, ask_structured
from app.core.cancel import NEVER_CANCEL, CancelToken

CODE_TOKEN = re.compile(r"(?<![A-Za-z0-9_])[A-Z]{1,6}[-_]?[A-Z]{0,3}\d{2,}(?![A-Za-z0-9_])")
DEFINITIVE = re.compile(r"확실|틀림없|명백|확정적|단정|100\s*%|원인(이다|입니다|임)(?![가-힣])")
ORDER_WORDS = re.compile(r"먼저|이후|다음에|전에|후에|뒤에|앞서|뒤이어|선행")
RESPONSE_KINDS = frozenset({"RESPONSE", "GUIDE"})


class Observation(BaseModel):
    text: str
    kind: Literal["FACT"] = "FACT"
    evidence: list[str] = Field(default_factory=list)


class Hypothesis(BaseModel):
    text: str
    kind: Literal["HYPOTHESIS"] = "HYPOTHESIS"
    evidence: list[str] = Field(default_factory=list)
    counter_evidence: list[str] = Field(default_factory=list)


class VerificationStep(BaseModel):
    text: str
    evidence: list[str] = Field(default_factory=list)


class ResponseStep(BaseModel):
    text: str
    evidence: list[str] = Field(default_factory=list)


class DiagnosisAnswer(BaseModel):
    observations: list[Observation] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    verification_steps: list[VerificationStep] = Field(default_factory=list)
    response_plan: list[ResponseStep] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)


@dataclass
class DiagnosisContext:
    package: EvidencePackage
    summary: str
    merged_files: set[int] = field(default_factory=set)  # 시각으로 한 시간축에 합쳐진 파일들
    confirmed_case_keys: set[str] = field(default_factory=set)  # 원인이 확정된 승인 사례를 가리키는 키


def rules(ctx: DiagnosisContext) -> list:
    """V-4~V-9 검사 함수 목록 (V-1~V-3은 ask_structured가 한다)."""
    package = ctx.package
    blob = package.text_blob()

    def v4_observations(answer: DiagnosisAnswer) -> list[str]:
        errors = []
        for obs in answer.observations:
            if not package.kinds_of(obs.evidence) & OBSERVATION_KINDS:
                errors.append(f"V-4: 관측 사실이 로그 근거(결정론 결과)를 가리키지 않음: {obs.text[:50]}")
            for token in CODE_TOKEN.findall(obs.text):
                if token not in blob:
                    errors.append(f"V-4: 근거에 없는 코드·이벤트를 관측으로 서술: {token}")
        return errors

    def v5_code_meaning(answer: DiagnosisAnswer) -> list[str]:
        errors = []
        for hyp in answer.hypotheses:
            if CODE_TOKEN.search(hyp.text) and not package.kinds_of(hyp.evidence) & KNOWLEDGE_KINDS:
                errors.append(f"V-5: 오류 코드를 해석하는 가설에 승인된 가이드 근거가 없음: {hyp.text[:50]}")
        return errors

    def v6_certainty(answer: DiagnosisAnswer) -> list[str]:
        errors = []
        for hyp in answer.hypotheses:
            if DEFINITIVE.search(hyp.text) and not (set(hyp.evidence) & ctx.confirmed_case_keys):
                errors.append(f"V-6: 원인이 확정되지 않았는데 확정 표현을 씀: {hyp.text[:50]}")
        return errors

    def v7_order(answer: DiagnosisAnswer) -> list[str]:
        errors = []
        for obs in answer.observations + answer.hypotheses:  # type: ignore[operator]
            files = {package.get(k).ref.get("log_file_id") for k in obs.evidence if k in package} - {None}
            if len(files) >= 2 and not files <= ctx.merged_files and ORDER_WORDS.search(obs.text):
                errors.append(f"V-7: 시간축을 합칠 수 없는 파일 사이의 순서를 단정함: {obs.text[:50]}")
        return errors

    def v8_unsupported(answer: DiagnosisAnswer) -> list[str]:
        return [f"V-8: 근거 없는 가설은 unknowns에 적어야 함: {h.text[:50]}" for h in answer.hypotheses if not h.evidence]

    def v9_response(answer: DiagnosisAnswer) -> list[str]:
        errors = []
        for step in answer.response_plan:
            kinds = package.kinds_of(step.evidence)
            if not kinds or not kinds <= RESPONSE_KINDS:
                errors.append(f"V-9: 대응 항목은 승인된 매뉴얼·가이드 근거만 가리켜야 함: {step.text[:50]}")
        return errors

    return [v4_observations, v5_code_meaning, v6_certainty, v7_order, v8_unsupported, v9_response]


def key_guide(package: EvidencePackage) -> str:
    """칸마다 쓸 수 있는 근거 키를 미리 알려 준다. 검증 규칙(V-4·V-5·V-9)은 그대로이고, 작은 모델이
    대응 문서의 문장을 관측 사실로 옮겨 적는 실수를 줄이려는 것이다."""

    def listing(kinds: frozenset[str]) -> str:
        keys = package.keys_of(kinds)
        return ", ".join(keys) if keys else "(없음)"

    return ("칸별로 쓸 수 있는 근거 키:\n"
            f"- observations(로그·결정론 결과에서 본 사실만): {listing(OBSERVATION_KINDS)}\n"
            f"- response_plan(승인된 대응·가이드): {listing(RESPONSE_KINDS)}\n"
            f"- 오류 코드의 뜻을 말하는 가설에 함께 넣을 승인 지식: {listing(KNOWLEDGE_KINDS)}\n"
            "대응 문서·가이드의 문장은 observations에 적지 말고 response_plan이나 hypotheses의 근거로만 쓴다.")


def diagnose(client: OllamaClient, model: str | None, ctx: DiagnosisContext,
             cancel: CancelToken = NEVER_CANCEL) -> StructuredResult[DiagnosisAnswer]:
    system = (
        "너는 장비 로그 분석 결과를 정리하는 보조자다. " + DATA_RULES + " 규칙:\n"
        "1) observations에는 근거 목록의 결정론 결과(FINDING)·로그(EVENT)가 보여 주는 사실만, 근거 키와 함께 적는다.\n"
        "2) hypotheses는 원인 가설이다. 근거 키와, 있으면 반대 근거(counter_evidence)를 함께 적는다. "
        "원인이 확정된 사례가 아니면 '~일 수 있다'처럼 쓴다. 오류 코드의 뜻은 승인된 가이드(GUIDE) 근거가 있을 때만 말한다.\n"
        "3) verification_steps는 추가로 확인할 절차 제안이다.\n"
        "4) response_plan은 RESPONSE·GUIDE 근거에 있는 대응만 골라 순서대로 적는다. 새 조치를 만들지 마라. 근거가 없으면 비워라.\n"
        "5) 근거가 부족한 것은 unknowns에 적는다. 시간축을 합칠 수 없는 파일 사이의 순서는 단정하지 마라. "
        "근거 있는 가설이 없으면 hypotheses는 빈 목록 []으로 두고, 'UNKNOWN' 같은 자리표시 항목을 넣지 마라.\n"
        "6) 모든 observations·hypotheses·response_plan 항목의 evidence에는 근거 목록의 키(예: EV-0003)를 하나 이상 넣는다. "
        "가설에서 오류 코드를 언급하려면 그 코드를 다루는 GUIDE 키를 함께 넣고, 그런 키가 없으면 그 내용은 unknowns에 적는다.\n"
        '형식 예시(키와 문장은 예시일 뿐): {"observations":[{"text":"…","evidence":["EV-0001"]}],'
        '"hypotheses":[{"text":"…일 수 있다","evidence":["EV-0004","EV-0006"],"counter_evidence":[]}],'
        '"verification_steps":[{"text":"…","evidence":[]}],"response_plan":[],"unknowns":["…"]}'
    )
    prompt = f"분석 요약:\n{ctx.summary}\n\n{key_guide(ctx.package)}\n\n근거 목록:\n{ctx.package.render(14_000)}"
    return ask_structured(client, model, system=system, prompt=prompt, schema=DiagnosisAnswer, package=ctx.package,
                          extra_checks=rules(ctx), cancel=cancel)
