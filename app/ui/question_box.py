"""판단을 돕는 질문 칸 (CR-15 JQ-07). 애매한 분석에서 코드가 근거로 만든 질문을 보이고, 답을 판단 기준으로 저장한다.

답 단추를 누르면 바로 기준이 된다(답 = 승인). 값 질문은 정상 하한·상한을 적고 '값 범위로 저장'. '판단 방법' 글은 함께 남는다.
저장한 기준은 다음 분석부터 판정에 쓰인다('이 로그 다시 분석'으로 바로 확인).
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from app.learning.criteria import VALUE_RANGE, CriteriaService, Question
from app.storage.database import Database
from app.ui.theme import mono_font, set_role


def _number(text: str) -> float | None:
    text = text.strip().replace(",", "")
    if not text:
        return None
    return float(text)


class QuestionRow(QWidget):
    def __init__(self, box: QuestionBox, question: Question) -> None:
        super().__init__()
        self.box, self.question = box, question
        self.text = QLabel(question.text)
        self.text.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.addWidget(self.text)
        if question.example:
            where = f"{question.line}행  " if question.line is not None else ""
            example = QLabel(where + question.example)
            example.setFont(mono_font(9))
            example.setWordWrap(True)
            set_role(example, "muted")
            layout.addWidget(example)
        row = QHBoxLayout()
        self.buttons: list[QPushButton] = []
        for kind, label in question.answers:
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, k=kind: self.answer(k))
            row.addWidget(button)
            self.buttons.append(button)
        self.low = self.high = None
        if question.kind == "VALUE" and question.field:
            self.low, self.high = QLineEdit(), QLineEdit()
            self.low.setPlaceholderText("정상 하한")
            self.high.setPlaceholderText("정상 상한")
            for edit in (self.low, self.high):
                edit.setMaximumWidth(110)
                row.addWidget(edit)
            self.range_button = QPushButton("값 범위로 저장")
            self.range_button.clicked.connect(lambda: self.answer(VALUE_RANGE))
            row.addWidget(self.range_button)
            self.buttons.append(self.range_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.status = QLabel()
        set_role(self.status, "muted")
        self.status.hide()
        layout.addWidget(self.status)

    def answer(self, kind: str) -> None:
        low = high = None
        if kind == VALUE_RANGE:
            try:
                low, high = _number(self.low.text()), _number(self.high.text())
            except ValueError:
                self.say("하한·상한은 숫자로 적어 주세요.")
                return
            if low is None and high is None:
                self.say("정상 하한이나 상한 중 하나는 적어 주세요.")
                return
        try:
            self.box.save(self.question, kind, low, high)
        except ValueError as exc:
            self.say(str(exc))
            return
        for button in self.buttons:
            button.setEnabled(False)
        label = dict(self.question.answers).get(kind, "값 범위")
        self.say(f"판단 기준으로 저장했습니다: {label}" + (f" ({low if low is not None else ''}~{high if high is not None else ''})"
                                                       if kind == VALUE_RANGE else ""))

    def say(self, text: str) -> None:
        self.status.setText(text)
        self.status.setVisible(bool(text))


class QuestionBox(QGroupBox):
    answered = Signal(list)  # 이 분석 단위의 로그 id들
    reanalyze = Signal(list)

    def __init__(self, database: Database, parent=None) -> None:
        super().__init__("판단을 돕는 질문 — 답은 판단 기준으로 저장돼 다음 분석부터 판정에 쓰입니다(건너뛰어도 됨)", parent)
        self.service = CriteriaService(database)
        self.log_ids: list[int] = []
        self.run_id: int | None = None
        self.rows: list[QuestionRow] = []
        self.holder = QVBoxLayout()
        self.method = QLineEdit()
        self.method.setPlaceholderText("판단 방법(선택): 어떻게 판단하는지 적으면 기준과 함께 남습니다")
        self.again = QPushButton("이 로그 다시 분석")
        self.again.clicked.connect(lambda: self.reanalyze.emit(list(self.log_ids)))
        self.again.setEnabled(False)
        bottom = QHBoxLayout()
        bottom.addWidget(self.method, 1)
        bottom.addWidget(self.again)
        layout = QVBoxLayout(self)
        layout.addLayout(self.holder)
        layout.addLayout(bottom)
        self.set_questions([], [], None)

    def set_questions(self, questions: list[Question], log_ids: list[int], run_id: int | None) -> None:
        for row in self.rows:  # 예전 줄이 잠깐 겹쳐 보이지 않게 먼저 숨기고 떼어 낸다
            row.hide()
            row.setParent(None)
            row.deleteLater()
        self.rows = []
        self.log_ids, self.run_id = sorted(set(log_ids)), run_id
        self.method.clear()
        self.again.setEnabled(False)
        for question in questions:
            row = QuestionRow(self, question)
            self.holder.addWidget(row)
            self.rows.append(row)
        self.setVisible(bool(questions))

    def save(self, question: Question, kind: str, low: float | None, high: float | None) -> int:
        criterion_id = self.service.save_answer(question, kind, low=low, high=high, method_text=self.method.text(),
                                                log_ids=self.log_ids, run_id=self.run_id)
        self.again.setEnabled(True)
        self.answered.emit(list(self.log_ids))
        return criterion_id
