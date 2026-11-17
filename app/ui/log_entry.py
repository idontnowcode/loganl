"""'로그 입력' 탭 (변경 요청 CR-06). 정상·비정상 학습 탭을 하나로 합쳤다.

사용자가 하는 일: 로그를 끌어 놓고 가져오기 → 목록에서 로그를 골라 정상/비정상 → 어떤 동작이었는지 메모
→ (비정상이면) 에러로 판단한 근거 → 저장. 저장하면 바로 학습에 쓰인다(learning.log_entry).
패턴 확정·가이드 첨부·질문은 '자세히 편집' 창(예전 학습 화면)에서 한다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QItemSelectionModel, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.bootstrap import AppContext
from app.learning.log_entry import LABELS, LogEntry, LogEntryError, LogEntryService
from app.models.enums import OriginPurpose
from app.ui.log_import_panel import LogImportPanel
from app.ui.natural_input import StatusLine
from app.ui.theme import comfortable_table, scroll_wrap, set_role

ENTRY_COLUMNS = ("파일", "구분", "동작 메모", "상태")
SAVED_COLUMNS = ("파일", "구분", "동작 메모", "저장 시각")


@dataclass
class Draft:
    label: str | None
    memo: str
    criteria: str


class LogEntryPage(QWidget):
    entry_saved = Signal(int)  # case_id

    def __init__(self, context: AppContext, client_factory=None, open_detail: Callable[[str, int], None] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.service = LogEntryService(context.database)
        self.open_detail = open_detail
        self._entries: list[LogEntry] = []
        self._drafts: dict[int, Draft] = {}  # 저장하지 않은 입력(파일별)
        self._loading = False
        self._dirty = False
        self._shown_ids: list[int] = []

        self.import_panel = LogImportPanel(context, OriginPurpose.NORMAL_CASE)
        self.import_panel.files_changed.connect(lambda _ids: self.refresh_entries())
        self.runner = self.import_panel.runner

        steps = QLabel("진행 순서  ① 왼쪽에 로그를 끌어 놓고 가져오기 → ② 아래 목록에서 로그를 고르기(여러 개 가능) → "
                       "③ 정상/비정상, 어떤 동작이었는지 메모, 비정상이면 에러로 판단한 근거 → ④ 저장. "
                       "저장하면 바로 학습에 쓰입니다.")
        steps.setWordWrap(True)
        set_role(steps, "hint")

        # 가져온 로그 목록
        self.entry_table = QTableWidget(0, len(ENTRY_COLUMNS))
        self.entry_table.setHorizontalHeaderLabels(ENTRY_COLUMNS)
        self.entry_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.entry_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.entry_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.entry_table.verticalHeader().setVisible(False)
        header = self.entry_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        comfortable_table(self.entry_table, elide_middle=True)
        self.entry_table.setMinimumHeight(170)
        self.entry_table.itemSelectionChanged.connect(self._on_selection_changed)
        self.empty_label = QLabel("왼쪽에서 로그를 가져오면 여기에 파일마다 한 줄씩 나옵니다.")
        set_role(self.empty_label, "muted")
        list_box = QGroupBox("가져온 로그 (파일마다 정상/비정상과 메모)")
        list_layout = QVBoxLayout(list_box)
        list_layout.addWidget(self.entry_table)
        list_layout.addWidget(self.empty_label)

        # 선택한 로그 설명
        self.normal_radio = QRadioButton("정상")
        self.abnormal_radio = QRadioButton("비정상")
        self.label_group = QButtonGroup(self)
        self.label_group.setExclusive(True)
        self.label_group.addButton(self.normal_radio)
        self.label_group.addButton(self.abnormal_radio)
        self.label_group.buttonToggled.connect(lambda *_: self._on_label_changed())
        label_row = QHBoxLayout()
        label_row.addWidget(QLabel("구분"))
        label_row.addSpacing(8)
        label_row.addWidget(self.normal_radio)
        label_row.addWidget(self.abnormal_radio)
        label_row.addStretch(1)
        self.memo_label = QLabel("어떤 동작을 했었나요?")
        set_role(self.memo_label, "cell-label")
        self.memo_edit = QPlainTextEdit()
        self.memo_edit.setPlaceholderText("예) ○○ 동작을 처음부터 끝까지 한 번 돌림")
        self.memo_edit.setFixedHeight(76)
        self.memo_edit.setTabChangesFocus(True)
        self.criteria_label = QLabel("어떻게 에러라고 판단했나요? (비정상일 때)")
        set_role(self.criteria_label, "cell-label")
        self.criteria_edit = QPlainTextEdit()
        self.criteria_edit.setPlaceholderText("예) 화면에 TEST-E001이 뜨고 멈춰서 에러로 봄")
        self.criteria_edit.setFixedHeight(76)
        self.criteria_edit.setTabChangesFocus(True)
        for widget in (self.memo_edit, self.criteria_edit):
            widget.textChanged.connect(self._mark_dirty)
        self.save_button = QPushButton("저장 (바로 학습에 씀)")
        set_role(self.save_button, variant="primary")
        self.save_button.clicked.connect(self.save_selected)
        self.detail_button = QPushButton("자세히 편집…")
        self.detail_button.setToolTip("패턴 확정, 진단 가이드 첨부, 운전 모드, 확인 질문 등 예전 학습 화면")
        self.detail_button.clicked.connect(self.open_selected_detail)
        buttons = QHBoxLayout()
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.detail_button)
        buttons.addStretch(1)
        self.selection_label = QLabel()
        set_role(self.selection_label, "muted")
        self.message = StatusLine()
        editor_box = QGroupBox("선택한 로그 설명")
        editor = QVBoxLayout(editor_box)
        editor.addWidget(self.selection_label)
        editor.addLayout(label_row)
        editor.addWidget(self.memo_label)
        editor.addWidget(self.memo_edit)
        editor.addWidget(self.criteria_label)
        editor.addWidget(self.criteria_edit)
        editor.addLayout(buttons)
        editor.addWidget(self.message)

        # 저장된 로그
        self.saved_table = QTableWidget(0, len(SAVED_COLUMNS))
        self.saved_table.setHorizontalHeaderLabels(SAVED_COLUMNS)
        self.saved_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.saved_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.saved_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.saved_table.verticalHeader().setVisible(False)
        saved_header = self.saved_table.horizontalHeader()
        saved_header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        saved_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        comfortable_table(self.saved_table, elide_middle=True)
        self.saved_table.setMinimumHeight(170)
        self.saved_table.cellDoubleClicked.connect(lambda row, _c: self.bring_saved(row))
        saved_hint = QLabel("두 번 누르면 위 목록에 올려 보고 고칠 수 있습니다. 여러 로그가 함께 걸린 예전 사례는 "
                            "'관리 → 학습 사례 관리'에 있습니다.")
        saved_hint.setWordWrap(True)
        set_role(saved_hint, "muted")
        saved_box = QGroupBox("저장된 로그 (학습에 쓰는 중)")
        saved_layout = QVBoxLayout(saved_box)
        saved_layout.addWidget(self.saved_table)
        saved_layout.addWidget(saved_hint)

        side = QVBoxLayout()
        side.addWidget(steps)
        side.addWidget(list_box)
        side.addWidget(editor_box)
        side.addWidget(saved_box)
        side.addStretch(1)
        side_widget = QWidget()
        side_widget.setLayout(side)
        scroll = QScrollArea()
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidgetResizable(True)
        scroll.setWidget(side_widget)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(scroll_wrap(self.import_panel, horizontal=True))
        split.addWidget(scroll)
        split.setSizes([640, 720])
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(split, 1)

        self.refresh_entries()
        self.refresh_saved()

    # ── 목록 ──
    def refresh_entries(self, select: list[int] | None = None) -> None:
        keep = select if select is not None else self.selected_ids()
        self._entries = self.service.entries(self.import_panel.log_file_ids())
        self._loading = True
        self.entry_table.setRowCount(len(self._entries))
        for row, entry in enumerate(self._entries):
            draft = self._drafts.get(entry.log_file_id)
            label = draft.label if draft else entry.label
            memo = draft.memo if draft else entry.memo
            state = "입력 중 (저장 전)" if draft else entry.state_text()
            for column, value in enumerate((entry.file_name, LABELS.get(label or "", "—"),
                                            memo.splitlines()[0] if memo else "", state)):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, entry.log_file_id)
                if column == 2 and memo:
                    item.setToolTip(memo[:1000])
                self.entry_table.setItem(row, column, item)
        self.empty_label.setVisible(not self._entries)
        self.entry_table.clearSelection()
        rows = [i for i, e in enumerate(self._entries) if e.log_file_id in keep] or ([0] if self._entries else [])
        for row in rows:
            self._add_row_to_selection(row)
        self._loading = False
        self._show_selection()

    def _add_row_to_selection(self, row: int) -> None:
        model = self.entry_table.selectionModel()
        index = self.entry_table.model().index(row, 0)
        model.select(index, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)

    def refresh_saved(self) -> None:
        saved = self.service.saved_entries()
        self.saved_table.setRowCount(len(saved))
        for row, entry in enumerate(saved):
            stamp = (entry.approved_at or "")[:16].replace("T", " ")
            for column, value in enumerate((entry.file_name, LABELS.get(entry.label or "", "—"),
                                            entry.memo.splitlines()[0] if entry.memo else "", stamp)):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, entry.log_file_id)
                self.saved_table.setItem(row, column, item)

    def selected_ids(self) -> list[int]:
        rows = sorted({index.row() for index in self.entry_table.selectionModel().selectedRows()})
        return [self._entries[r].log_file_id for r in rows if r < len(self._entries)]

    def selected_entries(self) -> list[LogEntry]:
        wanted = set(self.selected_ids())
        return [e for e in self._entries if e.log_file_id in wanted]

    # ── 편집 ──
    def _on_selection_changed(self) -> None:
        if self._loading:
            return
        had_input = self._dirty
        self._keep_draft()
        if self.autosave(only=self._shown_ids) or had_input:
            self.refresh_entries(select=self.selected_ids())  # 상태 칸을 '저장됨'·'입력 중'으로
        else:
            self._show_selection()

    def _show_selection(self) -> None:
        entries = self.selected_entries()
        self._shown_ids = [e.log_file_id for e in entries]
        self._loading = True
        drafts = [self._drafts.get(e.log_file_id) for e in entries]
        values = [(d.label, d.memo, d.criteria) if d else (e.label, e.memo, e.criteria) for e, d in zip(entries, drafts)]
        common = values[0] if values and all(v == values[0] for v in values) else (None, "", "")
        self.label_group.setExclusive(False)
        self.normal_radio.setChecked(common[0] == "NORMAL")
        self.abnormal_radio.setChecked(common[0] == "ABNORMAL")
        self.label_group.setExclusive(True)
        self.memo_edit.setPlainText(common[1])
        self.criteria_edit.setPlainText(common[2])
        self._loading = False
        self._dirty = False
        enabled = bool(entries)
        for widget in (self.normal_radio, self.abnormal_radio, self.memo_edit, self.criteria_edit, self.save_button):
            widget.setEnabled(enabled)
        self.detail_button.setEnabled(len(entries) == 1 and entries[0].case_id is not None)
        if not entries:
            self.selection_label.setText("목록에서 로그를 고르세요.")
        elif len(entries) == 1:
            self.selection_label.setText(f"{entries[0].file_name} · {entries[0].state_text()}")
        else:
            self.selection_label.setText(f"로그 {len(entries)}개 선택 — 저장하면 같은 내용이 모두에 들어갑니다.")
        self._update_criteria_visibility()

    def _on_label_changed(self) -> None:
        self._update_criteria_visibility()
        self._mark_dirty()

    def _update_criteria_visibility(self) -> None:
        abnormal = self.abnormal_radio.isChecked()
        self.criteria_label.setVisible(abnormal)
        self.criteria_edit.setVisible(abnormal)

    def _mark_dirty(self) -> None:
        if not self._loading:
            self._dirty = True

    def current_label(self) -> str | None:
        if self.normal_radio.isChecked():
            return "NORMAL"
        if self.abnormal_radio.isChecked():
            return "ABNORMAL"
        return None

    def _keep_draft(self) -> None:
        if not self._dirty:
            return
        draft = Draft(self.current_label(), self.memo_edit.toPlainText(), self.criteria_edit.toPlainText())
        for log_file_id in self._shown_ids:
            self._drafts[log_file_id] = draft
        self._dirty = False

    # ── 저장 ──
    def save_selected(self) -> bool:
        ids = self.selected_ids()
        if not ids:
            self.message.setText("저장할 로그를 목록에서 고르세요.")
            return False
        self._keep_draft()
        draft = Draft(self.current_label(), self.memo_edit.toPlainText(), self.criteria_edit.toPlainText())
        for log_file_id in ids:
            self._drafts[log_file_id] = draft
        saved, error = self._save_drafts(ids)
        if error:
            self.message.setText(error)
        else:
            self.message.setText(f"로그 {saved}개를 저장했습니다. 이제 신규 로그 분석에서 비교에 쓰입니다." if saved
                                 else "바뀐 내용이 없습니다.")
        self.refresh_entries(select=ids)
        self.refresh_saved()
        return not error

    def _save_drafts(self, ids: list[int]) -> tuple[int, str]:
        saved = 0
        for log_file_id in ids:
            draft = self._drafts.get(log_file_id)
            if draft is None:
                continue
            try:
                before = self.service.entry(log_file_id)
                entry = self.service.save(log_file_id, draft.label or "", draft.memo, draft.criteria)
            except LogEntryError as exc:
                return saved, str(exc)
            del self._drafts[log_file_id]
            if entry.revision != before.revision or entry.case_id != before.case_id:
                saved += 1
                self.entry_saved.emit(entry.case_id)
        return saved, ""

    def autosave(self, only: list[int] | None = None) -> int:
        """탭을 바꾸거나 앱을 닫을 때, 다른 줄로 옮길 때: 다 적은 입력(구분이 있고 비정상이면 근거도 있음)만 저장한다.
        덜 적은 입력은 '입력 중'으로 남겨 둔다(지우지 않음)."""
        self._keep_draft()
        ready = [i for i, d in self._drafts.items()
                 if (only is None or i in only) and d.label and (d.label == "NORMAL" or d.criteria.strip())]
        if not ready:
            return 0
        saved, error = self._save_drafts(ready)
        if error:
            self.message.setText(error)
        elif saved:
            self.message.setText(f"입력한 로그 {saved}개를 자동 저장했습니다.")
        self.refresh_saved()
        return saved

    # ── 다른 화면과 잇기 ──
    def receive(self, log_file_ids: list[int], label: str | None = None) -> None:
        """신규 분석의 '학습 사례로 등록': 로그를 목록에 올리고 구분을 미리 골라 둔다(저장은 사용자가)."""
        self.import_panel.set_log_file_ids(self.import_panel.log_file_ids() + list(log_file_ids))
        if label in LABELS:
            for log_file_id in log_file_ids:
                entry = self.service.entry(log_file_id)
                self._drafts[log_file_id] = Draft(label, entry.memo, entry.criteria)
        self.refresh_entries(select=list(log_file_ids))
        self.message.setText("분석한 로그를 올렸습니다. 동작 메모(비정상이면 판단 근거도)를 적고 저장하세요.")

    def bring_saved(self, row: int) -> None:
        item = self.saved_table.item(row, 0)
        if item is None:
            return
        log_file_id = int(item.data(Qt.ItemDataRole.UserRole))
        if log_file_id not in self.import_panel.log_file_ids():
            self.import_panel.set_log_file_ids(self.import_panel.log_file_ids() + [log_file_id])
        self.refresh_entries(select=[log_file_id])

    def open_selected_detail(self) -> None:
        entries = self.selected_entries()
        if len(entries) != 1 or entries[0].case_id is None or self.open_detail is None:
            return
        self.autosave(only=[entries[0].log_file_id])
        entry = self.service.entry(entries[0].log_file_id)
        self.open_detail(entry.label or "NORMAL", entry.case_id)
        self.refresh_entries(select=[entry.log_file_id])
        self.refresh_saved()
