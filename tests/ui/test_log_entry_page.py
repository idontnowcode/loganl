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
    widget.steps.go(1)  # CR-12: 목록·설명 칸은 ② 구분·메모 단계에 있다
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
    page.criteria_edit.setPlainText("TEST-E001이 두 번 나와서 에러로 봄")  # 판단 가이드(선택)
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


def test_switching_rows_autosaves_labeled_input_and_keeps_unlabeled(page) -> None:
    page.normal_radio.setChecked(True)
    page.memo_edit.setPlainText("다른 줄로 옮기면 저장")
    select(page, 2)  # 구분을 고른 입력은 자동 저장
    assert rows(page)[0][3] == "저장됨 (개정본 1)"
    page.memo_edit.setPlainText("구분을 아직 안 고름")
    select(page, 1)  # 구분이 없는 입력은 '입력 중'으로 남는다(지우지 않음)
    assert rows(page)[2][3] == "입력 중 (저장 전)" and rows(page)[2][1] == "—"
    select(page, 2)
    assert page.memo_edit.toPlainText() == "구분을 아직 안 고름"
    page.abnormal_radio.setChecked(True)  # 판단 가이드 없이 비정상만 골라도
    select(page, 1)
    assert rows(page)[2][1:4:2] == ("비정상", "저장됨 (개정본 1)")  # 저장된다(CR-07 LI-09)


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


def test_saving_asks_ai_questions_and_answers_are_kept(qtbot, ctx, tmp_path: Path) -> None:  # CR-07 QA-01·02
    from app.learning.ai_questions import knowledge_texts
    from tests.fake_ollama import FakeOllama

    ctx.settings = ctx.settings.model_copy(
        update={"ollama": ctx.settings.ollama.model_copy(update={"model": "test-model:7b"})})
    ids = LogImporter(ctx.database, ctx.paths, ctx.settings.import_).import_logs(ImportRequest(
        paths=[abnormal_log(tmp_path / "q.log")], purpose=OriginPurpose.NORMAL_CASE,
        session_mode=SessionMode.SEPARATE)).log_file_ids
    fake = FakeOllama([{"questions": [
        {"kind": "GUESS", "text": "TEST-RETRY 뒤에 멈춘 것으로 보이는데 맞나요?", "reason": "가상", "evidence": ["EV-0003"]},
        {"kind": "ASK", "text": "TEST-INIT은 어떤 단계인가요?", "evidence": []}]}])
    page = LogEntryPage(ctx, client_factory=fake.factory)
    qtbot.addWidget(page)
    page.steps.go(2)  # CR-12: AI 질문은 ③ 저장·AI 질문 단계에 있다
    assert page.questions_box.empty.isVisibleTo(page)
    page.import_panel.set_log_file_ids(ids)
    page.abnormal_radio.setChecked(True)
    assert page.save_selected()
    qtbot.waitUntil(lambda: not page.ai_runner.busy and len(page.questions_box.cards) == 2, timeout=15_000)
    assert "질문 2개" in page.questions_box.status.text(), page.questions_box.status.text()
    guess = next(c for c in page.questions_box.cards if c.view.kind == "GUESS")
    assert guess.view.file_name == "q.log"
    guess.edit.setText("가상 한마디")
    guess.buttons["YES"].click()
    assert len(page.questions_box.cards) == 1 and "다음 분석부터" in page.questions_box.status.text()
    ask = page.questions_box.cards[0]
    ask.buttons["TEXT"].click()  # 빈 답은 받지 않는다
    assert "답을 적어" in page.questions_box.status.text() and len(page.questions_box.cards) == 1
    ask.buttons["SKIP"].click()
    assert page.questions_box.cards == [] and page.questions_box.empty.isVisibleTo(page)
    case_id = page.service.entry(ids[0]).case_id
    assert knowledge_texts(ctx.database, [case_id]) == [
        "[사용자 확인] AI 추측 'TEST-RETRY 뒤에 멈춘 것으로 보이는데 맞나요?' → 사용자가 맞다고 확인 — 사용자 설명: 가상 한마디"]


def test_no_model_does_not_start_background_work(page) -> None:  # 닫힌 화면에 늦은 결과가 닿지 않게
    page.normal_radio.setChecked(True)
    assert page.save_selected()
    assert not page.ai_runner.busy and "모델" in page.questions_box.status.text()


def test_late_result_after_close_is_ignored(qtbot, ctx) -> None:
    from app.learning.ai_questions import QuestionOutcome

    widget = LogEntryPage(ctx)
    qtbot.addWidget(widget)
    box = widget.questions_box
    import shiboken6

    shiboken6.delete(box)  # 화면이 먼저 닫힌 것처럼
    widget._on_questions("x.log", QuestionOutcome("OK"))  # 예외 없이 넘어간다


def test_clear_list_saves_labeled_input_first(page) -> None:  # CR-10 LI-12
    first = page.import_panel.log_file_ids()[0]
    page.normal_radio.setChecked(True)  # 첫 줄을 '정상'으로 고르고 저장 단추는 누르지 않음
    page.memo_edit.setPlainText("가상 동작 한 번")
    assert not page.import_panel.clear_button.isHidden()
    page.import_panel.clear_button.click()
    assert page.entry_table.rowCount() == 0 and page.empty_label.isVisibleTo(page)
    saved = page.service.entry(first)
    assert saved.label == "NORMAL" and saved.memo == "가상 동작 한 번"  # 비우기 전에 자동 저장
    assert page.saved_table.rowCount() == 1


def test_abnormal_choice_shows_error_lines(page) -> None:  # CR-11 EM-04
    select(page, 2)  # a.log (가상 비정상 로그)
    page._on_selection_changed()
    assert page.error_box.isHidden()
    page.abnormal_radio.setChecked(True)
    assert not page.error_box.isHidden()
    texts = page.error_box.texts()
    assert texts[0].startswith("[에러로 기록된 줄] 4행 ×2") and "TEST-E001" in texts[0]
    assert texts[1].startswith("[경고·실패 낱말이 있는 줄] 5행")
    page.normal_radio.setChecked(True)
    assert page.error_box.isHidden()  # 정상이면 숨김
    select(page, 0, 2)
    page._on_selection_changed()
    page.abnormal_radio.setChecked(True)
    assert page.error_box.isHidden()  # 여러 개를 고르면 띄우지 않는다


def test_entry_steps(page) -> None:  # CR-12 ST-03
    assert page.steps.current() == 1 and [b.text().split("  ", 1)[1] for b in page.steps.bar.buttons] == [
        "로그 넣기", "구분·메모", "저장·AI 질문"]
    page.normal_radio.setChecked(True)
    page.steps.go(2)  # 단계를 바꾸면 고른 입력은 자동 저장
    assert page.saved_table.rowCount() == 1
    page.bring_saved(0)  # 저장된 로그를 두 번 누르면 ②로
    assert page.steps.current() == 1
