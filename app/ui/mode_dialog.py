"""운전 모드 관리 (보조 메뉴). 모드는 사용자가 정한다. AI는 이 목록 안에서만 고른다(REQUIREMENTS Q1 기본값)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.learning.case_manager import CaseError, ModeManager
from app.storage.database import Database
from app.ui.theme import comfortable_table

STATUS = {"ACTIVE": "사용 중", "PROPOSED": "제안", "RETIRED": "사용 안 함"}


class ModeDialog(QDialog):
    def __init__(self, database: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("운전 모드 관리")
        self.setMinimumSize(620, 420)
        self.modes = ModeManager(database)
        self.table = QTableWidget(0, 4)
        comfortable_table(self.table)
        self.table.setHorizontalHeaderLabels(["이름", "설명", "조건", "상태"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.name_edit = QLineEdit()
        self.desc_edit = QLineEdit()
        self.cond_edit = QLineEdit()
        add = QPushButton("추가")
        add.clicked.connect(self.add_mode)
        retire = QPushButton("선택 모드 사용 안 함")
        retire.clicked.connect(self.retire_selected)
        activate = QPushButton("선택 모드 다시 사용")
        activate.clicked.connect(self.activate_selected)
        self.message = QLabel()
        form = QFormLayout()
        form.addRow("이름", self.name_edit)
        form.addRow("설명", self.desc_edit)
        form.addRow("조건", self.cond_edit)
        row = QHBoxLayout()
        row.addWidget(add)
        row.addWidget(retire)
        row.addWidget(activate)
        row.addStretch(1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        close.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("운전 모드는 사용자가 등록합니다. 장비 사양에 근거한 이름·설명을 쓰세요."))
        layout.addWidget(self.table, 1)
        layout.addLayout(form)
        layout.addLayout(row)
        layout.addWidget(self.message)
        layout.addWidget(close)
        self.refresh()

    def refresh(self) -> None:
        modes = self.modes.list()
        self.table.setRowCount(len(modes))
        for r, mode in enumerate(modes):
            for c, value in enumerate((mode.name, mode.description or "", mode.conditions or "",
                                       STATUS.get(mode.status, mode.status))):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, mode.id)
                self.table.setItem(r, c, item)

    def add_mode(self) -> None:
        try:
            self.modes.add(self.name_edit.text(), self.desc_edit.text().strip() or None,
                           self.cond_edit.text().strip() or None)
        except CaseError as exc:
            self.message.setText(str(exc))
            return
        for edit in (self.name_edit, self.desc_edit, self.cond_edit):
            edit.clear()
        self.message.setText("추가했습니다.")
        self.refresh()

    def _selected_id(self) -> int | None:
        item = self.table.item(self.table.currentRow(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def retire_selected(self) -> None:
        if (mode_id := self._selected_id()) is not None:
            self.modes.retire(mode_id)
            self.refresh()

    def activate_selected(self) -> None:
        if (mode_id := self._selected_id()) is not None:
            from app.models import utc_now_iso

            self.modes.update(mode_id, status="ACTIVE", approved_at=utc_now_iso())
            self.refresh()
