"""LLM 로그 질문 (변경 요청 CR-07 QA-01, AI_POLICY V-12).

사용자가 저장한 로그 하나를 보고 LLM이 질문 1~3개를 만든다.
- GUESS: 근거를 보고 한 추측이 맞는지 묻는다. 근거 키가 있어야 한다.
- ASK: 모르는 것을 묻는다.
LLM은 사용자의 정상·비정상 판정을 바꾸지 않는다. 오류 코드의 뜻을 추측하려면 승인된 지식 근거가 있어야 하고,
없으면 ASK로 묻게 한다(V-5와 같은 뜻). 답이 지식이 되는 것은 사용자가 답한 뒤다.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, Field

from app.ai.context_builder import DATA_RULES, KNOWLEDGE_KINDS, OBSERVATION_KINDS, EvidencePackage
from app.ai.diagnosis import code_tokens
from app.ai.ollama_client import OllamaClient
from app.ai.response_validator import StructuredResult, ask_structured
from app.core.cancel import NEVER_CANCEL, CancelToken

MAX_QUESTIONS = 3
KINDS = ("GUESS", "ASK")


class AskedQuestion(BaseModel):
    kind: str = Field(description="GUESS(추측이 맞는지 확인) 또는 ASK(모르는 것 묻기)")
    text: str = Field(description="사용자에게 할 질문 한 문장(한국어)")
    reason: str = Field(default="", description="왜 묻는지 짧게")
    evidence: list[str] = Field(default_factory=list)


class AskedQuestions(BaseModel):
    questions: list[AskedQuestion] = Field(default_factory=list)


def question_rules(package: EvidencePackage) -> list:
    def v12(answer: AskedQuestions) -> list[str]:
        errors = []
        for q in answer.questions:
            kind = q.kind.strip().upper()
            if kind not in KINDS:
                errors.append(f"V-12: 질문 종류는 GUESS 또는 ASK만: {q.kind}")
            if not q.text.strip():
                errors.append("V-12: 빈 질문")
            if kind == "GUESS" and not q.evidence:
                errors.append(f"V-12: 추측(GUESS)에는 근거 키가 있어야 함: {q.text[:50]}")
            if kind == "GUESS" and code_tokens(q.text) and not package.kinds_of(q.evidence) & KNOWLEDGE_KINDS \
                    and ("뜻" in q.text or "의미" in q.text):
                errors.append(f"V-12: 오류 코드 뜻을 추측하려면 승인 지식 근거가 필요함. ASK로 물어라: {q.text[:50]}")
        return errors

    return [v12]


def write_questions(client: OllamaClient, model: str | None, package: EvidencePackage, summary: str, label: str,
                    asked_before: Sequence[str], cancel: CancelToken = NEVER_CANCEL) -> StructuredResult[AskedQuestions]:
    kind_text = "정상" if label == "NORMAL" else "비정상"
    observation_keys = ", ".join(package.keys_of(OBSERVATION_KINDS)) or "(없음)"
    knowledge_keys = ", ".join(package.keys_of(KNOWLEDGE_KINDS)) or "(없음)"
    system = (
        "너는 장비 로그를 배우는 보조자다. " + DATA_RULES +
        f" 사용자가 이 로그를 '{kind_text}'으로 표시했다. 이 판정은 바꾸지 않는다. "
        "앞으로 비슷한 로그를 더 잘 판단할 수 있도록 사용자에게 질문 1~3개를 만든다.\n"
        "- GUESS: 근거 목록을 보고 네가 추측한 것이 맞는지 묻는다. 예) '…로 보이는데 맞나요?' evidence에 근거 키를 넣는다.\n"
        "- ASK: 모르는 것을 묻는다. 예) 이벤트가 어떤 동작 단계인지, 어느 정도가 정상 범위인지.\n"
        "- 질문 중 적어도 하나는 GUESS로 한다. 로그에서 본 것을 바탕으로 이 로그가 왜 정상(또는 비정상)인지, "
        "어떤 동작 단계인지 등 네 추측을 말하고 맞는지 묻는다. 로그만 보면 바로 알 수 있는 것은 묻지 마라.\n"
        "- 사용자 메모·판단 가이드에 이미 있는 내용과 '이미 물은 질문'은 다시 묻지 마라.\n"
        "- 오류 코드의 뜻을 추측하려면 승인 지식(REFERENCE·ANSWER·GUIDE·CLAIM) 근거가 있어야 한다. 없으면 ASK로 '무슨 뜻인가요?'처럼 묻는다.\n"
        "- 질문은 짧은 한국어 한 문장. reason에는 왜 묻는지 짧게 쓴다. 물을 것이 없으면 questions는 빈 목록 [].\n"
        '형식 예시(문장·키는 예시일 뿐): {"questions":[{"kind":"GUESS","text":"…로 보이는데 맞나요?",'
        '"reason":"…","evidence":["EV-0002"]},{"kind":"ASK","text":"…은 어떤 단계인가요?","reason":"…","evidence":[]}]}'
    )
    before = "\n".join(f"- {text}" for text in asked_before) or "(없음)"
    prompt = (f"로그 요약:\n{summary}\n\n이미 물은 질문:\n{before}\n\n"
              f"근거 키: 로그·결과 {observation_keys} / 승인 지식 {knowledge_keys}\n\n근거 목록:\n{package.render(12_000)}")
    result = ask_structured(client, model, system=system, prompt=prompt, schema=AskedQuestions, package=package,
                            extra_checks=question_rules(package), cancel=cancel)
    if result.value is not None:
        for q in result.value.questions:
            q.kind = q.kind.strip().upper()
            q.text = q.text.strip()
        result.value.questions = result.value.questions[:MAX_QUESTIONS]
    return result
