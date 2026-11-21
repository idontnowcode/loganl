"""'예상 에러 메시지' 칸 (변경 요청 CR-11). 신규 분석 결과와 로그 입력 탭이 같이 쓴다.
줄은 로그 원문 그대로이고, 두 번 누르면 왼쪽 로그의 그 행으로 간다.

CR-12: 줄마다 글 상자(QLabel)로 그린다. 목록 위젯(QListWidget)은 칸 너비가 바뀌면 줄바꿈 높이를 다시 계산하지 못해
1366×768에서 줄 끝이 '…'로 잘렸다.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QGroupBox, QLabel, QVBoxLayout

from app.analysis.error_messages import ErrorMessage, message_line
from app.ui.theme import set_role

GROUP_TONE = {"ERROR": "bad", "DIFFERENT": "warn", "NEW_WARNING": "warn", "WARNING": "warn", "MISSING": "neutral"}


class MessageRow(QLabel):
    """예상 에러 한 줄. 두 번 누르면 원본 행으로."""

    double_clicked = Signal()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 (Qt 이름 그대로)
        self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)


class ErrorMessagesBox(QGroupBox):
    go_to = Signal(int, int)  # (log_file_id, 행)

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(title, parent)
        self.intro = QLabel()
        self.intro.setWordWrap(True)
        set_role(self.intro, "muted")
        self.rows = QFrame()
        set_role(self.rows, "message-rows")
        self.rows_layout = QVBoxLayout(self.rows)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(0)
        layout = QVBoxLayout(self)
        layout.addWidget(self.intro)
        layout.addWidget(self.rows)
        self.messages: list[ErrorMessage] = []
        self.labels: list[MessageRow] = []

    def show_messages(self, messages: Sequence[ErrorMessage], intro: str, empty: str) -> None:
        self.messages = list(messages)
        for label in self.labels:  # 이전 줄은 바로 숨겨 떼어 낸다(deleteLater만 하면 잠깐 겹쳐 보임)
            self.rows_layout.removeWidget(label)
            label.hide()
            label.setParent(None)
            label.deleteLater()
        self.labels = []
        for index, message in enumerate(self.messages):
            row = MessageRow(message_line(message))
            row.setWordWrap(True)
            row.setTextFormat(Qt.TextFormat.PlainText)
            row.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            set_role(row, "message-row", GROUP_TONE.get(message.group, "neutral"))
            if message.line is not None:
                row.setToolTip("두 번 누르면 원본 로그의 그 행으로 이동합니다.")
                row.setCursor(Qt.CursorShape.PointingHandCursor)
            row.double_clicked.connect(lambda i=index: self.jump(i))
            self.rows_layout.addWidget(row)
            self.labels.append(row)
        self.intro.setText(intro if self.messages else empty)
        self.rows.setVisible(bool(self.messages))
        self.show()

    def texts(self) -> list[str]:
        return [label.text() for label in self.labels]

    def jump(self, index: int) -> bool:
        if not 0 <= index < len(self.messages):
            return False
        message = self.messages[index]
        if message.log_file_id is None or message.line is None:
            return False
        self.go_to.emit(int(message.log_file_id), int(message.line))
        return True
