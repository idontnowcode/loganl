"""판단 기준 관리 (CR-15 JQ-06). 데이터 관리 → 판단 기준.

엔지니어가 판단 질문에 답해 만든 기준을 보고 끄기·켜기·지우기를 한다. 스스로 꺼진 기준은 이유가 보인다.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.bootstrap import AppContext
from app.learning.criteria import KIND_TEXT, CriteriaService, Criterion
from app.ui.theme import comfortable_table, set_role

HEADERS = ("상태", "기준", "판단 방법", "물은 질문", "끈 이유")


class CriteriaDialog(QDialog):
    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("판단 기준")
        self.resize(1100, 520)
        self.service = CriteriaService(context.database)
        self.items: list[Criterion] = []
        hint = QLabel("판단을 돕는 질문에 답해 만든 기준입니다. 켜진 기준은 다음 분석부터 판정에 쓰입니다. "
                      "확인된 로그와 자주 어긋나는 기준은 스스로 꺼지고 이유가 남습니다.")
        hint.setWordWrap(True)
        set_role(hint, "hint")
        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        comfortable_table(self.table)
        self.toggle = QPushButton("끄기")
        self.toggle.clicked.connect(self.toggle_selected)
        self.remove = QPushButton("지우기")
        self.remove.clicked.connect(self.remove_selected)
        close = QPushButton("닫기")
        close.clicked.connect(self.accept)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        buttons = QHBoxLayout()
        buttons.addWidget(self.toggle)
        buttons.addWidget(self.remove)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self) -> None:
        self.items = self.service.all()
        self.table.setRowCount(len(self.items))
        for row, c in enumerate(self.items):
            values = ("켜짐" if c.status == "ACTIVE" else "꺼짐", f"[{KIND_TEXT.get(c.kind, c.kind)}] {c.describe()}",
                      c.method_text or "", c.question, c.disabled_reason or "")
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row, col, item)
        self.table.resizeColumnToContents(0)
        self._update_buttons()

    def selected(self) -> Criterion | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return self.items[rows[0].row()] if rows else None

    def _update_buttons(self) -> None:
        c = self.selected()
        self.toggle.setEnabled(c is not None)
        self.remove.setEnabled(c is not None)
        self.toggle.setText("켜기" if c is not None and c.status != "ACTIVE" else "끄기")

    def toggle_selected(self) -> None:
        c = self.selected()
        if c is None:
            return
        if c.status == "ACTIVE":
            self.service.set_status(c.id, "DISABLED", "사용자가 껐습니다.")
        else:
            self.service.set_status(c.id, "ACTIVE")
        self.refresh()

    def remove_selected(self) -> None:
        c = self.selected()
        if c is None:
            return
        answer = QMessageBox.question(self, "판단 기준 지우기", f"이 기준을 지울까요?\n{c.describe()}")
        if answer == QMessageBox.StandardButton.Yes:
            self.service.delete(c.id)
            self.refresh()
