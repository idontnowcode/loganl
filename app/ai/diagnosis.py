"""계층 3 — Ollama 진단 (계획서 §6.3 계층 3, §7.4, docs/AI_POLICY.md §3 V-1~V-9, CR-01).

AI는 계층 1·2가 만든 근거 묶음만 받는다. 응답은 다섯 부분이다:
관측 사실 / 원인 가설(+반대 근거) / 추가 확인 절차(AI 제안) / 대응 순서(승인된 매뉴얼·가이드 근거만) / 판단 불가 항목.
CR-07: 비정상일 가능성과 가설별 가능성(0~100%)을 근거와 함께 받는다(V-11). 통계 확률이 아니라 AI 추정치다.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from app.ai.context_builder import DATA_RULES, KNOWLEDGE_KINDS, OBSERVATION_KINDS, EvidencePackage
from app.ai.ollama_client import OllamaClient
from app.ai.response_validator import StructuredResult, ask_structured
from app.core.cancel import NEVER_CANCEL, CancelToken

CODE_TOKEN = re.compile(r"(?<![A-Za-z0-9_])[A-Z]{1,6}[-_]?[A-Z]{0,3}\d{2,}(?![A-Za-z0-9_])")
EVIDENCE_KEY = re.compile(r"^(?:EV|QA|AI|U|S)-\d{4}$")  # 근거 키 모양. 코드가 아니다


def code_tokens(text: str) -> list[str]:
    """글 안의 오류 코드 모양 낱말. 모델이 문장에 적은 근거 키(EV-0001 등)는 코드가 아니므로 뺀다
    (실제 gemma3:4b가 문장 안에 근거 키를 적어 V-4가 잘못 막은 일이 있었다, CR-07 점검)."""
    return [t for t in CODE_TOKEN.findall(text) if not EVIDENCE_KEY.match(t)]
DEFINITIVE = re.compile(r"확실|틀림없|명백|확정적|단정|100\s*%|원인(이다|입니다|임)(?![가-힣])")
ORDER_WORDS = re.compile(r"먼저|이후|다음에|전에|후에|뒤에|앞서|뒤이어|선행")
RESPONSE_KINDS = frozenset({"RESPONSE", "GUIDE"})
# 근거 목록 글자 수. 중요한 근거부터 넣으므로(engine.ai_layer) 넘치면 덜 중요한 '본 적 없는 이벤트'가 빠진다.
# 사내 PC(CPU)에서 프롬프트 읽는 시간을 줄이려고 14,000자에서 줄였다(2026-11-19).
EVIDENCE_BUDGET = 10_000


class Observation(BaseModel):
    text: str
    kind: Literal["FACT"] = "FACT"
    evidence: list[str] = Field(default_factory=list)


class Hypothesis(BaseModel):
    text: str
    kind: Literal["HYPOTHESIS"] = "HYPOTHESIS"
    evidence: list[str] = Field(default_factory=list)
    counter_evidence: list[str] = Field(default_factory=list)
    likelihood: int | None = Field(default=None, description="이 가설의 가능성 0~100 (AI 추정치)")


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
    abnormal_likelihood: int | None = Field(default=None, description="이 로그가 비정상일 가능성 0~100 (AI 추정치)")
    likelihood_reason: str = Field(default="", description="가능성을 그렇게 본 이유 한두 문장")
    likelihood_evidence: list[str] = Field(default_factory=list)


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
            for token in code_tokens(obs.text):
                if token not in blob:
                    errors.append(f"V-4: 근거에 없는 코드·이벤트를 관측으로 서술: {token}")
        return errors

    def v5_code_meaning(answer: DiagnosisAnswer) -> list[str]:
        errors = []
        for hyp in answer.hypotheses:
            if code_tokens(hyp.text) and not package.kinds_of(hyp.evidence) & KNOWLEDGE_KINDS:
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
            if "RESPONSE" not in kinds or not kinds <= RESPONSE_KINDS:
                errors.append(f"V-9: 대응 항목은 승인된 조치(RESPONSE) 근거를 가리켜야 함: {step.text[:50]}")
        return errors

    def v11_likelihood(answer: DiagnosisAnswer) -> list[str]:
        errors = []
        values = [h.likelihood for h in answer.hypotheses if h.likelihood is not None]
        for value in values + ([answer.abnormal_likelihood] if answer.abnormal_likelihood is not None else []):
            if not 0 <= value <= 100:
                errors.append(f"V-11: 가능성은 0~100이어야 함: {value}")
        if sum(values) > 100:
            errors.append(f"V-11: 가설 가능성의 합이 100을 넘음({sum(values)}). 나머지는 판단 불가로 남겨라")
        if answer.abnormal_likelihood is not None:
            if not answer.likelihood_evidence:
                errors.append("V-11: 비정상일 가능성에는 근거 키(likelihood_evidence)가 하나 이상 있어야 함")
            unknown = [k for k in answer.likelihood_evidence if k not in package]
            if unknown:
                errors.append(f"V-3: 없는 근거 키 {sorted(unknown)}")
        return errors

    def v1_empty(answer: DiagnosisAnswer) -> list[str]:
        # 입력이 잘린 모델이 '{}'만 돌려주면 모든 칸이 기본값이라 검사를 다 통과해 버린다(2026-11-19 재현)
        filled = (answer.observations or answer.hypotheses or answer.verification_steps or answer.response_plan
                  or answer.unknowns or answer.abnormal_likelihood is not None)
        return [] if filled else ["V-1: 빈 응답 — 관측 사실·가설·판단 불가 중 하나는 적어야 함"]

    return [v1_empty, v4_observations, v5_code_meaning, v6_certainty, v7_order, v8_unsupported, v9_response,
            v11_likelihood]


PLACEHOLDER = re.compile(r"^\s*(UNKNOWN|N/?A|없음|모름|판단\s*불가)", re.IGNORECASE)


def repair(ctx: DiagnosisContext) -> Callable[[DiagnosisAnswer], None]:
    """검사 전에 규칙에 어긋난 항목만 버리거나 판단 불가로 옮긴다. 새 내용·근거를 만들지 않는다.
    실제 gemma3:4b가 근거가 적은 로그에서 'UNKNOWN일 수 있다' 가설·근거 없는 가능성·근거 없는 조치 문장 하나 때문에
    응답 전체가 두 번 거부돼 AI 의견이 아예 안 나온 일이 있었다(2026-11-19)."""
    package = ctx.package
    blob = package.text_blob()

    def fix(answer: DiagnosisAnswer) -> None:
        # V-4: 로그 근거를 가리키지 않거나 근거에 없는 코드를 적은 관측 사실은 버린다
        answer.observations = [obs for obs in answer.observations
                               if package.kinds_of(obs.evidence) & OBSERVATION_KINDS
                               and all(token in blob for token in code_tokens(obs.text))]
        kept = []
        for hyp in answer.hypotheses:  # V-8: 근거 없는 가설은 판단 불가로. 'UNKNOWN일 수 있다' 같은 자리표시는 버린다
            if PLACEHOLDER.match(hyp.text):
                continue
            if code_tokens(hyp.text) and not package.kinds_of(hyp.evidence) & KNOWLEDGE_KINDS:
                continue  # V-5: 승인 지식 없이 코드를 말하는 가설은 버린다(판단 불가로 옮기면 지어낸 뜻이 화면에 남음)
            if hyp.evidence:
                kept.append(hyp)
            else:
                answer.unknowns.append(hyp.text)
        answer.hypotheses = kept
        # V-9: 승인된 조치(RESPONSE)를 가리키지 않는 조치 문장은 버리고, 남긴 항목은 가리킨 조치의 원문 그대로 보인다.
        # 원인·확인 방법 같은 가이드 키를 붙이고 문장은 지어내던 일(리뷰 2026-11-22)을 막는다 — 등록된 조치 문구만 화면에.
        plan = []
        for step in answer.response_plan:
            responses = [k for k in step.evidence if k in package and package.get(k).kind == "RESPONSE"]
            if not responses:
                continue
            original = package.get(responses[0]).ref.get("content")
            if original:
                step.text = original
            step.evidence = [k for k in step.evidence if k in package and package.get(k).kind in RESPONSE_KINDS]
            plan.append(step)
        answer.response_plan = plan
        if answer.abnormal_likelihood is not None and not answer.likelihood_evidence:  # V-11: 근거 없는 가능성은 쓰지 않음
            if answer.likelihood_reason.strip():
                answer.unknowns.append(answer.likelihood_reason.strip())
            answer.abnormal_likelihood, answer.likelihood_reason = None, ""

    return fix


def key_guide(package: EvidencePackage, shown: set[str] | None = None) -> str:
    """칸마다 쓸 수 있는 근거 키를 미리 알려 준다. 검증 규칙(V-4·V-5·V-9)은 그대로이고, 작은 모델이
    대응 문서의 문장을 관측 사실로 옮겨 적는 실수를 줄이려는 것이다. shown이 있으면 프롬프트에 실제로 넣은 키만."""

    def listing(kinds: frozenset[str]) -> str:
        keys = [k for k in package.keys_of(kinds) if shown is None or k in shown]
        return ", ".join(keys) if keys else "(없음)"

    return ("칸별로 쓸 수 있는 근거 키(키는 'EV-0001'처럼 앞글자까지 그대로 쓴다):\n"
            f"- observations(로그·결정론 결과에서 본 사실만): {listing(OBSERVATION_KINDS)}\n"
            f"- response_plan(승인된 대응·가이드): {listing(RESPONSE_KINDS)}\n"
            f"- 오류 코드의 뜻을 말하는 가설에 함께 넣을 승인 지식: {listing(KNOWLEDGE_KINDS)}\n"
            "- likelihood_evidence: 위 근거 목록의 키 아무것이나(비슷한 사례 CASE 키도 된다)\n"
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
        "가설에서 오류 코드를 언급하려면 그 코드를 다루는 GUIDE·REFERENCE·ANSWER 키를 함께 넣고, 그런 키가 없으면 그 내용은 unknowns에 적는다.\n"
        "7) abnormal_likelihood에는 이 로그가 비정상일 가능성을 0~100 정수로, likelihood_reason에 이유를, likelihood_evidence에 근거 키를 적는다. "
        "비슷한 승인 로그의 사용자 판정·메모·판단 가이드(CASE)와 사용자 답(ANSWER)을 참고한다. "
        "그중 '근거 지문이 비슷한 확인 로그'와 '근거별 실적'(CASE)을 가장 중요하게 본다: 확인된 비정상 로그와 같은 근거면 "
        "그 판단 가이드를 가설의 근거로 쓰고, 확인된 정상 로그에도 있던 근거면 비정상 가능성을 낮춘다(새 뜻을 지어내지 마라). "
        "비교할 비슷한 정상 로그가 없어(normal_match=NO_MATCH) 판단할 수 없으면 abnormal_likelihood는 null로 두고 "
        "그 이유를 unknowns에 적는다. 비교하지 못한 것은 비정상의 근거가 아니다. "
        "가설마다 likelihood(0~100)를 적되 합이 100을 넘지 않게 하고, 모르는 만큼은 남겨 둔다(판단 불가). 근거가 약하면 낮게 매긴다.\n"
        '형식 예시(키와 문장은 예시일 뿐): {"observations":[{"text":"…","evidence":["EV-0001"]}],'
        '"hypotheses":[{"text":"…일 수 있다","evidence":["EV-0004","EV-0006"],"counter_evidence":[]}],'
        '"verification_steps":[{"text":"…","evidence":[]}],"response_plan":[],"unknowns":["…"],'
        '"abnormal_likelihood":60,"likelihood_reason":"…","likelihood_evidence":["EV-0004"]}'
    )
    shown = set(ctx.package.shown_keys(EVIDENCE_BUDGET))
    prompt = (f"분석 요약:\n{ctx.summary}\n\n{key_guide(ctx.package, shown)}\n\n"
              f"근거 목록:\n{ctx.package.render(EVIDENCE_BUDGET)}")
    return ask_structured(client, model, system=system, prompt=prompt, schema=DiagnosisAnswer, package=ctx.package,
                          extra_checks=rules(ctx), cancel=cancel, repair=repair(ctx))
