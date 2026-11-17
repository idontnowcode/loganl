"""공용 로그 입력 패널: 복수 파일 끌어 놓기·추가, 수집 세션 구분, 백그라운드 가져오기(진행률·취소), 파일 목록, 뷰어.

세 탭(정상 학습·비정상 학습·신규 분석)이 같은 패널을 용도(purpose)만 바꿔 쓴다.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.bootstrap import AppContext
from app.core.cancel import CancelToken
from app.core.importer import LogImporter
from app.models.enums import OriginPurpose
from app.models.records import FileMeta, ImportRequest, ImportResult, LogFileRecord, SessionMode
from app.parsers.parser_registry import ParserRegistry
from app.storage.repositories import EventRepository, LogFileRepository
from app.ui.log_viewer import LogViewer
from app.ui.natural_input import Collapsible
from app.ui.theme import comfortable_table, set_role
from app.ui.timeline_view import TimelineView
from app.ui.workers import TaskRunner

SESSION_CHOICES = (
    (SessionMode.SAME, "같은 수집 세션 — 같은 시점에 여러 컴포넌트에서 수집"),
    (SessionMode.SEPARATE, "서로 다른 수집 세션 — 다른 시점의 재현·반복 수집"),
    (SessionMode.UNKNOWN, "모름 — 나중에 확인"),
)
BASIS_LABELS = {
    "ABSOLUTE": "절대시각", "ABSOLUTE_NO_TZ": "절대시각(시간대 없음)", "RELATIVE": "경과 시간",
    "NONE": "없음", "UNKNOWN": "모름",
}
FILE_COLUMNS = ("파일", "세션", "인코딩", "줄", "이벤트", "타임스탬프", "파서", "역할")


class DropList(QListWidget):
    paths_dropped = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMinimumHeight(70)
        self.setMaximumHeight(150)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        self.paths_dropped.emit(paths)
        event.acceptProposedAction()


class LogImportPanel(QWidget):
    files_changed = Signal(list)  # 이 패널에 올라온 log_file_id 목록
    import_finished = Signal(object)  # ImportResult

    def __init__(self, context: AppContext, purpose: OriginPurpose, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.purpose = purpose
        self.registry = ParserRegistry.default()
        self.runner = TaskRunner(self)
        self._cancel: CancelToken | None = None
        self._ids: list[int] = []

        self.pending = DropList()
        self.pending.setMaximumHeight(130)  # 옵션을 접은 뒤 대기 목록이 자리를 다 먹지 않게
        self.pending.paths_dropped.connect(self.add_paths)
        self.pending.setToolTip("TXT/LOG 파일을 여기로 끌어 놓으세요")
        add_button = QPushButton("파일 추가…")
        add_button.clicked.connect(self._browse)
        clear_button = QPushButton("대기 목록 비우기")
        clear_button.clicked.connect(self.pending.clear)

        self.session_combo = QComboBox()
        for mode, label in SESSION_CHOICES:
            self.session_combo.addItem(label, mode)
        self.session_combo.setCurrentIndex(2)
        self.session_label_edit = QLineEdit()
        self.session_label_edit.setPlaceholderText("비우면 날짜로 이름 붙임")
        self.profile_combo = QComboBox()
        self.profile_combo.addItem("자동 감지", None)
        for name in self.registry.names():
            self.profile_combo.addItem(name, name)
        self.profile_combo.addItem("원문 (한 줄 = 이벤트 하나)", "raw")
        self.role_edit = QLineEdit()
        self.role_edit.setPlaceholderText("예: 컴포넌트 이름 (선택)")
        self.fw_edit = QLineEdit()
        self.fw_edit.setPlaceholderText("선택")

        self.import_button = QPushButton("가져오기")
        set_role(self.import_button, variant="primary")
        self.import_button.clicked.connect(self.start_import)
        self.cancel_button = QPushButton("취소")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_import)
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setFormat("대기")
        self.message_label = QLabel()
        self.message_label.setWordWrap(True)
        set_role(self.message_label, "muted")
        self.message_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.file_table = QTableWidget(0, len(FILE_COLUMNS))
        self.file_table.setHorizontalHeaderLabels(FILE_COLUMNS)
        self.file_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.file_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.file_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.file_table.verticalHeader().setVisible(False)
        self.file_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.file_table.horizontalHeader().setStretchLastSection(True)
        self.file_table.currentCellChanged.connect(lambda row, *_: self._show_row(row))
        comfortable_table(self.file_table, elide_middle=True)
        self.file_table.setMinimumHeight(150)  # 머리 + 3줄
        remove_button = QPushButton("선택 파일을 이 목록에서 빼기")
        remove_button.clicked.connect(self._remove_selected)

        self.viewer = LogViewer(context.database)

        options = QFormLayout()
        options.addRow("수집 세션", self.session_combo)  # CR-07 LI-10: 아래 '가져오기 옵션'에 접혀 있다
        options.addRow("세션 이름", self.session_label_edit)
        options.addRow("파서", self.profile_combo)
        options.addRow("로그 역할", self.role_edit)
        options.addRow("FW 버전", self.fw_edit)

        pending_box = QVBoxLayout()
        pending_box.addWidget(QLabel("가져올 파일 (끌어 놓기 가능)"))
        pending_box.addWidget(self.pending)
        buttons = QHBoxLayout()
        buttons.addWidget(add_button)
        buttons.addWidget(clear_button)
        pending_box.addLayout(buttons)

        # CR-07 LI-10: 옵션은 보통 건드릴 일이 없어 접어 둔다(지우지 않음). 대기 목록이 넓게 보인다.
        options_widget = QWidget()
        options_widget.setLayout(options)
        options.setContentsMargins(0, 0, 0, 0)
        self.options_section = Collapsible("가져오기 옵션 (수집 세션·파서·로그 역할·FW — 보통은 그대로)", options_widget)
        top = QVBoxLayout()
        top.addLayout(pending_box)
        top.addWidget(self.options_section)

        run_row = QHBoxLayout()
        run_row.addWidget(self.import_button)
        run_row.addWidget(self.cancel_button)
        run_row.addWidget(self.progress, 1)

        files_box = QWidget()
        files_layout = QVBoxLayout(files_box)
        files_layout.setContentsMargins(0, 0, 0, 0)
        files_layout.addWidget(QLabel("올라온 로그"))
        files_layout.addWidget(self.file_table)
        files_layout.addWidget(remove_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.timeline = TimelineView(context.database)
        self.timeline.go_to.connect(self.show_line)
        self.views = QTabWidget()
        self.views.addTab(self.viewer, "이벤트 표 (파일별)")
        self.views.addTab(self.timeline, "세션 타임라인 (여러 파일)")
        self.views.setMinimumHeight(400)
        self.views.currentChanged.connect(lambda index: index == 1 and self.timeline.set_records(self.records()))
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(files_box)
        split.addWidget(self.views)
        split.setSizes([180, 460])
        split.setChildrenCollapsible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        layout.addLayout(run_row)
        layout.addWidget(self.message_label)
        layout.addWidget(split, 1)

    # ── 공개 동작 ──
    def add_paths(self, paths: list[Path]) -> None:
        existing = {self.pending.item(i).text() for i in range(self.pending.count())}
        for path in paths:
            text = str(Path(path))
            if text not in existing:
                self.pending.addItem(text)
                existing.add(text)

    def pending_paths(self) -> list[Path]:
        return [Path(self.pending.item(i).text()) for i in range(self.pending.count())]

    def log_file_ids(self) -> list[int]:
        return list(self._ids)

    def set_log_file_ids(self, ids: list[int]) -> None:
        self._ids = list(dict.fromkeys(ids))
        self.refresh_files()
        self.files_changed.emit(self.log_file_ids())

    def set_import_enabled(self, enabled: bool) -> None:
        for widget in (self.pending, self.import_button, self.session_combo, self.session_label_edit,
                       self.profile_combo, self.role_edit, self.fw_edit):
            widget.setEnabled(enabled)
        for button in self.findChildren(QPushButton):
            if button.text() in ("파일 추가…", "대기 목록 비우기", "선택 파일을 이 목록에서 빼기"):
                button.setEnabled(enabled)

    def clear(self) -> None:
        self.pending.clear()
        self.message_label.clear()
        self.set_log_file_ids([])

    def build_request(self) -> ImportRequest:
        return ImportRequest(
            paths=self.pending_paths(),
            purpose=self.purpose,
            session_mode=self.session_combo.currentData(),
            session_label=self.session_label_edit.text().strip() or None,
            profile=self.profile_combo.currentData(),
            meta=FileMeta(log_role=self.role_edit.text().strip() or None, fw_version=self.fw_edit.text().strip() or None),
        )

    def start_import(self) -> None:
        if self.runner.busy:
            return
        request = self.build_request()
        if not request.paths:
            self.message_label.setText("가져올 파일을 먼저 추가하세요.")
            return
        importer = LogImporter(self.context.database, self.context.paths, self.context.settings.import_, self.registry)
        self._cancel = CancelToken()
        self._set_running(True)
        self.runner.start(
            importer.import_logs, request, cancel=self._cancel,
            on_done=self._on_done, on_error=self._on_error, on_progress=self._on_progress,
        )

    def cancel_import(self) -> None:
        if self._cancel is not None:
            self._cancel.cancel()
            self.progress.setFormat("취소하는 중…")

    def refresh_files(self) -> None:
        with closing(self.context.database.connect()) as conn:
            records = LogFileRepository(conn).get_many(self._ids)
        by_id = {r.id: r for r in records}
        self._records = [by_id[i] for i in self._ids if i in by_id]
        self.file_table.setRowCount(len(self._records))
        for row, record in enumerate(self._records):
            values = (
                record.original_name, record.session_label or str(record.session_id), record.encoding or "",
                f"{record.line_count or 0:,}", self._event_count(record), BASIS_LABELS.get(record.timestamp_basis, ""),
                record.parser_profile or "", record.log_role or "",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.ItemDataRole.UserRole, record.id)
                self.file_table.setItem(row, column, item)
        if self._records:
            self.file_table.selectRow(0)
            self._show_row(0)
        else:
            self.viewer.show_file(None)
        if self.views.currentIndex() == 1:
            self.timeline.set_records(self._records)

    def show_line(self, log_file_id: int, line_no: int) -> bool:
        """원본 행으로 이동: 파일을 고르고 이벤트 표 탭으로 바꾼다."""
        for index, record in enumerate(self.records()):
            if record.id == log_file_id:
                self.file_table.selectRow(index)
                break
        self.views.setCurrentIndex(0)
        return self.viewer.go_to_line(log_file_id, line_no)

    def records(self) -> list[LogFileRecord]:
        return list(getattr(self, "_records", []))

    # ── 내부 ──
    def _event_count(self, record: LogFileRecord) -> str:
        with closing(self.context.database.connect()) as conn:
            return f"{EventRepository(conn).count(record.id):,}"

    def _browse(self) -> None:
        exts = " ".join(f"*{e}" for e in self.context.settings.import_.log_extensions)
        files, _ = QFileDialog.getOpenFileNames(self, "로그 파일 선택", "", f"로그 파일 ({exts});;모든 파일 (*)")
        self.add_paths([Path(f) for f in files])

    def _show_row(self, row: int) -> None:
        records = self.records()
        if 0 <= row < len(records):
            self.viewer.show_file(records[row].id)

    def _remove_selected(self) -> None:
        row = self.file_table.currentRow()
        records = self.records()
        if 0 <= row < len(records):
            self.set_log_file_ids([i for i in self._ids if i != records[row].id])

    def _set_running(self, running: bool) -> None:
        self.import_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        if running:
            self.progress.setRange(0, 0)
            self.progress.setFormat("가져오는 중…")

    def _on_progress(self, done: int, total: int, message: str) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)
        self.progress.setFormat(f"{message} ({done}/{total})")

    def _on_done(self, result: ImportResult) -> None:
        self._set_running(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.progress.setFormat("취소함" if result.cancelled else "완료")
        lines = []
        new = [f for f in result.files if not f.duplicate]
        lines.append(f"가져온 파일 {len(new)}개, 이미 있던 파일 {len(result.files) - len(new)}개, 거부 {len(result.rejected)}개.")
        for rejected in result.rejected:
            lines.append(f"거부: {Path(rejected.path).name} — {rejected.reason}")
        for imported in result.files:
            for warning in imported.warnings:
                lines.append(f"{imported.original_name}: {warning}")
        if result.cancelled:
            lines.append("취소해서 나머지 파일은 가져오지 않았습니다.")
        self.message_label.setText("\n".join(lines))
        done_paths = {Path(r.path) for r in result.rejected}
        if not result.cancelled:
            self.pending.clear()
            for path in sorted(done_paths):
                self.pending.addItem(str(path))
        self.set_log_file_ids(self._ids + result.log_file_ids)
        self.import_finished.emit(result)

    def _on_error(self, message: str) -> None:
        self._set_running(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFormat("실패")
        self.message_label.setText(f"가져오지 못했습니다: {message}")
