"""'자세히 편집' 창 (변경 요청 CR-06 LI-07). 예전 정상·비정상 학습 화면을 그대로 띄운다.

패턴 확정(→ 승인된 규칙 위반 판정), 운전 모드, 진단 가이드·대응 매뉴얼 첨부, 확인 질문은 여기서 한다.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QVBoxLayout, QWidget

from app.ai.ollama_client import OllamaClient
from app.bootstrap import AppContext
from app.ui.abnormal_learning import AbnormalLearningPage
from app.ui.normal_learning import NormalLearningPage


class CaseDetailDialog(QDialog):
    def __init__(self, context: AppContext, case_type: str, case_id: int | None,
                 client_factory: Callable[[], OllamaClient] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        page_class = AbnormalLearningPage if case_type == "ABNORMAL" else NormalLearningPage
        self.page = page_class(context, client_factory)
        self.page.refresh_cases()
        if case_id is not None:
            self.page.load_case(case_id)
        kind = "비정상" if case_type == "ABNORMAL" else "정상"
        self.setWindowTitle(f"{kind} 사례 자세히 편집")
        self.resize(1360, 860)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.page, 1)
        layout.addWidget(buttons)

    def done(self, result: int) -> None:  # noqa: D401 (Qt 이름 그대로)
        self.page.autosave()
        self.page.runner.wait(10_000)
        super().done(result)
