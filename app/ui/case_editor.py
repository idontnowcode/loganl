"""정상·비정상 학습 화면의 공통 틀: 사례 고르기·저장·승인, 로그 입력, 운전 모드(AI 추정 + 사용자 확인), 메모.

CR-05: 오른쪽 맨 위는 자유 글 칸 하나다. 'AI로 정리'가 내 글에서 칸을 채우고(learning.intake),
칸 양식과 메모는 '칸으로 직접 고치기'에 접어 둔다.

화면은 learning 계층 함수만 부른다. 긴 작업(AI 호출)은 TaskRunner로 돌린다.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app.ai.ollama_client import OllamaClient
from app.bootstrap import AppContext
from app.learning.approval_workflow import ApprovalError, ApprovalWorkflow
from app.learning.case_manager import CaseDetail, CaseError, CaseManager, ModeManager
from app.learning.intake import IntakeResult, IntakeService, title_from_story
from app.learning.normal_learner import default_client_factory
from app.models.enums import OriginPurpose
from app.ui.log_import_panel import LogImportPanel
from app.ui.natural_input import Collapsible, StatusLine, StoryBox, SummaryRow
from app.ui.question_panel import QuestionPanel
from app.ui.theme import STATUS_TONE, scroll_wrap, set_role
from app.ui.workers import TaskRunner

__all__ = ["CaseEditorBase", "StatusLine", "set_form_note", "text_box"]

AUTOSAVE_MS = 1500
STATUS_LABELS = {
    "DRAFT": "초안", "NEEDS_INFORMATION": "정보 필요", "READY_FOR_REVIEW": "검토 대기", "APPROVED": "승인됨",
    "REJECTED": "반려", "ARCHIVED": "보관",
}


def set_form_note(form: QFormLayout, label: QLabel, text: str) -> None:
    """폼 안의 안내 줄: 글이 없으면 줄째 숨겨 빈자리가 생기지 않게 한다."""
    label.setText(text)
    form.setRowVisible(label, bool(text))


def text_box(height: int = 54, placeholder: str = "") -> QPlainTextEdit:
    box = QPlainTextEdit()
    box.setFixedHeight(height)
    box.setPlaceholderText(placeholder)
    return box


class CaseEditorBase(QWidget):
    case_saved = Signal(int)

    case_type = "NORMAL"
    purpose = OriginPurpose.NORMAL_CASE
    steps_text = ""
    story_placeholder = ""
    mode_required = False  # 정상 학습만 승인 필수(패턴이 모드에 종속)

    def __init__(self, context: AppContext, client_factory: Callable[[], OllamaClient] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.client_factory = client_factory
        self.cases = CaseManager(context.database)
        self.modes = ModeManager(context.database)
        self.workflow = ApprovalWorkflow(context.database)
        self.runner = TaskRunner(self)
        self.case_id: int | None = None
        self.detail: CaseDetail | None = None

        # 사례 선택 줄
        self.case_combo = QComboBox()
        self.case_combo.setMinimumWidth(320)
        self.case_combo.activated.connect(self._on_case_chosen)
        self.new_button = QPushButton("새 사례")
        self.new_button.clicked.connect(lambda: self.load_case(None))
        self.save_button = QPushButton("저장")
        self.save_button.clicked.connect(self.save)
        self.review_button = QPushButton("검토 요청")
        self.review_button.clicked.connect(self.request_review)
        self.approve_button = QPushButton("승인")
        self.approve_button.clicked.connect(self.approve)
        self.reopen_button = QPushButton("수정 시작")
        self.reopen_button.clicked.connect(self.reopen)
        self.delete_button = QPushButton("초안 삭제")
        self.delete_button.clicked.connect(self.delete_case)
        set_role(self.approve_button, variant="primary")
        set_role(self.delete_button, variant="danger")
        self.status_label = QLabel()
        top = QHBoxLayout()
        top.addWidget(QLabel("사례"))
        top.addWidget(self.case_combo, 1)
        for button in (self.new_button, self.save_button, self.review_button, self.reopen_button, self.approve_button):
            top.addWidget(button)
        top.addSpacing(10)
        top.addWidget(self.status_label)
        top.addSpacing(18)
        top.addWidget(self.delete_button)

        # 로그 입력
        self.import_panel = LogImportPanel(context, self.purpose)

        # 공통 입력
        self.title_edit = QLineEdit()
        self.mode_combo = QComboBox()
        self.mode_confirm = QCheckBox("확인함")
        self.estimate_button = QPushButton("AI로 추정")
        self.estimate_button.clicked.connect(self.estimate_mode)
        self.mode_guess_label = QLabel()
        self.mode_guess_label.setWordWrap(True)
        mode_row = QHBoxLayout()
        mode_row.addWidget(self.mode_combo, 1)
        mode_row.addWidget(self.mode_confirm)
        mode_row.addWidget(self.estimate_button)
        mode_holder = QWidget()
        mode_holder.setLayout(mode_row)
        mode_row.setContentsMargins(0, 0, 0, 0)
        self.conditions_edit = text_box(placeholder="동작 조건 및 상황")
        self.fw_edit = QLineEdit()
        self.device_edit = QLineEdit()

        self.title_edit.setPlaceholderText("비우면 글 첫 줄로 붙임")
        self.form = QFormLayout()
        self.form.addRow("제목", self.title_edit)
        self.form.addRow("운전 모드", mode_holder)
        self.form.addRow("", self.mode_guess_label)
        self.form.setRowVisible(self.mode_guess_label, False)
        self.form.addRow("운전 조건", self.conditions_edit)
        self.form.addRow("FW 버전", self.fw_edit)
        self.form.addRow("장비 구성", self.device_edit)
        basics = QGroupBox("사례 정보")
        basics.setLayout(self.form)

        # 자유 글 (CR-05)
        self.story = StoryBox(
            "이 로그에 대해 편하게 적어 주세요", self.story_placeholder,
            "칸을 다 채울 필요 없습니다. 'AI로 정리'를 누르면 내 글에서 필요한 부분만 칸에 옮기고, "
            "승인에 꼭 필요한데 빠진 것만 아래에서 묻습니다. AI는 새 내용을 지어내지 않습니다.")
        self.story_edit = self.story.edit
        self.story.organize_button.clicked.connect(self.organize_story)
        self.mode_ok_button = QPushButton("이 모드가 맞음")
        self.mode_ok_button.clicked.connect(self.confirm_mode)
        self.mode_ok_button.hide()

        self.note_list = QListWidget()
        self.note_list.setFixedHeight(80)
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("메모를 쓰고 '추가' (선택한 메모는 '고치기'·'지우기')")
        add_note = QPushButton("추가")
        add_note.clicked.connect(self.add_note)
        edit_note = QPushButton("고치기")
        edit_note.clicked.connect(self.edit_note)
        del_note = QPushButton("지우기")
        del_note.clicked.connect(self.delete_note)
        note_row = QHBoxLayout()
        note_row.addWidget(self.note_edit, 1)
        for b in (add_note, edit_note, del_note):
            note_row.addWidget(b)
        notes = QGroupBox("사용자 메모")
        notes_layout = QVBoxLayout(notes)
        notes_layout.addWidget(self.note_list)
        notes_layout.addLayout(note_row)

        self.check_label = QLabel()
        self.check_label.setWordWrap(True)
        self.check_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.message_label = StatusLine()
        set_role(self.check_label, "muted")

        self.question_panel = QuestionPanel(context.database, before_answer=self._save_before_answer)
        self.question_panel.answered.connect(self._after_answer)

        self.steps_label = QLabel(self.steps_text)
        self.steps_label.setWordWrap(True)
        set_role(self.steps_label, "hint")

        self.side = QVBoxLayout()
        self.side.addWidget(self.steps_label)
        self.side.addWidget(self.story)
        self.side.addWidget(self.question_panel)
        self.build_specific(self.side)
        manual = QWidget()
        manual_layout = QVBoxLayout(manual)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        manual_layout.addWidget(basics)
        manual_layout.addWidget(notes)
        self.fields_section = Collapsible("칸으로 직접 고치기 (제목·운전 모드·조건·FW·장비 구성·메모 등)", manual)
        self.side.addWidget(self.fields_section)
        self.side.addWidget(QLabel("승인 검사"))
        self.side.addWidget(self.check_label)
        self.side.addStretch(1)
        side_widget = QWidget()
        side_widget.setLayout(self.side)
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
        layout.addLayout(top)
        layout.addWidget(self.message_label)
        layout.addWidget(split, 1)

        # 자동 저장(§9.1 "학습 데이터 자동 저장"): 입력이 멈추고 잠시 뒤, 탭을 바꿀 때, 앱을 닫을 때
        self._loading = False
        self._dirty = False
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(AUTOSAVE_MS)
        self._autosave_timer.timeout.connect(self.autosave)
        self.watch_changes(self.story_edit, self.title_edit, self.conditions_edit, self.fw_edit, self.device_edit,
                           self.mode_combo, self.mode_confirm, *self.specific_widgets())
        self.import_panel.files_changed.connect(lambda _ids: self.mark_dirty())

        self.refresh_modes()
        self.refresh_cases()
        self.load_case(None)

    # ── 자동 저장 ──
    def specific_widgets(self) -> list[QWidget]:
        return []

    def watch_changes(self, *widgets: QWidget) -> None:
        for widget in widgets:
            if isinstance(widget, QLineEdit):
                widget.textEdited.connect(lambda _t: self.mark_dirty())
            elif isinstance(widget, QPlainTextEdit):
                widget.textChanged.connect(self.mark_dirty)
            elif isinstance(widget, QComboBox):
                widget.activated.connect(lambda _i: self.mark_dirty())
            elif isinstance(widget, QCheckBox):
                widget.clicked.connect(lambda _c: self.mark_dirty())

    def mark_dirty(self) -> None:
        if getattr(self, "_loading", True):
            return
        if self.detail and self.detail.case.status in ("APPROVED", "ARCHIVED"):
            return
        self._dirty = True
        self._autosave_timer.start()

    def autosave(self) -> bool:
        """화면을 다시 불러오지 않고 조용히 저장한다(입력 중인 커서가 튀지 않게). 새 사례는 제목이나 로그가 있을 때만 만든다."""
        self._autosave_timer.stop()
        if not self._dirty or self._loading:
            return False
        if self.detail and self.detail.case.status in ("APPROVED", "ARCHIVED"):
            self._dirty = False
            return False
        fields = self.collect_fields()
        ids = self.import_panel.log_file_ids()
        try:
            if self.case_id is None:
                if not self.title_edit.text().strip() and not ids and not self.story.text():
                    return False
                title = fields.pop("title")
                self.case_id = self.cases.create(self.case_type, title, ids, **fields)
                self._loading = True
                self.refresh_cases()
                self._loading = False
            else:
                self.cases.update(self.case_id, **fields)
                self.cases.set_logs(self.case_id, ids)
            self.detail = self.cases.detail(self.case_id)
        except CaseError as exc:
            self.message_label.setText(f"자동 저장하지 못했습니다: {exc}")
            return False
        self._dirty = False
        case = self.detail.case
        self.status_label.setText(f"{STATUS_LABELS.get(case.status, case.status)} · 개정본 {case.current_revision}"
                                  f" · 자동 저장 {datetime.now().strftime('%H:%M:%S')}")
        self.refresh_summary()
        return True

    # ── 하위 클래스가 채우는 부분 ──
    def build_specific(self, layout: QVBoxLayout) -> None:
        pass

    def specific_fields(self) -> dict[str, Any]:
        return {}

    def fill_specific(self, detail: CaseDetail | None) -> None:
        pass

    def approval_decisions(self) -> dict[str, dict[int, str]]:
        return {}

    def restore_decisions(self, decisions: dict[str, dict[int, str]]) -> None:
        """다시 불러온 뒤 사용자가 골라 둔(아직 승인 전) 결정을 되살린다."""

    def specific_summary_rows(self) -> list[SummaryRow]:
        """정리된 내용에 더할 화면별 줄(승인 필수 칸 등)."""
        return []

    # ── 자유 글 (CR-05) ──
    def summary_rows(self) -> list[SummaryRow]:
        mode_id = self.mode_combo.currentData()
        mode_name = self.mode_combo.currentText() if mode_id is not None else None
        pending = mode_name is not None and not self.mode_confirm.isChecked()
        rows = [SummaryRow("운전 모드", mode_name, required=self.mode_required,
                           pending="확인 전" if pending else None, action=self.mode_ok_button if pending else None)]
        rows += [SummaryRow("운전 조건", self.conditions_edit.toPlainText().strip() or None),
                 SummaryRow("FW 버전", self.fw_edit.text().strip() or None),
                 SummaryRow("장비 구성", self.device_edit.text().strip() or None)]
        return rows + self.specific_summary_rows()

    def refresh_summary(self) -> None:
        self.story.set_summary(self.summary_rows())

    def organize_story(self) -> None:
        text = self.story.text()
        if not text:
            self.story.result_label.setText("먼저 위 칸에 상황을 편하게 적어 주세요.")
            return
        if self.runner.busy:
            return
        pending = self.approval_decisions()
        if self.save() is None:
            return
        self.restore_decisions(pending)
        service = IntakeService(self.context.database, self.context.settings, self._client_factory())
        self.story.organize_button.setEnabled(False)
        self.story.result_label.setText("AI가 내 글에서 칸에 맞는 부분을 찾는 중…")
        self.runner.start(service.organize_case, self.case_id, text, on_done=self._on_organized,
                          on_error=self._on_organize_error)

    def _on_organized(self, result: IntakeResult) -> None:
        pending = self.approval_decisions()
        self.story.organize_button.setEnabled(True)
        self.refresh_cases()
        self.load_case(self.case_id)
        self.restore_decisions(pending)
        self.story.result_label.setText(result.message())
        if result.status == "RULES_ONLY":
            self.fields_section.set_expanded(True)

    def _on_organize_error(self, message: str) -> None:
        self.story.organize_button.setEnabled(True)
        self.story.result_label.setText(f"정리하지 못했습니다: {message} 글은 저장됐습니다. "
                                        "'칸으로 직접 고치기'에서 빠진 칸을 채우세요.")
        self.fields_section.set_expanded(True)

    def confirm_mode(self) -> None:
        if self.mode_combo.currentData() is None:
            return
        self.mode_confirm.setChecked(True)
        pending = self.approval_decisions()
        if self.save() is not None:
            self.restore_decisions(pending)
            self.message_label.setText(f"운전 모드를 '{self.mode_combo.currentText()}'(으)로 확인했습니다.")

    # ── 목록 ──
    def refresh_modes(self) -> None:
        current = self.mode_combo.currentData()
        self.mode_combo.clear()
        self.mode_combo.addItem("(미정)", None)
        for mode in self.modes.list(active_only=True):
            self.mode_combo.addItem(mode.name, mode.id)
        self._select_data(self.mode_combo, current)

    def refresh_cases(self) -> None:
        self.case_combo.clear()
        self.case_combo.addItem("(새 사례)", None)
        for case in self.cases.list(self.case_type):
            self.case_combo.addItem(f"{case.label} [{STATUS_LABELS.get(case.status, case.status)}]", case.id)
        self._select_data(self.case_combo, self.case_id)

    @staticmethod
    def _select_data(combo: QComboBox, value: Any) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(max(index, 0))

    def _on_case_chosen(self, index: int) -> None:
        self.load_case(self.case_combo.itemData(index))

    # ── 불러오기·저장 ──
    def load_case(self, case_id: int | None) -> None:
        self._loading = True
        try:
            self._load_case(case_id)
        finally:
            self._loading = False
            self._dirty = False
            self._autosave_timer.stop()

    def _load_case(self, case_id: int | None) -> None:
        if case_id != self.case_id or case_id is None:
            self.story.result_label.clear()  # 다른 사례의 정리 안내를 남기지 않는다
        self.case_id = case_id
        self.detail = self.cases.detail(case_id) if case_id else None
        case = self.detail.case if self.detail else None
        self.story_edit.setPlainText(case.user_description or "" if case else "")
        self.title_edit.setText(case.title if case else "")
        self._select_data(self.mode_combo, case.mode_id if case else None)
        self.mode_confirm.setChecked(bool(case and case.mode_confirmed))
        set_form_note(self.form, self.mode_guess_label, "")
        self.conditions_edit.setPlainText(case.operating_conditions or "" if case else "")
        self.fw_edit.setText(case.fw_version or "" if case else "")
        self.device_edit.setText(case.device_config or "" if case else "")
        self.note_list.clear()
        for note in self.detail.notes if self.detail else []:
            item = QListWidgetItem(note.body)
            item.setData(Qt.ItemDataRole.UserRole, note.id)
            self.note_list.addItem(item)
        self.import_panel.clear()
        if self.detail:
            self.import_panel.set_log_file_ids([f.id for f in self.detail.files])
        self.fill_specific(self.detail)
        self._select_data(self.case_combo, case_id)
        self.question_panel.set_case(case_id, status=case.status if case else None)
        if case is not None:  # 질문 판정이 상태(정보 필요)를 바꿨을 수 있다
            self.detail.case = self.cases.get(case_id)
        self._update_status()

    def _save_before_answer(self) -> bool:
        return self._ensure_saved()

    def _after_answer(self, message: str) -> None:
        self.refresh_cases()
        self.load_case(self.case_id)
        self.message_label.setText(message)

    def collect_fields(self) -> dict[str, Any]:
        mode_id = self.mode_combo.currentData()
        story = self.story.text()
        fields = {
            "title": self.title_edit.text().strip() or title_from_story(story) or "제목 없는 사례",
            "user_description": story or None,
            "mode_id": mode_id,
            "mode_confirmed": bool(mode_id is not None and self.mode_confirm.isChecked()),
            "operating_conditions": self.conditions_edit.toPlainText().strip() or None,
            "fw_version": self.fw_edit.text().strip() or None,
            "device_config": self.device_edit.text().strip() or None,
        }
        fields.update(self.specific_fields())
        return fields

    def save(self) -> int | None:
        pending = self.approval_decisions()  # 저장 후 다시 불러와도 결정 선택이 사라지지 않게
        try:
            fields = self.collect_fields()
            ids = self.import_panel.log_file_ids()
            if self.case_id is None:
                title = fields.pop("title")
                self.case_id = self.cases.create(self.case_type, title, ids, **fields)
            else:
                self.cases.update(self.case_id, **fields)
                self.cases.set_logs(self.case_id, ids)
        except CaseError as exc:
            self.message_label.setText(str(exc))
            return None
        self.after_save()
        self.refresh_cases()
        self.load_case(self.case_id)
        self.restore_decisions(pending)
        self.message_label.setText("저장했습니다.")
        self.case_saved.emit(self.case_id)
        return self.case_id

    def after_save(self) -> None:
        """하위 클래스가 저장 직후 할 일(예: 가이드 첨부)."""

    # ── 메모 ──
    # 메모를 바꾸기 전에 화면의 입력을 먼저 저장한다(다시 불러올 때 저장 안 한 입력이 사라지지 않게).
    def _ensure_saved(self) -> bool:
        if self.detail and self.detail.case.status in ("APPROVED", "ARCHIVED"):
            return True
        return self.save() is not None

    def _refresh_notes(self) -> None:
        self.detail = self.cases.detail(self.case_id)
        self.note_list.clear()
        for note in self.detail.notes:
            item = QListWidgetItem(note.body)
            item.setData(Qt.ItemDataRole.UserRole, note.id)
            self.note_list.addItem(item)

    def add_note(self) -> None:
        body = self.note_edit.text().strip()
        if not body or not self._ensure_saved():
            return
        try:
            self.cases.add_note(self.case_id, body)
        except CaseError as exc:
            self.message_label.setText(str(exc))
            return
        self.note_edit.clear()
        self._refresh_notes()

    def edit_note(self) -> None:
        item = self.note_list.currentItem()
        body = self.note_edit.text().strip()
        if item is None or not body:
            self.message_label.setText("고칠 메모를 고르고 새 내용을 입력하세요.")
            return
        note_id = item.data(Qt.ItemDataRole.UserRole)
        if not self._ensure_saved():
            return
        try:
            self.cases.update_note(self.case_id, note_id, body)
        except CaseError as exc:
            self.message_label.setText(str(exc))
            return
        self.note_edit.clear()
        self._refresh_notes()

    def delete_note(self) -> None:
        item = self.note_list.currentItem()
        if item is None:
            return
        note_id = item.data(Qt.ItemDataRole.UserRole)
        if not self._ensure_saved():
            return
        try:
            self.cases.delete_note(self.case_id, note_id)
        except CaseError as exc:
            self.message_label.setText(str(exc))
            return
        self._refresh_notes()

    # ── 모드 추정 ──
    def _client_factory(self) -> Callable[[], OllamaClient]:
        return self.client_factory or default_client_factory(self.context.settings)

    def estimate_mode(self) -> None:
        if not self._ensure_saved() or self.runner.busy:
            return
        self.save()
        from app.learning.normal_learner import NormalLearner

        learner = NormalLearner(self.context.database, self.context.settings, self._client_factory())
        set_form_note(self.form, self.mode_guess_label, "AI가 운전 모드를 추정하는 중…")
        self.estimate_button.setEnabled(False)
        self.runner.start(learner.estimate_mode, self.case_id, on_done=self._on_mode_guess,
                          on_error=lambda m: self._ai_failed(m))

    def _on_mode_guess(self, guess) -> None:
        self.estimate_button.setEnabled(True)
        set_form_note(self.form, self.mode_guess_label,
                      guess.message() + ("  → 맞으면 '확인함'을 표시하고 저장하세요." if guess.mode_id else ""))
        if guess.mode_id is not None:
            self._select_data(self.mode_combo, guess.mode_id)
            self.mode_confirm.setChecked(False)  # 사용자가 직접 확인해야 한다

    def _ai_failed(self, message: str) -> None:
        self.estimate_button.setEnabled(True)
        self.message_label.setText(f"AI 작업 실패: {message}")

    # ── 상태 전이 ──
    def request_review(self) -> None:
        if self.save() is None:
            return
        check = self.workflow.request_review(self.case_id)
        self.refresh_cases()
        self.load_case(self.case_id)
        self.message_label.setText("검토 대기로 바꿨습니다." if check.ok else "필수 정보가 부족해 '정보 필요' 상태입니다.")

    def approve(self) -> None:
        decisions = self.approval_decisions()  # 저장(다시 불러오기) 전에 사용자의 결정을 먼저 읽는다
        if self.case_id is None or (self.detail and self.detail.case.status not in ("APPROVED", "ARCHIVED")
                                    and self.save() is None):
            return
        try:
            revision = self.workflow.approve(
                self.case_id, pattern_decisions=decisions.get("patterns"), guide_decisions=decisions.get("guides"),
                claim_decisions=decisions.get("claims"))
        except ApprovalError as exc:
            self.message_label.setText(str(exc))
            self._update_status()
            return
        self.refresh_cases()
        self.load_case(self.case_id)
        self.message_label.setText(f"승인했습니다(개정본 {revision}).")

    def reopen(self) -> None:
        if self.case_id is None:
            return
        try:
            self.workflow.reopen(self.case_id)
        except ApprovalError as exc:
            self.message_label.setText(str(exc))
            return
        self.refresh_cases()
        self.load_case(self.case_id)
        self.message_label.setText("수정을 시작했습니다. 다시 승인할 때까지 이 사례는 분석에 쓰이지 않습니다.")

    def delete_case(self) -> None:
        if self.case_id is None:
            return
        if QMessageBox.question(self, "초안 삭제", "이 초안 사례를 지울까요? 원본 로그 사본은 남습니다.") != QMessageBox.StandardButton.Yes:
            return
        self.delete_current()

    def delete_current(self) -> bool:
        try:
            self.cases.delete(self.case_id)
        except CaseError as exc:
            self.message_label.setText(str(exc))
            return False
        self.case_id = None
        self.refresh_cases()
        self.load_case(None)
        return True

    def _update_status(self) -> None:
        case = self.detail.case if self.detail else None
        set_role(self.status_label, "stamp", STATUS_TONE.get(case.status, "neutral") if case else "neutral")
        if case is None:
            self.status_label.setText("새 사례 (저장 전)")
            self.check_label.setText("저장하면 승인 검사를 합니다.")
            locked = False
        else:
            self.status_label.setText(f"{STATUS_LABELS.get(case.status, case.status)} · 개정본 {case.current_revision}")
            check = self.workflow.check(case.id)
            lines = [f"막힘 · {b}" for b in check.blocking] + [f"참고 · {w}" for w in check.warnings]
            self.check_label.setText("\n".join(lines) if lines else "승인할 수 있습니다.")
            locked = case.status in ("APPROVED", "ARCHIVED")
        self.story_edit.setReadOnly(locked)
        self.story.organize_button.setEnabled(not locked)
        self.refresh_summary()
        self.reopen_button.setEnabled(bool(case and case.status == "APPROVED"))
        self.approve_button.setEnabled(not locked and case is not None)
        self.review_button.setEnabled(not locked and case is not None)
        self.save_button.setEnabled(not locked)
        self.import_panel.set_import_enabled(not locked)  # 승인·보관 사례에는 로그를 더하지 않는다(보기는 됨)
        self.delete_button.setEnabled(bool(case and case.status in ("DRAFT", "REJECTED") and case.current_revision == 0))
