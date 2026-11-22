"""로그 입력 (변경 요청 CR-06): 파일 하나 = 사례 하나, 저장 = 승인.

사용자가 하는 일: 정상/비정상 고르기(필수), 메모(선택), (비정상이면) 판단 가이드(선택, CR-07).
- 메모 → cases.operating_conditions ("동작 조건 및 상황", 계획서 §3.1)
- 판단 가이드 → cases.abnormal_criteria와 cases.symptom (같은 글. 0006 뒤로는 비워도 승인된다)
- 저장하면 바로 승인한다(사용자의 판정이 곧 승인, 질문 답 LI-04). 다시 저장하면 수정 시작 → 새 개정본.
- 정상↔비정상을 바꾸면 사례 종류는 바꿀 수 없으므로 이전 사례를 보관하고 새로 만든다.
AI는 여기서 아무것도 정하지 않는다.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass

from app.learning.approval_workflow import ApprovalError, ApprovalWorkflow
from app.learning.case_manager import CaseError, CaseManager
from app.models.records import CaseRecord
from app.storage.database import Database
from app.storage.repositories import CaseRepository, LogFileRepository

LABELS = {"NORMAL": "정상", "ABNORMAL": "비정상"}
TITLE_MEMO_MAX = 30


class LogEntryError(ValueError):
    """저장할 수 없음. 메시지는 화면에 그대로 보여 준다."""


@dataclass
class LogEntry:
    log_file_id: int
    file_name: str
    case_id: int | None = None
    label: str | None = None  # NORMAL / ABNORMAL, 저장 전이면 None
    memo: str = ""
    criteria: str = ""
    status: str | None = None
    revision: int = 0
    approved_at: str | None = None

    @property
    def saved(self) -> bool:
        return self.status == "APPROVED"

    def state_text(self) -> str:
        if self.status is None:
            return "저장 전"
        if self.status == "APPROVED":
            return f"저장됨 (개정본 {self.revision})"
        return "고치는 중 (저장하면 학습에 씀)"


def entry_title(file_name: str, memo: str) -> str:
    first = next((line.strip() for line in memo.splitlines() if line.strip()), "")
    if len(first) > TITLE_MEMO_MAX:
        first = first[:TITLE_MEMO_MAX].rstrip() + "…"
    return f"{file_name} · {first}" if first else file_name


class LogEntryService:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.cases = CaseManager(database)
        self.workflow = ApprovalWorkflow(database)

    # ── 조회 ──
    def entries(self, log_file_ids: Sequence[int]) -> list[LogEntry]:
        with closing(self.database.connect()) as conn:
            records = {r.id: r for r in LogFileRepository(conn).get_many(list(log_file_ids))}
            out = []
            for log_file_id in log_file_ids:
                record = records.get(log_file_id)
                if record is None:
                    continue
                current = CaseRepository(conn).single_log_cases(log_file_id)
                out.append(self._entry(log_file_id, record.original_name, current[0] if current else None))
            return out

    def entry(self, log_file_id: int) -> LogEntry:
        found = self.entries([log_file_id])
        if not found:
            raise LogEntryError(f"로그 파일 {log_file_id}이(가) 없습니다.")
        return found[0]

    def saved_entries(self) -> list[LogEntry]:
        """저장된 로그 목록(최근 것부터). 여러 로그가 걸린 예전 사례는 '학습 사례 관리'에서 본다."""
        with closing(self.database.connect()) as conn:
            cases = CaseRepository(conn).single_log_cases()
            ids = [CaseRepository(conn).log_file_ids(c.id)[0] for c in cases]
            names = {r.id: r.original_name for r in LogFileRepository(conn).get_many(ids)}
        seen: set[int] = set()
        out = []
        for case, log_file_id in zip(cases, ids):
            if log_file_id in seen:  # 같은 파일의 사례가 여럿이면 가장 최근 것만
                continue
            seen.add(log_file_id)
            out.append(self._entry(log_file_id, names.get(log_file_id, f"파일 {log_file_id}"), case))
        return out

    @staticmethod
    def _entry(log_file_id: int, name: str, case: CaseRecord | None) -> LogEntry:
        if case is None:
            return LogEntry(log_file_id, name)
        return LogEntry(log_file_id, name, case.id, case.case_type, case.operating_conditions or "",
                        case.abnormal_criteria or "", case.status, case.current_revision, case.approved_at)

    # ── 묶음(같은 수집 세션으로 함께 분석한 로그) ── CR-14 리뷰: 묶어 분석한 로그도 한 사례로 확인 결과를 남긴다
    def group_case(self, log_file_ids: Sequence[int]) -> CaseRecord | None:
        """로그 묶음과 같은 로그들이 걸린, 보관·반려가 아닌 사례(가장 최근 것)."""
        wanted = sorted(set(log_file_ids))
        if not wanted:
            return None
        with closing(self.database.connect()) as conn:
            repo = CaseRepository(conn)
            rows = conn.execute("SELECT case_id FROM case_log_files WHERE log_file_id = ? ORDER BY case_id DESC",
                                (wanted[0],)).fetchall()
            for row in rows:
                case = repo.get(int(row[0]))
                if case is not None and case.status not in ("ARCHIVED", "REJECTED")                         and sorted(repo.log_file_ids(case.id)) == wanted:
                    return case
        return None

    def group_entry(self, log_file_ids: Sequence[int]) -> LogEntry:
        """묶음의 확인 상태(첫 로그 id·묶음 이름으로). 로그 하나면 entry와 같다."""
        ids = sorted(set(log_file_ids))
        if len(ids) == 1:
            return self.entry(ids[0])
        return self._entry(ids[0], self._group_name(ids), self.group_case(ids))

    def save_group(self, log_file_ids: Sequence[int], label: str, memo: str, criteria: str = "") -> LogEntry:
        """묶음 전체를 사례 하나로 저장(학습 탭 저장과 같은 승인 흐름). 로그 하나면 save와 같다."""
        ids = sorted(set(log_file_ids))
        if len(ids) == 1:
            return self.save(ids[0], label, memo, criteria)
        if label not in LABELS:
            raise LogEntryError("정상인지 비정상인지 골라 주세요.")
        memo, criteria = memo.strip(), criteria.strip()
        fields: dict[str, object] = {"operating_conditions": memo or None}
        if label == "ABNORMAL":
            fields.update(symptom=criteria or None, abnormal_criteria=criteria or None)
        title = entry_title(self._group_name(ids), memo)
        before = self.group_case(ids)
        try:
            case_id = before.id if before is not None else None
            if before is not None and before.case_type != label:
                self.workflow.archive(before.id)
                case_id = None
            if case_id is None:
                case_id = self.cases.create(label, title, ids, **fields)
            else:
                current = self.cases.get(case_id)
                unchanged = all(getattr(current, k) == v for k, v in fields.items())
                if current.status == "APPROVED":
                    if unchanged:
                        return self.group_entry(ids)
                    self.workflow.reopen(case_id)
                self.cases.update(case_id, title=title, **fields)
            self.workflow.approve(case_id, change_summary="분석 결과에서 묶음으로 저장")
        except (ApprovalError, CaseError) as exc:
            raise LogEntryError(str(exc)) from exc
        return self.group_entry(ids)

    def _group_name(self, ids: Sequence[int]) -> str:
        with closing(self.database.connect()) as conn:
            records = {r.id: r.original_name for r in LogFileRepository(conn).get_many(list(ids))}
        first = records.get(ids[0], f"파일 {ids[0]}")
        return first if len(ids) == 1 else f"{first} 외 {len(ids) - 1}개(같은 수집 세션)"

    # ── 저장 ──
    def save(self, log_file_id: int, label: str, memo: str, criteria: str = "") -> LogEntry:
        if label not in LABELS:
            raise LogEntryError("정상인지 비정상인지 골라 주세요.")
        memo, criteria = memo.strip(), criteria.strip()
        before = self.entry(log_file_id)
        fields: dict[str, object] = {"operating_conditions": memo or None}
        if label == "ABNORMAL":
            fields.update(symptom=criteria or None, abnormal_criteria=criteria or None)
        title = entry_title(before.file_name, memo)
        try:
            case_id = before.case_id
            if case_id is not None and before.label != label:
                self.workflow.archive(case_id)  # 사례 종류는 못 바꾼다: 이전 판정은 보관(이력 유지)
                case_id = None
            if case_id is None:
                case_id = self.cases.create(label, title, [log_file_id], **fields)
            else:
                current = self.cases.get(case_id)
                unchanged = (current.title == title and current.operating_conditions == fields["operating_conditions"]
                             and all(getattr(current, k) == v for k, v in fields.items()))
                if current.status == "APPROVED":
                    if unchanged:
                        return before
                    self.workflow.reopen(case_id)
                if not current.title.startswith(before.file_name):
                    title = current.title  # '자세히 편집'에서 사용자가 붙인 제목은 지키기
                self.cases.update(case_id, title=title, **fields)
            self.workflow.approve(case_id, change_summary="로그 입력에서 저장")
        except (ApprovalError, CaseError) as exc:
            raise LogEntryError(str(exc)) from exc
        return self.entry(log_file_id)
