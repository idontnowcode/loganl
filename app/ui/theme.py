"""화면 테마 — DESIGN.md "부드러운 표제란". 색·크기는 이 파일에만 둔다.

화면 코드는 역할 속성만 단다:
  QPushButton[variant="primary"|"danger"], QLabel[role="stamp"][tone=…], QLabel[role="hint"|"warning"|"muted"|"title"],
  QFrame[role="cell"] (표제란 칸), QToolButton[role="disclosure"] (접는 구역 제목 줄),
  QFrame[role="dropzone"][active] (끌어 놓기 칸, CR-10). 표 칸 글자색은 tone_color().
"""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QAbstractItemView, QApplication, QHeaderView, QListWidget, QScrollArea, QTableWidget, QWidget

T = {
    "canvas": "#F4F5F7",
    "surface": "#FFFFFF",
    "sunken": "#F7F8FA",
    "line": "#E1E4E8",
    "line_input": "#CDD3DA",
    "ink": "#1D2125",
    "ink2": "#545D68",
    "accent": "#1F5FAD",
    "accent_hover": "#184C8B",
    "accent_soft": "#E8F0FB",
    "accent_line": "#B9CFEC",
    "ok": "#1E7A4C", "ok_soft": "#E6F4EC", "ok_line": "#A9D7BD",
    "warn": "#8A5300", "warn_soft": "#FDF3E1", "warn_line": "#EBCB94",
    "bad": "#B3261E", "bad_soft": "#FCEBEA", "bad_line": "#EDB4AF",
    "neutral": "#4A525C", "neutral_soft": "#EEF0F2", "neutral_line": "#D3D8DE",
}
FONT_FAMILY = "Malgun Gothic" if sys.platform == "win32" else ""
MONO_FAMILY = "Consolas"
BASE_PT = 10.5  # 현장 노트북에서 읽기 쉬운 크기(14px). 칸 라벨·보조 글도 9.5pt 아래로 내리지 않는다

STATUS_TONE = {
    "DRAFT": "neutral", "NEEDS_INFORMATION": "warn", "READY_FOR_REVIEW": "info", "APPROVED": "ok",
    "REJECTED": "bad", "ARCHIVED": "neutral",
}


