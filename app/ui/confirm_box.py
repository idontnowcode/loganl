"""분석 결과에서 바로 '확인 결과 남기기' (CR-14 CM-05, CR-16 UX-01·03). 엔지니어가 확인한 실제 결과를 사례로 저장한다.

저장한 로그는 사례 기억(근거 지문)에 들어가 다음 분석의 판정 보정에 쓰인다. 저장 경로는 학습 탭과 같다
(LogEntryService.save_group). 비정상은 판단 근거가 꼭 있어야 한다(사례 승인 조건) — 글 칸이 비었으면 저장할 때 묻는다.
② 결과 목록의 [정상]·[비정상] 단추, ② 확인 상자, ③ 아래 단추가 모두 save_confirmation 하나로 저장한다.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.learning.log_entry import LogEntry, LogEntryError, LogEntryService
from app.storage.database import Database
from app.ui.theme import set_role

LABEL_TEXT = {"NORMAL": "정상", "ABNORMAL": "비정상"}


class BasisDialog(QDialog):
    """비정상으로 저장할 때 판단 근거(꼭)와 메모를 받는 작은 창."""

    def __init__(self, what: str, basis: str = "", memo: str = "", hints: Sequence[str] = (),
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("비정상 사례로 저장")
        self.resize(560, 300)
        head = QLabel(f"{what}을(를) 비정상 사례로 저장합니다. 무엇을 보고 비정상이라고 판단했는지 적어 주세요(꼭). "
                      "적은 글이 그대로 다음 분석의 판단 근거가 됩니다.")
        head.setWordWrap(True)
        self.basis = QPlainTextEdit(basis)
        self.basis.setPlaceholderText("판단 근거 (예: 어느 줄·어떤 차이를 보고 비정상으로 판단했는지)")
        self.memo = QLineEdit(memo)
        self.memo.setPlaceholderText("메모 (선택: 운전 조건·확인한 내용)")
        layout = QVBoxLayout(self)
        layout.addWidget(head)
        if hints:
            hint = QLabel("참고 — 앱이 문제로 본 줄:\n" + "\n".join(f"· {h[:160]}" for h in hints[:3]))
            hint.setWordWrap(True)
            set_role(hint, "muted")
            layout.addWidget(hint)
        layout.addWidget(self.basis, 1)
        layout.addWidget(self.memo)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.basis.textChanged.connect(self._update)
        self._update()

    def _update(self) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(bool(self.basis.toPlainText().strip()))


def ask_abnormal_basis(parent: QWidget | None, what: str, basis: str = "", memo: str = "",
                       hints: Sequence[str] = ()) -> tuple[str, str] | None:
    """(판단 근거, 메모) 또는 취소하면 None. 시험에서는 이 함수를 바꿔 끼운다."""
    dialog = BasisDialog(what, basis, memo, hints, parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.basis.toPlainText().strip(), dialog.memo.text().strip()


AskBasis = Callable[..., "tuple[str, str] | None"]


def what_of(log_ids: Sequence[int]) -> str:
    return "이 로그" if len(log_ids) <= 1 else f"이 로그 묶음({len(log_ids)}개)"


def save_confirmation(database: Database, log_ids: Sequence[int], label: str, text: str | None,
                      ask: AskBasis | None = None, parent: QWidget | None = None,
                      hints: Sequence[str] = ()) -> LogEntry | None:
    """확인 결과를 사례로 저장한다. 정상: text는 메모(비었으면 저장된 메모를 지킴). 비정상: text는 판단 근거 — 비었으면
    ask로 묻는다(취소하면 None). 다른 칸(비정상일 때의 메모)은 저장된 값을 지킨다(리뷰 2026-11-22)."""
    service = LogEntryService(database)
    ids = sorted(set(log_ids))
    kept = service.group_entry(ids)
    text = (text or "").strip()
    if label == "NORMAL":
        return service.save_group(ids, "NORMAL", text or kept.memo, "")
    memo = kept.memo
    if not text:
        if ask is None:
            raise LogEntryError("비정상 사례는 판단 근거를 적어야 저장됩니다.")
        answer = ask(parent, what_of(ids), kept.criteria or "", memo or "", hints)
        if answer is None:
            return None
        text, memo = answer
    return service.save_group(ids, "ABNORMAL", memo, text)


class ConfirmBox(QGroupBox):
    saved = Signal(list)  # 저장한 로그 id들(같은 수집 세션 묶음이면 여러 개)

    def __init__(self, database: Database, parent=None) -> None:
        super().__init__("확인 결과 남기기 — 실제로 어땠는지 남기면 사례로 쌓여 다음 분석의 판정에 쓰입니다", parent)
        self.database = database
        self.log_ids: list[int] = []
        self.hints: list[str] = []  # 비정상 근거를 물을 때 보여 줄 '문제로 본 줄'
        self.ask: AskBasis = ask_abnormal_basis
        self.saved_label: str | None = None
        self.text = QLineEdit()
        self.text.setPlaceholderText("메모·판단 근거 (선택 — 비정상인데 비어 있으면 저장할 때 물어봅니다)")
        self.normal_button = QPushButton("정상이었음으로 저장")
        self.normal_button.clicked.connect(lambda: self.save("NORMAL"))
        self.abnormal_button = QPushButton("비정상이었음으로 저장…")
        self.abnormal_button.clicked.connect(lambda: self.save("ABNORMAL"))
        self.status = QLabel()
        self.status.setWordWrap(True)
        set_role(self.status, "muted")
        self.memory = QLabel()
        self.memory.setWordWrap(True)
        set_role(self.memory, "hint")
        row = QHBoxLayout()
        row.addWidget(self.text, 1)
        row.addWidget(self.normal_button)
        row.addWidget(self.abnormal_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.memory)
        layout.addLayout(row)
        layout.addWidget(self.status)
        self.set_log(None)

    @property
    def log_file_id(self) -> int | None:
        return self.log_ids[0] if self.log_ids else None

    def set_log(self, log_ids, memory_note: str = "", hints: Sequence[str] = ()) -> None:
        """보이는 결과의 분석 단위(로그 하나 또는 같은 수집 세션 묶음). 이미 저장했으면 그 판정·글을 보인다."""
        if isinstance(log_ids, int):
            log_ids = [log_ids]
        self.log_ids = sorted(set(log_ids or []))
        self.hints = list(hints)
        self.memory.setText(memory_note)
        self.memory.setVisible(bool(memory_note))
        self.text.clear()
        self.say("")
        self.saved_label = None
        if not self.log_ids:
            self.setVisible(False)
            return
        self.setVisible(True)
        entry = LogEntryService(self.database).group_entry(self.log_ids)
        if entry.label is not None:
            self.saved_label = entry.label
            self.text.setText(entry.criteria if entry.label == "ABNORMAL" else entry.memo)
            self.say(f"{what_of(self.log_ids)}는 이미 '{LABEL_TEXT[entry.label]}' 사례로 저장돼 있습니다({entry.state_text()}). "
                     "바꾸려면 다른 쪽 단추를 누르세요.")
        many = len(self.log_ids) > 1
        self.normal_button.setText("묶음을 정상이었음으로 저장" if many else "정상이었음으로 저장")
        self.abnormal_button.setText("묶음을 비정상이었음으로 저장…" if many else "비정상이었음으로 저장…")

    def say(self, text: str) -> None:
        """상태 글. 비었으면 자리를 차지하지 않게 숨긴다."""
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def save(self, label: str) -> bool:
        if not self.log_ids:
            return False
        try:
            entry = save_confirmation(self.database, self.log_ids, label, self.text.text(), self.ask, self, self.hints)
        except LogEntryError as exc:
            self.say(str(exc))
            return False
        if entry is None:
            self.say("저장하지 않았습니다(판단 근거를 적지 않고 닫음).")
            return False
        self.saved_label = label
        if label == "ABNORMAL":
            self.text.setText(entry.criteria)
        self.say(f"'{LABEL_TEXT[label]}' 사례로 저장했습니다({entry.state_text()}). 다음 분석부터 사례 기억에 쓰입니다.")
        self.saved.emit(list(self.log_ids))
        return True
