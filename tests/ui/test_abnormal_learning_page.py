"""비정상 학습 화면 (UI-04·15, DOD-06·08, CR-01)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.bootstrap import initialize
from app.core.importer import LogImporter
from app.models.enums import OriginPurpose
from app.models.records import ImportRequest, SessionMode
from app.ui.abnormal_learning import AbnormalLearningPage
from tests.logdata import abnormal_log
from tests.unit.test_guide_structurer import GUIDE, MANUAL


@pytest.fixture
def ctx(tmp_path: Path):
    context = initialize(tmp_path / "home", configure_logging=False)
    yield context
    context.close()


def doc(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    return path


def test_register_abnormal_case_with_guide_and_manual(qtbot, ctx, tmp_path: Path) -> None:
    ids = LogImporter(ctx.database, ctx.paths, ctx.settings.import_).import_logs(ImportRequest(
        paths=[abnormal_log(tmp_path / "a.log"), abnormal_log(tmp_path / "b.log", start_second=40)],
        purpose=OriginPurpose.ABNORMAL_CASE, session_mode=SessionMode.SEPARATE)).log_file_ids
    page = AbnormalLearningPage(ctx)
    qtbot.addWidget(page)
    page.import_panel.set_log_file_ids(ids)
    page.title_edit.setText("화면 비정상 사례")
    assert page.attach_file(doc(tmp_path, "guide.md", GUIDE), "DIAGNOSTIC_GUIDE")
    assert "TEST-E001" in page.attach_status.text()
    assert page.attach_file(doc(tmp_path, "manual.txt", MANUAL), "RESPONSE_MANUAL")
    assert page.attachment_list.count() == 2
    assert page.link_list.count() == 1  # 오류 코드 TEST-E001은 가이드 원문에만 나온다
    page.attachment_list.setCurrentRow(0)
    assert "가상 진단 가이드" in page.preview.toPlainText()

    page.fill_from_guide()
    assert "TEST-FAULT" in page.symptom_edit.toPlainText()
    page.cause_combo.setCurrentIndex(page.cause_combo.findData("UNKNOWN"))
    page.set_all_item_decisions("APPROVED")
    page.approve()
    assert "승인했습니다" in page.message_label.text(), page.message_label.text()

    reopened = AbnormalLearningPage(ctx)
    qtbot.addWidget(reopened)
    reopened.load_case(page.case_id)
    assert reopened.attachment_list.count() == 2
    assert reopened.cause_combo.currentData() == "UNKNOWN"
    assert all(reopened.item_table.cellWidget(r, 0).currentData() == "APPROVED"
               for r in range(reopened.item_table.rowCount()))
    assert "[CONFIRMED]" in reopened.link_list.item(0).text()


def test_bad_attachment_is_reported(qtbot, ctx, tmp_path: Path) -> None:
    page = AbnormalLearningPage(ctx)
    qtbot.addWidget(page)
    assert not page.attach_file(doc(tmp_path, "guide.pdf", "x"), "DIAGNOSTIC_GUIDE")
    assert "확장자" in page.attach_status.text()
