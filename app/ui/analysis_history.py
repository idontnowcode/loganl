"""분석 이력 (보조 메뉴, 계획서 §8.1·§8.5 "분석 결과 재확인"). DB에 저장된 실행을 다시 보고 보고서로 저장한다."""

from __future__ import annotations

from contextlib import closing

from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.bootstrap import AppContext
from app.reports.report_data import AI_STATUS_TEXT, AXIS_TEXT, load_report, report_text, save_reports
from app.storage.repositories import AnalysisRepository
from app.ui.theme import comfortable_table

STATUS_TEXT = {"RUNNING": "진행 중", "COMPLETED": "완료", "FAILED": "실패", "CANCELLED": "취소"}


class AnalysisHistoryDialog(QDialog):
    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("분석 이력")
        self.resize(980, 620)
        self.context = context
        self.table = QTableWidget(0, 6)
        comfortable_table(self.table)
        self.table.setHorizontalHeaderLabels(["번호", "시작", "상태", "이상 수준", "권장 대응", "AI"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.currentCellChanged.connect(lambda row, *_: self.show_row(row))
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.save_button = QPushButton("보고서 저장 (JSON·HTML)")
        self.save_button.clicked.connect(self.save_selected)
        self.open_button = QPushButton("HTML 보고서 열기")
        self.open_button.clicked.connect(self.open_html)
        self.message = QLabel()
        self.message.setWordWrap(True)
        buttons = QHBoxLayout()
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.open_button)
        buttons.addStretch(1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        close.rejected.connect(self.reject)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.table)
        split.addWidget(self.view)
        layout = QVBoxLayout(self)
        layout.addWidget(split, 1)
        layout.addLayout(buttons)
        layout.addWidget(self.message)
        layout.addWidget(close)
        self.refresh()

    def refresh(self) -> None:
        with closing(self.context.database.connect()) as conn:
            self.runs = AnalysisRepository(conn).list()
        anomaly = AXIS_TEXT["anomaly_level"][1]
        response = AXIS_TEXT["response_guidance"][1]
        self.table.setRowCount(len(self.runs))
        for r, run in enumerate(self.runs):
            values = (f"R-{run['id']:04d}", run["started_at"], STATUS_TEXT.get(run["status"], run["status"]),
                      anomaly.get(run["anomaly_level"], run["anomaly_level"]),
                      response.get(run["response_guidance"], run["response_guidance"]),
                      AI_STATUS_TEXT.get(run["ai_status"], run["ai_status"]))
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, run["id"])
                self.table.setItem(r, c, item)
        if self.runs:
            self.table.selectRow(0)
            self.show_row(0)

    def selected_run(self) -> int | None:
        item = self.table.item(self.table.currentRow(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def show_row(self, row: int) -> None:
        if not (0 <= row < len(self.runs)):
            return
        self.view.setPlainText(report_text(load_report(self.context.database, self.runs[row]["id"])))

    def save_selected(self) -> tuple[str, str] | None:
        run_id = self.selected_run()
        if run_id is None:
            return None
        paths = save_reports(self.context.database, self.context.paths, run_id)
        self.message.setText(f"저장했습니다: {paths[0]}, {paths[1]} (데이터 폴더 기준)")
        return paths

    def open_html(self) -> None:
        run_id = self.selected_run()
        if run_id is None:
            return
        _, html = self.save_selected() or (None, None)
        if html:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.context.paths.root / html)))
