"""CR-05 자유 글 입력 화면 (NL-01·04·05·07·08). 글·모드 이름·오류 코드는 가상이다."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.bootstrap import initialize
from app.core.importer import LogImporter
from app.learning.case_manager import CaseManager, ModeManager
from app.models.enums import OriginPurpose
from app.models.records import ImportRequest, SessionMode
from app.ui.abnormal_learning import AbnormalLearningPage
from app.ui.log_analysis import LogAnalysisPage
from app.ui.natural_input import MISSING_TEXT
from app.ui.normal_learning import NormalLearningPage
from tests.fake_ollama import FakeOllama
from tests.logdata import abnormal_log
from tests.unit.test_guide_structurer import GUIDE

STORY = "어제 오후 TEST-MODE-A로 돌렸습니다. 끝까지 TEST-E001 없이 끝났음.\nFW는 2.3이고 장비는 기본 구성"


@pytest.fixture
def ctx(tmp_path: Path):
    context = initialize(tmp_path / "home", configure_logging=False)
    context.settings = context.settings.model_copy(
        update={"ollama": context.settings.ollama.model_copy(update={"model": "test-model:7b"})})
    yield context
    context.close()


def no_model(ctx):
    return ctx.settings.model_copy(update={"ollama": ctx.settings.ollama.model_copy(update={"model": None})})


def test_story_box_is_first_and_fields_are_folded(qtbot, ctx) -> None:
    page = NormalLearningPage(ctx)
    qtbot.addWidget(page)
    assert page.side.indexOf(page.story) < page.side.indexOf(page.question_panel) < page.side.indexOf(page.fields_section)
    assert page.fields_section.content.isHidden()  # 칸 양식은 접혀 있다
    assert not page.fields_section.is_expanded()
    lines = page.story.summary_texts()
    assert f"운전 모드: {MISSING_TEXT}" in lines and f"정상 판정 근거: {MISSING_TEXT}" in lines
    assert not any(line.startswith("FW 버전") for line in lines)  # 빈 선택 칸은 안 보인다
    page.fields_section.toggle.click()
    assert page.fields_section.is_expanded() and not page.fields_section.content.isHidden()


def test_organize_fills_fields_and_mode_needs_one_click(qtbot, ctx) -> None:
    mode_id = ModeManager(ctx.database).add("TEST-MODE-A")
    fake = FakeOllama([{"picks": [{"field": "normal_rationale", "text": "끝까지 TEST-E001 없이 끝났음."},
                                  {"field": "fw_version", "text": "2.3"}],
                        "mode": "UNKNOWN", "mode_text": "", "cause_status": "NOT_SAID", "cause_text": ""}])
    page = NormalLearningPage(ctx, client_factory=fake.factory)
    qtbot.addWidget(page)
    page.story_edit.setPlainText(STORY)
    page.organize_story()
    qtbot.waitUntil(lambda: page.story.organize_button.isEnabled(), timeout=5000)
    case = CaseManager(ctx.database).get(page.case_id)
    assert case.user_description == STORY and case.normal_rationale == "끝까지 TEST-E001 없이 끝났음."
    assert case.fw_version == "2.3" and case.mode_id == mode_id and not case.mode_confirmed
    assert case.title.startswith("어제 오후")
    assert "내 글에서 옮겼습니다" in page.story.result_label.text()
    lines = page.story.summary_texts()
    assert "운전 모드: TEST-MODE-A" in lines and "FW 버전: 2.3" in lines
    assert page.mode_ok_button.isVisibleTo(page)
    page.mode_ok_button.click()
    assert CaseManager(ctx.database).get(page.case_id).mode_confirmed
    assert not page.mode_ok_button.isVisibleTo(page)
    assert "운전 모드" not in page.check_label.text()  # 승인 검사에서 모드 막힘이 풀렸다


def test_without_ai_text_is_kept_and_fields_open(qtbot, ctx) -> None:
    ctx.settings = no_model(ctx)
    page = NormalLearningPage(ctx, client_factory=FakeOllama().factory)
    qtbot.addWidget(page)
    page.story_edit.setPlainText(STORY)
    page.organize_story()
    qtbot.waitUntil(lambda: page.story.organize_button.isEnabled(), timeout=5000)
    assert CaseManager(ctx.database).get(page.case_id).user_description == STORY
    assert page.fields_section.is_expanded()
    assert "칸으로 직접 고치기" in page.story.result_label.text()


def test_story_alone_autosaves_with_title_from_first_line(qtbot, ctx) -> None:
    page = NormalLearningPage(ctx)
    qtbot.addWidget(page)
    page.story_edit.setPlainText("첫 줄 상황 설명\n둘째 줄")
    assert page.autosave()
    case = CaseManager(ctx.database).get(page.case_id)
    assert case.title == "첫 줄 상황 설명" and case.user_description == "첫 줄 상황 설명\n둘째 줄"


def test_empty_story_asks_for_text(qtbot, ctx) -> None:
    page = NormalLearningPage(ctx)
    qtbot.addWidget(page)
    page.organize_story()
    assert "먼저 위 칸에" in page.story.result_label.text() and page.case_id is None


def test_abnormal_summary_and_guide_items_default_approved(qtbot, ctx, tmp_path: Path) -> None:
    ids = LogImporter(ctx.database, ctx.paths, ctx.settings.import_).import_logs(ImportRequest(
        paths=[abnormal_log(tmp_path / "a.log")], purpose=OriginPurpose.ABNORMAL_CASE,
        session_mode=SessionMode.SAME)).log_file_ids
    page = AbnormalLearningPage(ctx)
    qtbot.addWidget(page)
    lines = page.story.summary_texts()
    for label in ("진단 가이드", "오류 증상", "판정 기준", "원인 확정 여부"):
        assert f"{label}: {MISSING_TEXT}" in lines
    assert not any(line.startswith("운전 모드") for line in lines)  # 비정상은 모드가 승인 필수가 아니다
    page.import_panel.set_log_file_ids(ids)
    guide = tmp_path / "guide.md"
    guide.write_bytes(GUIDE.encode("utf-8"))
    assert page.attach_file(guide, "DIAGNOSTIC_GUIDE")
    assert "진단 가이드: 첨부 1개" in page.story.summary_texts()
    assert "'승인'으로 두었습니다" in page.attach_status.text()
    assert page.item_table.rowCount() > 0
    sources = [page.item_table.item(r, 3).text() for r in range(page.item_table.rowCount())]
    decisions = [page.item_table.cellWidget(r, 0).currentData() for r in range(page.item_table.rowCount())]
    assert all(d == "APPROVED" for s, d in zip(sources, decisions) if s == "원문")
    assert not hasattr(page, "description_edit")  # 추가 설명 칸은 글 칸으로 합쳤다


def test_abnormal_organize_sets_symptom_and_cause(qtbot, ctx) -> None:
    fake = FakeOllama([{"picks": [{"field": "symptom", "text": "TEST-E001이 뜨고 멈춤."}], "mode": "UNKNOWN",
                        "mode_text": "", "cause_status": "UNKNOWN", "cause_text": "원인은 아직 모름."}])
    page = AbnormalLearningPage(ctx, client_factory=fake.factory)
    qtbot.addWidget(page)
    page.story_edit.setPlainText("TEST-E001이 뜨고 멈춤. 원인은 아직 모름.")
    page.organize_story()
    qtbot.waitUntil(lambda: page.story.organize_button.isEnabled(), timeout=5000)
    assert page.symptom_edit.toPlainText() == "TEST-E001이 뜨고 멈춤."
    assert page.cause_combo.currentData() == "UNKNOWN"
    lines = page.story.summary_texts()
    assert "오류 증상: TEST-E001이 뜨고 멈춤." in lines and "원인 확정 여부: 원인 모름" in lines


def test_analysis_story_fills_mode_and_conditions(qtbot, ctx) -> None:
    mode_id = ModeManager(ctx.database).add("TEST-MODE-A")
    fake = FakeOllama([{"picks": [{"field": "operating_conditions", "text": "어제 오후"}]}])
    page = LogAnalysisPage(ctx, client_factory=fake.factory)
    qtbot.addWidget(page)
    assert page.fields_section.content.isHidden()
    page.description_edit.setPlainText(STORY)
    page.organize_story()
    qtbot.waitUntil(lambda: page.story.organize_button.isEnabled(), timeout=5000)
    assert page.mode_combo.currentData() == mode_id and not page.mode_confirm.isChecked()
    assert page.conditions_edit.toPlainText() == "어제 오후"
    assert "운전 모드: TEST-MODE-A" in page.story.summary_texts()
    page.mode_ok_button.click()
    request = page.build_request()
    assert request.mode_confirmed and request.user_description == STORY
    assert request.operating_conditions == "어제 오후"
