"""신규 로그 분석 화면 (계획서 §8.4, CR-01). 결과 맨 위에 권장 대응, 그 아래 결과 축·근거·유사 사례·AI 가설.
근거를 두 번 누르면 원본 로그 행으로 이동한다.

CR-10 NA-20: 로그가 여러 개면 로그마다 따로 분석한다(같은 수집 세션으로 가져온 파일은 함께). 왼쪽 '올라온 로그'
목록에 로그마다 결과를 적고, 목록에서 로그를 고르면 그 결과를 보인다(드롭다운 없음)."""

from __future__ import annotations

import re
from collections.abc import Callable

import shiboken6
from PySide6.QtCore import QEvent, QRect, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
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
from app.analysis.error_messages import ErrorMessage, attach_context, presumed_errors, reference_texts, should_show
from app.bootstrap import AppContext
from app.core.cancel import CancelToken
from app.learning.intake import IntakeResult, IntakeService
from app.learning.normal_learner import default_client_factory
from app.models.enums import OriginPurpose
from app.ui.case_editor import text_box
from app.ui.confirm_box import ConfirmBox
from app.ui.question_box import QuestionBox
from app.ui.error_messages_box import ErrorMessagesBox
from app.ui.log_import_panel import LogImportPanel
from app.ui.navigation import StepPages, nav_row
from app.ui.natural_input import Collapsible, StoryBox, SummaryRow
from app.ui.theme import comfortable_table, fit_list_height, set_role, tone_color
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
# 왼쪽 목록 '분석 결과' 칸의 짧은 글(CR-10 NA-20). 자세한 판정은 오른쪽 결과 분류 칸에 있다.
LEVEL_SHORT = {"NONE_FOUND": "이상 못 찾음", "SUSPECTED": "비정상 의심", "RULE_VIOLATION": "규칙 위반",
               "NOT_EVALUATED": "판단 못 함"}


def ai_likelihood(result: AnalysisResult) -> int | None:
    answer = getattr(result.ai, "answer", None)
    return answer.abnormal_likelihood if answer is not None else None


