"""부분 구현 마무리: 입력 칸 자동 저장(STO-04), 세션 타임라인(UI-03·05, §4.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ai.ollama_client import OllamaStatus
from app.bootstrap import initialize
from app.core.importer import LogImporter
from app.learning.case_manager import CaseManager
from app.models.enums import OriginPurpose
from app.models.records import FileMeta, ImportRequest, SessionMode
from app.ui.main_window import MainWindow
from app.ui.normal_learning import NormalLearningPage
from tests.logdata import NORMAL_SEQUENCE, iso_lines, normal_log, write


@pytest.fixture
def ctx(tmp_path: Path):
    context = initialize(tmp_path / "home", configure_logging=False)
    yield context
    context.close()


def imported(ctx, paths, mode=SessionMode.SAME) -> list[int]:
    return LogImporter(ctx.database, ctx.paths, ctx.settings.import_).import_logs(ImportRequest(
        paths=list(paths), purpose=OriginPurpose.NORMAL_CASE, session_mode=mode, meta=FileMeta(log_role="R"))).log_file_ids


def test_typing_is_autosaved_without_reload(qtbot, ctx) -> None:
    # QtTest로 한글 키 입력을 흉내 내면 Qt 프로세스가 죽는다(Windows·PySide6 시험 도구 한계). 키 입력은 영문으로 본다.
    page = NormalLearningPage(ctx)
    qtbot.addWidget(page)
    qtbot.keyClicks(page.title_edit, "autosave test")
    qtbot.waitUntil(lambda: page.case_id is not None, timeout=5000)  # 제목을 쓰면 초안이 생긴다
    page.rationale_edit.setFocus()
    qtbot.keyClicks(page.rationale_edit, "virtual rationale")
    qtbot.waitUntil(lambda: "자동 저장" in page.status_label.text(), timeout=5000)
    qtbot.waitUntil(lambda: CaseManager(ctx.database).get(page.case_id).normal_rationale == "virtual rationale",
                    timeout=5000)
    assert page.rationale_edit.toPlainText() == "virtual rationale"  # 다시 불러오지 않아 입력이 그대로
    page.rationale_edit.setPlainText("한글 근거")  # 한글은 setPlainText로(textChanged 신호는 같다)
    qtbot.waitUntil(lambda: CaseManager(ctx.database).get(page.case_id).normal_rationale == "한글 근거", timeout=5000)


def test_empty_new_case_is_not_created(qtbot, ctx) -> None:
    page = NormalLearningPage(ctx)
    qtbot.addWidget(page)
    page.conditions_edit.setPlainText("조건만")  # 제목·로그 없이
    assert page.autosave() is False and CaseManager(ctx.database).list() == []


def test_close_flushes_pending_edits(qtbot, ctx, tmp_path: Path) -> None:
    status = lambda s: OllamaStatus(base_url=s.base_url, reachable=False)  # noqa: E731
    window = MainWindow(ctx, status_checker=status)
    qtbot.addWidget(window)
    page = window.entry_page
    ids = LogImporter(ctx.database, ctx.paths, ctx.settings.import_).import_logs(ImportRequest(
        paths=[write(tmp_path / "close.log", iso_lines(NORMAL_SEQUENCE))], purpose=OriginPurpose.NORMAL_CASE,
        session_mode=SessionMode.SEPARATE)).log_file_ids
    page.import_panel.set_log_file_ids(ids)
    page.normal_radio.setChecked(True)
    page.memo_edit.setPlainText("닫기 전 입력")  # 저장 버튼을 누르기 전에 닫는다
    window.close()
    assert [(c.operating_conditions, c.status) for c in CaseManager(ctx.database).list()] == [("닫기 전 입력", "APPROVED")]


def test_approved_case_is_not_autosaved(qtbot, ctx) -> None:
    case_id = CaseManager(ctx.database).create("NORMAL", "승인 사례", status="APPROVED",
                                               approved_at="2026-11-17T00:00:00.000Z", current_revision=1)
    page = NormalLearningPage(ctx)
    qtbot.addWidget(page)
    page.load_case(case_id)
    page.title_edit.setText("바뀌면 안 됨")
    page.mark_dirty()
    assert page.autosave() is False and CaseManager(ctx.database).get(case_id).title == "승인 사례"


def test_session_timeline_merges_same_session(qtbot, ctx, tmp_path: Path) -> None:
    a = write(tmp_path / "a.log", iso_lines(NORMAL_SEQUENCE[:2], tz="Z", step_ms=200))
    b = write(tmp_path / "b.log", iso_lines([("INFO", "TEST-SUB", "TEST-OTHER")], tz="Z", start_second=0))
    page = NormalLearningPage(ctx)
    qtbot.addWidget(page)
    page.import_panel.set_log_file_ids(imported(ctx, [a, b]))
    page.import_panel.views.setCurrentIndex(1)
    timeline = page.import_panel.timeline
    assert "한 시간축에 합쳤습니다" in timeline.note.text()
    files = [timeline.table.item(r, 2).text() for r in range(timeline.table.rowCount())]
    assert files.index("b.log") < files.index("a.log", files.index("a.log") + 1)  # 두 파일이 시각순으로 섞였다
    timeline._jump(0, 0)
    assert page.import_panel.views.currentIndex() == 0  # 원본 행으로 이동하면 이벤트 표로
    assert page.import_panel.viewer.selected_event() is not None


def test_session_timeline_keeps_sessions_apart(qtbot, ctx, tmp_path: Path) -> None:
    ids = imported(ctx, [normal_log(tmp_path / "a.log"), normal_log(tmp_path / "b.log", start_second=9)],
                   SessionMode.SEPARATE)
    page = NormalLearningPage(ctx)
    qtbot.addWidget(page)
    page.import_panel.set_log_file_ids(ids)
    page.import_panel.views.setCurrentIndex(1)
    timeline = page.import_panel.timeline
    assert timeline.session_combo.count() == 2
    assert "다른 수집 세션 1개는 합치지 않습니다" in timeline.note.text()
    assert timeline.table.rowCount() == 6  # 고른 세션의 로그 하나만
