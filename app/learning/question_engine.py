"""확인 질문 엔진 (계획서 §7). 질문 조건(9가지)은 **코드가 판정**하고, AI가 학습 초안에서 짚은 빠진 정보는
비필수 질문으로 더한다(normal_learner).

- 중요도 순 최대 3개씩 보여 준다(next_questions).
- 같은 질문은 dedup_key로 다시 만들지 않는다.
- 답변은 구조화해 저장하고, 가능한 것은 사례 항목에 바로 반영한 뒤 다시 판정한다(재분석).
- '모름'·'건너뛰기'를 허용한다. 필수 항목은 답이 '모름'이어도 승인 검사(ApprovalWorkflow)가 계속 막는다.
- 조건이 사라진 질문은 OBSOLETE로 바꾼다.
"""

from __future__ import annotations

import re
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any

from app.core.feature_extractor import ERROR_LEVELS
from app.models.enums import Actor, AuditAction, CaseStatus
from app.storage.database import Database, transaction
from app.storage.repositories import (
    AuditRepository,
    CaseRepository,
    EventRepository,
    GuideRepository,
    LogFileRepository,
    ModeRepository,
    NoteRepository,
    QuestionRepository,
    SessionRepository,
)

MAX_SHOWN = 3
CODE_TOKEN = re.compile(r"(?<![A-Za-z0-9_])[A-Z]{1,6}[-_]?[A-Z]{0,3}\d{2,}(?![A-Za-z0-9_])")
TIMESTAMP_CHOICES = (("ABSOLUTE", "절대시각(시간대 있음)"), ("ABSOLUTE_NO_TZ", "절대시각(시간대 없음)"),
                     ("RELATIVE", "기준 없는 경과 시간"), ("NONE", "타임스탬프 없음"))
SESSION_CHOICES = (("SAME", "같은 시점에 함께 수집"), ("SEPARATE", "서로 다른 시점의 재현·반복 수집"))
CAUSE_CHOICES = (("CONFIRMED", "원인 확정"), ("SUSPECTED", "원인 추정"), ("UNKNOWN", "원인 모름"))
STATUS_FROM = {CaseStatus.DRAFT.value, CaseStatus.NEEDS_INFORMATION.value, CaseStatus.READY_FOR_REVIEW.value}


@dataclass
class QuestionCandidate:
    trigger: str
    text: str
    reason: str
    priority: int
    required: bool
    dedup_key: str
    target_ref: str | None = None


@dataclass
class OpenQuestion:
    id: int
    trigger: str
    text: str
    reason: str
    required: bool
    generated_by: str
    target_ref: str | None
    choices: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class AnswerOutcome:
    applied: str
    reanalyzed: list[str] = field(default_factory=list)


class QuestionError(ValueError):
    pass