def unit_status(result: AnalysisResult, errors: list[ErrorMessage] | None = None) -> tuple[str, str, str]:
    """목록 칸에 적을 (글, 색조, 풀이). 최종 판정 하나(정상·비정상 의심·판단 보류, 리뷰 2026-11-22)와 AI 가능성,
    풀이에 판정 이유·결과 분류·예상 에러 메시지 앞 줄(CR-11 EM-03)."""
    verdict = result.verdict
    text = verdict.short
    answer = getattr(result.ai, "answer", None)
    if answer is not None and answer.abnormal_likelihood is not None:
        text += f" · AI {answer.abnormal_likelihood}%"
    lines = [f"최종 판정: {verdict.title}", *[f"  · {reason}" for reason in verdict.reasons]]
    lines += [f"{title}: {labels.get(result.axes[axis], result.axes[axis])}" for axis, (title, labels) in AXIS_LABELS.items()]
    tooltip = "\n".join(lines) + f"\n분석 번호 R-{result.run_id:04d}"
    if errors:
        tooltip += "\n예상 에러 메시지:" + "".join(
            f"\n  {m.line if m.line is not None else '—'}행  {m.text[:90]}" for m in errors[:3])
    return text, verdict.tone, tooltip


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
        self.result: AnalysisResult | None = None  # 지금 오른쪽에 보이는 결과
        self.results: dict[int, AnalysisResult] = {}  # log_file_id → 그 로그(분석 단위)의 결과 (CR-10 NA-20)
        self._todo: list[list[int]] = []  # 이번 실행에서 남은 분석 단위
        self._run_total = 0
        self._cancel: CancelToken | None = None

        self.import_panel = LogImportPanel(context, OriginPurpose.ANALYSIS, status_column="분석 결과", allow_clear=True,
                                           checkable=True, compact=True, detach_views=True)
        self.import_panel.current_file_changed.connect(self._on_file_selected)
        self.import_panel.files_changed.connect(self._on_files_changed)

        self.conditions_edit = text_box(40, "운전 조건 (선택)")
        # CR-05: 상황은 글 한 칸에. 운전 조건 칸은 접어 두고 'AI로 정리'가 글에서 채운다.
        # CR-09: 운전 모드로 나누지 않는다. 분석은 늘 가장 비슷한 정상 로그와 비교한다.
        self.story = StoryBox(
            "이번 로그 상황을 편하게 적어 주세요 (선택)",
            "예) 반복 동작 중 멈췄고 TEST-E001이 화면에 떴음. 어제부터 가끔 그럼.",
            "적은 글은 AI 원인 가설에 참고로 들어갑니다. 'AI로 정리'를 누르면 운전 조건을 글에서 찾아 채웁니다.")
        self.description_edit = self.story.edit
        self.story.organize_button.clicked.connect(self.organize_story)
        self.use_ai = QCheckBox("AI 원인 가설도 만들기(로컬 Ollama)")
        self.use_ai.setChecked(True)
        form = QFormLayout()
        form.addRow("운전 조건", self.conditions_edit)
        manual = QWidget()
        manual.setLayout(form)
        form.setContentsMargins(0, 0, 0, 0)
        self.fields_section = Collapsible("칸으로 직접 고치기 (운전 조건)", manual)
        self.conditions_edit.textChanged.connect(self.refresh_summary)

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

        # CR-04: 승인된 참고 자료에 있는 코드가 로그에 나오면 따로 보여 준다(권장 대응과 섞지 않음)
        # CR-11 EM-01: 비정상으로 보이는 로그는 문제로 보이는 원문 줄을 따로 보인다(뜻은 만들지 않음)
        self.error_box = ErrorMessagesBox("예상 에러 메시지 — 문제로 보이는 원문 줄 (자동으로 고름 · 두 번 누르면 원본 행)")
        self.error_box.go_to.connect(self.jump_to_line)
        self.error_box.hide()
        self._errors: dict[int, list[ErrorMessage]] = {}  # run_id → 예상 에러 메시지

        self.reference_box = QGroupBox("참고 자료 — 로그에 나온 코드 (승인된 참고 자료, 권장 대응과 별개)")
        reference_layout = QVBoxLayout(self.reference_box)
        self.reference_message = QLabel("분석을 실행하면 로그에 나온 코드 중 참고 자료에 있는 것을 보여 줍니다.")
        self.reference_message.setWordWrap(True)
        set_role(self.reference_message, "muted")
        self.reference_list = QListWidget()
        self.reference_list.setWordWrap(True)
        self.reference_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.reference_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.reference_list.itemDoubleClicked.connect(self.jump_to_reference)
        self.reference_list.hide()
        reference_layout.addWidget(self.reference_message)
        reference_layout.addWidget(self.reference_list)

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

        self.ai_box = QGroupBox("AI 분석 의견 (로컬 Ollama · 가능성은 AI 추정치 · 사용자 검토 필요)")
        ai_layout = QVBoxLayout(self.ai_box)
        self.ai_status = QLabel("아직 실행하지 않았습니다.")
        self.ai_status.setWordWrap(True)
        self.ai_likelihood = QLabel()  # CR-07: 비정상일 가능성(AI 추정치)
        set_role(self.ai_likelihood, "cell-value", "neutral")
        self.ai_likelihood.hide()
        self.ai_view = QPlainTextEdit()
        self.ai_view.setReadOnly(True)
        self.ai_view.setMinimumHeight(120)
        ai_layout.addWidget(self.ai_likelihood)
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

        # ── CR-12 단계별 화면: ① 로그 넣기 → ② 분석 결과 → ③ 상세 확인 ──
        # 어느 로그의 결과인지(CR-10 NA-20). 다른 로그는 ②·③ 왼쪽 결과 목록에서 고른다(드롭다운 없음).
        self.result_title = QLabel("결과")
        set_role(self.result_title, "title")
        self.result_title.setWordWrap(True)
        self.result_hint = QLabel("왼쪽 목록에서 다른 로그를 고르면 그 로그의 결과가 보입니다.")
        self.result_hint.setWordWrap(True)
        set_role(self.result_hint, "muted")
        self.result_hint.hide()
        self.verdict = QLabel("분석 전")  # 판정 칸(이상 수준을 크게)
        set_role(self.verdict, "verdict", "neutral")
        self.verdict.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.verdict.setMinimumWidth(190)
        self.evidence_summary = QLabel("분석을 실행하면 이상 근거 개수와 AI 의견을 요약합니다.")
        self.evidence_summary.setWordWrap(True)
        summary_cell = QFrame()
        set_role(summary_cell, "cell")
        summary_layout = QVBoxLayout(summary_cell)
        summary_layout.setContentsMargins(12, 8, 12, 8)
        summary_label = QLabel("근거 요약 (결정론 근거 개수 · AI는 추정치)")
        set_role(summary_label, "cell-label")
        summary_layout.addWidget(summary_label)
        summary_layout.addWidget(self.evidence_summary)
        self.axis_cells["anomaly_level"].parentWidget().hide()  # 이상 수준은 판정 칸이 크게 보인다
        self.error_preview = ErrorMessagesBox("예상 에러 메시지 — 앞 3줄 (전체는 ③ 상세 확인)")
        self.error_preview.go_to.connect(self.jump_to_line)
        self.error_preview.hide()
        self.result_list = self._result_list()
        self.detail_list = self._result_list()
        self.confirm_box = ConfirmBox(self.context.database)  # CR-14: 확인 결과를 바로 사례로
        self.question_box = QuestionBox(self.context.database)  # CR-15: 애매할 때 판단 기준을 묻기
        self.question_box.reanalyze.connect(self.reanalyze)

        # ① 로그 넣기
        self.story.edit.setMinimumHeight(64)
        self.story.edit.setMaximumHeight(96)
        intake_hint = QLabel("분석할 로그를 끌어 놓고, 분석할 로그에 체크한 뒤 '분석 실행'을 누르세요. 로그마다 따로 분석합니다.")
        intake_hint.setWordWrap(True)
        set_role(intake_hint, "hint")
        intake = QVBoxLayout()
        intake.setContentsMargins(0, 0, 4, 0)
        intake.addWidget(intake_hint)
        intake.addWidget(self.import_panel, 1)
        intake.addWidget(self.story)
        intake.addWidget(self.fields_section)
        intake.addLayout(nav_row(None, self.use_ai, self.run_button))
        step1 = QWidget()
        step1.setLayout(intake)

        # ② 분석 결과 — 판정과 할 일 (권장 대응이 결과 맨 위, CR-01)
        head = QHBoxLayout()
        head.addWidget(self.result_title, 1)
        self.progress.setMinimumWidth(200)
        self.progress.setMaximumWidth(300)
        head.addWidget(self.progress)
        head.addWidget(self.cancel_button)
        top = QHBoxLayout()
        top.setSpacing(10)
        top.addWidget(self.verdict)
        top.addWidget(summary_cell, 1)
        self.detail_button = QPushButton("상세 확인  ›")
        self.detail_button.clicked.connect(lambda: self.steps.go(2))
        self.report_button_summary = QPushButton("보고서 저장")
        self.report_button_summary.setEnabled(False)
        self.report_button_summary.clicked.connect(self.save_report)
        back_to_intake = QPushButton("‹  로그 넣기")
        back_to_intake.clicked.connect(lambda: self.steps.go(0))
        summary = QVBoxLayout()
        summary.addLayout(head)
        summary.addWidget(self.result_hint)
        summary.addLayout(top)
        summary.addLayout(self.axes_row)
        # 권장 대응 → 예상 에러. 넓은 화면(오른쪽 1300px 이상)에서는 두 칸 나란히(CR-12 ST-04)
        self.cards = QGridLayout()
        self.cards.setContentsMargins(0, 0, 0, 0)
        self.cards.setHorizontalSpacing(10)
        self._wide: bool | None = None
        summary.addLayout(self.cards)
        summary.addWidget(self.question_box)
        summary.addWidget(self.confirm_box)
        summary.addStretch(1)
        self.result_scroll = self._scroll(summary)
        self.result_scroll.installEventFilter(self)
        self._arrange_cards(False)
        right2 = QVBoxLayout()
        right2.setContentsMargins(0, 0, 0, 0)
        right2.addWidget(self.result_scroll, 1)
        right2.addLayout(nav_row(back_to_intake, self.detail_button, self.report_button_summary))
        step2 = self._split(self._list_box("분석 결과 목록", self.result_list), right2, 300)

        # ③ 상세 확인 — 근거
        self.detail_title = QLabel("결과")
        set_role(self.detail_title, "title")
        self.detail_title.setWordWrap(True)
        self.detail_stamp = QLabel("분석 전")
        set_role(self.detail_stamp, "stamp", "neutral")
        self.detail_axes = QLabel()
        self.detail_axes.setWordWrap(True)
        set_role(self.detail_axes, "muted")
        self.guidance_detail = QGroupBox(GUIDANCE_TITLES["NONE"])
        self.guidance_detail_list = QListWidget()
        self.guidance_detail_list.setWordWrap(True)
        self.guidance_detail_list.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.guidance_list.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.guidance_list.installEventFilter(self)
        self.guidance_detail_list.installEventFilter(self)
        self.guidance_detail_message = QLabel()  # 대응이 없을 때는 목록 상자 대신 글로(빈 줄이 남지 않게)
        self.guidance_detail_message.setWordWrap(True)
        detail_guidance = QVBoxLayout(self.guidance_detail)
        detail_guidance.addWidget(self.guidance_detail_message)
        detail_guidance.addWidget(self.guidance_detail_list)
        findings = QWidget()
        findings_layout = QVBoxLayout(findings)
        findings_layout.setContentsMargins(0, 0, 0, 0)
        findings_layout.addWidget(score_note)
        findings_layout.addWidget(self.findings_table)
        for box in (self.error_box, self.reference_box, self.ai_box):
            box.setTitle("")
            set_role(box, "plain-card")  # 제목은 접는 칸 머리에 있으니 카드 위 여백을 비우지 않는다
        self.detail_sections = {
            "errors": Collapsible("예상 에러 메시지 (전체)", self._holder(self.error_box), expanded=True),
            "references": Collapsible("참고 자료 — 로그에 나온 코드 (승인된 참고 자료, 권장 대응과 별개)",
                                      self._holder(self.reference_box), expanded=True),
            "findings": Collapsible("근거 (두 번 누르면 아래 로그 원문의 그 행으로)", findings, expanded=True),
            "ai": Collapsible("AI 분석 의견 (로컬 Ollama · 가능성은 AI 추정치 · 사용자 검토 필요)",
                              self._holder(self.ai_box), expanded=False),
        }
        back_to_summary = QPushButton("‹  분석 결과로")
        back_to_summary.clicked.connect(lambda: self.steps.go(1))
        detail_head = QHBoxLayout()
        detail_head.addWidget(self.detail_title, 1)
        detail_head.addWidget(self.detail_stamp)
        detail = QVBoxLayout()
        detail.addLayout(detail_head)
        detail.addWidget(self.detail_axes)
        detail.addWidget(self.guidance_detail)
        for section in self.detail_sections.values():
            detail.addWidget(section)
        detail.addWidget(self.import_panel.views_section)  # 로그 원문(이벤트 표·세션 타임라인): 근거에서 이동하는 곳
        detail.addWidget(self.notes_label)
        detail.addWidget(self.report_label)
        detail.addStretch(1)
        self.detail_scroll = self._scroll(detail)
        right3 = QVBoxLayout()
        right3.setContentsMargins(0, 0, 0, 0)
        right3.addWidget(self.detail_scroll, 1)
        right3.addLayout(nav_row(back_to_summary, self.register_normal, self.register_abnormal, self.report_button))
        step3 = self._split(self._list_box("분석한 로그", self.detail_list), right3, 220)

        self.steps = StepPages("신규 로그 분석", "로그를 넣고 분석해 정상과 다른 점·권장 대응·예상 에러를 확인합니다.",
                               ("로그 넣기", "분석 결과", "상세 확인"))
        self.steps.add(step1)
        self.steps.add(step2)
        self.steps.add(step3)
        self.steps.set_locks(self._step_locks)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.steps)
        self.import_panel.checks_changed.connect(self._update_run_button)
        self.refresh_summary()
        self._update_run_button()

    # ── 단계 화면 도우미 (CR-12) ──
    WIDE_RESULT = 1300

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 (Qt 이름 그대로)
        if watched is getattr(self, "result_scroll", None) and event.type() == QEvent.Type.Resize:
            self._arrange_cards(watched.width() >= self.WIDE_RESULT)
        elif event.type() == QEvent.Type.Resize and watched in (getattr(self, "guidance_list", None),
                                                                getattr(self, "guidance_detail_list", None)):
            self._fit_guidance()
        return super().eventFilter(watched, event)

    def _fit_guidance(self) -> None:
        """권장 대응 목록 높이를 줄바꿈까지 글 높이로 잰다(목록 위젯 줄 높이는 너비가 바뀌면 안 맞음, CR-13 UI-21)."""
        for view in (self.guidance_list, self.guidance_detail_list):
            if not view.count():
                continue
            view.doItemsLayout()
            metrics = view.fontMetrics()
            width = max(view.viewport().width() - 40, 120)
            rows = sum(metrics.boundingRect(QRect(0, 0, width, 100_000), Qt.TextFlag.TextWordWrap,
                                            view.item(i).text()).height() + 16 for i in range(view.count()))
            height = max(rows + 2 * view.frameWidth() + 4, 48)
            if view.minimumHeight() != height or view.maximumHeight() != height:
                view.setFixedHeight(height)

    def _arrange_cards(self, wide: bool) -> None:
        if wide == self._wide:
            return
        self._wide = wide
        for widget in (self.guidance_box, self.error_preview):
            self.cards.removeWidget(widget)
        if wide:
            self.cards.addWidget(self.guidance_box, 0, 0, Qt.AlignmentFlag.AlignTop)
            self.cards.addWidget(self.error_preview, 0, 1, Qt.AlignmentFlag.AlignTop)
            self.cards.setColumnStretch(0, 1)
            self.cards.setColumnStretch(1, 1)
        else:
            self.cards.addWidget(self.guidance_box, 0, 0)
            self.cards.addWidget(self.error_preview, 1, 0)
            self.cards.setColumnStretch(1, 0)

    def _step_locks(self) -> dict[int, str]:
        if self.results or self.runner.busy or self._todo:
            return {}
        reason = "먼저 ① 로그 넣기에서 '분석 실행'을 누르세요."
        return {1: reason, 2: reason}

    def _scroll(self, layout: QVBoxLayout) -> QScrollArea:
        body = QWidget()
        body.setLayout(layout)
        area = QScrollArea()
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setWidget(body)
        return area

    @staticmethod
    def _holder(widget: QWidget) -> QWidget:
        """접는 구역 안에 넣을 감싸개(안의 위젯이 스스로 숨거나 보여도 접힘 상태를 깨지 않게)."""
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(widget)
        return holder

    @staticmethod
    def _list_box(title: str, table: QTableWidget) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel(title)
        set_role(label, "cell-label")
        layout.addWidget(label)
        layout.addWidget(table, 1)
        return box

    @staticmethod
    def _split(left: QWidget, right: QVBoxLayout, left_width: int) -> QSplitter:
        right_widget = QWidget()
        right_widget.setLayout(right)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(left)
        split.addWidget(right_widget)
        split.setSizes([left_width, 900])
        split.setStretchFactor(1, 1)
        split.setChildrenCollapsible(False)
        left.setMinimumWidth(200)
        return split

    def _result_list(self) -> QTableWidget:
        table = QTableWidget(0, 2)
        table.setHorizontalHeaderLabels(["파일", "결과"])
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        comfortable_table(table, elide_middle=True)
        table.currentCellChanged.connect(lambda row, *_: self._pick_from_list(table, row))
        return table

    def _pick_from_list(self, table: QTableWidget, row: int) -> None:
        item = table.item(row, 0) if row >= 0 else None
        if item is not None and item.data(Qt.ItemDataRole.UserRole) != self.import_panel.current_file_id():
            self.import_panel.select_file(int(item.data(Qt.ItemDataRole.UserRole)))

    def _refresh_result_lists(self) -> None:
        records = self.import_panel.records()
        current = self.import_panel.current_file_id()
        for table in (self.result_list, self.detail_list):
            table.blockSignals(True)
            table.setRowCount(len(records))
            for row, record in enumerate(records):
                text, tone, tooltip = self.import_panel.status_of(record.id)
                name = QTableWidgetItem(record.original_name)
                name.setData(Qt.ItemDataRole.UserRole, record.id)
                state = QTableWidgetItem(text)
                state.setForeground(tone_color(tone or "neutral"))
                if tooltip:
                    state.setToolTip(tooltip)
                    name.setToolTip(tooltip)
                table.setItem(row, 0, name)
                table.setItem(row, 1, state)
                if record.id == current:
                    table.setCurrentCell(row, 0)
                    table.selectRow(row)
            table.blockSignals(False)

    def _set_status(self, log_file_id: int, text: str, tone: str | None = None, tooltip: str | None = None) -> None:
        self.import_panel.set_status(log_file_id, text, tone, tooltip)
        self._refresh_result_lists()

    def jump_to_line(self, log_file_id: int, line: int) -> bool:
        """근거·예상 에러·참고 자료에서 원본 행으로: ③으로 가서 로그 원문을 펴고 그 행을 고른다."""
        self.steps.go(2)
        return self.import_panel.show_line(log_file_id, line)

    # ── 자유 글 (CR-05) ──
    def refresh_summary(self) -> None:
        self.story.set_summary([SummaryRow("운전 조건", self.conditions_edit.toPlainText().strip() or None)])

    def organize_story(self) -> None:
        text = self.story.text()
        if not text:
            self.story.result_label.setText("먼저 위 칸에 상황을 편하게 적어 주세요.")
            return
        if self.runner.busy:
            return
        service = IntakeService(self.context.database, self.context.settings,
                                self.client_factory or default_client_factory(self.context.settings))
        self.story.organize_button.setEnabled(False)
        self.story.result_label.setText("AI가 내 글에서 운전 조건을 찾는 중…")
        self.runner.start(service.organize_text, text, "ANALYSIS", on_done=self._on_organized,
                          on_error=self._on_organize_error)

    def _on_organized(self, result: IntakeResult) -> None:
        self.story.organize_button.setEnabled(True)
        if "operating_conditions" in result.fields:
            self.conditions_edit.setPlainText(result.fields["operating_conditions"])
        self.refresh_summary()
        if result.status == "RULES_ONLY":
            self.fields_section.set_expanded(True)
        self.story.result_label.setText(result.message())

    def _on_organize_error(self, message: str) -> None:
        self.story.organize_button.setEnabled(True)
        self.story.result_label.setText(f"정리하지 못했습니다: {message} '칸으로 직접 고치기'에서 고를 수 있습니다.")
        self.fields_section.set_expanded(True)

    def _engine(self) -> DiagnosticEngine:
        return DiagnosticEngine(self.context.database, self.context.paths, self.context.settings, self.client_factory)

    def build_request(self, log_file_ids: list[int] | None = None) -> AnalysisRequest:
        # CR-09: 운전 모드 없이 분석한다(가장 비슷한 승인 정상 로그와 비교)
        return AnalysisRequest(
            log_file_ids=list(log_file_ids) if log_file_ids is not None else self.import_panel.log_file_ids(),
            operating_conditions=self.conditions_edit.toPlainText().strip() or None,
            user_description=self.description_edit.toPlainText().strip() or None,
            use_ai=self.use_ai.isChecked(),
        )

    def analysis_units(self) -> list[list[int]]:
        """CR-10 NA-20: 분석 단위. 같은 수집 세션 파일은 함께(§4.1 시간축), 나머지는 파일마다 따로."""
        groups: dict[int, list[int]] = {}
        checked = set(self.import_panel.checked_ids())  # CR-12: 체크한 로그만
        for record in self.import_panel.records():
            if record.id not in checked:
                continue
            groups.setdefault(record.session_id, []).append(record.id)
        return list(groups.values())

    def _units_to_run(self) -> tuple[list[list[int]], bool]:
        """(이번에 분석할 단위, 모두 다시인지). 결과가 없는 로그만, 모두 있으면 전부 다시."""
        units = self.analysis_units()
        fresh = [u for u in units if not all(i in self.results for i in u)]
        return (fresh, False) if fresh else (units, True)

    def _update_run_button(self) -> None:
        units, again = self._units_to_run()
        if not units:
            self.run_button.setText("분석 실행")
        elif again:
            self.run_button.setText(f"모두 다시 분석 ({len(units)}개)")
        else:
            self.run_button.setText(f"분석 실행 ({len(units)}개)" if len(units) > 1 else "분석 실행")

    # ── 분석 ──
    def start_analysis(self) -> None:
        units, _again = self._units_to_run()
        if not units:
            self.notes_label.setText("분석할 로그를 먼저 올리세요.")
            return
        self._start(units)

    def reanalyze(self, log_ids: list[int]) -> None:
        """이 분석 단위만 다시 분석(판단 기준을 남긴 뒤 바로 확인, CR-15)."""
        present = set(self.import_panel.log_file_ids())
        unit = sorted(i for i in log_ids if i in present)
        if unit:
            self._start([unit])

    def _start(self, units) -> None:
        if self.runner.busy:
            return
        self._todo = [list(u) for u in units]
        self._run_total = len(units)
        for unit in units:
            for log_file_id in unit:
                self.results.pop(log_file_id, None)
                self._set_status(log_file_id, "분석 대기", "neutral")
        self._cancel = CancelToken()
        self.run_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self._on_file_selected(self.import_panel.current_file_id())
        self.steps.go(1)  # CR-12: 분석을 시작하면 ② 분석 결과로
        self._run_next()

    def _run_next(self) -> None:
        if not self._todo:
            total = self._run_total
            self._end_run(f"완료 — 로그 {total}개" if total > 1 else
                          (f"완료 (R-{self.result.run_id:04d})" if self.result else "완료"))
            return
        present = set(self.import_panel.log_file_ids())
        unit = [i for i in self._todo.pop(0) if i in present]  # 분석 중에 목록에서 뺀 로그는 건너뛴다
        if not unit:
            self._run_next()
            return
        number = self._run_total - len(self._todo)
        for log_file_id in unit:
            self._set_status(log_file_id, "분석 중…", "info")
        self.progress.setRange(0, 0)
        self.progress.setFormat(f"로그 {number}/{self._run_total} 분석 중…" if self._run_total > 1 else "분석 중…")
        self.runner.start(self._engine().run, self.build_request(unit), cancel=self._cancel,
                          on_done=lambda result: self._on_unit_done(unit, result),
                          on_error=lambda message: self._on_unit_error(unit, message),
                          on_cancel=self._on_cancel,
                          on_progress=lambda done, total, message: self._on_progress(done, total, message, number))

    def cancel_analysis(self) -> None:
        if self._cancel is not None:
            self._cancel.cancel()
            self.progress.setFormat("취소하는 중…")

    def _on_progress(self, done: int, total: int, message: str, number: int = 1) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)
        self.progress.setFormat(f"로그 {number}/{self._run_total} · {message}" if self._run_total > 1 else message)

    def _end_run(self, text: str) -> None:
        self.run_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.progress.setFormat(text)
        self._update_run_button()
        self.steps.refresh()

    def _on_cancel(self) -> None:
        waiting = {i for unit in self._todo for i in unit}
        for log_file_id in self.import_panel.log_file_ids():
            if log_file_id in waiting:
                self._set_status(log_file_id, "—")  # 시작하지 않은 로그는 분석 전으로
            elif log_file_id not in self.results:
                self._set_status(log_file_id, "취소함", "neutral")
        self._todo = []
        self._end_run("취소함")
        self.notes_label.setText("분석을 취소했습니다. 끝난 로그의 결과는 그대로이고, 분석 이력에는 '취소'로 남습니다.")

    def _on_unit_error(self, unit: list[int], message: str) -> None:
        for log_file_id in unit:
            self._set_status(log_file_id, "분석 실패", "bad", message)
        if self.import_panel.current_file_id() in unit:
            self.notes_label.setText(f"분석하지 못했습니다: {message}")
        self._run_next()  # 다른 로그는 이어서 분석한다

    def _on_unit_done(self, unit: list[int], result: AnalysisResult) -> None:
        text, tone, tooltip = unit_status(result, self.error_messages(result))
        if len(unit) > 1:
            tooltip += f"\n같은 수집 세션 파일 {len(unit)}개를 함께 분석한 결과"
        present = set(self.import_panel.log_file_ids())
        for log_file_id in unit:
            if log_file_id in present:  # 분석 중에 목록에서 뺀 로그의 결과는 이력에만 남는다
                self.results[log_file_id] = result
                self._set_status(log_file_id, text, tone, tooltip)
        if self.import_panel.current_file_id() in unit:
            self.show_result(result)
            self.reveal_result()
        self.analysis_done.emit(result)
        self._run_next()

    # ── 목록에서 로그 고르기 (CR-10 NA-20) ──
    def _on_file_selected(self, log_file_id: int | None) -> None:
        if log_file_id is None:
            self.clear_result("왼쪽 칸에 분석할 로그를 끌어 놓으세요.")
            return
        result = self.results.get(log_file_id)
        if result is None:
            name = self._file_name(log_file_id)
            busy = self.runner.busy or bool(self._todo)
            self.clear_result(f"{name}은(는) 분석하는 중입니다. 끝나면 결과가 여기에 보입니다." if busy else
                              f"{name}은(는) 아직 분석하지 않았습니다. '분석 실행'을 누르세요.", name)
            self.reveal_result()
            self._refresh_result_lists()  # ②·③ 목록 선택도 같이 옮긴다
            return
        if result is not self.result:
            self.show_result(result)
        else:
            self._show_title()
        self.reveal_result()
        self._refresh_result_lists()

    def reveal_result(self) -> None:
        """다른 로그를 고르면 ②·③ 결과를 맨 위부터 보인다(CR-12: 판정·권장 대응이 첫 화면에)."""
        def scroll() -> None:
            for area in (self.result_scroll, self.detail_scroll):
                if shiboken6.isValid(area):
                    area.verticalScrollBar().setValue(0)

        QTimer.singleShot(0, scroll)

    def _on_files_changed(self, ids: list[int]) -> None:
        present = set(ids)
        self.results = {k: v for k, v in self.results.items() if k in present}
        if self.result is not None and not any(r is self.result for r in self.results.values()):
            self.result = None
        self.result_hint.setVisible(len(ids) > 1)
        if not self.runner.busy:
            self._update_run_button()
        self._refresh_result_lists()
        self.steps.refresh()

    def _file_name(self, log_file_id: int) -> str:
        return next((r.original_name for r in self.import_panel.records() if r.id == log_file_id), f"로그 {log_file_id}")

    def result_log_ids(self) -> list[int]:
        """지금 보이는 결과를 낸 분석 단위의 로그(같은 수집 세션이면 여러 개)."""
        if self.result is None:
            return []
        return [i for i in self.import_panel.log_file_ids() if self.results.get(i) is self.result]

    def _show_title(self) -> None:
        if self.result is None:
            return
        names = [self._file_name(i) for i in self.result_log_ids()]
        head = names[0] if len(names) == 1 else f"{names[0]} 외 {len(names) - 1}개(같은 수집 세션)" if names else "결과"
        self.result_title.setText(f"결과 — {head} · R-{self.result.run_id:04d}")
        self.detail_title.setText(self.result_title.text())

    def clear_result(self, message: str = "", name: str | None = None) -> None:
        """결과 칸을 분석 전 모양으로 되돌린다(고른 로그에 아직 결과가 없을 때)."""
        self.result = None
        self.result_title.setText(f"결과 — {name}" if name else "결과")
        self.detail_title.setText(self.result_title.text())
        self.verdict.setText("분석 전")
        set_role(self.verdict, "verdict", "neutral")
        self.evidence_summary.setText(message or "분석을 실행하면 이상 근거 개수와 AI 의견을 요약합니다.")
        self.detail_stamp.setText("분석 전")
        set_role(self.detail_stamp, "stamp", "neutral")
        self.detail_axes.clear()
        self.guidance_detail.setTitle(GUIDANCE_TITLES["NONE"])
        self.guidance_detail_list.clear()
        self.guidance_detail_list.hide()
        self.guidance_detail_message.setText(message or "분석을 실행하면 비슷한 승인 사례의 대응 방법을 보여 줍니다.")
        self.guidance_detail_message.show()
        self.error_preview.hide()
        self.report_button_summary.setEnabled(False)
        self.guidance_box.setTitle(GUIDANCE_TITLES["NONE"])
        self.guidance_message.setText(message or "분석을 실행하면 비슷한 승인 사례의 대응 방법을 보여 줍니다.")
        set_role(self.guidance_stamp, "stamp", "neutral")
        self.guidance_stamp.setText("분석 전")
        self.guidance_list.clear()
        self.guidance_list.hide()
        self.reference_list.clear()
        self.reference_list.hide()
        self.error_box.hide()
        self.reference_message.setText("분석을 실행하면 로그에 나온 코드 중 참고 자료에 있는 것을 보여 줍니다.")
        for axis, cell in self.axis_cells.items():
            cell.setText("—")
            set_role(cell, "cell-value", "neutral")
        self.axes_label.clear()
        self.findings_table.setRowCount(0)
        self.ai_status.setText("아직 실행하지 않았습니다.")
        self.ai_likelihood.hide()
        self.ai_view.clear()
        self.notes_label.clear()  # 안내는 위 권장 대응 칸에(같은 글을 두 번 보이지 않게)
        for button in (self.report_button, self.register_normal, self.register_abnormal):
            button.setEnabled(False)
        self.report_label.clear()
        self.confirm_box.set_log(None)
        self.question_box.set_questions([], [], None)

    def show_result(self, result: AnalysisResult) -> None:
        self.result = result
        self._show_title()
        self.confirm_box.set_log(self.result_log_ids(),  # 같은 수집 세션 묶음이면 묶음 전체를 사례 하나로(리뷰 2026-11-22)
                                 result.memory.note() if getattr(result, "memory", None) is not None else "")
        self.question_box.set_questions(list(getattr(result, "questions", []) or []), self.result_log_ids(), result.run_id)
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
        self._fit_guidance()  # 절차가 목록 안에서 잘리거나 빈 줄이 남지 않게(화면 전체가 스크롤된다)
        self.guidance_list.setVisible(self.guidance_list.count() > 0)  # 대응이 없으면 빈 상자를 숨긴다
        # ③ 상세 확인에도 같은 권장 대응(결과 맨 위, CR-01)
        self.guidance_detail.setTitle(GUIDANCE_TITLES[guidance.level])
        self.guidance_detail_list.clear()
        for index in range(self.guidance_list.count()):
            self.guidance_detail_list.addItem(self.guidance_list.item(index).text())
        has_items = self.guidance_list.count() > 0
        self.guidance_detail_list.setVisible(has_items)
        self.guidance_detail_message.setText("" if has_items else guidance.message)
        self.guidance_detail_message.setVisible(not has_items)
        self._fit_guidance()
        self.show_references(result)
        self.show_errors(result)

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
        verdict = result.verdict  # 최종 판정 하나: 정상 / 비정상 / 판단 보류(리뷰 2026-11-22)
        self.verdict.setText(verdict.title)
        self.verdict.setToolTip("\n".join(verdict.reasons))
        set_role(self.verdict, "verdict", verdict.tone)
        self.detail_stamp.setText(verdict.short)
        set_role(self.detail_stamp, "stamp", verdict.tone)
        self.detail_axes.setText(" · ".join(lines))
        self.evidence_summary.setText(self.summary_text(result))

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
        for button in (self.report_button, self.register_normal, self.register_abnormal, self.report_button_summary):
            button.setEnabled(True)
        self.report_label.clear()

    def summary_text(self, result: AnalysisResult) -> str:
        """판정 옆 근거 요약: 결정론 근거 개수와 AI 가능성만(문장을 만들지 않는다, CR-12 ST-02)."""
        counts: dict[str, int] = {}
        for message in self.error_messages(result):
            counts[message.group_title] = counts.get(message.group_title, 0) + 1
        evidence = " · ".join(f"{title} {count}" for title, count in counts.items()) or "이상으로 보이는 줄 없음"
        reasons = "판정 이유: " + " / ".join(result.verdict.reasons)
        judgment = getattr(result, "ai_judgment", None)
        likelihood = ai_likelihood(result)
        if judgment is not None:
            said = {"ABNORMAL": "비정상 쪽", "NORMAL": "정상 쪽", "UNDECIDED": "판단 보류"}[judgment.state]
            basis = f"문턱 {judgment.threshold}%" + (" · 확인 이력 보정" if judgment.calibrated else "")
            ai = f"AI: {said}" + (f" (가능성 {likelihood}% · {basis})" if likelihood is not None else " (가능성을 매기지 않음)")
        elif likelihood is not None:
            ai = f"AI 비정상 가능성 {likelihood}% (AI 추정치 · 사용자 검토 필요)"
        else:
            ai = "AI 의견 없음(AI를 쓰지 않음)"
        return evidence + "\n" + reasons + "\n" + ai

    def error_messages(self, result: AnalysisResult) -> list[ErrorMessage]:
        """CR-11 EM-01·02: 결정론 근거에서 고른 예상 에러 메시지(분석마다 한 번만 만든다)."""
        if result.run_id not in self._errors:
            from contextlib import closing

            from app.storage.repositories import EventRepository

            event_ids = [f.event_id for f in result.findings if f.event_id]
            with closing(self.context.database.connect()) as conn:
                raw = {e.id: e.raw_text for e in EventRepository(conn).get_many(event_ids)}
            messages = presumed_errors(result.findings, result.keys, raw, references=reference_texts(result.references))
            attach_context(self.context.database, messages)  # CR-13 SQ-01: 직전 단계·앞 3줄
            self._errors[result.run_id] = messages
        return self._errors[result.run_id]

    def show_errors(self, result: AnalysisResult) -> None:
        if not should_show(result.axes.get("anomaly_level"), ai_likelihood(result)):
            self.error_box.hide()  # 비정상으로 보이지 않는 로그에는 띄우지 않는다
            self.error_preview.show_messages([], "", "비정상으로 보이지 않아 예상 에러 메시지를 고르지 않았습니다.")
            return
        messages = self.error_messages(result)
        self.error_preview.show_messages(
            messages[:3], f"문제로 보이는 줄 {len(messages)}개 중 앞 {min(3, len(messages))}개. 전체는 ③ 상세 확인.",
            "에러로 보이는 줄을 찾지 못했습니다. ③ 상세 확인의 근거와 AI 의견을 보세요.")
        titles = list(dict.fromkeys(m.group_title for m in messages))
        self.error_box.show_messages(
            messages,
            f"이 로그에서 문제로 보이는 줄 {len(messages)}개({', '.join(titles)}). 줄은 로그 원문 그대로이고, "
            "뜻은 승인된 참고 자료에 있을 때만 붙입니다.",
            "에러로 보이는 줄을 찾지 못했습니다. 아래 근거(빠진 이벤트·흐름 차이)와 AI 의견을 보세요.")

    def show_references(self, result: AnalysisResult) -> None:
        from app.reports.report_data import reference_lines

        self.reference_list.clear()
        for match in result.references:
            ref = {"code": match.code, "count": match.count, "file": match.first_file_name, "line": match.first_line,
                   "entries": [hit.as_dict() for hit in match.entries]}
            item = QListWidgetItem("\n".join(reference_lines(ref)))
            item.setData(Qt.ItemDataRole.UserRole, (match.first_log_file_id, match.first_line))
            item.setToolTip("두 번 누르면 왼쪽 로그의 그 행으로 이동합니다.")
            self.reference_list.addItem(item)
        from app.reports.report_data import passage_line

        for hit in result.passages:  # CR-07 REF-07: 일반 문서의 관련 문단
            item = QListWidgetItem("[관련 문서 내용] " + passage_line(hit.as_dict()))
            item.setData(Qt.ItemDataRole.UserRole, None)
            self.reference_list.addItem(item)
        if result.references or result.passages:
            parts = []
            if result.references:
                parts.append(f"로그에 나온 코드 {len(result.references)}개가 승인된 참고 자료에 있습니다")
            if result.passages:
                parts.append(f"로그와 낱말이 겹치는 참고 문서 문단 {len(result.passages)}개를 찾았습니다")
            self.reference_message.setText(". ".join(parts) + ". 내용은 참고 자료 글 그대로입니다.")
            fit_list_height(self.reference_list, minimum=60)
            # 내용만큼(줄바꿈이 한 줄 늘어도 잘리지 않게 한 줄 여유). 기본 크기(192px)로 빈 공간이 생기지 않게 한다.
            self.reference_list.setMaximumHeight(self.reference_list.minimumHeight()
                                                 + self.reference_list.fontMetrics().height() * 2)
        else:
            from contextlib import closing

            from app.storage.repositories import ReferenceRepository

            with closing(self.context.database.connect()) as conn:
                any_reference = bool(ReferenceRepository(conn).approved_entries())
            self.reference_message.setText(
                "로그에 나온 코드 중 승인된 참고 자료에 있는 것이 없습니다." if any_reference else
                "승인된 참고 자료가 없습니다. 오류 코드 목록·설명 문서를 '관리 → 참고 자료 관리'에서 올리면 여기에 보입니다.")
        self.reference_list.setVisible(bool(result.references or result.passages))

    def jump_to_reference(self, item: QListWidgetItem) -> bool:
        if not item.data(Qt.ItemDataRole.UserRole):  # 관련 문서 문단은 로그 행이 없다
            return False
        log_file_id, line = item.data(Qt.ItemDataRole.UserRole)
        return self.jump_to_line(log_file_id, line)

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
        self.register_requested.emit(case_type, self.result_log_ids() or self.import_panel.log_file_ids(),
                                     f"R-{self.result.run_id:04d}에서 등록한 {kind} 사례")

    def show_ai(self, result: AnalysisResult) -> None:
        outcome = result.ai
        self.ai_view.clear()
        self.ai_likelihood.hide()
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
        self.ai_status.setText("AI 분석 의견 — 사용자 검토 필요. 가능성은 AI 추정치이며 통계 확률이 아닙니다. "
                               "근거 키는 위 근거 표와 같습니다. 근거가 유효해도 해석이 옳다는 보장은 없습니다.")
        lines: list[str] = []
        if answer.abnormal_likelihood is not None:  # CR-07 AO-01
            self.ai_likelihood.setText(f"비정상일 가능성 {answer.abnormal_likelihood}%")
            tone = "bad" if answer.abnormal_likelihood >= 70 else "warn" if answer.abnormal_likelihood >= 30 else "ok"
            set_role(self.ai_likelihood, "cell-value", tone)
            self.ai_likelihood.show()
            lines.append(f"■ 비정상일 가능성 {answer.abnormal_likelihood}% (AI 추정치 · 통계 확률 아님)")
            lines.append(f"  이유: {answer.likelihood_reason or '-'}  근거 [{', '.join(answer.likelihood_evidence)}]")
        else:
            lines.append("■ AI가 비정상일 가능성을 매기지 않았습니다.")
        judgment = getattr(result, "ai_judgment", None)
        if judgment is not None:  # CR-14 CM-07: 확인 이력으로 고른 문턱
            lines.append(f"  {judgment.note()}")
        if answer.observations:
            lines.append("■ 관측 사실")
            lines += [f"  · {o.text}  [{', '.join(o.evidence)}]" for o in answer.observations]
        if answer.hypotheses:
            lines.append("■ 원인 가설 (가능성은 AI 추정치)")
            for h in answer.hypotheses:
                counter = f"  반대 근거 [{', '.join(h.counter_evidence)}]" if h.counter_evidence else ""
                chance = f"{h.likelihood:>3}%  " if h.likelihood is not None else "  —   "
                lines.append(f"  · {chance}{h.text}  근거 [{', '.join(h.evidence)}]{counter}")
            rated = [h.likelihood for h in answer.hypotheses if h.likelihood is not None]
            if rated:
                lines.append(f"  · {100 - sum(rated):>3}%  판단 불가(가설로 설명하지 못한 나머지)")
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
        return self.jump_to_line(finding.log_file_id, finding.line_start)
