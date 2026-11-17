"""CR-06 '로그 입력' 탭 (LI-02·03·04). 로그·메모는 가상이다."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.bootstrap import initialize
from app.core.importer import LogImporter
from app.learning.case_manager import CaseManager
from app.models.enums import OriginPurpose
from app.models.records import ImportRequest, SessionMode
from app.ui.log_entry import LogEntryPage
from tests.logdata import abnormal_log, normal_log


@pytest.fixture
def ctx(tmp_path: Path):
    context = initialize(tmp_path / "home", configure_logging=False)
    yield context
    context.close()


@pytest.fixture
def page(qtbot, ctx, tmp_path: Path) -> LogEntryPage:
    ids = LogImporter(ctx.database, ctx.paths, ctx.settings.import_).import_logs(ImportRequest(
        paths=[normal_log(tmp_path / "n1.log"), normal_log(tmp_path / "n2.log", start_second=9),
               abnormal_log(tmp_path / "a.log", start_second=30)],
        purpose=OriginPurpose.NORMAL_CASE, session_mode=SessionMode.SEPARATE)).log_file_ids
    widget = LogEntryPage(ctx)
    qtbot.addWidget(widget)
    widget.import_panel.set_log_file_ids(ids)
    return widget


def rows(page: LogEntryPage) -> list[tuple[str, ...]]:
    return [tuple(page.entry_table.item(r, c).text() for c in range(page.entry_table.columnCount()))
            for r in range(page.entry_table.rowCount())]


def select(page: LogEntryPage, *indexes: int) -> None:
    page.entry_table.clearSelection()
    for index in indexes:
        page._add_row_to_selection(index)


def test_one_row_per_file_and_only_three_inputs(page) -> None:
    assert [r[0] for r in rows(page)] == ["n1.log", "n2.log", "a.log"]
    assert all(r[1] == "—" and r[3] == "저장 전" for r in rows(page))
    assert page.selected_ids() == [page.import_panel.log_file_ids()[0]]  # 첫 줄이 골라져 있다
    assert not page.criteria_edit.isVisibleTo(page)  # 판단 근거는 비정상일 때만
    page.abnormal_radio.setChecked(True)
    assert page.criteria_edit.isVisibleTo(page)
    page.normal_radio.setChecked(True)
    assert not page.criteria_edit.isVisibleTo(page)


def test_save_normal_and_abnormal(page, ctx) -> None:
    page.normal_radio.setChecked(True)
    page.memo_edit.setPlainText("가상 동작: 시험 운전")
    assert page.save_selected()
    assert rows(page)[0][1:] == ("정상", "가상 동작: 시험 운전", "저장됨 (개정본 1)")
    assert "저장했습니다" in page.message.text()
    assert page.saved_table.rowCount() == 1 and page.saved_table.item(0, 0).text() == "n1.log"

    select(page, 2)
    page.abnormal_radio.setChecked(True)
    page.memo_edit.setPlainText("가상 동작")
    assert not page.save_selected()  # 판단 근거 없이 비정상은 안 된다
    assert "판단한 근거" in page.message.text()
    page.criteria_edit.setPlainText("TEST-E001이 두 번 나와서 에러로 봄")
    assert page.save_selected()
    case = CaseManager(ctx.database).get(page.service.entry(page.selected_ids()[0]).case_id)
    assert (case.case_type, case.status, case.abnormal_criteria) == ("ABNORMAL", "APPROVED", "TEST-E001이 두 번 나와서 에러로 봄")


def test_multi_select_applies_same_content(page) -> None:
    select(page, 0, 1)
    assert "2개 선택" in page.selection_label.text()
    page.normal_radio.setChecked(True)
    page.memo_edit.setPlainText("같은 동작 두 번")
    assert page.save_selected()
    assert [r[1:3] for r in rows(page)[:2]] == [("정상", "같은 동작 두 번")] * 2
    assert page.saved_table.rowCount() == 2


def test_switching_rows_autosaves_complete_input_and_keeps_incomplete(page) -> None:
    page.normal_radio.setChecked(True)
    page.memo_edit.setPlainText("다른 줄로 옮기면 저장")
    select(page, 2)  # 다 적은 입력(정상 + 메모)은 자동 저장
    assert rows(page)[0][3] == "저장됨 (개정본 1)"
    page.abnormal_radio.setChecked(True)
    page.memo_edit.setPlainText("근거를 아직 안 씀")
    select(page, 1)  # 덜 적은 비정상 입력은 '입력 중'으로 남는다(지우지 않음)
    assert rows(page)[2][3] == "입력 중 (저장 전)" and rows(page)[2][1] == "비정상"
    select(page, 2)
    assert page.abnormal_radio.isChecked() and page.memo_edit.toPlainText() == "근거를 아직 안 씀"


def test_bring_saved_back_and_edit(page) -> None:
    page.normal_radio.setChecked(True)
    page.memo_edit.setPlainText("처음 메모")
    page.save_selected()
    saved_id = page.selected_ids()[0]
    page.import_panel.set_log_file_ids([])
    assert page.entry_table.rowCount() == 0 and page.empty_label.isVisibleTo(page)
    page.bring_saved(0)
    assert page.selected_ids() == [saved_id] and page.memo_edit.toPlainText() == "처음 메모"
    page.memo_edit.setPlainText("고친 메모")
    page.save_selected()
    assert rows(page)[0][2:] == ("고친 메모", "저장됨 (개정본 2)")
    assert page.detail_button.isEnabled()
