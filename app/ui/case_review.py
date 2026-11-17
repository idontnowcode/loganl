"""학습 사례 관리 (보조 메뉴). 사례 목록·개정 이력·보관·복원·초안 삭제·열기."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.bootstrap import AppContext
from app.learning.approval_workflow import ApprovalError, ApprovalWorkflow
from app.learning.case_manager import CaseError, CaseManager
from app.ui.case_editor import STATUS_LABELS
from app.ui.theme import comfortable_table

TYPE_LABELS = {"NORMAL": "정상", "ABNORMAL": "비정상"}


class CaseReviewDialog(QDialog):
    def __init__(self, context: AppContext, open_case: Callable[[str, int], None] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("학습 사례 관리")
        self.resize(900, 560)
        self.cases = CaseManager(context.database)
        self.workflow = ApprovalWorkflow(context.database)
        self.open_case = open_case
        self.type_filter = QComboBox()
        self.type_filter.addItem("전체", None)
        self.type_filter.addItem("정상", "NORMAL")
        self.type_filter.addItem("비정상", "ABNORMAL")
        self.type_filter.currentIndexChanged.connect(self.refresh)
        self.table = QTableWidget(0, 5)
        comfortable_table(self.table)
        self.table.setHorizontalHeaderLabels(["사례", "종류", "상태", "개정본", "수정 시각"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.currentCellChanged.connect(lambda row, *_: self.show_revisions())
        self.revisions = QPlainTextEdit()
        self.revisions.setReadOnly(True)
        self.revisions.setMaximumHeight(140)
        self.message = QLabel()
        self.message.setWordWrap(True)
        row = QHBoxLayout()
        for text, slot in (("열기", self.open_selected), ("보관", self.archive_selected),
                           ("보관 해제", self.restore_selected), ("초안 삭제", self.delete_selected)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            row.addWidget(button)
        row.addStretch(1)
        top = QHBoxLayout()
        top.addWidget(QLabel("종류"))
        top.addWidget(self.type_filter)
        top.addStretch(1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        close.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.table, 1)
        layout.addWidget(QLabel("개정 이력 (승인할 때마다 하나씩, 이전 개정본은 보존)"))
        layout.addWidget(self.revisions)
        layout.addLayout(row)
        layout.addWidget(self.message)
        layout.addWidget(close)
        self.refresh()

    def refresh(self) -> None:
        self.rows = self.cases.list(self.type_filter.currentData())
        self.table.setRowCount(len(self.rows))
        for r, case in enumerate(self.rows):
            values = (case.label, TYPE_LABELS[case.case_type], STATUS_LABELS.get(case.status, case.status),
                      str(case.current_revision), case.updated_at)
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, case.id)
                self.table.setItem(r, c, item)

    def selected(self):
        row = self.table.currentRow()
        return self.rows[row] if 0 <= row < len(self.rows) else None

    def show_revisions(self) -> None:
        case = self.selected()
        if case is None:
            self.revisions.clear()
            return
        lines = [f"개정본 {r['revision_no']} · {r['created_at']} · {r['change_summary'] or ''}"
                 for r in self.workflow.revisions(case.id)]
        self.revisions.setPlainText("\n".join(lines) or "아직 승인한 적이 없습니다.")

    def _act(self, action, done: str) -> None:
        case = self.selected()
        if case is None:
            return
        try:
            action(case.id)
        except (ApprovalError, CaseError) as exc:
            self.message.setText(str(exc))
            return
        self.message.setText(done)
        self.refresh()

    def archive_selected(self) -> None:
        self._act(self.workflow.archive, "보관했습니다. 보관된 사례는 분석에 쓰이지 않습니다.")

    def restore_selected(self) -> None:
        self._act(self.workflow.restore, "보관을 풀었습니다(초안). 다시 승인해야 분석에 쓰입니다.")

    def delete_selected(self) -> None:
        self._act(self.cases.delete, "초안을 지웠습니다. 원본 로그 사본은 남아 있습니다.")

    def open_selected(self) -> None:
        case = self.selected()
        if case is not None and self.open_case is not None:
            self.open_case(case.case_type, case.id)
            self.accept()
