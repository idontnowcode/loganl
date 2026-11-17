"""메인 화면 틀 (계획서 §8.1, 변경 요청 CR-06).

상단 탭은 '로그 입력'(정상·비정상 학습을 하나로, CR-06)과 '신규 로그 분석' 두 개, 보조 메뉴, 상태 표시줄
(데이터 폴더·DB·Ollama). 예전 정상·비정상 학습 화면은 '자세히 편집' 창으로 연다.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app import APP_NAME, __version__
from app.ai.ollama_client import OllamaClient, OllamaStatus
from app.bootstrap import AppContext
from app.config import AppSettings, OllamaSettings
from app.models.enums import OriginPurpose
from app.ui.analysis_history import AnalysisHistoryDialog
from app.ui.backup_dialog import BackupDialog
from app.ui.case_detail_dialog import CaseDetailDialog
from app.ui.case_review import CaseReviewDialog
from app.ui.knowledge_manager import KnowledgeDialog
from app.ui.log_analysis import LogAnalysisPage
from app.ui.log_entry import LogEntryPage
from app.ui.log_import_panel import LogImportPanel
from app.ui.mode_dialog import ModeDialog
from app.ui.reference_manager import ReferenceManagerDialog
from app.ui.settings_dialog import SettingsDialog
from app.ui.theme import set_role
from app.ui.workers import FunctionWorker, WorkerSignals

logger = logging.getLogger("app.ui")

StatusChecker = Callable[[OllamaSettings], OllamaStatus]
APP_ROOT = Path(__file__).resolve().parents[2]  # 개발: 프로젝트 폴더, 실행 파일: _internal
GUIDE_FILE = APP_ROOT / "docs" / "USER_GUIDE.html"
EXAMPLES_DIR = APP_ROOT / "examples"
AI_LAUNCHER_NAME = "AI와 함께 시작.cmd"  # packaging/bundle_ollama.py가 실행 파일 옆에 둔다 (CR-03)


def bundled_ai_launcher() -> Path | None:
    """실행 파일 옆에 휴대용 AI 시작 파일이 있으면 그 경로. 개발 실행에서는 없다."""
    if not getattr(sys, "frozen", False):
        return None
    path = Path(sys.executable).resolve().parent / AI_LAUNCHER_NAME
    return path if path.is_file() else None


@dataclass(frozen=True)
class PlannedScreen:
    title: str
    phase: str
    items: tuple[str, ...]


PLANNED_SCREENS: tuple[PlannedScreen, ...] = (
    PlannedScreen(
        "로그 입력",
        "CR-06",
        (
            "복수 로그 입력", "로그 미리보기", "이벤트 타임라인", "파일마다 정상/비정상", "동작 메모",
            "에러로 판단한 근거(비정상)", "저장 = 학습", "저장된 로그 목록", "자세히 편집(패턴·가이드·질문)",
        ),
    ),
    PlannedScreen(
        "신규 로그 분석",
        "Phase 6~8",
        (
            "권장 대응 (결과 맨 위, 승인된 매뉴얼·가이드 근거)", "복수 로그 입력", "운전 모드 확인", "원본 로그 뷰어",
            "이벤트 타임라인", "오류 및 이상 목록", "정상 사례 비교", "비정상 유사 사례", "AI 원인 가설",
            "증거 및 진단 가이드", "보고서 저장", "학습 사례 등록",
        ),
    ),
)

# (이름, 구현 예정 Phase). None이면 지금 동작한다.
AUX_MENU: tuple[tuple[str, str | None], ...] = (
    ("학습 사례 관리", None),
    ("분석 이력", None),
    ("참고 자료 관리", None),
    ("운전 모드 관리", None),
    ("설정", None),
    ("백업 및 복원", None),
)


def check_ollama(settings: OllamaSettings) -> OllamaStatus:
    with OllamaClient(
        settings.base_url,
        connect_timeout_s=settings.connect_timeout_s,
        request_timeout_s=settings.request_timeout_s,
    ) as client:
        return client.check_status(settings.model)


class PlaceholderPage(QWidget):
    """아직 구현하지 않은 화면 구성을 보여 준다. panel이 있으면 그 위에 둔다(Phase 2: 로그 입력 패널)."""

    def __init__(self, screen: PlannedScreen, panel: QWidget | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName(f"page:{screen.title}")
        self.panel = panel
        intro = QLabel(
            f"아래 화면 구성은 {screen.phase}에서 구현합니다: " + " · ".join(screen.items)
        )
        intro.setWordWrap(True)
        intro.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        set_role(intro, "muted")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        if panel is not None:
            layout.addWidget(panel, 1)
            layout.addWidget(intro)
        else:
            title = QLabel(screen.title)
            set_role(title, "title")
            body = QWidget()
            body_layout = QVBoxLayout(body)
            body_layout.addWidget(title)
            body_layout.addWidget(intro)
            body_layout.addStretch(1)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            scroll.setWidget(body)
            layout.addWidget(scroll)


class MainWindow(QMainWindow):
    def __init__(
        self,
        context: AppContext,
        *,
        status_checker: StatusChecker | None = None,
        client_factory: Callable[[], OllamaClient] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.context = context
        self.client_factory = client_factory
        self._status_checker: StatusChecker = status_checker or check_ollama
        self._pool = QThreadPool(self)
        self._pending_signals: set[WorkerSignals] = set()
        self._status_in_flight = False
        self.last_status: OllamaStatus | None = None

        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1200, 800)

        self.warning_banner = QLabel()
        self.warning_banner.setWordWrap(True)
        set_role(self.warning_banner, "warning")
        self.warning_banner.setVisible(bool(context.warnings))
        self.warning_banner.setText("\n".join(f"주의: {w}" for w in context.warnings))

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.import_panels: list[LogImportPanel] = []
        self.entry_page = LogEntryPage(context, client_factory, open_detail=self.open_case)
        self.import_panels.append(self.entry_page.import_panel)
        self.tabs.addTab(self.entry_page, PLANNED_SCREENS[0].title)
        self.analysis_page = LogAnalysisPage(context, client_factory)
        self.analysis_page.register_requested.connect(self.register_case_from_analysis)
        self.import_panels.append(self.analysis_page.import_panel)
        self.tabs.addTab(self.analysis_page, PLANNED_SCREENS[1].title)
        self.tabs.currentChanged.connect(lambda _i: self.flush_autosave())

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        self.hint_banner = QLabel()
        self.hint_banner.setWordWrap(True)
        set_role(self.hint_banner, "hint")
        banners = QWidget()
        banners_layout = QVBoxLayout(banners)
        banners_layout.setContentsMargins(12, 8, 12, 4)
        banners_layout.setSpacing(6)
        banners_layout.addWidget(self.warning_banner)
        banners_layout.addWidget(self.hint_banner)
        central_layout.addWidget(banners)
        self.banners = banners
        central_layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        self.menu_actions: dict[str, QAction] = {}
        self._build_menus()
        self._build_status_bar()
        self.refresh_hint()
        self.entry_page.entry_saved.connect(lambda _id: self.refresh_hint())
        self.refresh_ollama_status()

    # ── 메뉴 ──
    def _build_menus(self) -> None:
        manage = self.menuBar().addMenu("관리(&M)")
        for name, phase in AUX_MENU:
            if name == "설정":
                manage.addSeparator()
            action = QAction(name if phase is None else f"{name}  ({phase} 예정)", self)
            action.setObjectName(name)
            if phase is not None:
                action.setEnabled(False)
                action.setStatusTip(f"{name}은(는) {phase}에서 구현합니다.")
            manage.addAction(action)
            self.menu_actions[name] = action
        self.menu_actions["설정"].triggered.connect(self.open_settings)
        self.menu_actions["운전 모드 관리"].triggered.connect(self.open_modes)
        self.menu_actions["참고 자료 관리"].triggered.connect(self.open_references)
        self.menu_actions["학습 사례 관리"].triggered.connect(self.open_case_review)
        self.menu_actions["분석 이력"].triggered.connect(self.open_history)
        self.menu_actions["백업 및 복원"].triggered.connect(self.open_backup)
        knowledge = QAction("승인된 지식 보기", self)
        knowledge.triggered.connect(lambda: KnowledgeDialog(self.context.database, self).exec())
        manage.insertAction(self.menu_actions["설정"], knowledge)
        self.menu_actions["승인된 지식 보기"] = knowledge

        help_menu = self.menuBar().addMenu("도움말(&H)")
        guide = QAction("사용 가이드", self)
        guide.setShortcut("F1")
        guide.triggered.connect(self.open_guide)
        help_menu.addAction(guide)
        examples = QAction("연습용 예제 폴더 열기", self)
        examples.triggered.connect(self.open_examples)
        help_menu.addAction(examples)
        help_menu.addSeparator()
        self.menu_actions["사용 가이드"] = guide
        self.menu_actions["연습용 예제 폴더 열기"] = examples
        about = QAction("정보", self)
        about.triggered.connect(self.show_about)
        help_menu.addAction(about)
        open_folder = QAction("데이터 폴더 열기", self)
        open_folder.triggered.connect(self.open_data_folder)
        help_menu.addAction(open_folder)
        self.menu_actions["정보"] = about
        self.menu_actions["데이터 폴더 열기"] = open_folder

    # ── 상태 표시줄 ──
    def _build_status_bar(self) -> None:
        bar = self.statusBar()
        self.data_label = QLabel(f"데이터: {self.context.paths.root}")
        self.data_label.setToolTip(str(self.context.paths.root))
        caps = self.context.capabilities
        fts = "FTS5 사용 가능" if caps.fts5 else "FTS5 없음"
        self.db_label = QLabel(f"DB 스키마 v{self.context.schema_version} · SQLite {caps.version} · {fts}")
        self.ollama_label = QLabel("Ollama: 확인 전")
        self.recheck_button = QToolButton()
        self.recheck_button.setText("다시 확인")
        self.recheck_button.clicked.connect(self.refresh_ollama_status)
        bar.addWidget(self.data_label, 1)
        bar.addPermanentWidget(self.db_label)
        bar.addPermanentWidget(self.ollama_label)
        bar.addPermanentWidget(self.recheck_button)

    def refresh_ollama_status(self) -> None:
        """Ollama 상태를 백그라운드에서 확인한다. 화면은 기다리지 않는다."""
        if self._status_in_flight:
            return
        self._status_in_flight = True
        self.ollama_label.setText("Ollama: 확인 중…")
        self.ollama_label.setToolTip("")
        self.recheck_button.setEnabled(False)
        snapshot = self.context.settings.ollama.model_copy(deep=True)
        worker = FunctionWorker(self._status_checker, snapshot)
        signals = worker.signals
        self._pending_signals.add(signals)
        signals.finished.connect(lambda status, s=signals: self._on_status(s, status))
        signals.failed.connect(lambda message, s=signals: self._on_status_failed(s, message))
        self._pool.start(worker)

    def _on_status(self, signals: WorkerSignals, status: object) -> None:
        self._pending_signals.discard(signals)
        self._status_in_flight = False
        self.recheck_button.setEnabled(True)
        if not isinstance(status, OllamaStatus):
            self._on_status_failed(signals, "상태 확인 결과 형식이 올바르지 않습니다.")
            return
        self.last_status = status
        text, tip = status.summary(), status.details()
        launcher = None if status.reachable else bundled_ai_launcher()
        if launcher is not None:
            text += f" · 앱 폴더의 '{launcher.stem}'으로 켜면 AI를 쓸 수 있습니다"
            tip += "\n\n휴대용 AI 서버: " + str(launcher)
        self.ollama_label.setText(text)
        self.ollama_label.setToolTip(tip)

    def _on_status_failed(self, signals: WorkerSignals, message: str) -> None:
        self._pending_signals.discard(signals)
        self._status_in_flight = False
        self.recheck_button.setEnabled(True)
        self.ollama_label.setText("Ollama: 확인 실패")
        self.ollama_label.setToolTip(message)

    # ── 사례·이력·백업 ──
    def open_case(self, case_type: str, case_id: int | None) -> CaseDetailDialog:
        """사례 자세히 편집 창(예전 학습 화면, CR-06 LI-07)."""
        dialog = CaseDetailDialog(self.context, case_type, case_id, self.client_factory, self)
        self.detail_dialog = dialog  # 시험에서 열린 창을 찾을 수 있게
        dialog.finished.connect(lambda _r: self._after_detail())
        dialog.open()
        return dialog

    def _after_detail(self) -> None:
        self.entry_page.refresh_entries()
        self.entry_page.refresh_saved()
        self.refresh_hint()

    def register_case_from_analysis(self, case_type: str, log_file_ids: list, title: str) -> None:
        """분석한 로그를 로그 입력 탭에 올리고 구분을 미리 골라 둔다(저장은 사용자가, CR-06 LI-08)."""
        self.entry_page.receive(list(log_file_ids), case_type)
        self.tabs.setCurrentWidget(self.entry_page)
        kind = "정상" if case_type == "NORMAL" else "비정상"
        self.statusBar().showMessage(f"분석한 로그를 로그 입력 탭에 '{kind}'으로 올렸습니다. 메모를 적고 저장하세요.", 10_000)

    def open_case_review(self) -> None:
        CaseReviewDialog(self.context, self.open_case, self).exec()
        self._after_detail()

    def open_history(self) -> None:
        AnalysisHistoryDialog(self.context, self).exec()

    def busy(self) -> bool:
        return any(p.runner.busy for p in self.import_panels) or any(p.runner.busy for p in self.case_pages())

    def open_backup(self) -> None:
        dialog = BackupDialog(self.context, can_restore=lambda: not self.busy(), parent=self)
        dialog.exec()
        if dialog.restored:
            QMessageBox.information(self, "복원", "복원했습니다. 앱을 닫고 다시 시작하세요.")

    def open_references(self) -> ReferenceManagerDialog:
        """참고 자료 관리 (CR-04): 오류 코드 목록·설명 문서를 올리고 승인한다."""
        dialog = ReferenceManagerDialog(self.context, self)
        self.reference_dialog = dialog  # 시험에서 열린 창을 찾을 수 있게
        dialog.open()
        return dialog

    # ── 운전 모드 ──
    def open_modes(self) -> None:
        ModeDialog(self.context.database, self).exec()
        self.refresh_mode_lists()
        self.refresh_hint()

    def refresh_mode_lists(self) -> None:
        for page in self.case_pages():
            page.refresh_modes()

    def case_pages(self) -> list:
        return [w for w in (self.tabs.widget(i) for i in range(self.tabs.count())) if hasattr(w, "refresh_modes")]

    # ── 설정 ──
    def open_settings(self) -> None:
        dialog = SettingsDialog(
            self.context.settings,
            default_data_root=self.context.bootstrap_dir,
            known_models=self.last_status.models if self.last_status else (),
            parent=self,
        )
        if dialog.exec() == SettingsDialog.DialogCode.Accepted and dialog.result_settings() is not None:
            self.apply_settings(dialog.result_settings())

    def apply_settings(self, new_settings: AppSettings) -> list[str]:
        notes = self.context.update_settings(new_settings)
        self.statusBar().showMessage(" ".join(notes) if notes else "설정을 저장했습니다.", 10_000)
        self.refresh_ollama_status()
        self.refresh_hint()
        return notes

    # ── 도움말 ──
    def about_text(self) -> str:
        ctx = self.context
        caps = ctx.capabilities
        lines: Sequence[str] = (
            f"{APP_NAME} {__version__}",
            "기준 문서: LLA-DEV-PLAN-001 v0.2 R01 (+ 변경 요청 CR-01)",
            "",
            f"데이터 폴더: {ctx.paths.root}",
            f"설정 파일: {ctx.settings_file}",
            f"DB 파일: {ctx.paths.db_file} (스키마 v{ctx.schema_version})",
            f"SQLite {caps.version} · JSON {'사용 가능' if caps.json1 else '없음'} · FTS5 {'사용 가능' if caps.fts5 else '없음'}",
            "",
            "인터넷과 외부 AI 서비스를 쓰지 않습니다. AI는 이 PC의 Ollama만 씁니다.",
        )
        return "\n".join(lines)

    def show_about(self) -> None:
        QMessageBox.about(self, f"{APP_NAME} 정보", self.about_text())

    def open_data_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.context.paths.root)))

    def open_guide(self) -> bool:
        """앱과 함께 배포된 사용 가이드(HTML, 인터넷 없이 열림)를 기본 브라우저로 연다."""
        if not GUIDE_FILE.exists():
            QMessageBox.information(self, "사용 가이드", f"사용 가이드 파일을 찾지 못했습니다: {GUIDE_FILE}")
            return False
        return QDesktopServices.openUrl(QUrl.fromLocalFile(str(GUIDE_FILE)))

    def open_examples(self) -> bool:
        if not EXAMPLES_DIR.exists():
            QMessageBox.information(self, "연습용 예제", f"예제 폴더를 찾지 못했습니다: {EXAMPLES_DIR}")
            return False
        return QDesktopServices.openUrl(QUrl.fromLocalFile(str(EXAMPLES_DIR)))

    def first_run_hint(self) -> str:
        """처음 쓰는 사람에게 보여 줄 안내. 할 일이 없으면 빈 문자열."""
        from app.learning.case_manager import CaseManager

        steps = []
        if not any(c.status == "APPROVED" for c in CaseManager(self.context.database).list()):
            steps.append("'로그 입력' 탭에서 정상 로그를 하나 이상 저장해야 신규 로그를 비교할 수 있습니다")
        if not self.context.settings.ollama.model:
            steps.append("'관리 → 설정'에서 사용할 로컬 AI 모델을 고르세요(없어도 AI 외 기능은 동작)")
        if not steps:
            return ""
        return "처음 사용 안내: " + " · ".join(f"{i}) {s}" for i, s in enumerate(steps, start=1)) + \
            ". 자세한 방법은 '도움말 → 사용 가이드'(F1), 연습 파일은 '도움말 → 연습용 예제 폴더'에 있습니다."

    def refresh_hint(self) -> None:
        hint = self.first_run_hint()
        self.hint_banner.setText(hint)
        self.hint_banner.setVisible(bool(hint))
        self.banners.setVisible(bool(hint) or not self.warning_banner.isHidden())

    # ── 종료 ──
    def flush_autosave(self) -> None:
        self.entry_page.autosave()

    def shutdown(self) -> None:
        """입력 중이던 내용을 자동 저장하고, 진행 중인 백그라운드 작업을 취소하고 끝나기를 잠깐 기다린다."""
        self.flush_autosave()
        for panel in self.import_panels:
            panel.cancel_import()
            panel.runner.wait(10_000)
        for page in self.case_pages():
            page.runner.wait(10_000)
        timeout_ms = int((self.context.settings.ollama.connect_timeout_s + 6) * 1000)
        self._pool.waitForDone(timeout_ms)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.shutdown()
        super().closeEvent(event)