class QuestionEngine:
    def __init__(self, database: Database) -> None:
        self.database = database

    # ── 판정 ──
    def detect(self, case_id: int) -> list[QuestionCandidate]:
        with closing(self.database.connect()) as conn:
            return self._detect(conn, case_id)

    def _detect(self, conn, case_id: int) -> list[QuestionCandidate]:
        case = CaseRepository(conn).get(case_id)
        if case is None:
            raise QuestionError(f"사례 {case_id}이(가) 없습니다.")
        normal = case.case_type == "NORMAL"
        log_ids = CaseRepository(conn).log_file_ids(case_id)
        files = LogFileRepository(conn).get_many(log_ids)
        events = EventRepository(conn)
        out: list[QuestionCandidate] = []

        if case.mode_id is None or not case.mode_confirmed:
            out.append(QuestionCandidate(
                "MODE_UNKNOWN", "이 로그를 수집할 때 장비의 운전 모드는 무엇이었나요?",
                "운전 모드가 있으면 같은 모드끼리 비교하고 패턴을 확정할 수 있습니다(선택, CR-06).",
                90, False, "mode"))
        if normal and not (case.normal_rationale or "").strip():
            out.append(QuestionCandidate(
                "NORMAL_RATIONALE_MISSING", "이 로그를 정상으로 판단한 근거는 무엇인가요?",
                "근거가 있으면 나중에 사례를 이해하기 쉽습니다(선택, CR-06).", 85, False, "normal_rationale"))
        if len(files) >= 2:
            unconfirmed = [f for f in files if not SessionRepository(conn).get(f.session_id).relation_confirmed]
            if unconfirmed:
                out.append(QuestionCandidate(
                    "SESSION_RELATION_UNKNOWN",
                    f"로그 {len(files)}개는 같은 시점에 함께 수집한 것인가요, 다른 시점에 재현한 것인가요?",
                    "같은 세션이 아니면 하나의 시간축으로 합치지 않습니다(§4.1). 가져올 때 '모름'을 골랐습니다.",
                    60, False, "session"))
        for f in files:
            if str(f.timestamp_basis) == "UNKNOWN":
                out.append(QuestionCandidate(
                    "TIMESTAMP_BASIS_UNKNOWN", f"'{f.original_name}'의 시각은 어떤 기준인가요?",
                    "형식을 읽지 못해 시각 기준을 모릅니다. 기준을 알아야 시간 순서를 비교할 수 있습니다(§4.3).",
                    50, False, f"timestamp:{f.id}", target_ref=f"log_file:{f.id}"))
        if not normal:
            guides = GuideRepository(conn)
            if not guides.attachments(case_id, "DIAGNOSTIC_GUIDE"):
                out.append(QuestionCandidate(
                    "GUIDE_INFO_MISSING", "이 비정상 사례의 진단 가이드(TXT/MD)를 첨부해 주세요.",
                    "가이드가 있으면 원인·조치를 함께 학습합니다(선택, CR-06). 답 대신 '진단 가이드 첨부'로 해결합니다.",
                    95, False, "guide:attach"))
            else:
                live = {row["section"] for row in guides.items(case_id)
                        if row["status"] != "REJECTED" and row["attachment_kind"] in ("DIAGNOSTIC_GUIDE", None)}
                for section, label in (("SYMPTOM", "오류 증상"), ("CRITERIA", "비정상 판정 기준")):
                    if section not in live:
                        out.append(QuestionCandidate(
                            "GUIDE_INFO_MISSING", f"진단 가이드에서 '{label}' 항목을 찾지 못했습니다. 내용을 적어 주세요.",
                            f"가이드 구조화 결과에 {label} 항목이 없습니다.", 55, False, f"guide:{section}",
                            target_ref=f"section:{section}"))
            if case.cause_status is None:
                out.append(QuestionCandidate(
                    "CAUSE_STATUS_UNKNOWN", "이 오류의 원인은 확정됐나요?",
                    "원인 확정 여부를 따로 관리합니다(§3.2). 미확정이어도 등록할 수 있습니다.", 80, False, "cause"))
            criteria = (case.abnormal_criteria or "").strip()
            if criteria and not re.search(r"\d", criteria):
                out.append(QuestionCandidate(
                    "CRITERIA_DETAIL_UNKNOWN", "판정 기준에 수치나 조건(횟수·시간·값)이 있나요?",
                    "판정 기준에 숫자가 없습니다. 수치가 있으면 비교가 정확해집니다.", 40, False, "criteria_detail"))
            codes = events.error_codes_for_files(log_ids)
            out.extend(self._conflicting_knowledge(conn, case_id, codes))
        else:
            error_count = events.error_level_count(log_ids, sorted(ERROR_LEVELS))
            codes = events.error_codes_for_files(log_ids)
            if error_count or codes:
                code_text = f", 오류 코드 {', '.join(sorted(codes)[:5])}" if codes else ""
                out.append(QuestionCandidate(
                    "USER_LOG_CONFLICT",
                    f"정상 사례인데 오류 레벨 이벤트 {error_count}개{code_text}가 있습니다. 정상으로 보는 이유는?",
                    "사용자 판정과 로그 내용이 어긋날 수 있습니다(§7.2).", 70, False, "conflict:normal_errors"))
        out.extend(self._unknown_codes_in_text(conn, case, log_ids))
        return out

    def _unknown_codes_in_text(self, conn, case, log_ids) -> list[QuestionCandidate]:
        texts = [case.symptom, case.abnormal_criteria, case.normal_rationale, case.user_description,
                 case.operating_conditions] + [n.body for n in NoteRepository(conn).for_case(case.id)]
        tokens = {t for text in texts if text for t in CODE_TOKEN.findall(text)}
        if not tokens:
            return []
        known = EventRepository(conn).error_codes_for_files(log_ids)
        raw = " ".join(e.raw_text for es in EventRepository(conn).for_files(log_ids).values() for e in es)
        missing = sorted(t for t in tokens if t not in known and t not in raw)
        if not missing:
            return []
        return [QuestionCandidate(
            "USER_LOG_CONFLICT", f"설명에 나온 {', '.join(missing)}이(가) 이 사례의 로그에 없습니다. 다른 로그의 것인가요?",
            "사용자 설명과 로그가 어긋납니다(§7.2).", 65, False, "conflict:codes:" + ",".join(missing))]

    def _conflicting_knowledge(self, conn, case_id: int, codes: set[str]) -> list[QuestionCandidate]:
        if not codes:
            return []
        guides = GuideRepository(conn)
        mine = {r["content"].strip() for r in guides.items(case_id) if r["section"] == "CAUSE" and r["status"] != "REJECTED"}
        out = []
        for other in CaseRepository(conn).approved(case_type="ABNORMAL"):
            if other.id == case_id:
                continue
            shared = codes & EventRepository(conn).error_codes_for_files(CaseRepository(conn).log_file_ids(other.id))
            if not shared:
                continue
            theirs = {r["content"].strip() for r in guides.approved_items_for_cases([other.id]) if r["section"] == "CAUSE"}
            if theirs and mine and theirs != mine:
                code_text = ", ".join(sorted(shared))
                out.append(QuestionCandidate(
                    "CONFLICTING_KNOWLEDGE",
                    f"같은 오류 코드 {code_text}에 대해 승인된 사례 {other.label}의 원인 항목이 이 사례와 다릅니다. "
                    "같은 문제인가요, 다른 문제인가요?",
                    "같은 오류 코드에 상충하는 진단 지식이 있습니다(§7.2). 오류 코드와 해결책을 일대일로 고정하지 않습니다.",
                    45, False, f"conflict:knowledge:{other.id}"))
        return out

    # ── 저장·보여 주기 ──
    def refresh(self, case_id: int) -> list[OpenQuestion]:
        """조건을 다시 판정해 질문을 만들고, 사라진 조건의 질문은 OBSOLETE로, 사례 상태를 맞춘다."""
        with closing(self.database.connect()) as conn, transaction(conn):
            candidates = self._detect(conn, case_id)
            questions = QuestionRepository(conn)
            current = set()
            for c in candidates:
                questions.upsert(case_id, trigger_type=c.trigger, question_text=c.text, reason=c.reason,
                                 priority=c.priority, is_required=c.required, dedup_key=c.dedup_key,
                                 generated_by="RULE", target_ref=c.target_ref)
                current.add(c.dedup_key)
            for row in questions.for_case(case_id, ["OPEN"]):
                if row["generated_by"] == "RULE" and row["dedup_key"] not in current:
                    questions.set_status(row["id"], "OBSOLETE")
            self._sync_status(conn, case_id)
        return self.open_questions(case_id)

    def _sync_status(self, conn, case_id: int) -> None:
        cases = CaseRepository(conn)
        case = cases.get(case_id)
        if case.status not in STATUS_FROM:
            return
        open_required = QuestionRepository(conn).open_required_count(case_id)
        if open_required and case.status != CaseStatus.NEEDS_INFORMATION:
            new = CaseStatus.NEEDS_INFORMATION.value
        elif not open_required and case.status == CaseStatus.NEEDS_INFORMATION:
            new = CaseStatus.DRAFT.value
        else:
            return
        cases.update(case_id, status=new)
        AuditRepository(conn).add("case", case_id, AuditAction.STATUS_CHANGE, Actor.SYSTEM,
                                  before={"status": case.status}, after={"status": new}, note="확인 질문 판정")

    def open_questions(self, case_id: int) -> list[OpenQuestion]:
        with closing(self.database.connect()) as conn:
            rows = QuestionRepository(conn).for_case(case_id, ["OPEN"])
            modes = ModeRepository(conn).list(active_only=True)
        return [OpenQuestion(r["id"], r["trigger_type"], r["question_text"], r["reason"], bool(r["is_required"]),
                             r["generated_by"], r["target_ref"], self._choices(r["trigger_type"], r["dedup_key"], modes))
                for r in rows]

    def next_questions(self, case_id: int, limit: int = MAX_SHOWN) -> list[OpenQuestion]:
        return self.open_questions(case_id)[:limit]

    @staticmethod
    def _choices(trigger: str, dedup_key: str, modes) -> list[tuple[str, str]]:
        if dedup_key.startswith("ai:"):
            return []
        if trigger == "MODE_UNKNOWN":
            return [(str(m.id), m.name) for m in modes]
        if trigger == "SESSION_RELATION_UNKNOWN":
            return list(SESSION_CHOICES)
        if trigger == "TIMESTAMP_BASIS_UNKNOWN":
            return list(TIMESTAMP_CHOICES)
        if trigger == "CAUSE_STATUS_UNKNOWN":
            return list(CAUSE_CHOICES)
        return []

    # ── 답변 ──
    def answer(self, question_id: int, kind: str, text: str | None = None, value: str | None = None) -> AnswerOutcome:
        """kind: TEXT / CHOICE / UNKNOWN(모름) / SKIPPED(건너뛰기)."""
        if kind not in ("TEXT", "CHOICE", "UNKNOWN", "SKIPPED"):
            raise QuestionError(f"답변 종류가 올바르지 않습니다: {kind}")
        if kind == "TEXT" and not (text or "").strip():
            raise QuestionError("답변 내용을 입력하세요.")
        if kind == "CHOICE" and not value:
            raise QuestionError("보기를 고르세요.")
        with closing(self.database.connect()) as conn, transaction(conn):
            questions = QuestionRepository(conn)
            row = questions.get(question_id)
            if row is None or row["status"] != "OPEN":
                raise QuestionError("이미 처리했거나 없는 질문입니다.")
            case_id = row["case_id"]
            case = CaseRepository(conn).get(case_id)
            if case.status in ("APPROVED", "ARCHIVED"):
                raise QuestionError("승인·보관된 사례는 '수정 시작' 후 답할 수 있습니다.")
            structured = {"value": value} if value is not None else None
            questions.add_answer(question_id, kind, text, structured)
            status = {"TEXT": "ANSWERED", "CHOICE": "ANSWERED", "UNKNOWN": "UNKNOWN", "SKIPPED": "SKIPPED"}[kind]
            questions.set_status(question_id, status)
            applied = self._apply(conn, row, case, kind, text, value) if kind in ("TEXT", "CHOICE") else \
                ("모름으로 기록했습니다." if kind == "UNKNOWN" else "건너뛰었습니다.")
            AuditRepository(conn).add("learning_question", question_id, AuditAction.UPDATE, Actor.USER,
                                      after={"answer_kind": kind, "value": value, "text": text})
        reanalyzed = self._reanalyze(case_id, row["trigger_type"], kind)
        self.refresh(case_id)
        return AnswerOutcome(applied, reanalyzed)

    def _apply(self, conn, row, case, kind: str, text: str | None, value: str | None) -> str:
        trigger = row["trigger_type"]
        cases = CaseRepository(conn)
        notes = NoteRepository(conn)
        if row["generated_by"] == "AI":
            notes.add(case.id, f"[AI 질문 답변] {row['question_text']} → {text or value}")
            return "답변을 메모로 남겼습니다."
        if trigger == "MODE_UNKNOWN" and kind == "CHOICE":
            cases.update(case.id, mode_id=int(value), mode_confirmed=1)
            return "운전 모드를 정하고 확인으로 표시했습니다."
        if trigger == "NORMAL_RATIONALE_MISSING" and kind == "TEXT":
            cases.update(case.id, normal_rationale=text.strip())
            return "정상 판정 근거에 반영했습니다."
        if trigger == "CAUSE_STATUS_UNKNOWN" and kind == "CHOICE":
            cases.update(case.id, cause_status=value)
            return "원인 확정 여부를 반영했습니다."
        if trigger == "CRITERIA_DETAIL_UNKNOWN" and kind == "TEXT":
            cases.update(case.id, abnormal_criteria=f"{case.abnormal_criteria or ''}\n{text.strip()}".strip())
            return "판정 기준에 덧붙였습니다."
        if trigger == "TIMESTAMP_BASIS_UNKNOWN" and kind == "CHOICE" and row["target_ref"]:
            LogFileRepository(conn).update_metadata(int(row["target_ref"].split(":")[1]), timestamp_basis=value)
            return "파일의 타임스탬프 기준을 반영했습니다."
        if trigger == "SESSION_RELATION_UNKNOWN" and kind == "CHOICE":
            return self._apply_session(conn, case.id, value)
        if trigger == "GUIDE_INFO_MISSING" and kind == "TEXT" and (row["target_ref"] or "").startswith("section:"):
            GuideRepository(conn).add_item(case.id, section=row["target_ref"].split(":")[1], content=text.strip(),
                                           source_kind="USER", attachment_id=None)
            return "사용자 제공 가이드 항목으로 추가했습니다(승인 전 제안 상태)."
        notes.add(case.id, f"[질문 답변] {row['question_text']} → {text or value}")
        return "답변을 메모로 남겼습니다."

    @staticmethod
    def _apply_session(conn, case_id: int, value: str | None) -> str:
        files = LogFileRepository(conn).get_many(CaseRepository(conn).log_file_ids(case_id))
        sessions = SessionRepository(conn)
        if value == "SAME":
            target = files[0].session_id
            for f in files:
                if f.session_id != target:
                    LogFileRepository(conn).update_metadata(f.id, session_id=target)
            sessions.update(target, relation_confirmed=True)
            return "로그를 하나의 수집 세션으로 묶었습니다."
        for session_id in {f.session_id for f in files}:
            sessions.update(session_id, relation_confirmed=True)
        return "로그를 서로 다른 수집 세션으로 확정했습니다."

    def _reanalyze(self, case_id: int, trigger: str, kind: str) -> list[str]:
        """답변에 따라 결정론 초안을 다시 만든다: 정상 사례의 모드가 정해지면 패턴 후보를 새로 만든다."""
        if trigger != "MODE_UNKNOWN" or kind != "CHOICE":
            return []
        from app.learning.normal_learner import NormalLearner  # 순환 import 방지

        case = self._case(case_id)
        if case.case_type != "NORMAL":
            return []
        from app.config import AppSettings

        patterns = NormalLearner(self.database, AppSettings()).propose(case_id)
        return [f"패턴 후보 {len(patterns)}개를 다시 만들었습니다."]

    def _case(self, case_id: int):
        with closing(self.database.connect()) as conn:
            return CaseRepository(conn).get(case_id)


def summarize(questions: list[OpenQuestion]) -> dict[str, Any]:
    return {"open": len(questions), "required": sum(q.required for q in questions)}