def stylesheet() -> str:
    t = T
    return f"""
QWidget {{ color: {t['ink']}; font-size: {BASE_PT}pt; }}
QMainWindow, QDialog {{ background: {t['canvas']}; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
QToolTip {{ background: {t['ink']}; color: #FFFFFF; border: none; padding: 6px 8px; }}

/* 메뉴 */
QMenuBar {{ background: {t['surface']}; border-bottom: 1px solid {t['line']}; padding: 2px 6px; }}
QMenuBar::item {{ padding: 6px 10px; border-radius: 6px; background: transparent; }}
QMenuBar::item:selected {{ background: {t['neutral_soft']}; }}
QMenu {{ background: {t['surface']}; border: 1px solid {t['line']}; padding: 6px; }}
QMenu::item {{ padding: 7px 28px 7px 14px; border-radius: 6px; }}
QMenu::item:selected {{ background: {t['accent_soft']}; color: {t['ink']}; }}
QMenu::item:disabled {{ color: #858D96; }}
QMenu::separator {{ height: 1px; background: {t['line']}; margin: 6px 8px; }}

/* 탭: 평평한 글자 탭, 선택은 강조색 글자와 아래 강조선 */
QTabWidget::pane {{ border: none; background: {t['canvas']}; }}
QTabBar {{ background: {t['surface']}; }}
QTabBar::tab {{ background: transparent; color: {t['ink2']}; padding: 10px 18px; margin: 0 2px;
                border: none; border-bottom: 2px solid transparent; font-weight: 600; }}
QTabBar::tab:hover {{ color: {t['ink']}; }}
QTabBar::tab:selected {{ color: {t['accent']}; border-bottom: 2px solid {t['accent']}; }}

/* 카드 */
QGroupBox {{ background: {t['surface']}; border: 1px solid {t['line']}; border-radius: 8px;
             margin-top: 10px; padding: 16px 12px 12px 12px; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 12px; top: 2px;
                    padding: 0 4px; color: {t['ink']}; background: transparent; }}

/* 버튼 */
QPushButton, QToolButton {{ background: {t['surface']}; border: 1px solid {t['line_input']}; border-radius: 6px;
                            padding: 6px 14px; min-height: 18px; }}
QPushButton:hover, QToolButton:hover {{ background: {t['sunken']}; border-color: #B7BFC8; }}
QPushButton:pressed, QToolButton:pressed {{ background: {t['neutral_soft']}; }}
QPushButton:disabled, QToolButton:disabled {{ color: #7D868F; background: {t['sunken']}; border-color: {t['line']}; }}
QPushButton:focus, QToolButton:focus {{ border: 1px solid {t['accent']}; }}
QPushButton[variant="primary"] {{ background: {t['accent']}; color: #FFFFFF; border: 1px solid {t['accent']};
                                  font-weight: 600; padding: 6px 18px; }}
QPushButton[variant="primary"]:hover {{ background: {t['accent_hover']}; border-color: {t['accent_hover']}; }}
QPushButton[variant="primary"]:disabled {{ background: #9FB6D6; border-color: #9FB6D6; color: #F4F7FB; }}
QPushButton[variant="danger"] {{ color: {t['bad']}; border: 1px solid {t['bad_line']}; background: {t['surface']}; }}
QPushButton[variant="danger"]:hover {{ background: {t['bad_soft']}; }}
QPushButton[variant="danger"]:disabled {{ color: #C9A6A3; border-color: {t['line']}; background: {t['sunken']}; }}
/* 접는 구역 제목 줄 (CR-05) */
QToolButton[role="disclosure"] {{ background: transparent; border: none; padding: 6px 4px; font-weight: 600;
                                  color: {t['ink2']}; }}
QToolButton[role="disclosure"]:hover {{ color: {t['ink']}; background: {t['neutral_soft']}; }}
QToolButton[role="disclosure"]:checked {{ color: {t['ink']}; }}

/* 입력 */
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {t['surface']}; border: 1px solid {t['line_input']}; border-radius: 6px; padding: 5px 8px;
    selection-background-color: {t['accent_soft']}; selection-color: {t['ink']}; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 1px solid {t['accent']}; }}
QLineEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled {{ background: {t['sunken']}; color: {t['ink2']}; }}
QPlainTextEdit[readOnly="true"] {{ background: {t['sunken']}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {t['surface']}; border: 1px solid {t['line']};
                               selection-background-color: {t['accent_soft']}; selection-color: {t['ink']}; }}
QCheckBox {{ spacing: 6px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border: 1px solid {t['line_input']}; border-radius: 4px;
                        background: {t['surface']}; }}
QCheckBox::indicator:checked {{ background: {t['accent']}; border-color: {t['accent']}; }}

/* 표·목록 */
QTableView, QTableWidget, QListWidget, QListView {{ background: {t['surface']}; border: 1px solid {t['line']};
    border-radius: 8px; gridline-color: #EDEFF2; alternate-background-color: #FAFBFC;
    selection-background-color: {t['accent_soft']}; selection-color: {t['ink']}; }}
QHeaderView::section {{ background: {t['sunken']}; color: {t['ink2']}; border: none;
                        border-bottom: 1px solid {t['line']}; padding: 7px 10px; font-weight: 600; }}
QTableCornerButton::section {{ background: {t['sunken']}; border: none; }}
QListWidget::item {{ padding: 7px 10px; border-bottom: 1px solid #F0F2F4; }}
QTableView::item {{ padding: 0 6px; }}
QListWidget::item:selected {{ background: {t['accent_soft']}; color: {t['ink']}; }}

/* 스크롤바 */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle {{ background: #C8CED5; border-radius: 4px; min-height: 24px; min-width: 24px; }}
QScrollBar::handle:hover {{ background: #A9B1BA; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; border: none; }}

/* 진행 막대·분할선·상태 표시줄 */
QProgressBar {{ background: {t['neutral_soft']}; border: none; border-radius: 4px; text-align: center;
                color: {t['ink']}; min-height: 16px; }}
QProgressBar::chunk {{ background: #A9C3E8; border-radius: 4px; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 8px; }}
QSplitter::handle:vertical {{ height: 8px; }}
QStatusBar {{ background: {t['surface']}; border-top: 1px solid {t['line']}; color: {t['ink2']}; }}
QStatusBar QLabel {{ color: {t['ink2']}; padding: 0 8px; }}

/* 역할 라벨 */
QLabel[role="title"] {{ font-size: 14pt; font-weight: 700; color: {t['ink']}; }}
QLabel[role="muted"] {{ color: {t['ink2']}; }}
QLabel[role="hint"] {{ background: {t['accent_soft']}; color: #173F73; border: 1px solid {t['accent_line']};
                       border-radius: 8px; padding: 9px 12px; }}
QLabel[role="warning"] {{ background: {t['warn_soft']}; color: #5C3A00; border: 1px solid {t['warn_line']};
                          border-radius: 8px; padding: 9px 12px; }}
QLabel[role="stamp"] {{ border-radius: 6px; padding: 3px 10px; font-weight: 700; }}
QLabel[role="stamp"][tone="ok"] {{ color: {t['ok']}; background: {t['ok_soft']}; border: 1px solid {t['ok_line']}; }}
QLabel[role="stamp"][tone="warn"] {{ color: {t['warn']}; background: {t['warn_soft']}; border: 1px solid {t['warn_line']}; }}
QLabel[role="stamp"][tone="bad"] {{ color: {t['bad']}; background: {t['bad_soft']}; border: 1px solid {t['bad_line']}; }}
QLabel[role="stamp"][tone="info"] {{ color: {t['accent']}; background: {t['accent_soft']}; border: 1px solid {t['accent_line']}; }}
QLabel[role="stamp"][tone="neutral"] {{ color: {t['neutral']}; background: {t['neutral_soft']}; border: 1px solid {t['neutral_line']}; }}

/* 끌어 놓기 칸 (CR-10 LI-11): 놓으면 바로 가져온다 */
QFrame[role="dropzone"] {{ background: {t['sunken']}; border: 1px dashed #AEB8C4; border-radius: 8px; }}
QFrame[role="dropzone"][active="true"] {{ background: {t['accent_soft']}; border: 1px dashed {t['accent']}; }}
QFrame[role="dropzone"] QLabel {{ background: transparent; color: {t['ink2']}; }}

/* 표제란 칸 */
QFrame[role="cell"] {{ background: {t['surface']}; border: 1px solid {t['line']}; border-radius: 8px; }}
QLabel[role="cell-label"] {{ color: {t['ink2']}; font-size: 9.5pt; font-weight: 600; background: transparent; }}
QLabel[role="cell-value"] {{ font-size: 11.5pt; font-weight: 700; background: transparent; }}
QLabel[role="cell-value"][tone="ok"] {{ color: {t['ok']}; }}
QLabel[role="cell-value"][tone="warn"] {{ color: {t['warn']}; }}
QLabel[role="cell-value"][tone="bad"] {{ color: {t['bad']}; }}
QLabel[role="cell-value"][tone="neutral"] {{ color: {t['neutral']}; }}
QLabel[role="cell-value"][tone="info"] {{ color: {t['accent']}; }}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")  # 플랫폼 기본 스타일은 QSS 일부를 무시한다
    if FONT_FAMILY:
        app.setFont(QFont(FONT_FAMILY, BASE_PT))
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(T["canvas"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(T["surface"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#FAFBFC"))
    palette.setColor(QPalette.ColorRole.Text, QColor(T["ink"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(T["ink"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(T["ink"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(T["surface"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(T["accent_soft"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(T["ink"]))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#66707B"))  # 흰 바탕 대비 4.9:1
    app.setPalette(palette)
    app.setStyleSheet(stylesheet())


def set_role(widget: QWidget, role: str | None = None, tone: str | None = None, variant: str | None = None) -> QWidget:
    """역할 속성을 달고 스타일을 다시 적용한다(속성 변경은 다시 칠해야 반영된다)."""
    if role is not None:
        widget.setProperty("role", role)
    if tone is not None:
        widget.setProperty("tone", tone)
    if variant is not None:
        widget.setProperty("variant", variant)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    return widget


def tone_color(tone: str) -> QColor:
    """표 칸 글자색(ok·warn·bad·info·neutral). 색만으로 말하지 않도록 칸에는 늘 글자가 함께 있다."""
    return QColor({"ok": T["ok"], "warn": T["warn"], "bad": T["bad"], "info": T["accent"]}.get(tone, T["neutral"]))


def mono_font(point: float = 10) -> QFont:
    return QFont(MONO_FAMILY, point)


def comfortable_table(view: QAbstractItemView, elide_middle: bool = False) -> QAbstractItemView:
    """표 줄 높이를 글자 크기에 맞춰 넉넉하게(위아래 여백 포함). 긴 파일 이름은 가운데를 줄인다."""
    row = max(34, view.fontMetrics().height() + 16)  # 칸 안의 선택 상자(승인·거부)도 들어가는 높이
    header = getattr(view, "verticalHeader", None)
    if header is not None:
        header().setDefaultSectionSize(row)
        header().setMinimumSectionSize(row)
    view.setWordWrap(False)
    if elide_middle:
        view.setTextElideMode(Qt.TextElideMode.ElideMiddle)
    return view


def scroll_wrap(widget: QWidget, horizontal: bool = False) -> QScrollArea:
    """작은 화면에서 표를 누르지 않고 아래로 내려 보게 하는 감싸개. horizontal이면 넘칠 때 가로 스크롤도 허용(잘림 대신)."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded if horizontal
                                      else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setFrameShape(QScrollArea.Shape.NoFrame)
    area.setWidget(widget)
    return area


def fit_list_height(view: QListWidget, minimum: int = 120, maximum: int = 560) -> None:
    """목록 높이를 내용에 맞춘다(안쪽 스크롤로 줄이 잘려 보이지 않게). 화면 전체가 스크롤된다."""
    rows = sum(max(view.sizeHintForRow(i), 0) for i in range(view.count()))
    total = rows + 2 * view.frameWidth() + 6
    view.setMinimumHeight(max(minimum, min(total, maximum)))


def fit_widget_column(table: QTableWidget, column: int = 0) -> None:
    """칸 안 위젯(승인·거부 선택 상자)이 잘리지 않게 그 열 너비를 위젯에 맞춘다."""
    widths = []
    for r in range(table.rowCount()):
        w = table.cellWidget(r, column)
        if w is not None:
            w.ensurePolished()  # 스타일이 입혀진 뒤의 크기로 잰다
            widths.append(max(w.sizeHint().width(), w.minimumSizeHint().width()))
    if widths:
        table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(column, max(widths) + 12)
