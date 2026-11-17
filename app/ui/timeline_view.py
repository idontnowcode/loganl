"""세션 타임라인 (계획서 §8.2·§8.4 "이벤트 타임라인", §4.3).

같은 수집 세션의 로그들을 한 시간축으로 보여 준다. 시계 기준이 같고(시간대 있는 절대시각) 또는 확인된 오프셋이 있을 때만
시각으로 섞고, 그 밖에는 파일마다 내부 순서대로 이어 붙인 뒤 "섞지 않음"을 알린다. 서로 다른 세션은 합치지 않는다.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.sequence_analyzer import build_timeline
from app.models.records import LogFileRecord
from app.storage.database import Database
from app.storage.repositories import EventRepository, SessionRepository
from app.ui.theme import comfortable_table, set_role

MAX_ROWS = 5000


def format_us(value: int | None, kind: str) -> str:
    if value is None:
        return ""
    if kind == "RELATIVE":
        return f"+{value / 1_000_000:.6f}s"
    moment = datetime.fromtimestamp(value / 1_000_000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + ("Z" if kind == "ABSOLUTE" else "")


class TimelineView(QWidget):
    go_to = Signal(int, int)  # (log_file_id, line_no)

    def __init__(self, database: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.database = database
        self.records: list[LogFileRecord] = []
        self.session_combo = QComboBox()
        self.session_combo.currentIndexChanged.connect(lambda _: self.render())
        self.note = QLabel()
        self.note.setWordWrap(True)
        set_role(self.note, "muted")
        self.table = QTableWidget(0, 6)
        comfortable_table(self.table)
        self.table.setHorizontalHeaderLabels(["순서", "시각", "파일", "행", "레벨", "메시지"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.cellDoubleClicked.connect(self._jump)
        top = QHBoxLayout()
        top.addWidget(QLabel("수집 세션"))
        top.addWidget(self.session_combo, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        layout.addWidget(self.note)
        layout.addWidget(self.table, 1)

    def set_records(self, records: list[LogFileRecord]) -> None:
        self.records = list(records)
        sessions: dict[int, str] = {}
        for r in self.records:
            sessions.setdefault(r.session_id, r.session_label or f"세션 {r.session_id}")
        self.session_combo.blockSignals(True)
        self.session_combo.clear()
        for session_id, label in sessions.items():
            count = sum(1 for r in self.records if r.session_id == session_id)
            self.session_combo.addItem(f"{label} (로그 {count}개)", session_id)
        self.session_combo.blockSignals(False)
        self.render()

    def render(self) -> None:
        session_id = self.session_combo.currentData()
        self.table.setRowCount(0)
        if session_id is None:
            self.note.setText("로그를 올리면 수집 세션별 타임라인이 보입니다.")
            return
        group = [r for r in self.records if r.session_id == session_id]
        with closing(self.database.connect()) as conn:
            events = EventRepository(conn).for_files([r.id for r in group])
            session = SessionRepository(conn).get(session_id)
        timeline = build_timeline([(r, events[r.id]) for r in group],
                                  session_synced=session.time_sync_status in ("SYNCED", "OFFSET_KNOWN"))
        names = {r.id: r.original_name for r in group}
        rows = timeline.entries[:MAX_ROWS]
        self.table.setRowCount(len(rows))
        for index, entry in enumerate(rows):
            e = entry.event
            values = (str(index + 1), format_us(entry.corrected_us, str(e.ts_kind)) or (e.ts_raw or ""),
                      names[entry.log_file_id], str(e.line_no), e.level or "", e.message.split("\n", 1)[0])
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, (entry.log_file_id, e.line_no))
                self.table.setItem(index, column, item)
        others = len({r.session_id for r in self.records}) - 1
        parts = []
        if len(group) == 1:
            parts.append("로그 1개: 파일 안 순서 그대로입니다.")
        elif timeline.merged_by_time:
            parts.append(f"로그 {len(group)}개를 시각으로 한 시간축에 합쳤습니다.")
        else:
            parts.append(f"로그 {len(group)}개를 시각으로 섞지 않고 파일마다 이어 붙였습니다.")
            parts.extend(timeline.notes)
        if others > 0:
            parts.append(f"다른 수집 세션 {others}개는 합치지 않습니다(위에서 세션을 바꿔 보세요).")
        if len(timeline.entries) > MAX_ROWS:
            parts.append(f"이벤트가 많아 앞의 {MAX_ROWS:,}개만 보여 줍니다.")
        self.note.setText(" ".join(parts) + " 두 번 누르면 원본 행으로 이동합니다.")

    def _jump(self, row: int, _column: int) -> None:
        item = self.table.item(row, 0)
        if item is not None:
            log_file_id, line_no = item.data(Qt.ItemDataRole.UserRole)
            self.go_to.emit(log_file_id, line_no)
