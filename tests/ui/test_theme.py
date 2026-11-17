"""화면 테마(DESIGN.md): 전역 적용, 역할 속성, 상태 도장, 안내 띠 영역. offscreen으로 띄운다."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from app.ai.ollama_client import OllamaStatus
from app.bootstrap import initialize
from app.models.enums import CaseStatus
from app.ui.main_window import MainWindow
from app.ui.theme import STATUS_TONE, T, apply_theme, set_role, stylesheet


@pytest.fixture
def themed_app(qapp: QApplication):
    """전역 테마를 입히고, 다른 테스트에 번지지 않게 끝나면 되돌린다."""
    before = (qapp.styleSheet(), qapp.palette(), qapp.font(), qapp.style().name())
    apply_theme(qapp)
    yield qapp
    qapp.setStyleSheet(before[0])
    qapp.setPalette(before[1])
    qapp.setFont(before[2])
    qapp.setStyle(before[3])


def test_stylesheet_is_fully_rendered() -> None:
    sheet = stylesheet()
    assert "{t[" not in sheet and "{{" not in sheet  # 서식 자리표시가 남지 않음
    assert sheet.count("{") == sheet.count("}")
    assert T["accent"] in sheet and T["canvas"] in sheet


def test_apply_theme_sets_fusion_palette_and_sheet(themed_app: QApplication) -> None:
    # 스타일시트가 걸리면 style()은 감싼 객체라 이름으로 Fusion 여부를 볼 수 없다. 팔레트·시트로 확인한다.
    assert themed_app.styleSheet() == stylesheet()
    assert themed_app.palette().window().color().name().upper() == T["canvas"].upper()


def test_every_case_status_has_a_stamp_tone() -> None:
    assert set(STATUS_TONE) == {s.value for s in CaseStatus}
    assert set(STATUS_TONE.values()) <= {"ok", "warn", "bad", "neutral", "info"}


def test_set_role_marks_properties(qtbot, themed_app: QApplication) -> None:
    label = QLabel("도장")
    qtbot.addWidget(label)
    set_role(label, "stamp", "ok")
    assert (label.property("role"), label.property("tone")) == ("stamp", "ok")
    button = QPushButton("승인")
    qtbot.addWidget(button)
    set_role(button, variant="primary")
    assert button.property("variant") == "primary" and button.property("role") is None


def test_banner_area_hidden_when_no_banner(qtbot, tmp_path: Path) -> None:
    ctx = initialize(tmp_path / "home", configure_logging=False)
    try:
        window = MainWindow(ctx, status_checker=lambda s: OllamaStatus(
            base_url=s.base_url, reachable=True, version="t", models=["m:7b"],
            configured_model="m:7b", model_available=True))
        qtbot.addWidget(window)
        assert not ctx.warnings
        window.first_run_hint = lambda: ""
        window.refresh_hint()
        assert window.banners.isHidden()  # 빈 띠 영역이 자리를 차지하지 않음
        window.first_run_hint = lambda: "먼저 정상 로그를 학습하세요(테스트)"
        window.refresh_hint()
        assert not window.banners.isHidden() and not window.hint_banner.isHidden()
    finally:
        ctx.close()
