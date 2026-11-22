"""단계별 화면의 공용 부품 (변경 요청 CR-12): 왼쪽 메뉴, 단계 막대, 단계 화면 묶음.

색·간격은 theme.py의 역할(nav·nav-group·step·step-number)로만 칠한다.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.ui.theme import set_role


class SideNav(QFrame):
    """왼쪽 메뉴. 작업 항목은 화면을 바꾸고(checkable), 그 밖의 항목은 눌렀을 때 동작만 한다."""

    page_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        set_role(self, "nav")
        self.setFixedWidth(176)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(10, 12, 10, 12)
        self._layout.setSpacing(2)
        self.pages: list[QPushButton] = []
        self.actions: dict[str, QPushButton] = {}
        self.back_button: QPushButton | None = None
        self._groups = 0

    def add_back(self, handler: Callable[[], None]) -> QPushButton:
        """맨 위 '뒤로' 단추(CR-16 UX-04): 앞에 보던 화면(작업 화면 + 단계)으로. 이력이 없으면 흐리다."""
        button = QPushButton("‹  뒤로")
        button.setToolTip("앞에 보던 화면으로 돌아갑니다 (Alt+←)")
        button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setEnabled(False)
        set_role(button, "nav-back")
        button.clicked.connect(handler)
        self._layout.addWidget(button)
        self._layout.addSpacing(12)
        self.back_button = button
        return button

    def set_back_enabled(self, enabled: bool) -> None:
        if self.back_button is not None:
            self.back_button.setEnabled(enabled)

    def add_group(self, title: str) -> None:
        """섹션 머리: 작고 옅은 굵은 글씨, 앞 섹션과는 구분선과 넉넉한 여백으로 나눈다(항목과 헷갈리지 않게, 2026-11-22)."""
        label = QLabel(title)
        set_role(label, "nav-group")
        self._groups += 1
        if self._groups > 1:
            self._layout.addSpacing(12)
            rule = QFrame()
            set_role(rule, "nav-rule")
            self._layout.addWidget(rule)
            self._layout.addSpacing(10)
        self._layout.addWidget(label)
        self._layout.addSpacing(2)

    def _button(self, text: str) -> QPushButton:
        button = QPushButton(text)
        button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        set_role(button, "nav-item")
        self._layout.addWidget(button)
        return button

    def add_page(self, text: str) -> QPushButton:
        button = self._button(text)
        button.setCheckable(True)
        index = len(self.pages)
        button.clicked.connect(lambda: self.page_requested.emit(index))
        self.pages.append(button)
        self.actions[text] = button
        return button

    def add_action(self, text: str, handler: Callable[[], None]) -> QPushButton:
        button = self._button(text)
        button.clicked.connect(handler)
        self.actions[text] = button
        return button

    def finish(self) -> None:
        self._layout.addStretch(1)

    def set_current(self, index: int) -> None:
        for i, button in enumerate(self.pages):
            button.setChecked(i == index)


class StepBar(QWidget):
    """① → ② → ③ 단계 막대. 지금 단계는 강조, 아직 못 가는 단계는 흐리게 + 이유 툴팁."""

    step_requested = Signal(int)

    def __init__(self, names: Sequence[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.buttons: list[QPushButton] = []
        for index, name in enumerate(names):
            if index:
                arrow = QLabel("›")
                set_role(arrow, "muted")
                layout.addWidget(arrow)
            button = QPushButton(f"{index + 1}  {name}")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _c=False, i=index: self.step_requested.emit(i))
            set_role(button, "step")
            layout.addWidget(button)
            self.buttons.append(button)
        layout.addStretch(1)
        self.current = 0

    def set_state(self, current: int, locked: dict[int, str] | None = None) -> None:
        """locked: 단계 → 못 가는 이유."""
        locked = locked or {}
        self.current = current
        for index, button in enumerate(self.buttons):
            state = "current" if index == current else ("locked" if index in locked else "todo")
            button.setProperty("state", state)
            button.setEnabled(index not in locked)
            button.setToolTip(locked.get(index, ""))
            set_role(button)


class StepPages(QWidget):
    """단계 막대 + 단계 화면 + 아래 이동 줄. 화면마다 이전·다음 버튼을 따로 붙일 수 있다."""

    step_changed = Signal(int)

    def __init__(self, title: str, subtitle: str, names: Sequence[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.names = list(names)
        heading = QLabel(title)
        set_role(heading, "title")
        sub = QLabel(subtitle)
        sub.setWordWrap(True)
        set_role(sub, "muted")
        head = QHBoxLayout()
        head.addWidget(heading)
        head.addSpacing(12)
        head.addWidget(sub, 1)
        self.bar = StepBar(names)
        self.bar.step_requested.connect(self.go)
        self.stack = QStackedWidget()
        self._locks: Callable[[], dict[int, str]] = lambda: {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)
        layout.addLayout(head)
        layout.addWidget(self.bar)
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        set_role(line, "rule")
        layout.addWidget(line)
        layout.addWidget(self.stack, 1)

    def add(self, widget: QWidget) -> int:
        return self.stack.addWidget(widget)

    def set_locks(self, locks: Callable[[], dict[int, str]]) -> None:
        self._locks = locks
        self.refresh()

    def current(self) -> int:
        return self.stack.currentIndex()

    def go(self, index: int) -> bool:
        if index in self._locks():
            return False
        self.stack.setCurrentIndex(index)
        self.refresh()
        self.step_changed.emit(index)
        return True

    def refresh(self) -> None:
        self.bar.set_state(self.stack.currentIndex(), self._locks())


def nav_row(back: QPushButton | None, *right: QWidget) -> QHBoxLayout:
    """단계 화면 아래 이동 줄: 왼쪽 '이전', 오른쪽 버튼들."""
    row = QHBoxLayout()
    row.setContentsMargins(0, 6, 0, 0)
    if back is not None:
        row.addWidget(back)
    row.addStretch(1)
    for widget in right:
        row.addWidget(widget)
    return row
