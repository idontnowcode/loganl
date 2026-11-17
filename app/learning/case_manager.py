"""학습 사례와 운전 모드 관리 (계획서 §3.1·§3.2 입력, §5, §9.1 "학습 데이터 자동 저장", "승인 전 초안도 별도 보관").

- 승인(APPROVED)·보관(ARCHIVED) 사례는 바로 고칠 수 없다. `ApprovalWorkflow.reopen()`으로 수정을 시작해야 한다.
- 모든 변경은 audit_history에 남는다.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from app.models.enums import Actor, AuditAction, CaseStatus
from app.models.records import CaseRecord, ClaimRecord, LogFileRecord, ModeRecord, NoteRecord, PatternRecord
from app.storage.database import Database, transaction
from app.storage.repositories import (
    AuditRepository,
    CaseRepository,
    ClaimRepository,
    LogFileRepository,
    ModeRepository,
    NoteRepository,
    PatternRepository,
)

LOCKED_STATUSES = {CaseStatus.APPROVED.value, CaseStatus.ARCHIVED.value}


class CaseError(ValueError):
    """사례 작업을 할 수 없음. 메시지는 화면에 그대로 보여 준다."""


@dataclass
class CaseDetail:
    case: CaseRecord
    files: list[LogFileRecord]
    notes: list[NoteRecord]
    patterns: list[PatternRecord]
    claims: list[ClaimRecord]
    extras: dict[str, Any] = field(default_factory=dict)  # Phase 4·5가 가이드·질문을 붙인다


class ModeManager:
    def __init__(self, database: Database) -> None:
        self.database = database

    def list(self, *, active_only: bool = False) -> list[ModeRecord]:
        with closing(self.database.connect()) as conn:
            return ModeRepository(conn).list(active_only=active_only)

    def add(self, name: str, description: str | None = None, conditions: str | None = None) -> int:
        if not name.strip():
            raise CaseError("모드 이름을 입력하세요.")
        with closing(self.database.connect()) as conn, transaction(conn):
            if any(m.name == name.strip() for m in ModeRepository(conn).list()):
                raise CaseError(f"같은 이름의 모드가 있습니다: {name}")
            mode_id = ModeRepository(conn).add(name, description=description, conditions=conditions)
            AuditRepository(conn).add("mode", mode_id, AuditAction.CREATE, after={"name": name})
            return mode_id

    def update(self, mode_id: int, **fields: Any) -> None:
        with closing(self.database.connect()) as conn, transaction(conn):
            before = ModeRepository(conn).get(mode_id)
            ModeRepository(conn).update(mode_id, **fields)
            AuditRepository(conn).add("mode", mode_id, AuditAction.UPDATE,
                                      before=before.model_dump() if before else None, after=fields)

    def retire(self, mode_id: int) -> None:
        self.update(mode_id, status="RETIRED")


class CaseManager:
    def __init__(self, database: Database) -> None:
        self.database = database

    # ── 생성·조회 ──
    def create(self, case_type: str, title: str, log_file_ids: Iterable[int] = (), **fields: Any) -> int:
        title = title.strip() or "제목 없는 사례"
        with closing(self.database.connect()) as conn, transaction(conn):
            cases = CaseRepository(conn)
            case_id = cases.create(case_type, title, **fields)
            cases.link_logs(case_id, list(log_file_ids))
            AuditRepository(conn).add("case", case_id, AuditAction.CREATE,
                                      after={"case_type": case_type, "title": title, **fields})
            return case_id

    def get(self, case_id: int) -> CaseRecord:
        with closing(self.database.connect()) as conn:
            case = CaseRepository(conn).get(case_id)
        if case is None:
            raise CaseError(f"사례 {case_id}이(가) 없습니다.")
        return case

    def list(self, case_type: str | None = None, statuses: Sequence[str] | None = None) -> list[CaseRecord]:
        with closing(self.database.connect()) as conn:
            return CaseRepository(conn).list(case_type=case_type, statuses=statuses)

    def detail(self, case_id: int) -> CaseDetail:
        with closing(self.database.connect()) as conn:
            case = CaseRepository(conn).get(case_id)
            if case is None:
                raise CaseError(f"사례 {case_id}이(가) 없습니다.")
            return CaseDetail(
                case=case,
                files=LogFileRepository(conn).get_many(CaseRepository(conn).log_file_ids(case_id)),
                notes=NoteRepository(conn).for_case(case_id),
                patterns=PatternRepository(conn).for_case(case_id),
                claims=ClaimRepository(conn).for_case(case_id),
            )

    # ── 수정 ──
    def update(self, case_id: int, **fields: Any) -> None:
        with closing(self.database.connect()) as conn, transaction(conn):
            case = self._editable(conn, case_id)
            before = {k: getattr(case, k) for k in fields if hasattr(case, k)}
            changed = {k: v for k, v in fields.items() if before.get(k) != v}
            if not changed:
                return
            CaseRepository(conn).update(case_id, **changed)
            AuditRepository(conn).add("case", case_id, AuditAction.UPDATE,
                                      before={k: before.get(k) for k in changed}, after=changed)

    def set_logs(self, case_id: int, log_file_ids: Sequence[int]) -> None:
        with closing(self.database.connect()) as conn, transaction(conn):
            self._editable(conn, case_id)
            cases = CaseRepository(conn)
            current = set(cases.log_file_ids(case_id))
            wanted = set(log_file_ids)
            if current == wanted:
                return
            cases.unlink_logs(case_id, current - wanted)
            cases.link_logs(case_id, wanted - current)
            AuditRepository(conn).add("case", case_id, AuditAction.UPDATE,
                                      before={"log_file_ids": sorted(current)}, after={"log_file_ids": sorted(wanted)})

    def set_mode(self, case_id: int, mode_id: int | None, confirmed: bool) -> None:
        self.update(case_id, mode_id=mode_id, mode_confirmed=bool(confirmed and mode_id is not None))

    def add_note(self, case_id: int, body: str) -> int:
        if not body.strip():
            raise CaseError("메모 내용을 입력하세요.")
        with closing(self.database.connect()) as conn, transaction(conn):
            self._editable(conn, case_id)
            note_id = NoteRepository(conn).add(case_id, body)
            AuditRepository(conn).add("case_note", note_id, AuditAction.CREATE, after={"case_id": case_id})
            return note_id

    def update_note(self, case_id: int, note_id: int, body: str) -> None:
        with closing(self.database.connect()) as conn, transaction(conn):
            self._editable(conn, case_id)
            NoteRepository(conn).update(note_id, body)
            AuditRepository(conn).add("case_note", note_id, AuditAction.UPDATE, after={"case_id": case_id})

    def delete_note(self, case_id: int, note_id: int) -> None:
        with closing(self.database.connect()) as conn, transaction(conn):
            self._editable(conn, case_id)
            NoteRepository(conn).delete(note_id)
            AuditRepository(conn).add("case_note", note_id, AuditAction.DELETE, after={"case_id": case_id})

    def delete(self, case_id: int) -> None:
        with closing(self.database.connect()) as conn, transaction(conn):
            case = CaseRepository(conn).get(case_id)
            if case is None:
                return
            if case.status not in (CaseStatus.DRAFT, CaseStatus.REJECTED) or case.current_revision > 0:
                raise CaseError("한 번도 승인되지 않은 초안·반려 사례만 지울 수 있습니다. 나머지는 보관하세요.")
            AuditRepository(conn).add("case", case_id, AuditAction.DELETE, Actor.USER, before=case.model_dump())
            CaseRepository(conn).delete(case_id)

    @staticmethod
    def _editable(conn, case_id: int) -> CaseRecord:
        case = CaseRepository(conn).get(case_id)
        if case is None:
            raise CaseError(f"사례 {case_id}이(가) 없습니다.")
        if case.status in LOCKED_STATUSES:
            raise CaseError("승인·보관된 사례는 바로 고칠 수 없습니다. '수정 시작'을 누르면 새 개정본을 만듭니다.")
        return case
