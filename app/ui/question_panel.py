"""확인 질문 패널 (계획서 §7.3): 중요도 높은 질문부터 최대 3개, 질문 이유, 답변·모름·건너뛰기."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.learning.question_engine import MAX_SHOWN, OpenQuestion, QuestionEngine, QuestionError
from app.storage.database import Database
from app.ui.theme import set_role

EDITABLE = {"DRAFT", "NEEDS_INFORMATION", "READY_FOR_REVIEW"}


class QuestionRow(QFrame):
    def __init__(self, question: OpenQuestion, on_answer: Callable[[OpenQuestion, str, str | None, str | None], None],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.question = question
        self.setFrameShape(QFrame.Shape.StyledPanel)
        badge = "필수" if question.required else ("AI 질문" if question.generated_by == "AI" else "선택")
        title = QLabel(f"[{badge}] {question.text}")
        title.setWordWrap(True)
        title.setStyleSheet("font-weight: 600;")
        reason = QLabel(f"이유: {question.reason}")
        reason.setWordWrap(True)
        set_role(reason, "muted")
        self.choice = QComboBox()
        for value, label in question.choices:
            self.choice.addItem(label, value)
        self.text = QLineEdit()
        self.text.setPlaceholderText("답변 입력")
        answer = QPushButton("답변")
        unknown = QPushButton("모름")
        skip = QPushButton("건너뛰기")
        answer.clicked.connect(self._answer)
        unknown.clicked.connect(lambda: on_answer(question, "UNKNOWN", None, None))
        skip.clicked.connect(lambda: on_answer(question, "SKIPPED", None, None))
        self._on_answer = on_answer
        row = QHBoxLayout()
        if question.choices:
            row.addWidget(self.choice, 1)
            self.text.hide()
        else:
            row.addWidget(self.text, 1)
            self.choice.hide()
        for b in (answer, unknown, skip):
            row.addWidget(b)
        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(reason)
        layout.addLayout(row)

    def _answer(self) -> None:
        if self.question.choices:
            self._on_answer(self.question, "CHOICE", None, self.choice.currentData())
        else:
            self._on_answer(self.question, "TEXT", self.text.text(), None)


class QuestionPanel(QGroupBox):
    answered = Signal(str)

    def __init__(self, database: Database, before_answer: Callable[[], bool] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__("AI 확인 질문", parent)
        self.engine = QuestionEngine(database)
        self.before_answer = before_answer
        self.case_id: int | None = None
        self.rows: list[QuestionRow] = []
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        refresh = QPushButton("질문 다시 확인")
        refresh.clicked.connect(lambda: self.set_case(self.case_id, refresh=True))
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.body = QVBoxLayout()
        top = QHBoxLayout()
        top.addWidget(self.summary, 1)
        top.addWidget(refresh)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addLayout(self.body)
        layout.addWidget(self.message)

    def set_case(self, case_id: int | None, *, refresh: bool = True, status: str | None = None) -> None:
        self.case_id = case_id
        for row in self.rows:
            row.setParent(None)
            row.deleteLater()
        self.rows = []
        if case_id is None:
            self.summary.setText("사례를 저장하면 부족한 정보를 확인합니다.")
            return
        questions = self.engine.refresh(case_id) if refresh and (status is None or status in EDITABLE) \
            else self.engine.open_questions(case_id)
        shown = questions[:MAX_SHOWN]
        for question in shown:
            row = QuestionRow(question, self._answer)
            self.body.addWidget(row)
            self.rows.append(row)
        required = sum(q.required for q in questions)
        if not questions:
            self.summary.setText("열린 질문이 없습니다.")
        else:
            self.summary.setText(f"열린 질문 {len(questions)}개 (필수 {required}개). 중요한 것부터 {len(shown)}개를 보여 줍니다.")

    def _answer(self, question: OpenQuestion, kind: str, text: str | None, value: str | None) -> None:
        if self.before_answer is not None and not self.before_answer():
            return
        try:
            outcome = self.engine.answer(question.id, kind, text, value)
        except QuestionError as exc:
            self.message.setText(str(exc))
            return
        message = outcome.applied + (" " + " ".join(outcome.reanalyzed) if outcome.reanalyzed else "")
        self.message.setText(message)
        self.answered.emit(message)
