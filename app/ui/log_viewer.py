"""공용 로그 뷰어: 이벤트 표(필요할 때 더 읽기), 필터, 원본 행 보기·이동 (계획서 §8.5)."""

from __future__ import annotations

from contextlib import closing
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal

from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app.models.records import EventRecord
from app.storage.database import Database
from app.storage.repositories import EventRepository
from app.ui.theme import comfortable_table, mono_font, set_role

PAGE = 1000
COLUMNS = ("행", "시각", "레벨", "구성요소", "오류 코드", "메시지")


class EventTableModel(QAbstractTableModel):
    def __init__(self, database: Database, parent: Any = None) -> None:
        super().__init__(parent)
        self.database = database
        self.log_file_id: int | None = None
        self.text_filter: str | None = None
        self._rows: list[EventRecord] = []
        self._total = 0

    def set_file(self, log_file_id: int | None, text_filter: str | None = None) -> None:
        self.beginResetModel()
        self.log_file_id = log_file_id
        self.text_filter = text_filter or None
        self._rows = []
        self._total = 0
        if log_file_id is not None:
            with closing(self.database.connect()) as conn:
                repo = EventRepository(conn)
                self._total = repo.count(log_file_id, self.text_filter)
                self._rows = repo.list(log_file_id, limit=PAGE, text_filter=self.text_filter)
        self.endResetModel()

    @property
    def total(self) -> int:
        return self._total

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def canFetchMore(self, parent: QModelIndex = QModelIndex()) -> bool:  # noqa: N802
        return not parent.isValid() and len(self._rows) < self._total

    def fetchMore(self, parent: QModelIndex = QModelIndex()) -> None:  # noqa: N802
        if parent.isValid() or self.log_file_id is None:
            return
        with closing(self.database.connect()) as conn:
            more = EventRepository(conn).list(self.log_file_id, offset=len(self._rows), limit=PAGE,
                                              text_filter=self.text_filter)
        if not more:
            self._total = len(self._rows)
            return
        self.beginInsertRows(QModelIndex(), len(self._rows), len(self._rows) + len(more) - 1)
        self._rows.extend(more)
        self.endInsertRows()

    def ensure_loaded(self, row: int) -> None:
        while row >= len(self._rows) and self.canFetchMore():
            self.fetchMore()

    def event_at(self, row: int) -> EventRecord | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        event = self._rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            column = index.column()
            if column == 0:
                return event.line_no if event.line_end is None else f"{event.line_no}-{event.line_end}"
            if column == 1:
                return event.ts_raw or ""
            if column == 2:
                return event.level or ""
            if column == 3:
                return event.component or ""
            if column == 4:
                return event.error_code or ""
            return event.message.split("\n", 1)[0]
        if role == Qt.ItemDataRole.ToolTipRole and index.column() == 5:
            return event.raw_text[:2000]
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return COLUMNS[section]
        return None


class LogViewer(QWidget):
    event_selected = Signal(object)  # EventRecord

    def __init__(self, database: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = EventTableModel(database, self)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("필터: 원문·오류 코드에 포함된 글자")
        self.filter_edit.returnPressed.connect(self.apply_filter)
        self.count_label = QLabel("파일을 고르세요")
        set_role(self.count_label, "muted")

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        self.table.selectionModel().currentRowChanged.connect(self._on_row)
        comfortable_table(self.table)
        self.table.setMinimumHeight(220)  # 머리 + 6줄

        self.raw_view = QPlainTextEdit()
        self.raw_view.setReadOnly(True)
        self.raw_view.setFont(mono_font())
        self.raw_view.setMinimumHeight(96)
        self.raw_view.setPlaceholderText("이벤트를 고르면 원본 행이 여기에 보입니다.")

        top = QHBoxLayout()
        top.addWidget(self.filter_edit, 1)
        top.addWidget(self.count_label)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.table)
        split.addWidget(self.raw_view)
        split.setSizes([300, 110])
        split.setChildrenCollapsible(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        layout.addWidget(split, 1)

    def show_file(self, log_file_id: int | None) -> None:
        self.model.set_file(log_file_id, self.filter_edit.text())
        self._update_count()
        self.raw_view.clear()
        for column in range(max(self.model.columnCount() - 1, 0)):  # 마지막(메시지) 열은 남는 폭을 쓴다
            self.table.resizeColumnToContents(column)

    def apply_filter(self) -> None:
        self.model.set_file(self.model.log_file_id, self.filter_edit.text())
        self._update_count()

    def go_to_line(self, log_file_id: int, line_no: int) -> bool:
        """원본 행으로 이동한다. 필터 때문에 안 보이면 필터를 지우고 다시 찾는다."""
        if self.model.log_file_id != log_file_id:
            self.model.set_file(log_file_id, self.filter_edit.text())
        with closing(self.model.database.connect()) as conn:
            row = EventRepository(conn).index_of_line(log_file_id, line_no, self.model.text_filter)
        if row is None and self.model.text_filter:
            self.filter_edit.clear()
            self.model.set_file(log_file_id, None)
            with closing(self.model.database.connect()) as conn:
                row = EventRepository(conn).index_of_line(log_file_id, line_no, None)
        self._update_count()
        if row is None:
            return False
        self.model.ensure_loaded(row)
        index = self.model.index(row, 0)
        self.table.setCurrentIndex(index)
        self.table.scrollTo(index, QTableView.ScrollHint.PositionAtCenter)
        return True

    def selected_event(self) -> EventRecord | None:
        return self.model.event_at(self.table.currentIndex().row())

    def _on_row(self, current: QModelIndex, _previous: QModelIndex) -> None:
        event = self.model.event_at(current.row())
        if event is None:
            return
        lines = event.raw_text.split("\n")
        numbered = "\n".join(f"{event.line_no + i:>7} | {text}" for i, text in enumerate(lines))
        self.raw_view.setPlainText(numbered)
        self.event_selected.emit(event)

    def _update_count(self) -> None:
        if self.model.log_file_id is None:
            self.count_label.setText("파일을 고르세요")
        else:
            self.count_label.setText(f"이벤트 {self.model.total:,}개")
