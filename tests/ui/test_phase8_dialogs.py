"""Phase 8 화면: 분석 이력·학습 사례 관리·승인된 지식·백업, 분석에서 학습 사례 등록, 메뉴 활성화."""

from __future__ import annotations

from pathlib import Path

from app.ai.ollama_client import OllamaStatus
from app.learning.case_manager import CaseManager
from app.models.enums import OriginPurpose
from app.ui.analysis_history import AnalysisHistoryDialog
from app.ui.backup_dialog import BackupDialog
from app.ui.case_review import CaseReviewDialog
from app.ui.knowledge_manager import approved_knowledge_text
from app.ui.main_window import MainWindow
from tests.integration.test_analysis import ctx, knowledge, load  # noqa: F401
from tests.logdata import abnormal_log


def quiet(settings) -> OllamaStatus:
    return OllamaStatus(base_url=settings.base_url, reachable=False, error="테스트")


def run_analysis(qtbot, window: MainWindow, ctx, knowledge, tmp_path: Path) -> None:  # noqa: F811
    page = window.analysis_page
    page.refresh_modes()
    page.import_panel.set_log_file_ids(load(ctx, [abnormal_log(tmp_path / "bad.log", start_second=70)],
                                            OriginPurpose.ANALYSIS))
    page.mode_combo.setCurrentIndex(page.mode_combo.findData(knowledge[0]))
    page.mode_confirm.setChecked(True)
    page.use_ai.setChecked(False)
    with qtbot.waitSignal(page.analysis_done, timeout=15_000):
        page.start_analysis()


def test_all_menus_enabled(qtbot, ctx) -> None:  # noqa: F811
    window = MainWindow(ctx, status_checker=quiet)
    qtbot.addWidget(window)
    for name in ("학습 사례 관리", "분석 이력", "운전 모드 관리", "설정", "백업 및 복원", "승인된 지식 보기"):
        assert window.menu_actions[name].isEnabled(), name


def test_report_and_register_case_from_analysis(qtbot, ctx, knowledge, tmp_path: Path) -> None:  # noqa: F811
    window = MainWindow(ctx, status_checker=quiet)
    qtbot.addWidget(window)
    run_analysis(qtbot, window, ctx, knowledge, tmp_path)
    page = window.analysis_page
    rel_json, rel_html = page.save_report()
    assert (ctx.paths.root / rel_html).exists() and "보고서를 저장" in page.report_label.text()
    page.request_case("ABNORMAL")
    assert window.tabs.currentWidget() is window.abnormal_page
    case = window.abnormal_page.detail.case
    # 새 비정상 초안은 진단 가이드·원인 확정 여부가 없어 질문 엔진이 '정보 필요'로 표시한다
    assert case.status == "NEEDS_INFORMATION" and case.title.startswith(f"R-{page.result.run_id:04d}")
    assert window.abnormal_page.import_panel.log_file_ids() == page.import_panel.log_file_ids()


def test_history_dialog_reopens_and_saves(qtbot, ctx, knowledge, tmp_path: Path) -> None:  # noqa: F811
    window = MainWindow(ctx, status_checker=quiet)
    qtbot.addWidget(window)
    run_analysis(qtbot, window, ctx, knowledge, tmp_path)
    dialog = AnalysisHistoryDialog(ctx)
    qtbot.addWidget(dialog)
    assert dialog.table.rowCount() == 1
    assert "권장 대응 — 승인된 대응 매뉴얼" in dialog.view.toPlainText()  # 저장된 결과 재확인
    paths = dialog.save_selected()
    assert paths and (ctx.paths.root / paths[0]).exists()


def test_case_review_actions(qtbot, ctx, knowledge) -> None:  # noqa: F811
    opened = []
    draft = CaseManager(ctx.database).create("NORMAL", "관리 화면 초안")
    dialog = CaseReviewDialog(ctx, lambda t, i: opened.append((t, i)))
    qtbot.addWidget(dialog)
    assert dialog.table.rowCount() == 3
    approved_row = next(r for r, c in enumerate(dialog.rows) if c.status == "APPROVED")
    dialog.table.selectRow(approved_row)
    assert "개정본 1" in dialog.revisions.toPlainText()
    dialog.archive_selected()
    assert any(c.status == "ARCHIVED" for c in dialog.rows)
    draft_row = next(r for r, c in enumerate(dialog.rows) if c.id == draft)
    dialog.table.selectRow(draft_row)
    dialog.delete_selected()
    assert all(c.id != draft for c in dialog.rows)
    dialog.table.selectRow(0)
    dialog.open_selected()
    assert opened and opened[0][1] == dialog.rows[0].id


def test_knowledge_text_and_backup_dialog(qtbot, ctx, knowledge) -> None:  # noqa: F811
    text = approved_knowledge_text(ctx.database)
    assert "승인 패턴" in text and "매뉴얼" in text
    dialog = BackupDialog(ctx)
    qtbot.addWidget(dialog)
    path = dialog.backup_now()
    assert path and path.exists() and "백업했습니다" in dialog.message.text()
    blocked = BackupDialog(ctx, can_restore=lambda: False)
    qtbot.addWidget(blocked)
    assert not blocked.restore_from(path) and "진행 중인 작업" in blocked.message.text()
    assert dialog.restore_from(path) and dialog.restored
