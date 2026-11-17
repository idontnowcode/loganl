"""로그 입력 (변경 요청 CR-06): 파일 하나 = 사례 하나, 저장 = 승인.

사용자가 하는 일은 세 가지뿐이다: 정상/비정상 고르기, 동작 메모, (비정상이면) 에러로 판단한 근거.
- 동작 메모 → cases.operating_conditions ("동작 조건 및 상황", 계획서 §3.1)
- 판단 근거 → cases.abnormal_criteria와 cases.symptom (승인된 비정상은 둘 다 필수인 DB 제약을 지킨다)
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

    # ── 저장 ──
    def save(self, log_file_id: int, label: str, memo: str, criteria: str = "") -> LogEntry:
        if label not in LABELS:
            raise LogEntryError("정상인지 비정상인지 골라 주세요.")
        memo, criteria = memo.strip(), criteria.strip()
        if label == "ABNORMAL" and not criteria:
            raise LogEntryError("비정상 로그는 '에러로 판단한 근거'를 적어 주세요.")
        before = self.entry(log_file_id)
        fields: dict[str, object] = {"operating_conditions": memo or None}
        if label == "ABNORMAL":
            fields.update(symptom=criteria, abnormal_criteria=criteria)
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
