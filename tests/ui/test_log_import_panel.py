"""로그 입력 패널·뷰어 (Phase 2 "복수 로그를 읽고 데이터 손실 없이 저장·표시", UI-06·07·10·11·12)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.bootstrap import initialize
from app.models.enums import OriginPurpose
from app.models.records import SessionMode
from app.ui.log_import_panel import LogImportPanel
from tests.logdata import abnormal_log, normal_log, write


@pytest.fixture
def context(tmp_path: Path):
    ctx = initialize(tmp_path / "home", configure_logging=False)
    yield ctx
    ctx.close()


@pytest.fixture
def panel(qtbot, context) -> LogImportPanel:
    widget = LogImportPanel(context, OriginPurpose.ABNORMAL_CASE)
    qtbot.addWidget(widget)
    return widget


def test_drop_adds_paths_without_duplicates(panel: LogImportPanel, tmp_path: Path) -> None:
    a = normal_log(tmp_path / "a.log")
    panel.pending.paths_dropped.emit([a, a])
    panel.add_paths([a])
    assert panel.pending_paths() == [a]


def test_import_shows_files_and_events(qtbot, panel: LogImportPanel, tmp_path: Path) -> None:
    panel.add_paths([normal_log(tmp_path / "a.log"), abnormal_log(tmp_path / "b.log"), write(tmp_path / "x.csv", ["no"])])
    panel.session_combo.setCurrentIndex(0)  # 같은 수집 세션
    assert panel.build_request().session_mode == SessionMode.SAME
    with qtbot.waitSignal(panel.import_finished, timeout=10_000):
        panel.start_import()
    assert len(panel.log_file_ids()) == 2
    assert panel.file_table.rowCount() == 2
    assert panel.file_table.item(0, 0).text() == "a.log"
    assert panel.viewer.model.total == 6
    assert "거부: x.csv" in panel.message_label.text()
    assert panel.pending_paths() == [tmp_path / "x.csv"]  # 거부된 파일만 대기 목록에 남는다
    assert panel.import_button.isEnabled() and not panel.cancel_button.isEnabled()


def test_viewer_filter_and_go_to_line(qtbot, panel: LogImportPanel, tmp_path: Path) -> None:
    panel.add_paths([abnormal_log(tmp_path / "b.log")])
    with qtbot.waitSignal(panel.import_finished, timeout=10_000):
        panel.start_import()
    viewer = panel.viewer
    viewer.filter_edit.setText("TEST-E001")
    viewer.apply_filter()
    assert viewer.model.total == 2
    assert viewer.go_to_line(panel.log_file_ids()[0], 2)  # 필터에 안 걸리는 행 → 필터를 풀고 이동
    assert viewer.filter_edit.text() == ""
    selected = viewer.selected_event()
    assert selected is not None and selected.line_no == 2
    assert "2 | 2026-11-16 10:00:00.000 INFO TEST-CTRL: TEST-START" in viewer.raw_view.toPlainText()


def test_remove_from_panel_and_set_ids(qtbot, panel: LogImportPanel, tmp_path: Path) -> None:
    panel.add_paths([normal_log(tmp_path / "a.log"), abnormal_log(tmp_path / "b.log")])
    with qtbot.waitSignal(panel.import_finished, timeout=10_000):
        panel.start_import()
    ids = panel.log_file_ids()
    with qtbot.waitSignal(panel.files_changed):
        panel.file_table.selectRow(0)
        panel._remove_selected()
    assert panel.log_file_ids() == ids[1:]
    panel.set_log_file_ids(ids)
    assert panel.file_table.rowCount() == 2


def test_empty_request_message(panel: LogImportPanel) -> None:
    panel.start_import()
    assert "먼저 추가" in panel.message_label.text()


def test_import_options_are_folded(panel: LogImportPanel) -> None:  # CR-07 LI-10
    assert not panel.options_section.is_expanded() and panel.options_section.content.isHidden()
    assert panel.session_combo.parent() is not None  # 지우지 않고 접어 두었다
    panel.options_section.toggle.click()
    assert not panel.options_section.content.isHidden()
