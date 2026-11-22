"""'관리 → 참고 자료 관리' (변경 요청 CR-04). 오류 코드 목록·설명 문서를 올리고, 항목을 확인·고친 뒤 문서 단위로 승인한다.

승인한 문서만 신규 분석과 AI가 참고한다. 앱은 코드의 뜻을 만들지 않는다(값은 문서 글자 그대로).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.bootstrap import AppContext
from app.core.reference_reader import FORMATS, ReferenceReadError
from app.core.reference_structurer import ROLE_LABELS, ROLES, judgment_side
from app.learning.reference_library import STATUS_LABELS, ReferenceError, ReferenceLibrary
from app.ui.natural_input import StatusLine
from app.ui.theme import STATUS_TONE, comfortable_table, set_role

DOC_COLUMNS = ("제목", "형식", "상태", "항목·문단")
ENTRY_COLUMNS = ("포함", "코드", "명칭", "설명", "원인", "조치", "판정", "원문 위치")
ENTRY_FIELDS = (None, "code", "title", "description", "cause", "action", "judgment", "locator")
JUDGMENT_USE = {"ABNORMAL": "판정에 씀: 이 항목이 로그에 나오면 비정상 의심", "NORMAL": "판정에 씀: 이 항목이 나온 줄은 문제없음"}
SOURCE_LABELS = {"EXTRACTED": "문서에서", "USER": "직접 입력"}


class ReferenceManagerDialog(QDialog):
    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("참고 자료 관리")
        self.resize(1320, 820)
        self.library = ReferenceLibrary(context.database, context.paths, context.settings.import_)
        self.document_id: int | None = None
        self._filling = False
        self._read = None
        self._header_row = 0

        intro = QLabel("에러 코드 목록·코드별 설명, 이슈 설명서·해결 기록 같은 자료(TXT·MD·엑셀·PDF·워드)를 올리세요. "
                       "코드표는 항목으로, 글은 문단으로 읽습니다. 확인하고 '승인'하면 그때부터 신규 분석에서 로그에 나온 코드의 "
                       "설명·원인·조치와 관련 문단을 따로 보여 주고, AI도 근거로 참고합니다. "
                       "앱은 코드의 뜻을 만들지 않습니다. 승인한 문서는 고칠 수 없고, 고친 파일을 다시 올리면 새 버전이 됩니다.")
        intro.setWordWrap(True)
        set_role(intro, "hint")

        # 왼쪽: 문서 목록
        self.doc_table = QTableWidget(0, len(DOC_COLUMNS))
        self.doc_table.setHorizontalHeaderLabels(DOC_COLUMNS)
        self.doc_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.doc_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.doc_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.doc_table.verticalHeader().setVisible(False)
        self.doc_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.doc_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        comfortable_table(self.doc_table, elide_middle=True)
        self.doc_table.itemSelectionChanged.connect(self._on_doc_selected)
        self.show_archived = QCheckBox("보관한 문서도 보기")
        self.show_archived.toggled.connect(lambda _c: self.refresh_documents())
        self.import_button = QPushButton("자료 올리기…")
        set_role(self.import_button, variant="primary")
        self.import_button.clicked.connect(self._browse)
        self.approve_button = QPushButton("승인")
        self.approve_button.clicked.connect(self.approve)
        self.archive_button = QPushButton("보관")
        self.archive_button.clicked.connect(self.archive)
        self.delete_button = QPushButton("초안 삭제")
        set_role(self.delete_button, variant="danger")
        self.delete_button.clicked.connect(self.delete_draft)
        doc_buttons = QGridLayout()
        for index, button in enumerate((self.import_button, self.approve_button, self.archive_button, self.delete_button)):
            doc_buttons.addWidget(button, index // 2, index % 2)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addLayout(doc_buttons)
        left_layout.addWidget(self.doc_table, 1)
        left_layout.addWidget(self.show_archived)

        # 오른쪽: 문서 정보·열 역할·항목·원문
        self.status_stamp = QLabel()
        self.title_edit = QLineEdit()
        self.rename_button = QPushButton("제목 바꾸기")
        self.rename_button.clicked.connect(self.rename)
        title_row = QHBoxLayout()
        title_row.addWidget(QLabel("제목"))
        title_row.addWidget(self.title_edit, 1)
        title_row.addWidget(self.rename_button)
        title_row.addWidget(self.status_stamp)

        self.table_combo = QComboBox()
        self.table_combo.currentIndexChanged.connect(lambda _i: self._fill_role_combos())
        self.role_combos: dict[str, QComboBox] = {role: QComboBox() for role in ROLES}
        self.apply_roles_button = QPushButton("열 역할 적용")
        self.apply_roles_button.clicked.connect(self.apply_roles)
        roles_grid = QGridLayout()
        roles_grid.addWidget(QLabel("표"), 0, 0)
        roles_grid.addWidget(self.table_combo, 0, 1, 1, 3)
        for index, role in enumerate(ROLES):
            roles_grid.addWidget(QLabel(f"{ROLE_LABELS[role]} 열"), 1 + index // 3, (index % 3) * 2)
            roles_grid.addWidget(self.role_combos[role], 1 + index // 3, (index % 3) * 2 + 1)
        roles_grid.addWidget(self.apply_roles_button, 2 + (len(ROLES) - 1) // 3, 4, 1, 2)  # 역할 칸 다음 줄 오른쪽
        self.roles_box = QGroupBox("표의 열 역할 (머리글로 추정 — 틀리면 고르고 '열 역할 적용')")
        self.roles_box.setLayout(roles_grid)

        self.entry_table = QTableWidget(0, len(ENTRY_COLUMNS))
        self.entry_table.setHorizontalHeaderLabels(ENTRY_COLUMNS)
        self.entry_table.verticalHeader().setVisible(False)
        self.entry_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.entry_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        comfortable_table(self.entry_table)
        self.entry_table.setMinimumHeight(260)
        self.entry_table.itemChanged.connect(self._on_entry_changed)
        self.add_entry_button = QPushButton("항목 추가…")
        self.add_entry_button.clicked.connect(self.add_entry)
        self.delete_entry_button = QPushButton("선택 항목 지우기")
        self.delete_entry_button.clicked.connect(self.delete_entry)
        entry_buttons = QHBoxLayout()
        entry_buttons.addWidget(self.add_entry_button)
        entry_buttons.addWidget(self.delete_entry_button)
        entry_buttons.addStretch(1)
        self.entry_hint = QLabel()
        set_role(self.entry_hint, "muted")
        self.entry_hint.setWordWrap(True)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("문서를 고르면 원문이 보입니다.")
        self.message = StatusLine()

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addLayout(title_row)
        right_layout.addWidget(self.message)
        right_layout.addWidget(self.roles_box)
        right_layout.addWidget(QLabel("코드 항목 (초안일 때 칸을 두 번 눌러 고칠 수 있습니다)"))
        right_layout.addWidget(self.entry_table, 3)
        right_layout.addLayout(entry_buttons)
        right_layout.addWidget(self.entry_hint)
        right_layout.addWidget(QLabel("원문 미리보기"))
        right_layout.addWidget(self.preview, 2)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([380, 940])
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(split, 1)
        layout.addWidget(buttons)
        self.refresh_documents()

    # ── 문서 ──
    def refresh_documents(self, select: int | None = None) -> None:
        keep = select if select is not None else self.document_id
        docs = self.library.documents(include_archived=self.show_archived.isChecked())
        self._filling = True
        self.doc_table.setRowCount(len(docs))
        target_row = None
        for row, doc in enumerate(docs):
            values = (doc["title"], doc["format"], STATUS_LABELS.get(doc["status"], doc["status"]),
                      f"{doc['included_count']}·{doc['passage_count']}")
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, int(doc["id"]))
                if column == 0:
                    item.setToolTip(doc["original_name"])
                self.doc_table.setItem(row, column, item)
            if int(doc["id"]) == keep:
                target_row = row
        self._filling = False
        if target_row is None and docs:
            target_row = 0
        if target_row is not None:
            self.doc_table.selectRow(target_row)
        self.load_document(int(docs[target_row]["id"]) if target_row is not None else None)

    def _on_doc_selected(self) -> None:
        if self._filling:
            return
        rows = self.doc_table.selectionModel().selectedRows()
        if rows:
            self.load_document(int(self.doc_table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)))

    def load_document(self, document_id: int | None) -> None:
        self.document_id = document_id
        doc = self.library.document(document_id) if document_id else None
        draft = bool(doc and doc["status"] == "DRAFT")
        self.title_edit.setText(doc["title"] if doc else "")
        self.title_edit.setReadOnly(not draft)
        set_role(self.status_stamp, "stamp", STATUS_TONE.get(doc["status"], "neutral") if doc else "neutral")
        self.status_stamp.setText(STATUS_LABELS.get(doc["status"], "") if doc else "문서 없음")
        for widget in (self.rename_button, self.add_entry_button, self.delete_entry_button, self.apply_roles_button,
                       self.approve_button, self.delete_button):
            widget.setEnabled(draft)
        self.archive_button.setEnabled(bool(doc and doc["status"] != "ARCHIVED"))
        self._read = None
        self.preview.clear()
        if doc is not None:
            try:
                self._read = self.library.read(document_id)
                self.preview.setPlainText(self._read.preview())
            except (OSError, ReferenceReadError) as exc:
                self.preview.setPlainText(f"원문을 다시 읽지 못했습니다: {exc}")
        self._fill_tables()
        self.fill_entries()
        passages = len(self.library.passages(document_id)) if doc is not None else 0
        if doc is None:
            self.entry_hint.setText("'자료 올리기…'로 문서를 올리세요.")
        elif draft and not self.library.entries(document_id) and passages:
            self.entry_hint.setText(f"코드 항목이 없는 일반 문서입니다(문단 {passages}개). 원문을 확인하고 '승인'하면 분석할 때 "
                                    "로그와 관련 있는 문단을 참고합니다. 코드가 있으면 '항목 추가…'로 더할 수도 있습니다.")
        elif draft:
            self.entry_hint.setText("초안입니다. 항목을 확인하고, 빼고 싶은 항목은 '포함' 체크를 풀고, 맞으면 '승인'을 누르세요. "
                                    "승인 전에는 분석·AI에 쓰이지 않습니다.")
        elif doc["status"] == "APPROVED":
            self.entry_hint.setText("승인됨 — 신규 분석과 AI가 참고합니다. 고치려면 고친 파일을 같은 제목으로 다시 올려 승인하세요.")
        else:
            self.entry_hint.setText("보관됨 — 분석에 쓰이지 않습니다.")

    def _fill_tables(self) -> None:
        tables = self._read.tables if self._read else []
        self.table_combo.blockSignals(True)
        self.table_combo.clear()
        for table in tables:
            self.table_combo.addItem(table.name, table.name)
        self.table_combo.blockSignals(False)
        self.roles_box.setVisible(bool(tables))
        self._fill_role_combos()

    def _fill_role_combos(self) -> None:
        if not self._read or self.table_combo.count() == 0:
            return
        table = next(t for t in self._read.tables if t.name == self.table_combo.currentData())
        maps = self.library.column_maps(self.document_id) if self.document_id else {}
        mapping = maps.get(table.name, {})
        header_row = int(mapping.get("header_row", 0))
        header = table.rows[header_row] if 0 <= header_row < len(table.rows) else []
        width = max((len(r) for r in table.rows), default=0)
        for role, combo in self.role_combos.items():
            combo.clear()
            combo.addItem("(없음)", None)
            for column in range(width):
                name = header[column] if column < len(header) and header[column] else f"{column + 1}번째 열"
                combo.addItem(f"{column + 1}. {name}", column)
            combo.setCurrentIndex(max(combo.findData(mapping.get(role)), 0))
        self._header_row = header_row if mapping else 0

    def _browse(self) -> None:
        pattern = " ".join(f"*{ext}" for ext in FORMATS)
        paths, _ = QFileDialog.getOpenFileNames(self, "참고 자료 선택", "", f"참고 자료 ({pattern})")
        for path in paths:
            self.import_path(Path(path))

    def import_path(self, path: Path, title: str | None = None) -> int | None:
        try:
            outcome = self.library.import_file(path, title)
        except (ReferenceError, OSError) as exc:
            self.message.setText(f"올리지 못했습니다: {exc}")
            return None
        self.refresh_documents(select=outcome.document_id)
        if outcome.duplicate:
            text = "이미 올린 파일이라 기존 문서를 열었습니다."
        else:
            text = (f"'{path.name}'에서 코드 항목 {outcome.entries}개, 문단 {outcome.passages}개를 읽었습니다. "
                    "확인하고 승인하세요.")
        self.message.setText(" ".join([text, *outcome.warnings[:2]]))
        return outcome.document_id

    def rename(self) -> None:
        self._run(lambda: self.library.rename(self.document_id, self.title_edit.text()), "제목을 바꿨습니다.")

    def apply_roles(self) -> None:
        if self.document_id is None or self.table_combo.count() == 0:
            return
        mapping = {"header_row": self._header_row}
        mapping.update({role: combo.currentData() for role, combo in self.role_combos.items()})
        try:
            count = self.library.set_column_map(self.document_id, self.table_combo.currentData(), mapping)
        except ReferenceError as exc:
            self.message.setText(str(exc))
            return
        self.load_document(self.document_id)
        self.refresh_documents(select=self.document_id)
        self.message.setText(f"열 역할을 적용해 항목 {count}개를 다시 뽑았습니다(직접 더한 항목은 그대로).")

    def approve(self) -> None:
        if self.document_id is None:
            return
        try:
            older = self.library.approve(self.document_id)
        except ReferenceError as exc:
            self.message.setText(str(exc))
            return
        self.refresh_documents(select=self.document_id)
        self.message.setText("승인했습니다. 이제 신규 분석과 AI가 이 자료를 참고합니다."
                             + (f" 같은 제목의 이전 버전 {len(older)}개는 보관했습니다." if older else ""))

    def archive(self) -> None:
        if self.document_id is None:
            return
        if QMessageBox.question(self, "보관", "이 참고 자료를 보관할까요? 보관하면 분석에 쓰이지 않습니다(지우지는 않음).") \
                != QMessageBox.StandardButton.Yes:
            return
        self._run(lambda: self.library.archive(self.document_id), "보관했습니다.")

    def delete_draft(self) -> None:
        if self.document_id is None:
            return
        if QMessageBox.question(self, "초안 삭제", "이 초안을 지울까요? 보관한 사본도 함께 지웁니다.") != QMessageBox.StandardButton.Yes:
            return
        try:
            self.library.delete_draft(self.document_id)
        except ReferenceError as exc:
            self.message.setText(str(exc))
            return
        self.document_id = None
        self.refresh_documents()
        self.message.setText("초안을 지웠습니다.")

    def _run(self, action, done: str) -> None:
        try:
            action()
        except ReferenceError as exc:
            self.message.setText(str(exc))
            return
        self.refresh_documents(select=self.document_id)
        self.message.setText(done)

    # ── 항목 ──
    def fill_entries(self) -> None:
        rows = self.library.entries(self.document_id) if self.document_id else []
        draft = bool(self.document_id and self.library.document(self.document_id)["status"] == "DRAFT")
        self._filling = True
        self.entry_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            include = QTableWidgetItem()
            include.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
                             if draft else Qt.ItemFlag.ItemIsEnabled)
            include.setCheckState(Qt.CheckState.Checked if row["status"] == "INCLUDED" else Qt.CheckState.Unchecked)
            include.setData(Qt.ItemDataRole.UserRole, int(row["id"]))
            self.entry_table.setItem(r, 0, include)
            for column, name in enumerate(ENTRY_FIELDS):
                if column == 0:
                    continue
                value = (row[name] if name in row.keys() else None) or ""
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, int(row["id"]))
                editable = draft and name != "locator"
                flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
                item.setFlags(flags | Qt.ItemFlag.ItemIsEditable if editable else flags)
                if name in ("description", "cause", "action", "title"):
                    item.setToolTip(value[:1000])
                elif name == "judgment":  # CR-16 RJ-02: 이 낱말을 판정에 어떻게 쓰는지
                    item.setToolTip(JUDGMENT_USE.get(judgment_side(value) or "", "판정에 안 씀(에러·정상처럼 적으면 판정에 씀)"
                                                     if value else "비어 있음 — '에러'·'정상'처럼 적으면 판정에 씁니다"))
                elif name == "code":  # 어디서 왔는지와 원문은 코드 칸 풍선 도움말로(열을 줄여 설명 칸을 넓게)
                    origin = SOURCE_LABELS.get(row["source_kind"], "")
                    source = (row["source_text"] or "")[:800]
                    item.setToolTip(f"{origin}\n원문: {source}".strip())
                self.entry_table.setItem(r, column, item)
        for column, width in ((0, 48), (1, 110), (2, 120), (4, 120), (5, 140), (6, 72), (7, 96)):
            self.entry_table.setColumnWidth(column, width)
        self._filling = False

    def _on_entry_changed(self, item: QTableWidgetItem) -> None:
        if self._filling or self.document_id is None:
            return
        entry_id = int(item.data(Qt.ItemDataRole.UserRole))
        name = ENTRY_FIELDS[item.column()]
        try:
            if item.column() == 0:
                status = "INCLUDED" if item.checkState() == Qt.CheckState.Checked else "EXCLUDED"
                self.library.update_entry(entry_id, status=status)
            elif name:
                self.library.update_entry(entry_id, **{name: item.text()})
        except ReferenceError as exc:
            self.message.setText(str(exc))
            self.fill_entries()
            return
        if item.column() == 0:
            self.refresh_documents(select=self.document_id)  # 문서 목록의 항목 수를 맞춘다

    def add_entry(self) -> None:
        if self.document_id is None:
            return
        code, ok = QInputDialog.getText(self, "항목 추가", "코드 (예: TEST-E001)")
        if not ok or not code.strip():
            return
        try:
            self.library.add_entry(self.document_id, code)
        except ReferenceError as exc:
            self.message.setText(str(exc))
            return
        self.fill_entries()
        self.refresh_documents(select=self.document_id)
        self.message.setText("항목을 더했습니다. 칸을 두 번 눌러 명칭·설명·원인·조치를 적으세요.")

    def delete_entry(self) -> None:
        rows = sorted({i.row() for i in self.entry_table.selectionModel().selectedRows()}, reverse=True)
        for row in rows:
            entry_id = int(self.entry_table.item(row, 0).data(Qt.ItemDataRole.UserRole))
            try:
                self.library.delete_entry(entry_id)
            except ReferenceError as exc:
                self.message.setText(str(exc))
                return
        if rows:
            self.fill_entries()
            self.refresh_documents(select=self.document_id)
