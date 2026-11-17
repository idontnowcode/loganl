"""신규 로그 분석 화면 (계획서 §8.4, CR-01). 결과 맨 위에 권장 대응, 그 아래 결과 축·근거·유사 사례·AI 가설.
근거를 두 번 누르면 원본 로그 행으로 이동한다."""

from __future__ import annotations

import re
from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.ai.ollama_client import OllamaClient
from app.analysis.diagnostic_engine import AnalysisRequest, AnalysisResult, DiagnosticEngine
from app.bootstrap import AppContext
from app.core.cancel import CancelToken
from app.learning.case_manager import ModeManager
from app.models.enums import OriginPurpose
from app.ui.case_editor import set_form_note, text_box
from app.ui.log_import_panel import LogImportPanel
from app.ui.theme import comfortable_table, fit_list_height, scroll_wrap, set_role
from app.ui.workers import TaskRunner

AXIS_LABELS = {
    "normal_match": ("정상 패턴 일치", {"MATCH": "일치", "PARTIAL": "일부 일치", "NO_MATCH": "불일치", "NOT_EVALUATED": "판단 못 함(정상 지식 없음)"}),
    "anomaly_level": ("이상 수준", {"NONE_FOUND": "찾은 이상 없음(정상 판정 아님)", "SUSPECTED": "비정상 의심",
                                   "RULE_VIOLATION": "승인된 규칙 위반", "NOT_EVALUATED": "판단 못 함"}),
    "similar_abnormal": ("기존 비정상 사례", {"FOUND": "유사 사례 있음", "NOT_FOUND": "유사 사례 없음", "NOT_EVALUATED": "승인된 비정상 사례 없음"}),
    "evidence_sufficiency": ("근거 충분성", {"SUFFICIENT": "충분", "PARTIAL": "일부 부족", "INSUFFICIENT": "판단 불가(근거 부족)", "NOT_EVALUATED": "-"}),
}
GUIDANCE_TITLES = {
    "MANUAL": "권장 대응 — 승인된 대응 매뉴얼 근거",
    "GUIDE_ONLY": "권장 대응 — 진단 가이드의 해결 방법(대응 매뉴얼 없음)",
    "NONE": "권장 대응 — 등록된 대응 방법 없음",
}
SECTION_SHORT = {"PRECAUTION": "주의", "RESPONSE_STEP": "절차", "VERIFICATION": "조치 후 확인", "ESCALATION": "상위 지원",
                 "RESOLUTION": "해결 방법", "CHECK_METHOD": "원인 확인"}
LAYER_LABELS = {"DETERMINISTIC": "결정론", "RETRIEVAL": "사례 검색", "AI": "AI"}
# 결과 칸의 글자색. 색만으로 말하지 않도록 칸에는 항상 글자 라벨이 함께 있다.
AXIS_TONE = {
    "normal_match": {"MATCH": "ok", "PARTIAL": "warn", "NO_MATCH": "bad"},
    "anomaly_level": {"NONE_FOUND": "ok", "SUSPECTED": "warn", "RULE_VIOLATION": "bad"},
    "similar_abnormal": {"FOUND": "info"},
    "evidence_sufficiency": {"SUFFICIENT": "ok", "PARTIAL": "warn", "INSUFFICIENT": "bad"},
}
GUIDANCE_TONE = {"MANUAL": "ok", "GUIDE_ONLY": "warn", "NONE": "neutral"}
GUIDANCE_STAMP = {"MANUAL": "매뉴얼 근거", "GUIDE_ONLY": "가이드만", "NONE": "대응 없음"}


def section_label(text: str) -> QLabel:
    label = QLabel(text)

    set_role(label, "cell-label")
    return label
_MARKER = re.compile(r"^\s*(?:\d{1,3}[.)]|[-*•]|[①-⑳])\s*")


def strip_marker(text: str) -> str:
    """화면 표시용: 원문 첫 줄의 번호·글머리를 뗀다(앱이 번호를 따로 붙이므로). 저장된 원문은 바꾸지 않는다."""
    return _MARKER.sub("", text, count=1)


