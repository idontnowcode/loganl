"""LLM이 로그를 보고 묻고, 사용자 답을 지식으로 쌓기 (변경 요청 CR-07 QA-01~03, AO-02).

- generate(case_id): 저장한 로그(사례) 하나의 근거 묶음 + 비슷한 승인 로그의 메모·판단 가이드·사용자 답 +
  로그에 나온 코드의 참고 자료로 LLM에게 질문 1~3개를 받는다(app.ai.question_writer, V-12).
  다시 만들면 답하지 않은 이전 질문은 OBSOLETE로 바꾼다. 이미 물은 질문은 다시 만들지 않는다(dedup_key).
- answer(): 맞음(YES)·틀림(NO)·글(TEXT)은 사용자 지식이 된다. 모름(UNKNOWN)·건너뛰기는 지식이 아니다.
- knowledge_texts(case_ids): 분석 AI에 넘길 사용자 답(승인된 사례의 것만, confirmed_ai_answers 뷰).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass, field

from app.ai.context_builder import EvidencePackage
from app.ai.ollama_client import OllamaClient
from app.ai.question_writer import write_questions
from app.analysis.case_retriever import find_similar
from app.analysis.reference_matcher import match_references
from app.config import AppSettings
from app.core.cancel import NEVER_CANCEL, CancelToken
from app.learning.case_manager import CaseError, CaseManager
from app.learning.normal_learner import build_package, case_file_features
from app.models.enums import AuditAction
from app.storage.database import Database, transaction
from app.storage.repositories import AiQuestionRepository, AuditRepository, CaseRepository, LogFileRepository

LABELS = {"NORMAL": "정상", "ABNORMAL": "비정상"}
ANSWER_LABELS = {"YES": "맞음", "NO": "틀림", "UNKNOWN": "모름", "TEXT": "답"}
SIMILAR_LIMIT = 3
ClientFactory = Callable[[], OllamaClient]


@dataclass
class QuestionOutcome:
    status: str  # OK / NO_MODEL / AI_UNAVAILABLE / VALIDATION_FAILED / NO_LOG
    added: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def message(self, file_name: str) -> str:
        if self.status == "OK":
            return (f"AI가 {file_name}에 대해 질문 {len(self.added)}개를 남겼습니다(답은 선택)." if self.added
                    else f"AI가 {file_name}에 대해 더 물을 것이 없다고 했습니다.")
        return {"NO_MODEL": "설정에서 AI 모델을 고르지 않아 질문을 만들지 않았습니다.",
                "AI_UNAVAILABLE": "AI 서버에 연결되지 않아 질문을 만들지 않았습니다('AI와 함께 시작'으로 켜세요).",
                "VALIDATION_FAILED": f"AI 질문이 검사를 통과하지 못해 버렸습니다({file_name}).",
                "NO_LOG": "로그가 없는 사례라 질문을 만들지 않았습니다."}.get(self.status, self.status)


@dataclass
class QuestionView:
    id: int
    case_id: int
    file_name: str
    label: str | None
    kind: str
    text: str
    reason: str
    evidence: list[dict]
    status: str
    answer: str | None
    answer_text: str | None


def dedup_key(text: str) -> str:
    return hashlib.sha1(re.sub(r"\s+", " ", text.strip().lower()).encode("utf-8")).hexdigest()[:16]


def answer_knowledge(row) -> str:
    """사용자 답 하나를 AI에 줄 글로. 질문은 AI가 만든 것이고, 답이 사용자 지식이다."""
    answer = row["answer"]
    extra = f" — 사용자 설명: {row['answer_text']}" if row["answer_text"] else ""
    if row["kind"] == "GUESS":
        verdict = {"YES": "사용자가 맞다고 확인", "NO": "사용자가 틀렸다고 함"}.get(answer, "사용자 답")
        return f"[사용자 확인] AI 추측 '{row['question_text']}' → {verdict}{extra}"
    return f"[사용자 답] 질문 '{row['question_text']}' → {row['answer_text'] or ANSWER_LABELS.get(answer, answer)}"


class AiQuestionService:
    def __init__(self, database: Database, settings: AppSettings, client_factory: ClientFactory) -> None:
        self.database = database
        self.settings = settings
        self.client_factory = client_factory

    # ── 질문 만들기 ──
    def generate(self, case_id: int, cancel: CancelToken = NEVER_CANCEL) -> QuestionOutcome:
        case = CaseManager(self.database).get(case_id)
        features = case_file_features(self.database, case_id)
        if not features:
            return QuestionOutcome("NO_LOG")
        package, log_summary = build_package(features)
        with closing(self.database.connect()) as conn:
            log_ids = CaseRepository(conn).log_file_ids(case_id)
            asked = [r["question_text"] for r in AiQuestionRepository(conn).for_cases([case_id])
                     if r["status"] != "OBSOLETE"]
        for match in match_references(self.database, log_ids):
            for hit in match.entries[:2]:
                package.add("REFERENCE", f"[참고 자료] 코드 {match.code}: " + "; ".join(
                    f"{label} {value}" for label, value in (("명칭", hit.title), ("설명", hit.description),
                                                            ("원인", hit.cause), ("조치", hit.action)) if value))
        others = [s for kind in ("NORMAL", "ABNORMAL") for s in find_similar(self.database, features, kind, None)
                  if s.case.id != case_id and s.relevant][:SIMILAR_LIMIT]
        for s in others:
            package.add("CASE", case_knowledge(s.case) + f" (비슷한 정도 {s.similarity:.2f}, 확률 아님)", case_id=s.case.id)
        for text in self.knowledge_texts([s.case.id for s in others]):
            package.add("ANSWER", text)
        summary = (f"사용자 판정: {LABELS.get(case.case_type, case.case_type)}\n"
                   f"메모: {case.operating_conditions or '(없음)'}\n"
                   + (f"판단 가이드: {case.abnormal_criteria or '(없음)'}\n" if case.case_type == "ABNORMAL" else "")
                   + log_summary)
        with self.client_factory() as client:
            result = write_questions(client, self.settings.ollama.model, package, summary, case.case_type, asked, cancel)
        if result.value is None:
            return QuestionOutcome(result.status, errors=result.errors)
        added: list[int] = []
        with closing(self.database.connect()) as conn, transaction(conn):
            repo = AiQuestionRepository(conn)
            repo.obsolete_open(case_id)
            for q in result.value.questions:
                evidence = [self._evidence(package, key) for key in q.evidence if key in package]
                new_id = repo.add(case_id, kind=q.kind, question_text=q.text, reason=q.reason.strip() or None,
                                  evidence=evidence, model=self.settings.ollama.model, dedup_key=dedup_key(q.text))
                if new_id is not None:
                    added.append(new_id)
            AuditRepository(conn).add("case", case_id, AuditAction.UPDATE, after={"ai_questions": added})
        return QuestionOutcome("OK", added)

    @staticmethod
    def _evidence(package: EvidencePackage, key: str) -> dict:
        item = package.get(key)
        return {"key": key, "kind": item.kind, "line": item.ref.get("line_no"),
                "log_file_id": item.ref.get("log_file_id"), "text": item.text[:240]}

    # ── 보기·답하기 ──
    def questions(self, case_ids: Sequence[int] | None = None, open_only: bool = True) -> list[QuestionView]:
        with closing(self.database.connect()) as conn:
            rows = AiQuestionRepository(conn).for_cases(case_ids, ("OPEN",) if open_only else None)
            names, labels = {}, {}
            for case_id in {r["case_id"] for r in rows}:
                case = CaseRepository(conn).get(case_id)
                labels[case_id] = case.case_type if case else None
                ids = CaseRepository(conn).log_file_ids(case_id)
                files = LogFileRepository(conn).get_many(ids)
                names[case_id] = ", ".join(f.original_name for f in files) or f"사례 {case_id}"
        return [QuestionView(int(r["id"]), int(r["case_id"]), names[r["case_id"]], labels[r["case_id"]], r["kind"],
                             r["question_text"], r["reason"] or "", json.loads(r["evidence_json"] or "[]"), r["status"],
                             r["answer"], r["answer_text"]) for r in rows]

    def answer(self, question_id: int, answer: str, text: str | None = None) -> None:
        if answer not in ANSWER_LABELS:
            raise CaseError("맞음·틀림·모름·답 중에서 고르세요.")
        text = (text or "").strip() or None
        if answer == "TEXT" and not text:
            raise CaseError("답을 적어 주세요.")
        with closing(self.database.connect()) as conn, transaction(conn):
            repo = AiQuestionRepository(conn)
            row = repo.get(question_id)
            if row is None:
                raise CaseError(f"질문 {question_id}이(가) 없습니다.")
            repo.set_answer(question_id, status="ANSWERED", answer=answer, answer_text=text)
            AuditRepository(conn).add("case", row["case_id"], AuditAction.UPDATE,
                                      after={"ai_question": question_id, "answer": answer, "text": text})

    def skip(self, question_id: int) -> None:
        with closing(self.database.connect()) as conn, transaction(conn):
            repo = AiQuestionRepository(conn)
            if repo.get(question_id) is not None:
                repo.set_answer(question_id, status="SKIPPED", answer=None, answer_text=None)

    # ── 지식 ──
    def knowledge_texts(self, case_ids: Sequence[int]) -> list[str]:
        return knowledge_texts(self.database, case_ids)


def knowledge_texts(database: Database, case_ids: Sequence[int]) -> list[str]:
    """승인된 사례에 달린 사용자 답(맞음·틀림·글)을 AI에 줄 글로."""
    with closing(database.connect()) as conn:
        rows = AiQuestionRepository(conn).confirmed(list(case_ids))
    return [answer_knowledge(r) for r in rows]


def case_knowledge(case) -> str:
    """비슷한 승인 로그의 사용자 판정·메모·판단 가이드(AO-02)."""
    parts = [f"[비슷한 승인 로그] {case.label} · 사용자 판정 {LABELS.get(case.case_type, case.case_type)}"]
    if case.operating_conditions:
        parts.append(f"메모: {case.operating_conditions}")
    if case.case_type == "ABNORMAL" and case.abnormal_criteria:
        parts.append(f"판단 가이드: {case.abnormal_criteria}")
    if case.normal_rationale:
        parts.append(f"정상 근거: {case.normal_rationale}")
    return " / ".join(parts)
