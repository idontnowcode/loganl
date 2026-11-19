"""'예상 에러 메시지' 칸 (변경 요청 CR-11). 신규 분석 결과와 로그 입력 탭이 같이 쓴다.
줄은 로그 원문 그대로이고, 두 번 누르면 왼쪽 로그의 그 행으로 간다."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QGroupBox, QLabel, QListWidget, QListWidgetItem, QVBoxLayout

from app.analysis.error_messages import ErrorMessage, message_line
from app.ui.theme import fit_list_height, set_role, tone_color

GROUP_TONE = {"ERROR": "bad", "DIFFERENT": "warn", "NEW_WARNING": "warn", "WARNING": "warn", "MISSING": "neutral"}


class ErrorMessagesBox(QGroupBox):
    go_to = Signal(int, int)  # (log_file_id, 행)

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(title, parent)
        self.intro = QLabel()
        self.intro.setWordWrap(True)
        set_role(self.intro, "muted")
        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.itemDoubleClicked.connect(self._jump)
        layout = QVBoxLayout(self)
        layout.addWidget(self.intro)
        layout.addWidget(self.list)
        self.messages: list[ErrorMessage] = []

    def show_messages(self, messages: Sequence[ErrorMessage], intro: str, empty: str) -> None:
        self.messages = list(messages)
        self.list.clear()
        for message in self.messages:
            item = QListWidgetItem(message_line(message))
            item.setForeground(tone_color(GROUP_TONE.get(message.group, "neutral")))
            item.setData(Qt.ItemDataRole.UserRole, (message.log_file_id, message.line))
            if message.line is not None:
                item.setToolTip("두 번 누르면 왼쪽 로그의 그 행으로 이동합니다.")
            self.list.addItem(item)
        self.intro.setText(intro if self.messages else empty)
        self.list.setVisible(bool(self.messages))
        if self.messages:
            fit_list_height(self.list, minimum=60, maximum=420)
        self.show()

    def texts(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def _jump(self, item: QListWidgetItem) -> None:
        log_file_id, line = item.data(Qt.ItemDataRole.UserRole)
        if log_file_id is not None and line is not None:
            self.go_to.emit(int(log_file_id), int(line))
