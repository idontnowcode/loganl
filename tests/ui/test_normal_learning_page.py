"""정상 학습 화면 (UI-03, DOD-01~03·05 일부, 완료 기준 "정상 사례를 등록하고 재조회 가능")."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.bootstrap import initialize
from app.core.importer import LogImporter
from app.learning.case_manager import ModeManager
from app.models.enums import OriginPurpose
from app.models.records import FileMeta, ImportRequest, SessionMode
from app.ui.mode_dialog import ModeDialog
from app.ui.normal_learning import NormalLearningPage
from tests.fake_ollama import FakeOllama
from tests.logdata import normal_log


@pytest.fixture
def ctx(tmp_path: Path):
    context = initialize(tmp_path / "home", configure_logging=False)
    context.settings = context.settings.model_copy(
        update={"ollama": context.settings.ollama.model_copy(update={"model": "test-model:7b"})})
    yield context
    context.close()


@pytest.fixture
def log_ids(ctx, tmp_path: Path) -> list[int]:
    result = LogImporter(ctx.database, ctx.paths, ctx.settings.import_).import_logs(ImportRequest(
        paths=[normal_log(tmp_path / "a.log"), normal_log(tmp_path / "b.log", start_second=10)],
        purpose=OriginPurpose.NORMAL_CASE, session_mode=SessionMode.SEPARATE, meta=FileMeta(log_role="TEST-ROLE")))
    return result.log_file_ids


def test_register_approve_and_reload(qtbot, ctx, log_ids) -> None:
    mode_id = ModeManager(ctx.database).add("테스트모드A")
    page = NormalLearningPage(ctx)
    qtbot.addWidget(page)
    page.import_panel.set_log_file_ids(log_ids)
    page.title_edit.setText("화면에서 만든 정상 사례")
    page.save()
    assert page.case_id is not None
    assert "운전 모드" in page.check_label.text()

    page.mode_combo.setCurrentIndex(page.mode_combo.findData(mode_id))
    page.mode_confirm.setChecked(True)
    page.rationale_edit.setPlainText("사용자가 정상 동작을 확인함")
    page.note_edit.setText("화면 메모")
    page.add_note()
    page.propose_patterns()
    assert page.pattern_table.rowCount() > 0
    page.set_all_decisions("APPROVED", {"REQUIRED", "ORDER_CONSTRAINT"})
    page.set_all_decisions("REJECTED", {"OPTIONAL", "REPEATED", "INDEPENDENT", "UNCLASSIFIED"})
    page.approve()
    assert "승인했습니다" in page.message_label.text()
    assert page.status_label.text().startswith("승인됨")
    statuses = {(p.pattern_type, p.status) for p in page.detail.patterns}
    assert statuses == {("REQUIRED", "APPROVED"), ("ORDER_CONSTRAINT", "APPROVED")}  # 화면에서 고른 결정이 반영됨
    assert not page.save_button.isEnabled() and page.reopen_button.isEnabled()

    reopened = NormalLearningPage(ctx)  # 다시 열어도 그대로 (재조회)
    qtbot.addWidget(reopened)
    reopened.load_case(page.case_id)
    assert reopened.title_edit.text() == "화면에서 만든 정상 사례"
    assert reopened.note_list.count() == 1
    assert reopened.import_panel.file_table.rowCount() == 2
    assert reopened.rationale_edit.toPlainText() == "사용자가 정상 동작을 확인함"


def test_ai_mode_estimate_needs_user_confirmation(qtbot, ctx, log_ids) -> None:
    mode_id = ModeManager(ctx.database).add("테스트모드A")
    fake = FakeOllama([{"mode": "테스트모드A", "reason": "가상 근거", "evidence": ["EV-0001"]}])
    page = NormalLearningPage(ctx, fake.factory)
    qtbot.addWidget(page)
    page.import_panel.set_log_file_ids(log_ids)
    page.estimate_mode()
    qtbot.waitUntil(lambda: "AI 추정" in page.mode_guess_label.text(), timeout=5000)
    assert page.mode_combo.currentData() == mode_id
    assert not page.mode_confirm.isChecked()  # 확인은 사용자가 직접


def test_ai_draft_shows_proposed_claims(qtbot, ctx, log_ids) -> None:
    fake = FakeOllama([{"summary": "가상 요약", "observations": [
        {"text": "TEST-START가 먼저", "kind": "FACT", "evidence": ["EV-0002"]}], "missing_info": ["반복 여부"]}])
    page = NormalLearningPage(ctx, fake.factory)
    qtbot.addWidget(page)
    page.import_panel.set_log_file_ids(log_ids)
    page.generate_draft()
    qtbot.waitUntil(lambda: "AI 주장" in page.draft_status.text(), timeout=5000)
    text = page.draft_view.toPlainText()
    assert "[관측] TEST-START가 먼저" in text and "[불명] [확인 필요] 반복 여부" in text


def test_ai_unavailable_is_reported(qtbot, ctx, log_ids) -> None:
    import httpx

    fake = FakeOllama([httpx.ConnectError("refused")])
    page = NormalLearningPage(ctx, fake.factory)
    qtbot.addWidget(page)
    page.import_panel.set_log_file_ids(log_ids)
    page.generate_draft()
    qtbot.waitUntil(lambda: "AI 사용 불가" in page.draft_status.text(), timeout=5000)


def test_mode_dialog_adds_and_retires(qtbot, ctx) -> None:
    dialog = ModeDialog(ctx.database)
    qtbot.addWidget(dialog)
    dialog.name_edit.setText("테스트모드B")
    dialog.add_mode()
    assert dialog.table.rowCount() == 1
    dialog.name_edit.setText("테스트모드B")
    dialog.add_mode()
    assert "같은 이름" in dialog.message.text()
    dialog.table.selectRow(0)
    dialog.retire_selected()
    assert dialog.table.item(0, 3).text() == "사용 안 함"
    assert ModeManager(ctx.database).list(active_only=True) == []
