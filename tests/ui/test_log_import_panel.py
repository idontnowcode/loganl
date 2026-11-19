"""로그 입력 패널·뷰어 (Phase 2 "복수 로그를 읽고 데이터 손실 없이 저장·표시", UI-06·07·10·11·12, CR-10 LI-11~13)."""

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


def add_and_wait(qtbot, panel: LogImportPanel, paths: list[Path]) -> None:
    with qtbot.waitSignal(panel.import_finished, timeout=10_000):
        panel.add_paths(paths)


def test_drop_imports_right_away_without_duplicates(qtbot, panel: LogImportPanel, tmp_path: Path) -> None:  # LI-11
    a = normal_log(tmp_path / "a.log")
    with qtbot.waitSignal(panel.import_finished, timeout=10_000):
        panel.drop_zone.paths_dropped.emit([a, a])  # 끌어 놓기 = 바로 가져오기(대기 목록·'가져오기' 단추 없음)
    assert len(panel.log_file_ids()) == 1 and panel.file_table.rowCount() == 1
    assert panel.pending_paths() == [] and not hasattr(panel, "import_button")
    add_and_wait(qtbot, panel, [a])  # 같은 파일을 다시 넣어도 한 줄
    assert panel.file_table.rowCount() == 1 and "이미 있던 파일 1개" in panel.message_label.text()


def test_import_shows_files_and_events(qtbot, panel: LogImportPanel, tmp_path: Path) -> None:
    panel.session_combo.setCurrentIndex(0)  # 같은 수집 세션 (옵션은 파일을 넣기 전에)
    assert panel.build_request().session_mode == SessionMode.SAME
    add_and_wait(qtbot, panel, [normal_log(tmp_path / "a.log"), abnormal_log(tmp_path / "b.log"),
                                write(tmp_path / "x.csv", ["no"])])
    assert len(panel.log_file_ids()) == 2
    assert panel.file_table.rowCount() == 2
    assert panel.file_table.item(0, 0).text() == "a.log"
    assert panel.files_title.text() == "올라온 로그 (2개)"
    assert "거부: x.csv" in panel.message_label.text()  # 거부된 파일은 안내 줄에
    assert panel.pending_paths() == [] and not panel.cancel_button.isEnabled()
    assert len({r.session_id for r in panel.records()}) == 1
    assert panel.viewer.model.total == 0  # 원문 보기는 접혀 있어 이벤트를 읽지 않는다(LI-13)
    panel.views_section.toggle.click()
    assert panel.viewer.model.total == 6


def test_files_added_while_importing_follow(qtbot, panel: LogImportPanel, tmp_path: Path) -> None:  # LI-11
    a, b = normal_log(tmp_path / "a.log"), abnormal_log(tmp_path / "b.log")
    panel.add_paths([a])
    assert panel.runner.busy
    panel.add_paths([b])  # 가져오는 중 → 끝나면 이어서
    assert panel.pending_paths() == [b]
    qtbot.waitUntil(lambda: len(panel.log_file_ids()) == 2 and not panel.runner.busy, timeout=10_000)
    assert [r.original_name for r in panel.records()] == ["a.log", "b.log"]


def test_viewer_filter_and_go_to_line(qtbot, panel: LogImportPanel, tmp_path: Path) -> None:
    add_and_wait(qtbot, panel, [abnormal_log(tmp_path / "b.log")])
    panel.views_section.toggle.click()  # 원문 보기 펴기
    viewer = panel.viewer
    viewer.filter_edit.setText("TEST-E001")
    viewer.apply_filter()
    assert viewer.model.total == 2
    assert viewer.go_to_line(panel.log_file_ids()[0], 2)  # 필터에 안 걸리는 행 → 필터를 풀고 이동
    assert viewer.filter_edit.text() == ""
    selected = viewer.selected_event()
    assert selected is not None and selected.line_no == 2
    assert "2 | 2026-11-16 10:00:00.000 INFO TEST-CTRL: TEST-START" in viewer.raw_view.toPlainText()


def test_show_line_opens_folded_views(qtbot, panel: LogImportPanel, tmp_path: Path) -> None:  # LI-13
    add_and_wait(qtbot, panel, [abnormal_log(tmp_path / "b.log")])
    assert not panel.views_section.is_expanded() and panel.views.isHidden()
    assert panel.show_line(panel.log_file_ids()[0], 2)
    assert panel.views_section.is_expanded() and panel.views.currentIndex() == 0
    assert panel.viewer.selected_event().line_no == 2


def test_remove_from_panel_and_set_ids(qtbot, panel: LogImportPanel, tmp_path: Path) -> None:
    add_and_wait(qtbot, panel, [normal_log(tmp_path / "a.log"), abnormal_log(tmp_path / "b.log")])
    ids = panel.log_file_ids()
    panel.file_table.selectRow(0)
    with qtbot.waitSignal(panel.removing) as removing, qtbot.waitSignal(panel.files_changed):
        panel._remove_selected()
    assert removing.args == [[ids[0]]]
    assert panel.log_file_ids() == ids[1:]
    panel.set_log_file_ids(ids)
    assert panel.file_table.rowCount() == 2


def test_clear_whole_list(qtbot, context, tmp_path: Path) -> None:  # LI-12
    panel = LogImportPanel(context, OriginPurpose.ANALYSIS, allow_clear=True)
    qtbot.addWidget(panel)
    add_and_wait(qtbot, panel, [normal_log(tmp_path / "a.log"), abnormal_log(tmp_path / "b.log")])
    ids = panel.log_file_ids()
    assert not panel.clear_button.isHidden()
    with qtbot.waitSignal(panel.removing) as removing:
        panel.clear_button.click()
    assert removing.args == [ids] and panel.log_file_ids() == [] and panel.file_table.rowCount() == 0
    assert "그대로" in panel.message_label.text()
    from contextlib import closing

    from app.storage.repositories import LogFileRepository

    with closing(context.database.connect()) as conn:
        assert len(LogFileRepository(conn).get_many(ids)) == 2  # DB의 로그는 지우지 않는다


def test_clear_button_only_where_asked(panel: LogImportPanel) -> None:
    assert panel.clear_button.isHidden()  # '자세히 편집'의 사례 로그 목록에는 없음


def test_status_column_per_log(qtbot, context, tmp_path: Path) -> None:  # NA-20 목록 칸
    panel = LogImportPanel(context, OriginPurpose.ANALYSIS, status_column="분석 결과")
    qtbot.addWidget(panel)
    add_and_wait(qtbot, panel, [normal_log(tmp_path / "a.log"), abnormal_log(tmp_path / "b.log")])
    assert panel.file_table.horizontalHeaderItem(1).text() == "분석 결과"
    assert panel.file_table.item(1, 1).text() == "—"
    panel.set_status(panel.log_file_ids()[1], "비정상 의심", "warn", "풀이")
    assert panel.file_table.item(1, 1).text() == "비정상 의심" and panel.file_table.item(1, 1).toolTip() == "풀이"
    with qtbot.waitSignal(panel.current_file_changed) as changed:
        panel.select_file(panel.log_file_ids()[1])
    assert changed.args == [panel.log_file_ids()[1]]


def test_empty_request_message(panel: LogImportPanel) -> None:
    panel.start_import()
    assert "끌어 놓거나" in panel.message_label.text()


def test_import_options_are_folded(panel: LogImportPanel) -> None:  # CR-07 LI-10
    assert not panel.options_section.is_expanded() and panel.options_section.content.isHidden()
    assert panel.session_combo.parent() is not None  # 지우지 않고 접어 두었다
    panel.options_section.toggle.click()
    assert not panel.options_section.content.isHidden()
