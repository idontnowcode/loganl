"""'AI 질문' 칸 (변경 요청 CR-07 QA-02). LLM이 저장된 로그를 보고 남긴 질문에 답한다. 답은 모두 선택이다.

- 추측 확인(GUESS): 맞음 · 틀림 · 모름 + 한마디(선택)
- 묻기(ASK): 글로 답하기 · 모름 · 건너뛰기
답한 내용(모름 제외)은 다음 분석부터 AI가 근거로 쓴다(learning.ai_questions).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.learning.ai_questions import QuestionView
from app.ui.natural_input import StatusLine
from app.ui.theme import set_role

MAX_CARDS = 8
KIND_LABELS = {"GUESS": "AI 추측 — 맞나요?", "ASK": "AI 질문"}
LABEL_TEXT = {"NORMAL": "정상", "ABNORMAL": "비정상"}


class QuestionCard(QFrame):
    def __init__(self, view: QuestionView, on_answer: Callable[[int, str, str | None], None],
                 on_skip: Callable[[int], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.view = view
        set_role(self, "cell")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)
        head = QLabel(f"{view.file_name} ({LABEL_TEXT.get(view.label or '', '-')}) · {KIND_LABELS.get(view.kind, view.kind)}")
        set_role(head, "cell-label")
        text = QLabel(view.text)
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(head)
        layout.addWidget(text)
        details = []
        if view.reason:
            details.append(f"왜 묻나: {view.reason}")
        lines = [f"{e['line']}행" for e in view.evidence if e.get("line")]
        if lines:
            details.append("근거: " + ", ".join(dict.fromkeys(lines)))
        elif view.evidence:
            details.append("근거: " + ", ".join(e["key"] for e in view.evidence))
        if details:
            note = QLabel(" · ".join(details))
            note.setWordWrap(True)
            set_role(note, "muted")
            layout.addWidget(note)
        self.edit = QLineEdit()
        row = QHBoxLayout()
        row.setSpacing(6)
        self.buttons: dict[str, QPushButton] = {}
        if view.kind == "GUESS":
            self.edit.setPlaceholderText("한마디 (선택)")
            for answer, label in (("YES", "맞음"), ("NO", "틀림"), ("UNKNOWN", "모름")):
                button = QPushButton(label)
                button.clicked.connect(lambda _c=False, a=answer: on_answer(view.id, a, self.edit.text()))
                self.buttons[answer] = button
                row.addWidget(button)
            row.addWidget(self.edit, 1)
        else:
            self.edit.setPlaceholderText("답 (선택)")
            self.edit.returnPressed.connect(lambda: on_answer(view.id, "TEXT", self.edit.text()))
            row.addWidget(self.edit, 1)
            for answer, label in (("TEXT", "답하기"), ("UNKNOWN", "모름")):
                button = QPushButton(label)
                button.clicked.connect(lambda _c=False, a=answer: on_answer(view.id, a, self.edit.text()))
                self.buttons[answer] = button
                row.addWidget(button)
            skip = QPushButton("건너뛰기")
            skip.clicked.connect(lambda: on_skip(view.id))
            self.buttons["SKIP"] = skip
            row.addWidget(skip)
        layout.addLayout(row)


class AiQuestionsBox(QGroupBox):
    def __init__(self, on_answer: Callable[[int, str, str | None], None], on_skip: Callable[[int], None],
                 on_ask_again: Callable[[], None], parent: QWidget | None = None) -> None:
        super().__init__("AI 질문 (답은 선택 — 답하면 다음 분석부터 AI가 참고합니다)", parent)
        self._on_answer = on_answer
        self._on_skip = on_skip
        self.status = StatusLine()
        self.empty = QLabel("열린 질문이 없습니다. 로그를 저장하면 AI가 그 로그를 보고 궁금한 점을 여기에 남깁니다.")
        self.empty.setWordWrap(True)
        set_role(self.empty, "muted")
        self.cards_layout = QVBoxLayout()
        self.cards_layout.setSpacing(8)
        self.more = QLabel()
        set_role(self.more, "muted")
        self.more.hide()
        self.ask_button = QPushButton("선택한 로그에 대해 AI에게 다시 묻기")
        self.ask_button.clicked.connect(on_ask_again)
        bottom = QHBoxLayout()
        bottom.addWidget(self.ask_button)
        bottom.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(self.empty)
        layout.addLayout(self.cards_layout)
        layout.addWidget(self.more)
        layout.addLayout(bottom)
        self.cards: list[QuestionCard] = []

    def show_questions(self, views: list[QuestionView]) -> None:
        for card in self.cards:
            self.cards_layout.removeWidget(card)
            card.hide()
            card.deleteLater()
        self.cards = [QuestionCard(view, self._on_answer, self._on_skip) for view in views[:MAX_CARDS]]
        for card in self.cards:
            self.cards_layout.addWidget(card)
        self.empty.setVisible(not views)
        extra = len(views) - len(self.cards)
        self.more.setText(f"열린 질문이 {extra}개 더 있습니다. 위 질문에 답하면 이어서 보입니다.")
        self.more.setVisible(extra > 0)
