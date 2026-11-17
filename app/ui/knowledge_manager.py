"""승인된 지식 보기 (계획서 §10 knowledge_manager). 분석에 실제로 쓰이는 승인 패턴·승인 가이드·매뉴얼 항목만 보여 준다."""

from __future__ import annotations

from contextlib import closing

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QPlainTextEdit, QVBoxLayout, QWidget

from app.storage.database import Database
from app.storage.repositories import CaseRepository, GuideRepository, ModeRepository, PatternRepository


def approved_knowledge_text(database: Database) -> str:
    lines: list[str] = []
    with closing(database.connect()) as conn:
        modes = ModeRepository(conn).list()
        for mode in modes:
            patterns = PatternRepository(conn).approved_for_mode(mode.id)
            if patterns:
                lines.append(f"■ 운전 모드 '{mode.name}' 승인 패턴 {len(patterns)}개")
                lines += [f"  · [{p.pattern_type}] {p.describe()}" for p in patterns]
        abnormal = CaseRepository(conn).approved(case_type="ABNORMAL")
        items = GuideRepository(conn).approved_items_for_cases([c.id for c in abnormal])
    labels = {c.id: c.label for c in abnormal}
    if items:
        lines.append(f"■ 승인된 진단 가이드·대응 매뉴얼 항목 {len(items)}개")
        for row in items:
            kind = "매뉴얼" if row["attachment_kind"] == "RESPONSE_MANUAL" else "가이드"
            lines.append(f"  · {labels[row['case_id']]} [{kind} · {row['section']}] {row['content'][:120]}")
    return "\n".join(lines) or "승인된 지식이 아직 없습니다."


class KnowledgeDialog(QDialog):
    def __init__(self, database: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("승인된 지식")
        self.resize(820, 560)
        view = QPlainTextEdit(approved_knowledge_text(database))
        view.setReadOnly(True)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        close.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(view)
        layout.addWidget(close)
