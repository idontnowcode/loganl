"""비정상 로그 학습 화면 (계획서 §8.3, CR-01). 진단 가이드(필수)·대응 매뉴얼(선택) 첨부, 구조화 결과 승인,
오류 증상·판정 기준·원인 확정 여부, 가이드-로그 연결."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.core.decoding import BinaryFileError
from app.core.importer import ImportRejected
from app.learning.abnormal_learner import AbnormalLearner
from app.learning.case_manager import CaseDetail, CaseError
from app.models.enums import OriginPurpose
from app.storage.repositories import GuideRepository
from app.ui.case_editor import CaseEditorBase, text_box
from app.ui.natural_input import SummaryRow
from app.ui.theme import comfortable_table, fit_widget_column

SECTION_LABELS = {
    "SYMPTOM": "오류 증상", "CONDITION": "발생 조건", "CRITERIA": "판정 기준", "CAUSE": "가능한 원인",
    "CHECK_METHOD": "원인 확인 방법", "RESOLUTION": "해결 방법", "VERIFICATION": "조치 후 검증", "NOTE": "참고",
    "RESPONSE_STEP": "대응 절차", "PRECAUTION": "주의사항", "ESCALATION": "상위 지원 기준",
}
KIND_SHORT = {"DIAGNOSTIC_GUIDE": "가이드", "RESPONSE_MANUAL": "매뉴얼", None: "사용자"}
SOURCE_LABELS = {"GUIDE_TEXT": "원문", "AI_PROPOSED": "AI 분류", "USER": "사용자"}
CAUSE_CHOICES = ((None, "(선택 안 함)"), ("CONFIRMED", "원인 확정"), ("SUSPECTED", "원인 추정"), ("UNKNOWN", "원인 모름"))
DECISIONS = (("PROPOSED", "보류"), ("APPROVED", "승인"), ("REJECTED", "거부"))


class AbnormalLearningPage(CaseEditorBase):
    case_type = "ABNORMAL"
    purpose = OriginPurpose.ABNORMAL_CASE
    steps_text = ("진행 순서  ① 왼쪽 칸에 비정상 로그를 끌어 놓기(바로 올라옴) → ② 아래 칸에 무슨 일이 있었는지 편하게 적고 'AI로 정리' → "
                  "③ 진단 가이드(필수)·대응 매뉴얼(선택) 첨부 → ④ 빠진 것만 질문에 답하기 → ⑤ '승인'. 입력은 자동 저장됩니다.")
    story_placeholder = ("예) 반복 동작 중 TEST-E001이 뜨고 멈춤. 이 코드가 세 번 넘게 나오면 비정상으로 봄. "
                         "원인은 아직 모름.")

    def specific_summary_rows(self) -> list[SummaryRow]:
        cause = self.cause_combo.currentData()
        return [
            SummaryRow("진단 가이드", f"첨부 {self._guide_count}개" if self._guide_count else None, required=True),
            SummaryRow("오류 증상", self.symptom_edit.toPlainText().strip() or None, required=True),
            SummaryRow("판정 기준", self.criteria_edit.toPlainText().strip() or None, required=True),
            SummaryRow("원인 확정 여부", self.cause_combo.currentText() if cause else None, required=True),
        ]

    def build_specific(self, layout: QVBoxLayout) -> None:
        self._guide_count = 0
        self.symptom_edit = text_box(54, "오류 증상 (승인 필수)")
        self.criteria_edit = text_box(54, "비정상 판정 기준 (승인 필수)")
        self.cause_combo = QComboBox()
        for value, label in CAUSE_CHOICES:
            self.cause_combo.addItem(label, value)
        self.form.addRow("오류 증상", self.symptom_edit)
        self.form.addRow("판정 기준", self.criteria_edit)
        self.form.addRow("원인 확정 여부", self.cause_combo)

        box = QGroupBox("진단 가이드(필수) · 대응 매뉴얼(선택)")
        inner = QVBoxLayout(box)
        buttons = QHBoxLayout()
        self.guide_button = QPushButton("진단 가이드 첨부…")
        self.guide_button.clicked.connect(lambda: self._browse_attachment("DIAGNOSTIC_GUIDE"))
        self.manual_button = QPushButton("대응 매뉴얼 첨부…")
        self.manual_button.clicked.connect(lambda: self._browse_attachment("RESPONSE_MANUAL"))
        self.ai_structure_button = QPushButton("선택 문서를 AI로 다시 분류")
        self.ai_structure_button.clicked.connect(self.restructure_selected_with_ai)
        self.fill_button = QPushButton("가이드에서 증상·기준 채우기")
        self.fill_button.clicked.connect(self.fill_from_guide)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        for index, b in enumerate((self.guide_button, self.manual_button, self.fill_button, self.ai_structure_button)):
            grid.addWidget(b, index // 2, index % 2)
        inner.addLayout(grid)
        self.attachment_list = QListWidget()
        self.attachment_list.setFixedHeight(70)
        self.attachment_list.currentItemChanged.connect(self._show_attachment)
        inner.addWidget(self.attachment_list)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFixedHeight(110)
        self.preview.setPlaceholderText("첨부를 고르면 원문이 보입니다.")
        inner.addWidget(self.preview)
        self.item_table = QTableWidget(0, 5)
        comfortable_table(self.item_table)
        self.item_table.setHorizontalHeaderLabels(["결정", "문서", "항목", "출처", "내용"])
        self.item_table.verticalHeader().setVisible(False)
        self.item_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.item_table.horizontalHeader().setStretchLastSection(True)
        self.item_table.setMinimumHeight(170)
        inner.addWidget(self.item_table)
        self.attach_status = QLabel()
        self.attach_status.setWordWrap(True)
        inner.addWidget(self.attach_status)
        layout.addWidget(box)

        link_box = QGroupBox("가이드-로그 연결 (로그의 오류 코드가 문서 원문에 나오는 곳)")
        link_layout = QVBoxLayout(link_box)
        self.link_list = QListWidget()
        self.link_list.setFixedHeight(70)
        self.confirm_links = QCheckBox("승인할 때 이 연결을 확정")
        self.confirm_links.setChecked(True)
        link_layout.addWidget(self.link_list)
        link_layout.addWidget(self.confirm_links)
        layout.addWidget(link_box)

    def _learner(self) -> AbnormalLearner:
        return AbnormalLearner(self.context.database, self.context.paths, self.context.settings, self._client_factory())

    def specific_fields(self) -> dict[str, Any]:
        return {
            "symptom": self.symptom_edit.toPlainText().strip() or None,
            "abnormal_criteria": self.criteria_edit.toPlainText().strip() or None,
            "cause_status": self.cause_combo.currentData(),
        }

    def specific_widgets(self) -> list:
        return [self.symptom_edit, self.criteria_edit, self.cause_combo]

    def fill_specific(self, detail: CaseDetail | None) -> None:
        case = detail.case if detail else None
        self.symptom_edit.setPlainText(case.symptom or "" if case else "")
        self.criteria_edit.setPlainText(case.abnormal_criteria or "" if case else "")
        self._select_data(self.cause_combo, case.cause_status if case else None)
        self.attachment_list.clear()
        self.preview.clear()
        rows, items = [], []
        if case is not None:
            from contextlib import closing

            with closing(self.context.database.connect()) as conn:
                rows = GuideRepository(conn).attachments(case.id)
                items = GuideRepository(conn).items(case.id)
        self._guide_count = sum(1 for row in rows if row["attachment_kind"] == "DIAGNOSTIC_GUIDE")
        for row in rows:
            entry = QListWidgetItem(f"[{KIND_SHORT[row['attachment_kind']]}] {row['original_name']}")
            entry.setData(Qt.ItemDataRole.UserRole, row["id"])
            self.attachment_list.addItem(entry)
        self.item_table.setRowCount(len(items))
        for r, item in enumerate(items):
            combo = QComboBox()
            for value, label in DECISIONS:
                combo.addItem(label, value)
            shown = item["status"]
            if shown == "PROPOSED" and item["source_kind"] == "GUIDE_TEXT":
                shown = "APPROVED"  # CR-05 NL-07: 원문 그대로 나눈 항목은 '승인'을 기본으로(사례 승인은 사용자가 누름)
            combo.setCurrentIndex(max(combo.findData(shown), 0))
            combo.setProperty("item_id", item["id"])
            self.item_table.setCellWidget(r, 0, combo)
            fit_widget_column(self.item_table)
            values = (KIND_SHORT.get(item["attachment_kind"], "사용자"), SECTION_LABELS.get(item["section"], item["section"]),
                      SOURCE_LABELS.get(item["source_kind"], item["source_kind"]), item["content"])
            for c, value in enumerate(values, start=1):
                cell = QTableWidgetItem(value)
                cell.setToolTip(item["content"][:1000])
                self.item_table.setItem(r, c, cell)
        self.link_list.clear()
        for claim in (detail.claims if detail else []):
            if claim.source == "GUIDE":
                self.link_list.addItem(f"{claim.claim_text} [{claim.status}]")

    def approval_decisions(self) -> dict[str, dict[int, str]]:
        guides: dict[int, str] = {}
        for r in range(self.item_table.rowCount()):
            combo = self.item_table.cellWidget(r, 0)
            if combo is not None:
                guides[int(combo.property("item_id"))] = combo.currentData()
        claims = {}
        if self.confirm_links.isChecked() and self.detail:
            claims = {c.id: "CONFIRMED" for c in self.detail.claims if c.source == "GUIDE" and c.status == "PROPOSED"}
        return {"guides": guides, "claims": claims}

    def restore_decisions(self, decisions: dict[str, dict[int, str]]) -> None:
        wanted = decisions.get("guides", {})
        for r in range(self.item_table.rowCount()):
            combo = self.item_table.cellWidget(r, 0)
            value = wanted.get(int(combo.property("item_id")))
            if value is not None:
                combo.setCurrentIndex(max(combo.findData(value), 0))

    def set_all_item_decisions(self, value: str) -> None:
        for r in range(self.item_table.rowCount()):
            combo = self.item_table.cellWidget(r, 0)
            combo.setCurrentIndex(max(combo.findData(value), 0))

    def after_save(self) -> None:
        if self.case_id is not None and self.detail is not None and self.detail.case.status not in ("APPROVED", "ARCHIVED"):
            try:
                self._learner().link_events(self.case_id)
            except (CaseError, OSError):
                pass

    # ── 첨부 ──
    def _browse_attachment(self, kind: str) -> None:
        exts = self.context.settings.import_.guide_extensions if kind == "DIAGNOSTIC_GUIDE" \
            else self.context.settings.import_.manual_extensions
        pattern = " ".join(f"*{e}" for e in exts)
        title = "진단 가이드 선택" if kind == "DIAGNOSTIC_GUIDE" else "대응 매뉴얼 선택"
        path, _ = QFileDialog.getOpenFileName(self, title, "", f"문서 ({pattern})")
        if path:
            self.attach_file(Path(path), kind)

    def attach_file(self, path: Path, kind: str) -> bool:
        if self.save() is None:
            return False
        try:
            outcome = self._learner().attach(self.case_id, path, kind)
        except (CaseError, ImportRejected, BinaryFileError, OSError) as exc:
            self.attach_status.setText(f"첨부하지 못했습니다: {exc}")
            return False
        self.load_case(self.case_id)
        label = "진단 가이드" if kind == "DIAGNOSTIC_GUIDE" else "대응 매뉴얼"
        if outcome.duplicate:
            message = f"이미 첨부된 {label}입니다."
        elif outcome.headings_found:
            message = (f"{label}를 첨부하고 항목 {outcome.items}개로 나눴습니다. 원문 그대로 나눈 항목은 '승인'으로 "
                       "두었습니다. 빼고 싶은 항목만 '거부'로 바꾸세요.")
        else:
            message = f"{label}에서 항목 제목을 찾지 못해 전체를 '참고'로 두었습니다. AI 분류나 직접 정리가 필요합니다."
        if outcome.linked_codes:
            message += f" 로그 오류 코드 {', '.join(outcome.linked_codes)}가 문서에 나옵니다."
        self.attach_status.setText(message)
        return True

    def _show_attachment(self, current: QListWidgetItem | None, _previous=None) -> None:
        if current is None:
            self.preview.clear()
            return
        self.preview.setPlainText(self._learner().attachment_text(current.data(Qt.ItemDataRole.UserRole)))

    def fill_from_guide(self) -> None:
        if self.case_id is None:
            return
        suggestion = self._learner().suggest_fields(self.case_id)
        if not suggestion:
            self.attach_status.setText("가이드에서 증상·판정 기준 항목을 찾지 못했습니다.")
            return
        if "symptom" in suggestion and not self.symptom_edit.toPlainText().strip():
            self.symptom_edit.setPlainText(suggestion["symptom"])
        if "abnormal_criteria" in suggestion and not self.criteria_edit.toPlainText().strip():
            self.criteria_edit.setPlainText(suggestion["abnormal_criteria"])
        self.attach_status.setText("가이드 원문으로 빈 칸을 채웠습니다. 확인하고 저장하세요.")

    def restructure_selected_with_ai(self) -> None:
        current = self.attachment_list.currentItem()
        if current is None or self.case_id is None or self.runner.busy:
            self.attach_status.setText("다시 분류할 문서를 고르세요.")
            return
        self.ai_structure_button.setEnabled(False)
        self.attach_status.setText("AI가 문서 조각에 항목 이름을 붙이는 중…(원문은 바꾸지 않음)")
        self.runner.start(self._learner().restructure_with_ai, self.case_id, current.data(Qt.ItemDataRole.UserRole),
                          on_done=self._on_ai_structured, on_error=self._on_ai_structure_error)

    def _on_ai_structured(self, result) -> None:
        self.ai_structure_button.setEnabled(True)
        self.load_case(self.case_id)
        if result.value is None:
            self.attach_status.setText(f"AI 분류 실패({result.status}): {'; '.join(result.errors)[:300]}")
        else:
            self.attach_status.setText(f"AI가 조각 {len(result.value.labels)}개를 분류했습니다. 결과를 확인하고 승인하세요.")

    def _on_ai_structure_error(self, message: str) -> None:
        self.ai_structure_button.setEnabled(True)
        self.attach_status.setText(f"AI 분류 실패: {message}")
