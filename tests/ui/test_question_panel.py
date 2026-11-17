"""확인 질문 패널 (UI: 최대 3개, 이유, 답변·모름·건너뛰기, 답변 후 화면 반영)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QPushButton

from app.bootstrap import initialize
from app.core.importer import LogImporter
from app.learning.case_manager import ModeManager
from app.models.enums import OriginPurpose
from app.models.records import ImportRequest, SessionMode
from app.ui.normal_learning import NormalLearningPage
from tests.logdata import abnormal_log, write


@pytest.fixture
def ctx(tmp_path: Path):
    context = initialize(tmp_path / "home", configure_logging=False)
    yield context
    context.close()


def test_panel_shows_top_three_and_applies_answers(qtbot, ctx, tmp_path: Path) -> None:
    mode_id = ModeManager(ctx.database).add("테스트모드A")
    ids = LogImporter(ctx.database, ctx.paths, ctx.settings.import_).import_logs(ImportRequest(
        paths=[abnormal_log(tmp_path / "a.log"), write(tmp_path / "free.txt", ["TEST free"])],
        purpose=OriginPurpose.NORMAL_CASE, session_mode=SessionMode.UNKNOWN)).log_file_ids
    page = NormalLearningPage(ctx)
    qtbot.addWidget(page)
    page.import_panel.set_log_file_ids(ids)
    page.title_edit.setText("질문 화면 시험")
    page.save()
    panel = page.question_panel
    assert len(panel.rows) == 3  # 질문은 더 많아도 3개만
    assert "필수 2개" in panel.summary.text()
    assert page.status_label.text().startswith("정보 필요")

    mode_row = next(r for r in panel.rows if r.question.trigger == "MODE_UNKNOWN")
    mode_row.choice.setCurrentIndex(mode_row.choice.findData(str(mode_id)))
    next(b for b in mode_row.findChildren(QPushButton) if b.text() == "답변").click()
    assert page.mode_combo.currentData() == mode_id and page.mode_confirm.isChecked()
    assert "패턴 후보" in page.message_label.text()

    rationale_row = next(r for r in page.question_panel.rows if r.question.trigger == "NORMAL_RATIONALE_MISSING")
    rationale_row.text.setText("가상 근거")
    next(b for b in rationale_row.findChildren(QPushButton) if b.text() == "답변").click()
    assert page.rationale_edit.toPlainText() == "가상 근거"
    assert "필수 0개" in page.question_panel.summary.text() or "열린 질문이 없습니다" in page.question_panel.summary.text()

    skip_row = page.question_panel.rows[0]
    before = skip_row.question.id
    next(b for b in skip_row.findChildren(QPushButton) if b.text() == "건너뛰기").click()
    assert all(r.question.id != before for r in page.question_panel.rows)
