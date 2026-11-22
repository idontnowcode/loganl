"""분석 결과에서 바로 '확인 결과 남기기' (CR-14 CM-05). 엔지니어가 확인한 실제 결과를 사례로 저장한다.

저장한 로그는 사례 기억(근거 지문)에 들어가 다음 분석의 판정 보정과 AI 근거로 쓰인다. 저장 경로는 학습 탭과 같다
(LogEntryService.save). 비정상은 원인·판단 근거가 꼭 있어야 한다(사례 승인 조건).
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QButtonGroup, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QRadioButton, QVBoxLayout

from app.learning.log_entry import LogEntryError, LogEntryService
from app.storage.database import Database
from app.ui.theme import set_role


class ConfirmBox(QGroupBox):
    saved = Signal(list)  # 저장한 로그 id들(같은 수집 세션 묶음이면 여러 개)

    def __init__(self, database: Database, parent=None) -> None:
        super().__init__("확인 결과 남기기 — 남긴 결과는 사례로 쌓여 다음 분석과 AI 판단에 쓰입니다", parent)
        self.database = database
        self.log_ids: list[int] = []
        self.normal = QRadioButton("정상이었음")
        self.abnormal = QRadioButton("비정상이었음")
        self.group = QButtonGroup(self)
        for button in (self.normal, self.abnormal):
            self.group.addButton(button)
            button.toggled.connect(self._update)
        self.text = QLineEdit()
        self.text.textChanged.connect(self._update)
        self.save_button = QPushButton("사례로 저장")
        self.save_button.clicked.connect(self.save)
        self.status = QLabel()
        self.status.setWordWrap(True)
        set_role(self.status, "muted")
        self.memory = QLabel()
        self.memory.setWordWrap(True)
        set_role(self.memory, "hint")
        row = QHBoxLayout()
        row.addWidget(self.normal)
        row.addWidget(self.abnormal)
        row.addWidget(self.text, 1)
        row.addWidget(self.save_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.memory)
        layout.addLayout(row)
        layout.addWidget(self.status)
        self.set_log(None)

    @property
    def log_file_id(self) -> int | None:
        return self.log_ids[0] if self.log_ids else None

    def set_log(self, log_ids, memory_note: str = "") -> None:
        """보이는 결과의 분석 단위(로그 하나 또는 같은 수집 세션 묶음). 이미 저장했으면 그 판정·글을 채운다."""
        if isinstance(log_ids, int):
            log_ids = [log_ids]
        self.log_ids = sorted(set(log_ids or []))
        self.memory.setText(memory_note)
        self.memory.setVisible(bool(memory_note))
        self.group.setExclusive(False)
        self.normal.setChecked(False)
        self.abnormal.setChecked(False)
        self.group.setExclusive(True)
        self.text.clear()
        self.say("")
        if not self.log_ids:
            self.setVisible(False)
            return
        self.setVisible(True)
        entry = LogEntryService(self.database).group_entry(self.log_ids)
        if entry.label is not None:
            (self.abnormal if entry.label == "ABNORMAL" else self.normal).setChecked(True)
            self.text.setText(entry.criteria if entry.label == "ABNORMAL" else entry.memo)
            what = "이 로그" if len(self.log_ids) == 1 else f"이 로그 묶음({len(self.log_ids)}개)"
            self.say(f"{what}는 이미 '{'비정상' if entry.label == 'ABNORMAL' else '정상'}' 사례로 저장돼 있습니다"
                     f"({entry.state_text()}). 바꾸려면 고쳐서 다시 저장하세요.")
        self._update()

    def say(self, text: str) -> None:
        """상태 글. 비었으면 자리를 차지하지 않게 숨긴다."""
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def _update(self) -> None:
        abnormal = self.abnormal.isChecked()
        self.text.setPlaceholderText("원인·판단 근거 (비정상이면 꼭 적기, 적은 글이 그대로 다음 판단의 근거가 됩니다)" if abnormal
                                     else "메모 (선택: 운전 조건·확인한 내용)")
        ready = self.normal.isChecked() or (abnormal and bool(self.text.text().strip()))
        self.save_button.setEnabled(bool(self.log_ids) and ready)
        self.save_button.setText("사례로 저장" if len(self.log_ids) <= 1 else f"묶음 {len(self.log_ids)}개를 사례 하나로 저장")

    def save(self) -> None:
        if not self.log_ids or not (self.normal.isChecked() or self.abnormal.isChecked()):
            return
        text = self.text.text().strip()
        label = "ABNORMAL" if self.abnormal.isChecked() else "NORMAL"
        service = LogEntryService(self.database)
        try:
            # 이 칸의 글은 비정상이면 판단 근거, 정상이면 메모다. 다른 칸(비정상일 때의 운전 조건 메모)은 저장된 값을 지킨다(리뷰 2026-11-22)
            kept_memo = service.group_entry(self.log_ids).memo
            entry = service.save_group(self.log_ids, label, kept_memo if label == "ABNORMAL" else text,
                                       text if label == "ABNORMAL" else "")
        except LogEntryError as exc:
            self.say(str(exc))
            return
        kind = "비정상" if label == "ABNORMAL" else "정상"
        self.say(f"'{kind}' 사례로 저장했습니다({entry.state_text()}). 다음 분석부터 사례 기억에 쓰입니다.")
        self.saved.emit(list(self.log_ids))
