"""공용 로그 입력 패널: 끌어 놓거나 고르면 바로 가져오기(진행률·취소), 수집 세션 구분, 올라온 로그 목록, 원문 보기.

로그 입력·신규 분석·자세히 편집 화면이 같은 패널을 용도(purpose)만 바꿔 쓴다.
CR-10: 대기 목록과 '가져오기' 단추 없이 넣으면 바로 가져온다(LI-11), 목록 전체 비우기(LI-12),
이벤트 표·세션 타임라인은 '로그 원문 보기'에 접어 둔다(LI-13).
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
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
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
from app.ui.theme import comfortable_table, set_role, tone_color
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


class DropZone(QFrame):
    """로그 파일을 끌어 놓는 칸. 놓으면 바로 가져온다(CR-10 LI-11)."""

    paths_dropped = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        set_role(self, "dropzone")
        self.label = QLabel("로그 파일(TXT·LOG)을 여기로 끌어 놓거나 '파일 추가…'를 누르세요.\n"
                            "넣으면 바로 가져와 아래 '올라온 로그'에 올라갑니다.")
        self.label.setWordWrap(True)
        self.add_button = QPushButton("파일 추가…")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.addWidget(self.label, 1)
        layout.addWidget(self.add_button, 0, Qt.AlignmentFlag.AlignVCenter)

    def _set_active(self, active: bool) -> None:
        self.setProperty("active", "true" if active else "false")
        set_role(self)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._set_active(True)

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._set_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        self._set_active(False)
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        self.paths_dropped.emit(paths)
        event.acceptProposedAction()


class LogImportPanel(QWidget):
    files_changed = Signal(list)  # 이 패널에 올라온 log_file_id 목록
    import_finished = Signal(object)  # ImportResult (한 번 가져올 때마다)
    current_file_changed = Signal(object)  # 목록에서 고른 log_file_id (없으면 None)
    removing = Signal(list)  # 목록에서 빼기 직전의 log_file_id (로그 입력 탭이 입력을 먼저 저장한다)
    checks_changed = Signal()  # 체크 칸을 바꿈(CR-12, checkable일 때)

    def __init__(self, context: AppContext, purpose: OriginPurpose, parent: QWidget | None = None, *,
                 status_column: str | None = None, allow_clear: bool = False, checkable: bool = False,
                 compact: bool = False, detach_views: bool = False) -> None:
        super().__init__(parent)
        self.context = context
        self.purpose = purpose
        self.registry = ParserRegistry.default()
        self.runner = TaskRunner(self)
        self._cancel: CancelToken | None = None
        self._ids: list[int] = []
        self._records: list[LogFileRecord] = []
        self._queue: list[Path] = []  # 가져오는 중에 더 넣은 파일(끝나면 이어서)
        self._status: dict[int, tuple[str, str | None, str | None]] = {}  # id → (글, 색조, 풀이)
        self.checkable = checkable  # CR-12: 분석할 로그를 체크로 고른다(기본 모두 체크)
        self._unchecked: set[int] = set()
        self.status_column = status_column
        self.columns = (FILE_COLUMNS[0], status_column, *FILE_COLUMNS[1:]) if status_column else FILE_COLUMNS

        self.drop_zone = DropZone()
        self.drop_zone.paths_dropped.connect(self.add_paths)
        self.add_button = self.drop_zone.add_button
        self.add_button.clicked.connect(self._browse)

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

        self.cancel_button = QPushButton("가져오기 취소")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_import)
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setFormat("대기")
        self.message_label = QLabel()
        self.message_label.setWordWrap(True)
        set_role(self.message_label, "muted")
        self.message_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.file_table = QTableWidget(0, len(self.columns))
        self.file_table.setHorizontalHeaderLabels(self.columns)
        self.file_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.file_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.file_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.file_table.verticalHeader().setVisible(False)
        self.file_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.file_table.horizontalHeader().setStretchLastSection(True)
        self.file_table.currentCellChanged.connect(lambda row, *_: self._on_current_row(row))
        self.file_table.itemChanged.connect(self._on_item_changed)
        if compact:  # CR-12: 자주 안 보는 열은 숨긴다(파일 정보는 툴팁·원문 보기에)
            for name in ("인코딩", "타임스탬프", "파서", "역할"):
                self.file_table.setColumnHidden(self.columns.index(name), True)
        comfortable_table(self.file_table, elide_middle=True)
        self.file_table.setMinimumHeight(220)  # 머리 + 5줄. 많으면 표 안에서 스크롤
        self.files_title = QLabel("올라온 로그")
        set_role(self.files_title, "cell-label")
        self.remove_button = QPushButton("선택 파일을 이 목록에서 빼기")
        self.remove_button.clicked.connect(self._remove_selected)
        self.clear_button = QPushButton("목록 비우기")
        self.clear_button.setToolTip("올라온 로그를 모두 목록에서 뺍니다. 가져온 로그와 저장한 내용은 지우지 않습니다.")
        self.clear_button.clicked.connect(self.clear_list)
        self.clear_button.setVisible(allow_clear)

        self.viewer = LogViewer(context.database)

        options = QFormLayout()
        options.addRow("수집 세션", self.session_combo)  # CR-07 LI-10: 아래 '가져오기 옵션'에 접혀 있다
        options.addRow("세션 이름", self.session_label_edit)
        options.addRow("파서", self.profile_combo)
        options.addRow("로그 역할", self.role_edit)
        options.addRow("FW 버전", self.fw_edit)
        options_widget = QWidget()
        options_widget.setLayout(options)
        options.setContentsMargins(0, 0, 0, 0)
        self.options_section = Collapsible("가져오기 옵션 (수집 세션·파서·로그 역할·FW — 보통은 그대로, 파일을 넣기 전에)",
                                           options_widget)

        run_row = QHBoxLayout()
        run_row.addWidget(self.progress, 1)
        run_row.addWidget(self.cancel_button)

        list_buttons = QHBoxLayout()
        list_buttons.addWidget(self.remove_button)
        list_buttons.addWidget(self.clear_button)
        list_buttons.addStretch(1)

        # CR-10 LI-13: 이벤트 표·세션 타임라인은 보통 볼 일이 없어 접어 둔다(지우지 않음)
        self.timeline = TimelineView(context.database)
        self.timeline.go_to.connect(self.show_line)
        self.views = QTabWidget()
        self.views.addTab(self.viewer, "이벤트 표 (파일별)")
        self.views.addTab(self.timeline, "세션 타임라인 (여러 파일)")
        self.views.setMinimumHeight(420)
        self.views.currentChanged.connect(lambda index: index == 1 and self.timeline.set_records(self.records()))
        self.views_section = Collapsible("로그 원문 보기 (이벤트 표·세션 타임라인)", self.views)
        self.views_section.toggle.toggled.connect(self._on_views_toggled)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.drop_zone)
        layout.addWidget(self.options_section)
        layout.addLayout(run_row)
        layout.addWidget(self.message_label)
        layout.addWidget(self.files_title)
        layout.addWidget(self.file_table, 1)
        layout.addLayout(list_buttons)
        if not detach_views:  # detach_views면 화면이 views_section을 다른 자리(분석 ③)에 둔다
            layout.addWidget(self.views_section)

    # ── 공개 동작 ──
    def add_paths(self, paths: list[Path]) -> None:
        """넣은 파일을 바로 가져온다. 가져오는 중이면 끝난 뒤 이어서 가져온다."""
        for path in paths:
            path = Path(path)
            if path not in self._queue:
                self._queue.append(path)
        self.start_import()

    def pending_paths(self) -> list[Path]:
        """아직 가져오지 않은 파일(가져오는 중에 더 넣은 것)."""
        return list(self._queue)

    def log_file_ids(self) -> list[int]:
        return list(self._ids)

    def set_log_file_ids(self, ids: list[int]) -> None:
        self._ids = list(dict.fromkeys(ids))
        self._status = {k: v for k, v in self._status.items() if k in self._ids}
        self.refresh_files()
        self.files_changed.emit(self.log_file_ids())

    def set_import_enabled(self, enabled: bool) -> None:
        for widget in (self.drop_zone, self.add_button, self.session_combo, self.session_label_edit,
                       self.profile_combo, self.role_edit, self.fw_edit, self.remove_button, self.clear_button):
            widget.setEnabled(enabled)

    def clear(self) -> None:
        self._queue.clear()
        self.message_label.clear()
        self.set_log_file_ids([])

    def clear_list(self) -> None:
        """CR-10 LI-12: 올라온 로그를 모두 목록에서 뺀다. DB의 로그·사례는 그대로다."""
        if not self._ids:
            return
        count = len(self._ids)
        self.removing.emit(list(self._ids))
        self.set_log_file_ids([])
        self.message_label.setText(f"목록에서 로그 {count}개를 뺐습니다. 가져온 로그와 저장한 내용은 그대로 있습니다.")

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
            return  # 지금 가져오기가 끝나면 이어서 가져온다(_on_done)
        request = self.build_request()
        if not request.paths:
            self.message_label.setText("가져올 파일을 위 칸에 끌어 놓거나 '파일 추가…'로 고르세요.")
            return
        self._queue.clear()
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
            self._queue.clear()
            self.progress.setFormat("취소하는 중…")

    def set_status(self, log_file_id: int, text: str, tone: str | None = None, tooltip: str | None = None) -> None:
        """결과 칸(status_column)에 글을 적는다. 신규 분석의 로그별 결과(CR-10 NA-20)."""
        self._status[log_file_id] = (text, tone, tooltip)
        for row, record in enumerate(self._records):
            if record.id == log_file_id:
                self._fill_status(row, record.id)

    def checked_ids(self) -> list[int]:
        """체크한 로그(체크 칸이 없으면 모두)."""
        return [i for i in self._ids if not (self.checkable and i in self._unchecked)]

    def status_of(self, log_file_id: int) -> tuple[str, str | None, str | None]:
        return self._status.get(log_file_id, ("—", None, None))

    def current_file_id(self) -> int | None:
        row = self.file_table.currentRow()
        return self._records[row].id if 0 <= row < len(self._records) else None

    def select_file(self, log_file_id: int) -> bool:
        for row, record in enumerate(self._records):
            if record.id == log_file_id:
                self.file_table.selectRow(row)
                return True
        return False

    def refresh_files(self) -> None:
        keep = self.current_file_id()
        with closing(self.context.database.connect()) as conn:
            records = LogFileRepository(conn).get_many(self._ids)
            counts = {r.id: EventRepository(conn).count(r.id) for r in records}
        by_id = {r.id: r for r in records}
        self._records = [by_id[i] for i in self._ids if i in by_id]
        self.file_table.blockSignals(True)
        self.file_table.setRowCount(len(self._records))
        for row, record in enumerate(self._records):
            values = (
                record.original_name, record.session_label or str(record.session_id), record.encoding or "",
                f"{record.line_count or 0:,}", f"{counts.get(record.id, 0):,}", BASIS_LABELS.get(record.timestamp_basis, ""),
                record.parser_profile or "", record.log_role or "",
            )
            if self.status_column:
                values = (values[0], "", *values[1:])
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.ItemDataRole.UserRole, record.id)
                if column == 0:
                    item.setToolTip(f"{record.original_name}\n인코딩 {record.encoding or '-'} · 시각 "
                                    f"{BASIS_LABELS.get(record.timestamp_basis, '-')} · 파서 {record.parser_profile or '-'}")
                    if self.checkable:
                        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                        item.setCheckState(Qt.CheckState.Unchecked if record.id in self._unchecked
                                           else Qt.CheckState.Checked)
                self.file_table.setItem(row, column, item)
            self._fill_status(row, record.id)
        self.file_table.blockSignals(False)
        self.files_title.setText(f"올라온 로그 ({len(self._records)}개)" if self._records else "올라온 로그")
        if self._records:
            ids = [r.id for r in self._records]
            row = ids.index(keep) if keep in ids else 0
            self.file_table.setCurrentCell(row, 0)
            self.file_table.selectRow(row)
            self._on_current_row(row)
        else:
            self.viewer.show_file(None)
            self.current_file_changed.emit(None)
        if self.views.currentIndex() == 1:
            self.timeline.set_records(self._records)

    def show_line(self, log_file_id: int, line_no: int) -> bool:
        """원본 행으로 이동: 원문 보기를 펴고, 파일을 고르고, 이벤트 표 탭으로 바꾼다."""
        self.views_section.set_expanded(True)
        self.select_file(log_file_id)
        self.views.setCurrentIndex(0)
        return self.viewer.go_to_line(log_file_id, line_no)

    def records(self) -> list[LogFileRecord]:
        return list(self._records)

    # ── 내부 ──
    def _fill_status(self, row: int, log_file_id: int) -> None:
        if not self.status_column:
            return
        text, tone, tooltip = self._status.get(log_file_id, ("—", None, None))
        item = QTableWidgetItem(text)
        item.setData(Qt.ItemDataRole.UserRole, log_file_id)
        if tone:
            item.setForeground(tone_color(tone))
        if tooltip:
            item.setToolTip(tooltip)
        self.file_table.setItem(row, 1, item)

    def _browse(self) -> None:
        exts = " ".join(f"*{e}" for e in self.context.settings.import_.log_extensions)
        files, _ = QFileDialog.getOpenFileNames(self, "로그 파일 선택", "", f"로그 파일 ({exts});;모든 파일 (*)")
        if files:
            self.add_paths([Path(f) for f in files])

    def _on_current_row(self, row: int) -> None:
        records = self.records()
        if not 0 <= row < len(records):
            return
        if self.views_section.is_expanded():  # 접혀 있으면 이벤트를 읽지 않는다(큰 로그도 목록 넘김이 빠르게)
            self.viewer.show_file(records[row].id)
        self.current_file_changed.emit(records[row].id)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if not self.checkable or item.column() != 0:
            return
        log_file_id = item.data(Qt.ItemDataRole.UserRole)
        if log_file_id is None:
            return
        before = set(self._unchecked)
        if item.checkState() == Qt.CheckState.Checked:
            self._unchecked.discard(log_file_id)
        else:
            self._unchecked.add(log_file_id)
        if before != self._unchecked:
            self.checks_changed.emit()

    def set_checked(self, log_file_id: int, checked: bool) -> None:
        for row, record in enumerate(self._records):
            if record.id == log_file_id:
                self.file_table.item(row, 0).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def _on_views_toggled(self, expanded: bool) -> None:
        if not expanded:
            return
        current = self.current_file_id()
        if current is not None and self.viewer.model.log_file_id != current:
            self.viewer.show_file(current)
        if self.views.currentIndex() == 1:
            self.timeline.set_records(self._records)

    def _remove_selected(self) -> None:
        current = self.current_file_id()
        if current is not None:
            self.removing.emit([current])
            self.set_log_file_ids([i for i in self._ids if i != current])

    def _set_running(self, running: bool) -> None:
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
        self.progress.setFormat("취소함" if result.cancelled else "가져오기 완료")
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
        self.set_log_file_ids(self._ids + result.log_file_ids)
        self.import_finished.emit(result)
        if self._queue and not result.cancelled:
            self.start_import()  # 가져오는 동안 더 넣은 파일

    def _on_error(self, message: str) -> None:
        self._set_running(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFormat("실패")
        self.message_label.setText(f"가져오지 못했습니다: {message}")
        if self._queue:
            self.start_import()
