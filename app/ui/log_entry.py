"""'로그 입력' 탭 (변경 요청 CR-06). 정상·비정상 학습 탭을 하나로 합쳤다.

사용자가 하는 일: 로그를 끌어 놓기(바로 올라옴, CR-10) → 목록에서 로그를 골라 정상/비정상 → 어떤 동작이었는지 메모
→ (비정상이면) 에러로 판단한 근거 → 저장. 저장하면 바로 학습에 쓰인다(learning.log_entry).
패턴 확정·가이드 첨부·질문은 '자세히 편집' 창(예전 학습 화면)에서 한다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import shiboken6
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
from app.learning.ai_questions import AiQuestionService, QuestionOutcome
from app.learning.case_manager import CaseError
from app.learning.log_entry import LABELS, LogEntry, LogEntryError, LogEntryService
from app.learning.normal_learner import default_client_factory
from app.models.enums import OriginPurpose
from app.analysis.error_messages import ErrorMessage, file_error_candidates
from app.ui.ai_questions_box import AiQuestionsBox
from app.ui.error_messages_box import ErrorMessagesBox
from app.ui.log_import_panel import LogImportPanel
from app.ui.natural_input import StatusLine
from app.ui.navigation import StepPages, nav_row
from app.ui.theme import comfortable_table, scroll_wrap, set_role
from app.ui.workers import TaskRunner

ENTRY_COLUMNS = ("파일", "구분", "동작 메모", "상태")
SAVED_COLUMNS = ("파일", "구분", "동작 메모", "저장 시각")


@dataclass
class Draft:
    label: str | None
    memo: str
    criteria: str


class LogEntryPage(QWidget):
    entry_saved = Signal(int)  # case_id
    analysis_requested = Signal()  # ③에서 '신규 분석으로'(CR-12)

    def __init__(self, context: AppContext, client_factory=None, open_detail: Callable[[str, int], None] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.service = LogEntryService(context.database)
        self.open_detail = open_detail
        # CR-07: 저장하면 AI가 그 로그를 보고 질문을 만든다(백그라운드, 한 번에 하나씩)
        self.questions = AiQuestionService(context.database, context.settings,
                                           client_factory or default_client_factory(context.settings))
        self.ai_runner = TaskRunner(self)
        self._question_queue: list[int] = []
        self._entries: list[LogEntry] = []
        self._drafts: dict[int, Draft] = {}  # 저장하지 않은 입력(파일별)
        self._loading = False
        self._dirty = False
        self._shown_ids: list[int] = []

        self.import_panel = LogImportPanel(context, OriginPurpose.NORMAL_CASE, allow_clear=True, compact=True)
        self.import_panel.files_changed.connect(lambda _ids: self.refresh_entries())
        # CR-10 LI-12: 목록에서 빼기 전에 정상/비정상을 고른 입력은 저장한다(자동 저장과 같은 규칙)
        self.import_panel.removing.connect(lambda ids: self.autosave(only=list(ids)))
        self.runner = self.import_panel.runner

        steps = QLabel("진행 순서  ① 왼쪽 칸에 로그를 끌어 놓기(바로 올라옴) → ② 아래 목록에서 로그를 고르기(여러 개 가능) → "
                       "③ 정상/비정상 고르기(필수), 메모·판단 가이드(선택) → ④ 저장. 저장하면 바로 학습에 쓰이고, "
                       "AI가 그 로그를 보고 궁금한 점을 'AI 질문'에 남깁니다(답은 선택).")
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
        self.empty_label = QLabel("왼쪽 칸에 로그를 끌어 놓으면 여기에 파일마다 한 줄씩 나옵니다.")
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
        self.memo_label = QLabel("메모 (선택) — 어떤 동작이었는지 등")
        set_role(self.memo_label, "cell-label")
        self.memo_edit = QPlainTextEdit()
        self.memo_edit.setPlaceholderText("예) ○○ 동작을 처음부터 끝까지 한 번 돌림")
        self.memo_edit.setFixedHeight(76)
        self.memo_edit.setTabChangesFocus(True)
        self.criteria_label = QLabel("판단 가이드 (선택) — 어떻게 에러로 판단하는지")
        set_role(self.criteria_label, "cell-label")
        self.criteria_edit = QPlainTextEdit()
        self.criteria_edit.setPlaceholderText("예) 화면에 TEST-E001이 뜨고 멈추면 에러로 봄. 적어 두면 다음 분석에 씁니다.")
        self.criteria_edit.setFixedHeight(76)
        self.criteria_edit.setTabChangesFocus(True)
        for widget in (self.memo_edit, self.criteria_edit):
            widget.textChanged.connect(self._mark_dirty)
        # CR-11 EM-04: '비정상'을 고르면 그 로그에서 에러로 보이는 줄(정상 로그와 비교하지 않은 후보)
        self.error_box = ErrorMessagesBox("이 로그에서 에러로 보이는 줄 (자동으로 찾음 · 두 번 누르면 원본 행)")
        self.error_box.go_to.connect(lambda log_file_id, line: self.import_panel.show_line(log_file_id, line))
        self.error_box.hide()
        self._candidates: dict[int, list[ErrorMessage]] = {}
        self.save_button = QPushButton("저장 (바로 학습에 씀)")
        set_role(self.save_button, variant="primary")
        self.save_button.clicked.connect(self.save_selected)
        self.detail_button = QPushButton("자세히 편집…")
        self.detail_button.setToolTip("진단 가이드·대응 매뉴얼 첨부, 확인 질문, AI 학습 초안 등 예전 학습 화면")
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
        editor.addWidget(self.error_box)
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

        self.questions_box = AiQuestionsBox(self.answer_question, self.skip_question, self.ask_again)

        # ── CR-12 단계별 화면: ① 로그 넣기 → ② 구분·메모 → ③ 저장·AI 질문 ──
        steps.setText("로그를 끌어 놓은 뒤 '다음'을 누르세요. 다음 단계에서 로그마다 정상/비정상(필수)과 메모(선택)를 적어 "
                      "저장하면 바로 학습에 쓰입니다.")
        to_entries = QPushButton("다음: 구분·메모  ›")
        set_role(to_entries, variant="primary")
        to_entries.clicked.connect(lambda: self.steps.go(1))
        # CR-13 LI-14: 넣는 로그의 구분을 미리 정해 두면 넣자마자 그 구분으로 표시된다(저장은 사용자가)
        self.preset_none = QRadioButton("정하지 않음")
        self.preset_normal = QRadioButton("모두 정상")
        self.preset_abnormal = QRadioButton("모두 비정상")
        self.preset_group = QButtonGroup(self)
        for button in (self.preset_none, self.preset_normal, self.preset_abnormal):
            self.preset_group.addButton(button)
        self.preset_none.setChecked(True)
        preset_row = QHBoxLayout()
        preset_label = QLabel("넣는 로그 구분 미리 정하기")
        set_role(preset_label, "cell-label")
        preset_row.addWidget(preset_label)
        for button in (self.preset_none, self.preset_normal, self.preset_abnormal):
            preset_row.addWidget(button)
        preset_row.addStretch(1)
        self.import_panel.import_finished.connect(self._apply_preset)
        intake = QVBoxLayout()
        intake.setContentsMargins(0, 0, 4, 0)
        intake.addWidget(steps)
        intake.addLayout(preset_row)
        intake.addWidget(self.import_panel, 1)
        intake.addLayout(nav_row(None, to_entries))
        step1 = QWidget()
        step1.setLayout(intake)

        select_all = QPushButton("모두 고르기")
        select_all.clicked.connect(self.entry_table.selectAll)
        list_layout.addWidget(select_all, 0, Qt.AlignmentFlag.AlignLeft)
        back_to_intake = QPushButton("‹  로그 넣기")
        back_to_intake.clicked.connect(lambda: self.steps.go(0))
        to_saved = QPushButton("다음: 저장·AI 질문  ›")
        to_saved.clicked.connect(lambda: self.steps.go(2))
        right2 = QVBoxLayout()
        right2.setContentsMargins(0, 0, 0, 0)
        editor_holder = QWidget()
        holder_layout = QVBoxLayout(editor_holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.addWidget(editor_box)
        holder_layout.addStretch(1)
        right2.addWidget(scroll_wrap(editor_holder), 1)
        right2.addLayout(nav_row(back_to_intake, to_saved))
        right2_widget = QWidget()
        right2_widget.setLayout(right2)
        step2 = QSplitter(Qt.Orientation.Horizontal)
        step2.addWidget(list_box)
        step2.addWidget(right2_widget)
        step2.setSizes([520, 700])
        step2.setChildrenCollapsible(False)

        back_to_entries = QPushButton("‹  구분·메모")
        back_to_entries.clicked.connect(lambda: self.steps.go(1))
        to_analysis = QPushButton("신규 분석으로  ›")
        to_analysis.clicked.connect(self.analysis_requested.emit)
        done = QVBoxLayout()
        done.addWidget(self.questions_box)
        done.addWidget(saved_box)
        done.addStretch(1)
        done_widget = QWidget()
        done_widget.setLayout(done)
        step3_layout = QVBoxLayout()
        step3_layout.setContentsMargins(0, 0, 0, 0)
        step3_layout.addWidget(scroll_wrap(done_widget), 1)
        step3_layout.addLayout(nav_row(back_to_entries, to_analysis))
        step3 = QWidget()
        step3.setLayout(step3_layout)

        self.steps = StepPages("학습 (로그 입력)", "정상·비정상 로그를 저장해 두면 신규 분석이 비교 기준으로 씁니다.",
                               ("로그 넣기", "구분·메모", "저장·AI 질문"))
        self.steps.add(step1)
        self.steps.add(step2)
        self.steps.add(step3)
        self.steps.step_changed.connect(lambda _i: self.autosave())  # 단계를 바꾸면 고른 입력은 저장(탭 전환과 같음)
        self.steps.refresh()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.steps, 1)

        self.refresh_entries()
        self.refresh_saved()
        self.refresh_questions()

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
        self._show_candidates(abnormal)

    def _show_candidates(self, abnormal: bool) -> None:
        ids = self._shown_ids
        if not abnormal or len(ids) != 1:  # 로그 하나를 비정상으로 골랐을 때만
            self.error_box.hide()
            return
        log_file_id = ids[0]
        if log_file_id not in self._candidates:
            self._candidates[log_file_id] = file_error_candidates(self.context.database, log_file_id)
        messages = self._candidates[log_file_id]
        self.error_box.show_messages(
            messages,
            f"에러 레벨·오류 코드 줄과 경고·실패 낱말 줄 {len(messages)}개입니다. 정상 로그와 비교하지 않은 후보이니 "
            "판단 가이드를 적을 때 참고하세요. 줄은 로그 원문 그대로입니다.",
            "에러 레벨·오류 코드·경고 낱말이 있는 줄을 찾지 못했습니다. 어떤 점을 보고 에러로 판단했는지 위에 적어 주세요.")

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
                self.queue_questions([entry.case_id])
        return saved, ""

    def autosave(self, only: list[int] | None = None) -> int:
        """탭을 바꾸거나 앱을 닫을 때, 다른 줄로 옮길 때: 정상/비정상을 고른 입력은 저장한다.
        구분을 고르지 않은 입력은 '입력 중'으로 남겨 둔다(지우지 않음)."""
        self._keep_draft()
        ready = [i for i, d in self._drafts.items() if (only is None or i in only) and d.label]
        if not ready:
            return 0
        saved, error = self._save_drafts(ready)
        if error:
            self.message.setText(error)
        elif saved:
            self.message.setText(f"입력한 로그 {saved}개를 자동 저장했습니다.")
        self.refresh_saved()
        return saved

    # ── AI 질문 (CR-07) ──
    def queue_questions(self, case_ids: list[int]) -> None:
        if not self.context.settings.ollama.model:  # 모델이 없으면 백그라운드 작업을 띄우지 않는다
            self.questions_box.status.setText(QuestionOutcome("NO_MODEL").message(""))
            return
        for case_id in case_ids:
            if case_id not in self._question_queue:
                self._question_queue.append(case_id)
        self._next_question()

    def _next_question(self) -> None:
        if self.ai_runner.busy or not self._question_queue:
            return
        case_id = self._question_queue.pop(0)
        name = self._case_file_name(case_id)
        self.questions_box.status.setText(f"AI가 {name}을(를) 보고 질문을 만드는 중…")
        self.ai_runner.start(self.questions.generate, case_id,
                             on_done=lambda outcome: self._on_questions(name, outcome),
                             on_error=lambda message: self._on_questions(name, QuestionOutcome("AI_UNAVAILABLE",
                                                                                               errors=[message])))

    def _on_questions(self, name: str, outcome: QuestionOutcome) -> None:
        if not shiboken6.isValid(self.questions_box):  # 작업이 끝나기 전에 화면이 닫혔다(질문은 DB에 이미 저장됨)
            return
        self.questions_box.status.setText(outcome.message(name))
        self.refresh_questions()
        self._next_question()

    def _case_file_name(self, case_id: int) -> str:
        entry = next((e for e in self._entries if e.case_id == case_id), None)
        return entry.file_name if entry else f"사례 {case_id}"

    def refresh_questions(self) -> None:
        self.questions_box.show_questions(self.questions.questions())

    def answer_question(self, question_id: int, answer: str, text: str | None) -> None:
        try:
            self.questions.answer(question_id, answer, text)
        except CaseError as exc:
            self.questions_box.status.setText(str(exc))
            return
        self.questions_box.status.setText("답을 저장했습니다. 다음 분석부터 AI가 참고합니다."
                                          if answer != "UNKNOWN" else "'모름'으로 남겼습니다. 같은 질문은 다시 하지 않습니다.")
        self.refresh_questions()

    def skip_question(self, question_id: int) -> None:
        self.questions.skip(question_id)
        self.refresh_questions()

    def ask_again(self) -> None:
        saved = [e.case_id for e in self.selected_entries() if e.case_id is not None]
        if not saved:
            self.questions_box.status.setText("저장된 로그를 목록에서 고르세요.")
            return
        self.queue_questions(saved)

    def preset_label(self) -> str | None:
        if self.preset_normal.isChecked():
            return "NORMAL"
        if self.preset_abnormal.isChecked():
            return "ABNORMAL"
        return None

    def _apply_preset(self, result) -> None:
        """방금 넣은 로그 중 아직 구분이 없는 것을 미리 정한 구분으로 표시한다(저장 전 '입력 중'). 저장된 구분은 바꾸지 않는다."""
        label = self.preset_label()
        if label is None:
            return
        marked = []
        for log_file_id in result.log_file_ids:
            entry = self.service.entry(log_file_id)
            if entry.label is None and log_file_id not in self._drafts:
                self._drafts[log_file_id] = Draft(label, entry.memo, entry.criteria)
                marked.append(log_file_id)
        if marked:
            self.refresh_entries(select=marked)
            self.message.setText(f"새로 넣은 로그 {len(marked)}개를 '{LABELS[label]}'(으)로 표시했습니다. "
                                 "② 구분·메모에서 확인하고 저장하세요(단계를 넘기면 자동 저장).")

    # ── 다른 화면과 잇기 ── (신규 분석에서 사례 저장은 그 자리에서 한다, CR-16 UX-03)
    def bring_saved(self, row: int) -> None:
        item = self.saved_table.item(row, 0)
        if item is None:
            return
        log_file_id = int(item.data(Qt.ItemDataRole.UserRole))
        if log_file_id not in self.import_panel.log_file_ids():
            self.import_panel.set_log_file_ids(self.import_panel.log_file_ids() + [log_file_id])
        self.refresh_entries(select=[log_file_id])
        self.steps.go(1)

    def open_selected_detail(self) -> None:
        entries = self.selected_entries()
        if len(entries) != 1 or entries[0].case_id is None or self.open_detail is None:
            return
        self.autosave(only=[entries[0].log_file_id])
        entry = self.service.entry(entries[0].log_file_id)
        self.open_detail(entry.label or "NORMAL", entry.case_id)
        self.refresh_entries(select=[entry.log_file_id])
        self.refresh_saved()
