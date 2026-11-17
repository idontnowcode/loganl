"""CR-04 참고 자료 관리 창과 분석 화면의 참고 자료 칸 (REF-01~04). 모든 코드·설명은 가상이다."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from app.ai.ollama_client import OllamaStatus
from app.bootstrap import initialize
from app.core.importer import LogImporter
from app.models.enums import OriginPurpose
from app.models.records import ImportRequest, SessionMode
from app.ui.log_analysis import LogAnalysisPage
from app.ui.main_window import MainWindow
from app.ui.reference_manager import ReferenceManagerDialog
from tests.logdata import abnormal_log
from tests.refdata import write_xlsx


@pytest.fixture
def ctx(tmp_path: Path):
    context = initialize(tmp_path / "home", configure_logging=False)
    yield context
    context.close()


def test_upload_review_and_approve(qtbot, ctx, tmp_path: Path) -> None:
    dialog = ReferenceManagerDialog(ctx)
    qtbot.addWidget(dialog)
    assert dialog.doc_table.rowCount() == 0 and not dialog.approve_button.isEnabled()
    doc_id = dialog.import_path(write_xlsx(tmp_path / "가상 코드표.xlsx"))
    assert doc_id and "코드 항목 3개" in dialog.message.text()
    assert dialog.doc_table.item(0, 2).text() == "초안" and dialog.entry_table.rowCount() == 3
    assert dialog.roles_box.isVisibleTo(dialog)  # 표 문서는 열 역할을 고를 수 있다
    assert dialog.role_combos["action"].currentData() == 4
    assert "TEST-E001" in dialog.preview.toPlainText()

    dialog.entry_table.item(2, 0).setCheckState(Qt.CheckState.Unchecked)  # 세 번째 항목 제외
    dialog.entry_table.item(0, 3).setText("고친 가상 설명")
    entries = {r["code"]: r for r in dialog.library.entries(doc_id)}
    assert entries["TEST-W010"]["status"] == "EXCLUDED" and entries["TEST-E001"]["description"] == "고친 가상 설명"
    assert dialog.doc_table.item(0, 3).text() == "2"

    dialog.approve()
    assert "승인했습니다" in dialog.message.text() and dialog.doc_table.item(0, 2).text() == "승인됨"
    assert not dialog.add_entry_button.isEnabled() and dialog.archive_button.isEnabled()
    assert not (dialog.entry_table.item(0, 3).flags() & Qt.ItemFlag.ItemIsEditable)  # 승인 뒤에는 고칠 수 없다


def test_menu_opens_reference_manager(qtbot, ctx) -> None:
    window = MainWindow(ctx, status_checker=lambda s: OllamaStatus(base_url=s.base_url, reachable=False))
    qtbot.addWidget(window)
    window.menu_actions["참고 자료 관리"].trigger()
    assert isinstance(window.reference_dialog, ReferenceManagerDialog)
    window.reference_dialog.reject()


def test_analysis_page_shows_reference_box(qtbot, ctx, tmp_path: Path) -> None:
    from app.learning.reference_library import ReferenceLibrary

    ids = LogImporter(ctx.database, ctx.paths, ctx.settings.import_).import_logs(ImportRequest(
        paths=[abnormal_log(tmp_path / "a.log")], purpose=OriginPurpose.ANALYSIS,
        session_mode=SessionMode.SEPARATE)).log_file_ids
    page = LogAnalysisPage(ctx)
    qtbot.addWidget(page)
    page.import_panel.set_log_file_ids(ids)
    page.use_ai.setChecked(False)
    page.start_analysis()
    qtbot.waitUntil(lambda: page.result is not None, timeout=10_000)
    assert "승인된 참고 자료가 없습니다" in page.reference_message.text() and page.reference_list.isHidden()

    library = ReferenceLibrary(ctx.database, ctx.paths, ctx.settings.import_)
    library.approve(library.import_file(write_xlsx(tmp_path / "codes.xlsx")).document_id)
    page.result = None
    page.start_analysis()
    qtbot.waitUntil(lambda: page.result is not None, timeout=10_000)
    assert page.reference_list.count() == 1 and not page.reference_list.isHidden()
    text = page.reference_list.item(0).text()
    assert "TEST-E001 ×2" in text and "조치: 가상 조치 A" in text and "출처 codes · 코드표 2행" in text
    assert page.jump_to_reference(page.reference_list.item(0))
