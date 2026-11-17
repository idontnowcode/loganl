"""승인·반려·보관·수정 시작 (계획서 §5.3, §3.2 필수 정책, §7.3 "필수 정보 미충족 시 승인 보류").

- 승인 = 개정본 번호 +1, 승인 시점 스냅샷 저장, 사용자가 고른 패턴·가이드 항목·주장 확정, 감사 이력. 한 트랜잭션.
- 승인 사례를 고치려면 reopen() → 상태가 DRAFT로 돌아가고, 다시 승인할 때까지 분석의 확정 지식에서 빠진다
  (DATABASE.md §5의 미결 항목을 '안전 쪽'으로 결정, 2026-11-17).
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.models import utc_now_iso
from app.models.enums import AuditAction, CaseStatus
from app.storage.database import Database, transaction
from app.storage.repositories import (
    AuditRepository,
    CaseRepository,
    ClaimRepository,
    GuideRepository,
    LogFileRepository,
    NoteRepository,
    PatternRepository,
    QuestionRepository,
    RevisionRepository,
)

APPROVABLE_FROM = {CaseStatus.DRAFT.value, CaseStatus.NEEDS_INFORMATION.value, CaseStatus.READY_FOR_REVIEW.value}


class ApprovalError(ValueError):
    pass


@dataclass
class ApprovalCheck:
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocking


def _blank(value: str | None) -> bool:
    return value is None or not value.strip()


class ApprovalWorkflow:
    def __init__(self, database: Database) -> None:
        self.database = database

    def check(self, case_id: int) -> ApprovalCheck:
        with closing(self.database.connect()) as conn:
            return self._check(conn, case_id)

    def _check(self, conn, case_id: int) -> ApprovalCheck:
        case = CaseRepository(conn).get(case_id)
        if case is None:
            raise ApprovalError(f"사례 {case_id}이(가) 없습니다.")
        result = ApprovalCheck()
        if not CaseRepository(conn).log_file_ids(case_id):
            result.blocking.append("로그 파일이 하나도 없습니다.")
        # CR-06 LI-05: 정상은 로그만, 비정상은 로그 + 증상·판정 기준(DB 제약). 나머지는 선택이고 경고만 한다.
        if case.case_type == "NORMAL":
            if case.mode_id is None or not case.mode_confirmed:
                result.warnings.append("운전 모드가 없습니다(선택). 분석할 때 가장 비슷한 정상 로그와 비교하고, "
                                       "패턴은 확정할 수 없습니다.")
        else:
            guides = GuideRepository(conn)
            if _blank(case.symptom):
                result.blocking.append("오류 증상을 입력해야 합니다.")
            if _blank(case.abnormal_criteria):
                result.blocking.append("비정상 판정 기준(에러로 판단한 근거)을 입력해야 합니다.")
            if case.cause_status is None:
                result.warnings.append("원인 확정 여부를 적지 않았습니다(선택). AI 진단은 원인을 가설로만 씁니다.")
            if not guides.attachments(case_id, "DIAGNOSTIC_GUIDE") and not guides.attachments(case_id, "RESPONSE_MANUAL"):
                result.warnings.append("진단 가이드·대응 매뉴얼이 없습니다(선택). 오류 코드 뜻은 참고 자료에서 찾습니다.")
        open_required = QuestionRepository(conn).open_required_count(case_id)
        if open_required:
            result.blocking.append(f"답하지 않은 필수 확인 질문이 {open_required}개 있습니다.")
        proposed = [p for p in PatternRepository(conn).for_case(case_id) if p.status == "PROPOSED"]
        if proposed:
            result.warnings.append(f"결정하지 않은 패턴 후보 {len(proposed)}개는 승인 뒤에도 쓰이지 않습니다.")
        if case.status not in APPROVABLE_FROM:
            result.blocking.append(f"현재 상태({case.status})에서는 승인할 수 없습니다.")
        return result

    def request_review(self, case_id: int) -> ApprovalCheck:
        """검토 요청: 막는 사유가 있으면 NEEDS_INFORMATION, 없으면 READY_FOR_REVIEW."""
        with closing(self.database.connect()) as conn, transaction(conn):
            check = self._check(conn, case_id)
            case = CaseRepository(conn).get(case_id)
            if case.status in APPROVABLE_FROM:
                new_status = CaseStatus.READY_FOR_REVIEW if check.ok else CaseStatus.NEEDS_INFORMATION
                if case.status != new_status:
                    CaseRepository(conn).update(case_id, status=new_status.value)
                    AuditRepository(conn).add("case", case_id, AuditAction.STATUS_CHANGE,
                                              before={"status": case.status}, after={"status": new_status.value})
            return check

    def approve(self, case_id: int, *, pattern_decisions: Mapping[int, str] | None = None,
                guide_decisions: Mapping[int, str] | None = None, claim_decisions: Mapping[int, str] | None = None,
                change_summary: str | None = None) -> int:
        """승인하고 새 개정본 번호를 돌려준다. 결정값: 패턴·가이드 APPROVED/REJECTED, 주장 CONFIRMED/REJECTED."""
        with closing(self.database.connect()) as conn, transaction(conn):
            check = self._check(conn, case_id)
            if not check.ok:
                raise ApprovalError("승인할 수 없습니다:\n- " + "\n- ".join(check.blocking))
            patterns = PatternRepository(conn)
            own_patterns = {p.id for p in patterns.for_case(case_id)}
            for pattern_id, status in (pattern_decisions or {}).items():
                if pattern_id not in own_patterns or status not in ("APPROVED", "REJECTED", "PROPOSED"):
                    raise ApprovalError(f"패턴 결정이 올바르지 않습니다: {pattern_id}={status}")
                patterns.set_status(pattern_id, status)
            guides = GuideRepository(conn)
            own_items = {row["id"] for row in guides.items(case_id)}
            for item_id, status in (guide_decisions or {}).items():
                if item_id not in own_items or status not in ("APPROVED", "REJECTED", "PROPOSED"):
                    raise ApprovalError(f"가이드 항목 결정이 올바르지 않습니다: {item_id}={status}")
                guides.update_item(item_id, status=status)
            claims = ClaimRepository(conn)
            own_claims = {c.id for c in claims.for_case(case_id)}
            for claim_id, status in (claim_decisions or {}).items():
                if claim_id not in own_claims or status not in ("CONFIRMED", "REJECTED", "PROPOSED"):
                    raise ApprovalError(f"주장 결정이 올바르지 않습니다: {claim_id}={status}")
                claims.set_status(claim_id, status)

            cases = CaseRepository(conn)
            case = cases.get(case_id)
            revision = case.current_revision + 1
            now = utc_now_iso()
            cases.update(case_id, status=CaseStatus.APPROVED.value, current_revision=revision, approved_at=now)
            snapshot = self._snapshot(conn, case_id)
            RevisionRepository(conn).add(case_id, revision, snapshot, change_summary)
            AuditRepository(conn).add("case", case_id, AuditAction.APPROVE, before={"status": case.status},
                                      after={"status": "APPROVED", "revision": revision}, note=change_summary)
            return revision

    def reject(self, case_id: int, reason: str) -> None:
        self._transition(case_id, CaseStatus.REJECTED, AuditAction.REJECT, allowed=APPROVABLE_FROM, note=reason)

    def archive(self, case_id: int) -> None:
        self._transition(case_id, CaseStatus.ARCHIVED, AuditAction.ARCHIVE,
                         allowed={s.value for s in CaseStatus} - {CaseStatus.ARCHIVED.value})

    def restore(self, case_id: int) -> None:
        """보관 해제: 승인 이력이 있으면 APPROVED가 아니라 DRAFT로 돌린다(다시 검토하고 승인해야 쓰인다)."""
        self._transition(case_id, CaseStatus.DRAFT, AuditAction.RESTORE, allowed={CaseStatus.ARCHIVED.value})

    def reopen(self, case_id: int) -> None:
        """승인 사례 수정 시작. 다시 승인하면 개정본이 하나 늘고 이전 개정본은 보존된다."""
        self._transition(case_id, CaseStatus.DRAFT, AuditAction.STATUS_CHANGE, allowed={CaseStatus.APPROVED.value},
                         note="수정 시작")

    def revisions(self, case_id: int) -> list[dict[str, Any]]:
        with closing(self.database.connect()) as conn:
            return [dict(r) for r in RevisionRepository(conn).for_case(case_id)]

    def _transition(self, case_id: int, status: CaseStatus, action: AuditAction, *, allowed: set[str],
                    note: str | None = None) -> None:
        with closing(self.database.connect()) as conn, transaction(conn):
            case = CaseRepository(conn).get(case_id)
            if case is None:
                raise ApprovalError(f"사례 {case_id}이(가) 없습니다.")
            if case.status not in allowed:
                raise ApprovalError(f"현재 상태({case.status})에서는 이 작업을 할 수 없습니다.")
            CaseRepository(conn).update(case_id, status=status.value)
            AuditRepository(conn).add("case", case_id, action, before={"status": case.status},
                                      after={"status": status.value}, note=note)

    @staticmethod
    def _snapshot(conn, case_id: int) -> dict[str, Any]:
        cases = CaseRepository(conn)
        case = cases.get(case_id)
        files = LogFileRepository(conn).get_many(cases.log_file_ids(case_id))
        return {
            "case": case.model_dump(),
            "files": [{"id": f.id, "name": f.original_name, "sha256": f.sha256, "session_id": f.session_id,
                       "log_role": f.log_role, "timestamp_basis": str(f.timestamp_basis)} for f in files],
            "notes": [n.model_dump() for n in NoteRepository(conn).for_case(case_id)],
            "patterns": [p.model_dump() for p in PatternRepository(conn).for_case(case_id)],
            "guide_items": [dict(r) for r in GuideRepository(conn).items(case_id)],
            "claims": [c.model_dump() for c in ClaimRepository(conn).for_case(case_id)],
            "questions": [dict(r) for r in QuestionRepository(conn).for_case(case_id)],
        }
