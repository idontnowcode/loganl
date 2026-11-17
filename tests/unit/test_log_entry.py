"""CR-06 로그 입력: 파일 하나 = 사례 하나, 저장 = 승인 (LI-03·04). 로그·메모는 가상이다."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from app.bootstrap import initialize
from app.core.importer import LogImporter
from app.learning.case_manager import CaseManager
from app.learning.log_entry import LogEntryError, LogEntryService, entry_title
from app.models.enums import OriginPurpose
from app.models.records import ImportRequest, SessionMode
from app.storage.repositories import CaseRepository
from tests.logdata import abnormal_log, normal_log


@pytest.fixture
def ctx(tmp_path: Path):
    context = initialize(tmp_path / "home", configure_logging=False)
    yield context
    context.close()


@pytest.fixture
def files(ctx, tmp_path: Path) -> list[int]:
    return LogImporter(ctx.database, ctx.paths, ctx.settings.import_).import_logs(ImportRequest(
        paths=[normal_log(tmp_path / "n.log"), abnormal_log(tmp_path / "a.log", start_second=20)],
        purpose=OriginPurpose.NORMAL_CASE, session_mode=SessionMode.SEPARATE)).log_file_ids


def test_entry_title() -> None:
    assert entry_title("a.log", "  \n가상 동작 시험\n둘째 줄") == "a.log · 가상 동작 시험"
    assert entry_title("a.log", "") == "a.log"
    assert entry_title("a.log", "가" * 40) == "a.log · " + "가" * 30 + "…"


def test_unsaved_entries(ctx, files) -> None:
    entries = LogEntryService(ctx.database).entries(files)
    assert [e.file_name for e in entries] == ["n.log", "a.log"]
    assert all(e.label is None and e.state_text() == "저장 전" for e in entries)


def test_save_normal_approves_at_once(ctx, files) -> None:
    service = LogEntryService(ctx.database)
    entry = service.save(files[0], "NORMAL", "가상 동작: 시험 운전 한 번")
    case = CaseManager(ctx.database).get(entry.case_id)
    assert case.case_type == "NORMAL" and case.status == "APPROVED" and case.current_revision == 1
    assert case.operating_conditions == "가상 동작: 시험 운전 한 번" and case.title == "n.log · 가상 동작: 시험 운전 한 번"
    assert case.mode_id is None and case.normal_rationale is None  # 모드·근거 없이도 승인 (LI-05)
    with closing(ctx.database.connect()) as conn:
        assert [c.id for c in CaseRepository(conn).approved(case_type="NORMAL")] == [case.id]  # 분석에 쓰인다
    assert entry.saved and entry.state_text() == "저장됨 (개정본 1)"


def test_save_abnormal_guide_optional_and_fills_both_columns(ctx, files) -> None:  # CR-07 LI-09
    service = LogEntryService(ctx.database)
    bare = service.save(files[1], "ABNORMAL", "", "  ")  # 메모·판단 가이드 없이도 저장(승인)된다
    bare_case = CaseManager(ctx.database).get(bare.case_id)
    assert bare_case.status == "APPROVED" and bare_case.symptom is None and bare_case.abnormal_criteria is None
    entry = service.save(files[1], "ABNORMAL", "가상 동작", "TEST-E001이 두 번 나와서 에러로 봄")
    case = CaseManager(ctx.database).get(entry.case_id)
    assert case.status == "APPROVED" and case.cause_status is None
    assert case.symptom == case.abnormal_criteria == "TEST-E001이 두 번 나와서 에러로 봄"
    assert entry.criteria == "TEST-E001이 두 번 나와서 에러로 봄"


def test_resave_makes_new_revision_and_unchanged_does_not(ctx, files) -> None:
    service = LogEntryService(ctx.database)
    first = service.save(files[0], "NORMAL", "가상 동작 1")
    again = service.save(files[0], "NORMAL", "가상 동작 1")
    assert again.revision == 1  # 바뀐 것이 없으면 개정본을 늘리지 않는다
    changed = service.save(files[0], "NORMAL", "가상 동작 2")
    assert changed.case_id == first.case_id and changed.revision == 2 and changed.memo == "가상 동작 2"


def test_switching_label_archives_old_case(ctx, files) -> None:
    service = LogEntryService(ctx.database)
    normal = service.save(files[0], "NORMAL", "가상 동작")
    abnormal = service.save(files[0], "ABNORMAL", "가상 동작", "다시 보니 TEST-WARN이 나와서 에러")
    assert abnormal.case_id != normal.case_id and abnormal.label == "ABNORMAL"
    assert CaseManager(ctx.database).get(normal.case_id).status == "ARCHIVED"  # 이전 판정은 지우지 않고 보관
    assert [e.case_id for e in service.saved_entries()] == [abnormal.case_id]


def test_saved_entries_skip_multi_log_cases(ctx, files) -> None:
    CaseManager(ctx.database).create("NORMAL", "예전 여러 로그 사례", files)
    service = LogEntryService(ctx.database)
    assert service.saved_entries() == []  # 예전 사례는 '학습 사례 관리'에서 본다
    assert service.entry(files[0]).case_id is None
    entry = service.save(files[0], "NORMAL", "")
    assert [e.case_id for e in service.saved_entries()] == [entry.case_id]


def test_title_set_in_detail_editor_is_kept(ctx, files) -> None:
    service = LogEntryService(ctx.database)
    entry = service.save(files[0], "NORMAL", "가상 동작")
    workflow_case = CaseManager(ctx.database)
    from app.learning.approval_workflow import ApprovalWorkflow

    ApprovalWorkflow(ctx.database).reopen(entry.case_id)
    workflow_case.update(entry.case_id, title="내가 붙인 제목")
    saved = service.save(files[0], "NORMAL", "가상 동작 바뀜")
    assert CaseManager(ctx.database).get(saved.case_id).title == "내가 붙인 제목"


def test_rejects_unknown_label(ctx, files) -> None:
    with pytest.raises(LogEntryError, match="골라"):
        LogEntryService(ctx.database).save(files[0], "", "메모")
