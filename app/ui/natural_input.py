"""자유 글 입력 부품 (변경 요청 CR-05): 글 칸 + 'AI로 정리' + 정리된 내용, 접는 구역, 안내 줄."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.ui.theme import set_role

MISSING_TEXT = "빠짐 — 승인에 필요"


class StatusLine(QLabel):
    """동작 결과 안내 줄. 비어 있으면 자리를 차지하지 않는다."""

    def __init__(self, role: str = "hint") -> None:
        super().__init__()
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        set_role(self, role)
        self.hide()

    def setText(self, text: str) -> None:  # noqa: N802 (Qt 이름 그대로)
        super().setText(text)
        self.setVisible(bool(text))

    def clear(self) -> None:
        self.setText("")


class Collapsible(QWidget):
    """제목 줄을 눌러 펴고 접는 구역. 안의 위젯은 지우지 않고 숨기기만 한다."""

    def __init__(self, title: str, content: QWidget, expanded: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.content = content
        self.toggle = QToolButton()
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        set_role(self.toggle, "disclosure")
        # 좁은 화면에서 세로로 눌려 글자 아래가 잘리지 않게(1366×768 점검)
        self.toggle.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.toggle.toggled.connect(self.set_expanded)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.toggle)
        layout.addWidget(content)
        self.set_expanded(expanded)

    def set_expanded(self, expanded: bool) -> None:
        self.toggle.blockSignals(True)
        self.toggle.setChecked(expanded)
        self.toggle.blockSignals(False)
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.content.setVisible(expanded)

    def is_expanded(self) -> bool:
        return self.toggle.isChecked()

    def showEvent(self, event) -> None:  # noqa: N802 (Qt 이름 그대로)
        # 스타일이 입혀진 뒤의 크기로 제목 줄 높이를 정한다(스타일시트 여백 때문에 기본 높이가 3px 모자람)
        self.toggle.setFixedHeight(max(self.toggle.sizeHint().height(), self.toggle.minimumSizeHint().height()))
        super().showEvent(event)


@dataclass
class SummaryRow:
    label: str
    value: str | None
    required: bool = False  # 비어 있으면 '빠짐 — 승인에 필요'
    pending: str | None = None  # 값 옆 도장(예: '확인 전')
    action: QPushButton | None = None  # 도장 옆 버튼(예: '이 모드가 맞음')


class StoryBox(QGroupBox):
    """자유 글 칸과 정리된 내용. 값이 없는 선택 칸은 보이지 않는다(칸 강박을 줄이려는 것)."""

    def __init__(self, title: str, placeholder: str, hint: str, parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        self.edit = QPlainTextEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.setMinimumHeight(110)
        self.edit.setMaximumHeight(170)  # 정리된 내용이 첫 화면에 보이도록 글 칸이 남는 자리를 다 먹지 않게
        self.edit.setTabChangesFocus(True)
        self.organize_button = QPushButton("AI로 정리")
        self.organize_button.setToolTip("내 글에서 필요한 칸에 맞는 부분을 찾아 옮겨 적습니다. 새 내용을 지어내지 않습니다.")
        hint_label = QLabel(hint)
        hint_label.setWordWrap(True)
        set_role(hint_label, "muted")
        row = QHBoxLayout()
        row.addWidget(hint_label, 1)
        row.addWidget(self.organize_button, 0, Qt.AlignmentFlag.AlignTop)
        self.result_label = StatusLine()
        self.summary_title = QLabel("정리된 내용")
        set_role(self.summary_title, "cell-label")
        self.summary_grid = QGridLayout()
        self.summary_grid.setHorizontalSpacing(12)
        self.summary_grid.setVerticalSpacing(6)
        self.summary_grid.setColumnStretch(1, 1)
        self._summary_widgets: list[QWidget] = []
        self._keep: set[QWidget] = set()

        layout = QVBoxLayout(self)
        layout.addWidget(self.edit)
        layout.addLayout(row)
        layout.addWidget(self.result_label)
        layout.addWidget(self.summary_title)
        layout.addLayout(self.summary_grid)

    def text(self) -> str:
        return self.edit.toPlainText().strip()

    def set_summary(self, rows: list[SummaryRow]) -> None:
        for action in self._keep:  # 버튼은 다시 쓰므로 지우는 줄에서 먼저 빼 둔다
            action.hide()
            action.setParent(self)
        for widget in self._summary_widgets:
            self.summary_grid.removeWidget(widget)
            widget.hide()  # 지워지기 전까지 옛 줄이 새 줄과 겹쳐 보이지 않게
            widget.deleteLater()
        self._summary_widgets = []
        shown = [r for r in rows if r.value or r.required]
        for index, row in enumerate(shown):
            name = QLabel(row.label)
            set_role(name, "cell-label")
            self._place(name, index, 0, Qt.AlignmentFlag.AlignTop)
            if row.value:
                value = QLabel(row.value)
                value.setWordWrap(True)  # 칸 너비를 다 쓰게 정렬을 주지 않는다(정렬을 주면 좁게 접혀 한 줄 높이에 잘림)
                value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                self._place(value, index, 1)
            else:
                value = QLabel(MISSING_TEXT)
                set_role(value, "stamp", "warn")
                self._place(value, index, 1, Qt.AlignmentFlag.AlignLeft)
            if row.pending:
                tail = QWidget()
                tail_layout = QHBoxLayout(tail)
                tail_layout.setContentsMargins(0, 0, 0, 0)
                stamp = QLabel(row.pending)
                set_role(stamp, "stamp", "info")
                tail_layout.addWidget(stamp)
                if row.action is not None:
                    self._keep.add(row.action)
                    tail_layout.addWidget(row.action)
                    row.action.show()
                self._place(tail, index, 2)
        self.summary_title.setVisible(bool(shown))

    def _place(self, widget: QWidget, row: int, column: int, align: Qt.AlignmentFlag | None = None) -> None:
        if align is None:
            self.summary_grid.addWidget(widget, row, column)
        else:
            self.summary_grid.addWidget(widget, row, column, align)
        self._summary_widgets.append(widget)

    def summary_texts(self) -> list[str]:
        """테스트·복사용: 정리된 내용을 '라벨: 값' 줄로."""
        lines = []
        for r in range(self.summary_grid.rowCount()):
            name = self.summary_grid.itemAtPosition(r, 0)
            value = self.summary_grid.itemAtPosition(r, 1)
            if name and value and isinstance(name.widget(), QLabel) and isinstance(value.widget(), QLabel):
                lines.append(f"{name.widget().text()}: {value.widget().text()}")
        return lines
