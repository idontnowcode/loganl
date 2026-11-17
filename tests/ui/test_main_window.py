"""메인 화면 틀 (UI-01·02·06·14·16, T-DONE-06, T-NA-08). offscreen으로 띄운다."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from app.ai.ollama_client import OllamaStatus
from app.bootstrap import initialize
from app.config import OllamaSettings
from app.ui.main_window import AUX_MENU, MainWindow
from app.ui.settings_dialog import SettingsDialog


def reachable(settings: OllamaSettings) -> OllamaStatus:
    return OllamaStatus(base_url=settings.base_url, reachable=True, version="0.0.1-test",
                        models=["test-model:7b"], configured_model=settings.model,
                        model_available=None if settings.model is None else settings.model == "test-model:7b")


def unreachable(settings: OllamaSettings) -> OllamaStatus:
    return OllamaStatus(base_url=settings.base_url, reachable=False, error="연결 거부(테스트)")


@pytest.fixture
def context(tmp_path: Path):
    ctx = initialize(tmp_path / "home", configure_logging=False)
    yield ctx
    ctx.close()


def make_window(qtbot, context, checker=reachable) -> MainWindow:
    window = MainWindow(context, status_checker=checker)
    qtbot.addWidget(window)
    return window


def test_three_main_tabs_in_plan_order(qtbot, context) -> None:
    window = make_window(qtbot, context)
    titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert titles == ["정상 로그 학습", "비정상 로그 학습", "신규 로그 분석"]


def test_tabs_hold_real_pages(qtbot, context) -> None:
    from PySide6.QtWidgets import QLabel, QPushButton

    from app.ui.abnormal_learning import AbnormalLearningPage
    from app.ui.normal_learning import NormalLearningPage

    window = make_window(qtbot, context)
    assert isinstance(window.tabs.widget(0), NormalLearningPage)
    assert isinstance(window.tabs.widget(1), AbnormalLearningPage)
    buttons = [b.text() for b in window.tabs.widget(1).findChildren(QPushButton)]
    assert "대응 매뉴얼 첨부…" in buttons  # CR-01
    from app.ui.log_analysis import LogAnalysisPage

    analysis = window.tabs.widget(2)
    assert isinstance(analysis, LogAnalysisPage)
    assert analysis.guidance_box.title().startswith("권장 대응")  # CR-01, UI-16: 결과 맨 위 권장 대응
    assert QLabel  # noqa: B018 (import 유지)


def test_aux_menu_has_five_items_and_only_settings_enabled(qtbot, context) -> None:
    window = make_window(qtbot, context)
    names = [name for name, _ in AUX_MENU]
    assert names == ["학습 사례 관리", "분석 이력", "운전 모드 관리", "설정", "백업 및 복원"]
    for name, phase in AUX_MENU:
        action = window.menu_actions[name]
        assert action.text().startswith(name)
        assert action.isEnabled() is (phase is None)


def test_status_bar_shows_db_and_data_folder(qtbot, context) -> None:
    window = make_window(qtbot, context)
    assert str(context.paths.root) in window.data_label.text()
    assert "스키마 v4" in window.db_label.text()


def test_ollama_status_connected(qtbot, context) -> None:
    window = make_window(qtbot, context)
    qtbot.waitUntil(lambda: "연결됨" in window.ollama_label.text(), timeout=3000)
    assert "test-model:7b" in window.ollama_label.toolTip()
    assert window.recheck_button.isEnabled()


def test_ollama_status_not_running(qtbot, context) -> None:  # T-NA-08
    window = make_window(qtbot, context, unreachable)
    qtbot.waitUntil(lambda: window.ollama_label.text() == "Ollama: 연결 안 됨", timeout=3000)
    assert "연결 거부" in window.ollama_label.toolTip()


def test_checker_exception_is_shown_not_raised(qtbot, context) -> None:
    def broken(settings: OllamaSettings) -> OllamaStatus:
        raise RuntimeError("테스트 실패")

    window = make_window(qtbot, context, broken)
    qtbot.waitUntil(lambda: window.ollama_label.text() == "Ollama: 확인 실패", timeout=3000)
    assert "테스트 실패" in window.ollama_label.toolTip()


def test_slow_check_does_not_block_window(qtbot, context) -> None:  # T-DONE-06
    release = threading.Event()

    def slow(settings: OllamaSettings) -> OllamaStatus:
        release.wait(5)
        return reachable(settings)

    started = time.perf_counter()
    window = make_window(qtbot, context, slow)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, f"창 생성이 상태 확인을 기다렸다 ({elapsed:.2f}s)"
    assert "확인 중" in window.ollama_label.text()
    assert not window.recheck_button.isEnabled()
    window.refresh_ollama_status()  # 진행 중에는 중복 실행하지 않는다
    release.set()
    qtbot.waitUntil(lambda: "연결됨" in window.ollama_label.text(), timeout=3000)


def test_warning_banner_shows_startup_warnings(qtbot, tmp_path: Path) -> None:
    home = tmp_path / "warn_home"
    (home / "config").mkdir(parents=True)
    (home / "config" / "settings.json").write_text("{broken", encoding="utf-8")
    ctx = initialize(home, configure_logging=False)
    try:
        window = make_window(qtbot, ctx)
        assert not window.warning_banner.isHidden()
        assert "기본값으로 시작" in window.warning_banner.text()
    finally:
        ctx.close()


def test_no_banner_without_warnings(qtbot, context) -> None:
    window = make_window(qtbot, context)
    assert window.warning_banner.isHidden()


def test_settings_dialog_rejects_external_url(qtbot, context) -> None:
    dialog = SettingsDialog(context.settings, default_data_root=context.bootstrap_dir)
    qtbot.addWidget(dialog)
    dialog.base_url_edit.setText("http://192.0.2.10:11434")
    dialog.accept()
    assert dialog.result_settings() is None
    assert "이 PC 안의 주소" in dialog.error_label.text()


def test_settings_dialog_saves_valid_values(qtbot, context) -> None:
    dialog = SettingsDialog(context.settings, default_data_root=context.bootstrap_dir, known_models=["test-model:7b"])
    qtbot.addWidget(dialog)
    dialog.model_combo.setCurrentText("test-model:7b")
    dialog.max_total_spin.setValue(250)
    dialog.accept()
    result = dialog.result_settings()
    assert result is not None
    assert result.ollama.model == "test-model:7b"
    assert result.import_.max_total_mb == 250
    assert result.data_root is None


def test_file_limit_spin_cannot_exceed_plan(qtbot, context) -> None:
    dialog = SettingsDialog(context.settings, default_data_root=context.bootstrap_dir)
    qtbot.addWidget(dialog)
    dialog.max_file_spin.setValue(50)
    assert dialog.max_file_spin.value() == 10


def test_apply_settings_persists_and_rechecks(qtbot, context) -> None:
    calls: list[str | None] = []

    def recording(settings: OllamaSettings) -> OllamaStatus:
        calls.append(settings.model)
        return reachable(settings)

    window = make_window(qtbot, context, recording)
    qtbot.waitUntil(lambda: len(calls) == 1, timeout=3000)
    qtbot.waitUntil(lambda: window.recheck_button.isEnabled(), timeout=3000)
    new = context.settings.model_copy(update={"ollama": context.settings.ollama.model_copy(update={"model": "test-model:7b"})})
    assert window.apply_settings(new) == []
    qtbot.waitUntil(lambda: calls[-1] == "test-model:7b", timeout=3000)
    saved = json.loads(context.settings_file.read_text(encoding="utf-8"))
    assert saved["ollama"]["model"] == "test-model:7b"
    qtbot.waitUntil(lambda: "test-model:7b" in window.ollama_label.text(), timeout=3000)


def test_about_text_mentions_offline_and_paths(qtbot, context) -> None:
    window = make_window(qtbot, context)
    text = window.about_text()
    assert str(context.paths.db_file) in text
    assert "인터넷" in text and "CR-01" in text


def test_unreachable_status_points_to_bundled_ai_launcher(qtbot, context, monkeypatch, tmp_path: Path) -> None:
    """CR-03: 휴대용 AI 시작 파일이 있는 배포본에서 서버가 꺼져 있으면 켜는 방법을 알려 준다."""
    launcher = tmp_path / "AI와 함께 시작.cmd"
    launcher.write_text("rem test", encoding="utf-8")
    monkeypatch.setattr("app.ui.main_window.bundled_ai_launcher", lambda: launcher)
    window = make_window(qtbot, context, unreachable)
    qtbot.waitUntil(lambda: "AI와 함께 시작" in window.ollama_label.text(), timeout=5000)
    assert str(launcher) in window.ollama_label.toolTip()

    window2 = make_window(qtbot, context, reachable)
    qtbot.waitUntil(lambda: window2.last_status is not None, timeout=5000)
    assert "AI와 함께 시작" not in window2.ollama_label.text()


def test_bundled_ai_launcher_absent_in_dev_run() -> None:
    from app.ui.main_window import bundled_ai_launcher
    assert bundled_ai_launcher() is None  # 개발 실행(frozen 아님)에서는 없다