class LogAnalysisPage(QWidget):
    analysis_done = Signal(object)
    register_requested = Signal(str, list, str)  # (사례 종류, log_file_ids, 제목)

    def __init__(self, context: AppContext, client_factory: Callable[[], OllamaClient] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.client_factory = client_factory
        self.runner = TaskRunner(self)
        self.modes = ModeManager(context.database)
        self.result: AnalysisResult | None = None
        self._cancel: CancelToken | None = None

        self.import_panel = LogImportPanel(context, OriginPurpose.ANALYSIS)

        self.mode_combo = QComboBox()
        self.mode_confirm = QCheckBox("확인함")
        self.estimate_button = QPushButton("AI로 추정")
        self.estimate_button.clicked.connect(self.estimate_mode)
        self.mode_label = QLabel()
        self.mode_label.setWordWrap(True)
        self.conditions_edit = text_box(40, "운전 조건 (선택)")
        self.description_edit = text_box(40, "사용자 설명 (선택)")
        self.use_ai = QCheckBox("AI 원인 가설도 만들기(로컬 Ollama)")
        self.use_ai.setChecked(True)
        mode_row = QHBoxLayout()
        mode_row.addWidget(self.mode_combo, 1)
        mode_row.addWidget(self.mode_confirm)
        mode_row.addWidget(self.estimate_button)
        mode_holder = QWidget()
        mode_holder.setLayout(mode_row)
        mode_row.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        form.addRow("운전 모드", mode_holder)
        form.addRow("", self.mode_label)
        form.setRowVisible(self.mode_label, False)
        self.mode_form = form
        form.addRow("운전 조건", self.conditions_edit)
        form.addRow("설명", self.description_edit)
        form.addRow("", self.use_ai)

        self.run_button = QPushButton("분석 실행")
        set_role(self.run_button, variant="primary")
        self.run_button.clicked.connect(self.start_analysis)
        self.cancel_button = QPushButton("취소")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_analysis)
        self.progress = QProgressBar()
        self.progress.setFormat("대기")
        run_row = QHBoxLayout()
        run_row.addWidget(self.run_button)
        run_row.addWidget(self.cancel_button)
        run_row.addWidget(self.progress, 1)

        # 결과: 권장 대응이 맨 위 (CR-01)
        self.guidance_box = QGroupBox(GUIDANCE_TITLES["NONE"])
        guidance_layout = QVBoxLayout(self.guidance_box)
        self.guidance_message = QLabel("분석을 실행하면 비슷한 승인 사례의 대응 방법을 보여 줍니다.")
        self.guidance_message.setWordWrap(True)
        set_role(self.guidance_message, "muted")
        self.guidance_stamp = QLabel("분석 전")
        set_role(self.guidance_stamp, "stamp", "neutral")
        stamp_row = QHBoxLayout()
        stamp_row.addWidget(self.guidance_stamp)
        stamp_row.addWidget(self.guidance_message, 1)
        self.guidance_list = QListWidget()
        self.guidance_list.setMinimumHeight(140)
        self.guidance_list.setWordWrap(True)
        guidance_layout.addLayout(stamp_row)
        guidance_layout.addWidget(self.guidance_list)

        # 결과 축 = 표제란 칸 (DESIGN.md): 라벨 칸 네 개를 한 줄에
        self.axis_cells: dict[str, QLabel] = {}
        self.axes_row = QHBoxLayout()
        self.axes_row.setSpacing(8)
        for axis, (title, _labels) in AXIS_LABELS.items():
            cell = QFrame()
            set_role(cell, "cell")
            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(10, 8, 10, 8)
            cell_layout.setSpacing(2)
            label = QLabel(title)
            set_role(label, "cell-label")
            value = QLabel("—")
            value.setWordWrap(True)
            set_role(value, "cell-value", "neutral")
            cell_layout.addWidget(label)
            cell_layout.addWidget(value)
            self.axes_row.addWidget(cell, 1)
            self.axis_cells[axis] = value
        self.axes_label = QLabel()  # 같은 내용을 글로 (복사·테스트용, 화면에는 칸으로 보인다)
        self.axes_label.setVisible(False)
        score_note = QLabel("유사도 점수는 비슷한 사례를 고르기 위한 순위 점수이며 진단 확률이 아닙니다.")
        set_role(score_note, "muted")

        self.findings_table = QTableWidget(0, 4)
        comfortable_table(self.findings_table)
        self.findings_table.setHorizontalHeaderLabels(["근거 키", "계층", "종류", "내용"])
        self.findings_table.verticalHeader().setVisible(False)
        self.findings_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.findings_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.findings_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.findings_table.horizontalHeader().setStretchLastSection(True)
        self.findings_table.setMinimumHeight(180)
        self.findings_table.cellDoubleClicked.connect(self.jump_to_finding)

        self.ai_box = QGroupBox("AI 원인 가설 (로컬 Ollama · 사용자 검토 필요)")
        ai_layout = QVBoxLayout(self.ai_box)
        self.ai_status = QLabel("아직 실행하지 않았습니다.")
        self.ai_status.setWordWrap(True)
        self.ai_view = QPlainTextEdit()
        self.ai_view.setReadOnly(True)
        self.ai_view.setMinimumHeight(120)
        ai_layout.addWidget(self.ai_status)
        ai_layout.addWidget(self.ai_view)

        self.notes_label = QLabel()
        self.notes_label.setWordWrap(True)
        set_role(self.notes_label, "muted")

        self.action_row = QHBoxLayout()
        self.report_button = QPushButton("보고서 저장 (JSON·HTML)")
        self.report_button.clicked.connect(self.save_report)
        self.register_normal = QPushButton("정상 학습 사례로 등록")
        self.register_normal.clicked.connect(lambda: self.request_case("NORMAL"))
        self.register_abnormal = QPushButton("비정상 학습 사례로 등록")
        self.register_abnormal.clicked.connect(lambda: self.request_case("ABNORMAL"))
        for button in (self.report_button, self.register_normal, self.register_abnormal):
            button.setEnabled(False)
            self.action_row.addWidget(button)
        self.action_row.addStretch(1)
        self.report_label = QLabel()
        self.report_label.setWordWrap(True)

        results = QVBoxLayout()
        results.addWidget(self.guidance_box)
        results.addWidget(section_label("결과 분류 (서로 독립된 판정)"))
        results.addLayout(self.axes_row)
        results.addWidget(self.axes_label)
        results.addWidget(score_note)
        results.addWidget(section_label("근거 (두 번 누르면 원본 로그 행으로 이동)"))
        results.addWidget(self.findings_table)
        results.addWidget(self.ai_box)
        results.addWidget(self.notes_label)
        results.addLayout(self.action_row)
        results.addWidget(self.report_label)
        results.addStretch(1)

        steps = QLabel("진행 순서  ① 왼쪽에 분석할 로그를 넣고 가져오기 → ② 운전 모드를 고르고 '확인함'(모르면 '모름') → "
                       "③ '분석 실행' → ④ 맨 위 '권장 대응'과 근거 확인 → ⑤ 보고서 저장")
        steps.setWordWrap(True)
        set_role(steps, "hint")
        side = QVBoxLayout()
        side.addWidget(steps)
        side.addLayout(form)
        side.addLayout(run_row)
        side.addLayout(results)
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
        layout.addWidget(split)
        self.refresh_modes()

    def _engine(self) -> DiagnosticEngine:
        return DiagnosticEngine(self.context.database, self.context.paths, self.context.settings, self.client_factory)

    def refresh_modes(self) -> None:
        current = self.mode_combo.currentData()
        self.mode_combo.clear()
        self.mode_combo.addItem("(모름)", None)
        for mode in self.modes.list(active_only=True):
            self.mode_combo.addItem(mode.name, mode.id)
        index = self.mode_combo.findData(current)
        self.mode_combo.setCurrentIndex(max(index, 0))

    def build_request(self) -> AnalysisRequest:
        mode_id = self.mode_combo.currentData()
        return AnalysisRequest(
            log_file_ids=self.import_panel.log_file_ids(), mode_id=mode_id,
            mode_confirmed=bool(mode_id is not None and self.mode_confirm.isChecked()),
            operating_conditions=self.conditions_edit.toPlainText().strip() or None,
            user_description=self.description_edit.toPlainText().strip() or None,
            use_ai=self.use_ai.isChecked(),
        )

    # ── 모드 추정 ──
    def estimate_mode(self) -> None:
        ids = self.import_panel.log_file_ids()
        if not ids or self.runner.busy:
            set_form_note(self.mode_form, self.mode_label, "로그를 먼저 올리세요.")
            return
        self.estimate_button.setEnabled(False)
        set_form_note(self.mode_form, self.mode_label, "AI가 운전 모드를 추정하는 중…")
        self.runner.start(self._engine().estimate_mode, ids, on_done=self._on_mode, on_error=self._on_mode_error)

    def _on_mode(self, guess) -> None:
        self.estimate_button.setEnabled(True)
        set_form_note(self.mode_form, self.mode_label, guess.message())
        if guess.mode_id is not None:
            self.mode_combo.setCurrentIndex(max(self.mode_combo.findData(guess.mode_id), 0))
            self.mode_confirm.setChecked(False)

    def _on_mode_error(self, message: str) -> None:
        self.estimate_button.setEnabled(True)
        set_form_note(self.mode_form, self.mode_label, f"AI 추정 실패: {message}")

    # ── 분석 ──
    def start_analysis(self) -> None:
        request = self.build_request()
        if not request.log_file_ids:
            self.notes_label.setText("분석할 로그를 먼저 올리세요.")
            return
        if self.runner.busy:
            return
        self._cancel = CancelToken()
        self.run_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setRange(0, 0)
        self.progress.setFormat("분석 중…")
        self.runner.start(self._engine().run, request, cancel=self._cancel, on_done=self._on_done,
                          on_error=self._on_error, on_cancel=self._on_cancel, on_progress=self._on_progress)

    def cancel_analysis(self) -> None:
        if self._cancel is not None:
            self._cancel.cancel()
            self.progress.setFormat("취소하는 중…")

    def _on_progress(self, done: int, total: int, message: str) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)
        self.progress.setFormat(message)

    def _end_run(self, text: str) -> None:
        self.run_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.progress.setFormat(text)

    def _on_cancel(self) -> None:
        self._end_run("취소함")
        self.notes_label.setText("분석을 취소했습니다. 분석 이력에는 '취소'로 남습니다.")

    def _on_error(self, message: str) -> None:
        self._end_run("실패")
        self.notes_label.setText(f"분석하지 못했습니다: {message}")

    def _on_done(self, result: AnalysisResult) -> None:
        self._end_run(f"완료 (R-{result.run_id:04d})")
        self.show_result(result)
        self.analysis_done.emit(result)

    def show_result(self, result: AnalysisResult) -> None:
        self.result = result
        guidance = result.guidance
        self.guidance_box.setTitle(GUIDANCE_TITLES[guidance.level])
        self.guidance_message.setText(guidance.message)
        self.guidance_list.clear()
        step = 0
        for item in guidance.items:
            label = SECTION_SHORT.get(item.section, item.section)
            if item.section in ("RESPONSE_STEP", "RESOLUTION"):
                step += 1
                label = f"{step}. {label}"
            self.guidance_list.addItem(f"[{label}] {strip_marker(item.content)}\n    출처: {item.case_label} · {item.source_name or '-'}")
        for item in guidance.check_methods:
            self.guidance_list.addItem(f"[원인 확인] {item.content}\n    출처: {item.case_label} · {item.source_name or '-'}")
        fit_list_height(self.guidance_list)  # 절차가 목록 안에서 잘려 보이지 않게(화면 전체가 스크롤된다)

        set_role(self.guidance_stamp, "stamp", GUIDANCE_TONE[guidance.level])
        self.guidance_stamp.setText(GUIDANCE_STAMP[guidance.level])
        lines = []
        for axis, (title, labels) in AXIS_LABELS.items():
            text = labels.get(result.axes[axis], result.axes[axis])
            lines.append(f"{title}: {text}")
            cell = self.axis_cells[axis]
            cell.setText(text)
            set_role(cell, "cell-value", AXIS_TONE[axis].get(result.axes[axis], "neutral"))
        self.axes_label.setText("\n".join(lines))

        self.findings_table.setRowCount(len(result.findings))
        for row, finding in enumerate(result.findings):
            values = (result.keys.get(row, ""), LAYER_LABELS.get(finding.layer, finding.layer), finding.finding_type,
                      finding.summary)
            for col, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setData(Qt.ItemDataRole.UserRole, row)
                self.findings_table.setItem(row, col, cell)
        self.notes_label.setText("\n".join(result.notes))
        self.show_ai(result)
        for button in (self.report_button, self.register_normal, self.register_abnormal):
            button.setEnabled(True)
        self.report_label.clear()

    def save_report(self) -> tuple[str, str] | None:
        if self.result is None:
            return None
        from app.reports.report_data import save_reports

        paths = save_reports(self.context.database, self.context.paths, self.result.run_id)
        self.report_label.setText(f"보고서를 저장했습니다: {self.context.paths.root / paths[1]}")
        return paths

    def request_case(self, case_type: str) -> None:
        """분석한 로그로 학습 사례 초안을 만들도록 메인 창에 요청한다(§3.3 14단계)."""
        if self.result is None:
            return
        kind = "정상" if case_type == "NORMAL" else "비정상"
        self.register_requested.emit(case_type, self.import_panel.log_file_ids(),
                                     f"R-{self.result.run_id:04d}에서 등록한 {kind} 사례")

    def show_ai(self, result: AnalysisResult) -> None:
        outcome = result.ai
        self.ai_view.clear()
        if outcome is None:
            self.ai_status.setText("AI 단계를 실행하지 않았습니다.")
            return
        labels = {"NOT_RUN": "실행 안 함", "UNAVAILABLE": "AI 사용 불가", "VALIDATION_FAILED": "AI 응답이 검증을 통과하지 못함",
                  "CANCELLED": "취소함"}
        if outcome.answer is None:
            self.ai_status.setText(f"{labels.get(outcome.status, outcome.status)} — 결정론·사례 검색 결과는 그대로 쓸 수 있습니다. "
                                   + "; ".join(outcome.errors)[:300])
            return
        answer = outcome.answer
        self.ai_status.setText("AI 가설 — 사용자 검토 필요. 근거 키는 위 근거 표와 같습니다. 근거가 유효해도 해석이 옳다는 보장은 없습니다.")
        lines: list[str] = []
        if answer.observations:
            lines.append("■ 관측 사실")
            lines += [f"  · {o.text}  [{', '.join(o.evidence)}]" for o in answer.observations]
        if answer.hypotheses:
            lines.append("■ 원인 가설")
            for h in answer.hypotheses:
                counter = f"  반대 근거 [{', '.join(h.counter_evidence)}]" if h.counter_evidence else ""
                lines.append(f"  · {h.text}  근거 [{', '.join(h.evidence)}]{counter}")
        if answer.response_plan:
            lines.append("■ AI가 정리한 대응 순서 (모두 승인된 매뉴얼·가이드 근거)")
            lines += [f"  {i}. {s.text}  [{', '.join(s.evidence)}]" for i, s in enumerate(answer.response_plan, start=1)]
        if answer.verification_steps:
            lines.append("■ 추가 확인 절차")
            for s in answer.verification_steps:
                based = s.evidence and all(outcome.kinds.get(k) in ("RESPONSE", "GUIDE") for k in s.evidence)
                tag = "" if based else "  (AI 제안 확인 절차 — 매뉴얼 근거 없음)"
                lines.append(f"  · {s.text}{tag}" + (f"  [{', '.join(s.evidence)}]" if s.evidence else ""))
        if answer.unknowns:
            lines.append("■ 판단 불가")
            lines += [f"  · {u}" for u in answer.unknowns]
        self.ai_view.setPlainText("\n".join(lines))

    def jump_to_finding(self, row: int, _column: int = 0) -> bool:
        if self.result is None or not (0 <= row < len(self.result.findings)):
            return False
        finding = self.result.findings[row]
        if finding.log_file_id is None or finding.line_start is None:
            return False
        return self.import_panel.show_line(finding.log_file_id, finding.line_start)
