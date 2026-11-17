"""정상 로그 학습 화면 (계획서 §8.2). 정상 판정 근거, 패턴 후보(사용자가 승인·거부), AI 초안."""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.learning.case_manager import CaseDetail, CaseError
from app.learning.normal_learner import NormalLearner
from app.models.enums import OriginPurpose
from app.ui.case_editor import CaseEditorBase, text_box
from app.ui.natural_input import SummaryRow
from app.ui.theme import comfortable_table, fit_widget_column

PATTERN_LABELS = {
    "REQUIRED": "필수", "OPTIONAL": "선택", "ORDER_CONSTRAINT": "순서 제약", "INDEPENDENT": "독립",
    "REPEATED": "반복", "UNCLASSIFIED": "미분류",
}
DECISIONS = (("PROPOSED", "보류"), ("APPROVED", "승인"), ("REJECTED", "거부"))
KIND_LABELS = {"FACT": "관측", "HYPOTHESIS": "추정", "UNKNOWN": "불명"}


class NormalLearningPage(CaseEditorBase):
    case_type = "NORMAL"
    purpose = OriginPurpose.NORMAL_CASE
    steps_text = ("진행 순서  ① 왼쪽에 정상 로그를 넣고 가져오기 → ② 아래 칸에 상황과 정상이라고 본 이유를 편하게 적고 "
                  "'AI로 정리' → ③ 빠진 것만 질문에 답하기 → ④ '패턴 후보 만들기' 후 후보마다 승인·거부 → ⑤ '승인'. "
                  "입력은 자동 저장됩니다.")
    story_placeholder = "예) 어제 오후 ○○ 모드로 돌렸고, 처음부터 끝까지 경고 없이 정상 종료됨. FW 2.3, 기본 구성."
    mode_required = True

    def specific_summary_rows(self) -> list[SummaryRow]:
        return [SummaryRow("정상 판정 근거", self.rationale_edit.toPlainText().strip() or None, required=True)]

    def build_specific(self, layout: QVBoxLayout) -> None:
        self.rationale_edit = text_box(60, "이 로그를 정상으로 판단한 근거 (승인 필수)")
        self.form.addRow("정상 판정 근거", self.rationale_edit)

        box = QGroupBox("정상 패턴 후보 (관찰 기반 · 필수·순서 확정은 사용자 승인)")
        inner = QVBoxLayout(box)
        buttons = QHBoxLayout()
        self.propose_button = QPushButton("패턴 후보 만들기")
        self.propose_button.clicked.connect(self.propose_patterns)
        self.draft_button = QPushButton("AI 초안 만들기")
        self.draft_button.clicked.connect(self.generate_draft)
        buttons.addWidget(self.propose_button)
        buttons.addWidget(self.draft_button)
        buttons.addStretch(1)
        inner.addLayout(buttons)
        self.pattern_table = QTableWidget(0, 5)
        comfortable_table(self.pattern_table)
        self.pattern_table.setHorizontalHeaderLabels(["결정", "유형", "내용", "관찰", "설명"])
        self.pattern_table.verticalHeader().setVisible(False)
        self.pattern_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.pattern_table.horizontalHeader().setStretchLastSection(True)
        self.pattern_table.setMinimumHeight(180)
        inner.addWidget(self.pattern_table)
        layout.addWidget(box)

        draft_box = QGroupBox("AI 학습 초안 (제안 · 승인 전에는 확정 지식 아님)")
        draft_layout = QVBoxLayout(draft_box)
        self.draft_view = QPlainTextEdit()
        self.draft_view.setReadOnly(True)
        self.draft_view.setMinimumHeight(110)
        self.draft_status = QLabel()
        draft_layout.addWidget(self.draft_status)
        draft_layout.addWidget(self.draft_view)
        layout.addWidget(draft_box)

    def specific_fields(self) -> dict[str, Any]:
        return {"normal_rationale": self.rationale_edit.toPlainText().strip() or None}

    def specific_widgets(self) -> list:
        return [self.rationale_edit]

    def fill_specific(self, detail: CaseDetail | None) -> None:
        case = detail.case if detail else None
        self.rationale_edit.setPlainText(case.normal_rationale or "" if case else "")
        patterns = detail.patterns if detail else []
        self.pattern_table.setRowCount(len(patterns))
        for row, pattern in enumerate(patterns):
            combo = QComboBox()
            for value, label in DECISIONS:
                combo.addItem(label, value)
            combo.setCurrentIndex(max(combo.findData(pattern.status), 0))
            if pattern.status not in ("PROPOSED", "APPROVED", "REJECTED"):
                combo.addItem(pattern.status, pattern.status)
                combo.setCurrentIndex(combo.count() - 1)
            combo.setProperty("pattern_id", pattern.id)
            self.pattern_table.setCellWidget(row, 0, combo)
            fit_widget_column(self.pattern_table)
            values = (PATTERN_LABELS.get(pattern.pattern_type, pattern.pattern_type), pattern.describe(),
                      str(pattern.observed_count), pattern.basis_note or "")
            for column, value in enumerate(values, start=1):
                self.pattern_table.setItem(row, column, QTableWidgetItem(value))
        claims = [c for c in (detail.claims if detail else []) if c.source == "AI"]
        self.draft_view.setPlainText("\n".join(
            f"[{KIND_LABELS.get(c.claim_kind, c.claim_kind)}] {c.claim_text}"
            + (f"  (근거 {len(c.evidence)}개)" if c.evidence else "") for c in claims))
        self.draft_status.setText(f"AI 주장 {len(claims)}개 (모두 제안 상태)" if claims else "AI 초안 없음")

    def approval_decisions(self) -> dict[str, dict[int, str]]:
        decisions: dict[int, str] = {}
        for row in range(self.pattern_table.rowCount()):
            combo = self.pattern_table.cellWidget(row, 0)
            if combo is not None and combo.currentData() in ("PROPOSED", "APPROVED", "REJECTED"):
                decisions[int(combo.property("pattern_id"))] = combo.currentData()
        return {"patterns": decisions}

    def restore_decisions(self, decisions: dict[str, dict[int, str]]) -> None:
        wanted = decisions.get("patterns", {})
        for row in range(self.pattern_table.rowCount()):
            combo = self.pattern_table.cellWidget(row, 0)
            value = wanted.get(int(combo.property("pattern_id")))
            if value is not None:
                combo.setCurrentIndex(max(combo.findData(value), 0))

    def set_all_decisions(self, value: str, pattern_types: set[str] | None = None) -> None:
        """테스트·일괄 처리용: 유형이 맞는 후보의 결정을 한꺼번에 바꾼다."""
        if self.detail is None:
            return
        by_id = {p.id: p for p in self.detail.patterns}
        for row in range(self.pattern_table.rowCount()):
            combo = self.pattern_table.cellWidget(row, 0)
            pattern = by_id.get(int(combo.property("pattern_id")))
            if pattern and (pattern_types is None or pattern.pattern_type in pattern_types):
                combo.setCurrentIndex(max(combo.findData(value), 0))

    def _learner(self) -> NormalLearner:
        return NormalLearner(self.context.database, self.context.settings, self._client_factory())

    def propose_patterns(self) -> None:
        if self.save() is None:
            return
        try:
            patterns = self._learner().propose(self.case_id)
        except CaseError as exc:
            self.message_label.setText(str(exc))
            return
        self.load_case(self.case_id)
        self.message_label.setText(f"패턴 후보 {len(patterns)}개를 만들었습니다. 승인할 후보를 고르세요.")

    def generate_draft(self) -> None:
        if self.save() is None or self.runner.busy:
            return
        self.draft_button.setEnabled(False)
        self.draft_status.setText("AI가 초안을 쓰는 중…")
        self.runner.start(self._learner().generate_draft, self.case_id, on_done=self._on_draft,
                          on_error=self._on_draft_error)

    def _on_draft(self, outcome) -> None:
        self.draft_button.setEnabled(True)
        self.load_case(self.case_id)
        if outcome.status != "OK":
            labels = {"NO_MODEL": "모델이 설정되지 않음", "AI_UNAVAILABLE": "AI 사용 불가",
                      "VALIDATION_FAILED": "AI 응답이 검증을 통과하지 못함"}
            self.draft_status.setText(f"{labels.get(outcome.status, outcome.status)}: {'; '.join(outcome.errors)[:300]}")

    def _on_draft_error(self, message: str) -> None:
        self.draft_button.setEnabled(True)
        self.draft_status.setText(f"AI 초안 실패: {message}")
