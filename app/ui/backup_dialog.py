"""백업 및 복원 (보조 메뉴)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app import __version__
from app.bootstrap import AppContext
from app.storage.backup import BackupError, create_backup, inspect_backup, restore_backup


class BackupDialog(QDialog):
    def __init__(self, context: AppContext, can_restore=lambda: True, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("백업 및 복원")
        self.setMinimumWidth(560)
        self.context = context
        self.can_restore = can_restore
        self.restored = False
        intro = QLabel(f"백업 위치: {context.paths.backups}\n"
                       "백업에는 DB, 원본 로그·가이드 사본, 분석 결과, 설정이 들어갑니다. "
                       "복원하면 지금 데이터는 지우지 않고 backups 폴더로 옮긴 뒤, 복원 직전 상태도 따로 백업합니다.")
        intro.setWordWrap(True)
        backup_button = QPushButton("지금 백업")
        backup_button.clicked.connect(self.backup_now)
        restore_button = QPushButton("백업에서 복원…")
        restore_button.clicked.connect(self.choose_restore)
        self.message = QLabel()
        self.message.setWordWrap(True)
        row = QHBoxLayout()
        row.addWidget(backup_button)
        row.addWidget(restore_button)
        row.addStretch(1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        close.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(row)
        layout.addWidget(self.message)
        layout.addWidget(close)

    def backup_now(self) -> Path | None:
        try:
            info = create_backup(self.context.paths, self.context.database, __version__)
        except (OSError, BackupError) as exc:
            self.message.setText(f"백업하지 못했습니다: {exc}")
            return None
        self.message.setText(f"백업했습니다: {info.path.name} (파일 {info.files}개, 스키마 v{info.schema_version})")
        return info.path

    def choose_restore(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "복원할 백업", str(self.context.paths.backups), "백업 (*.zip)")
        if not path:
            return
        if QMessageBox.question(self, "복원", "지금 데이터를 옆으로 옮기고 백업 내용으로 바꿉니다. 계속할까요?") \
                != QMessageBox.StandardButton.Yes:
            return
        self.restore_from(Path(path))

    def restore_from(self, path: Path) -> bool:
        if not self.can_restore():
            self.message.setText("진행 중인 작업이 있습니다. 작업이 끝난 뒤 복원하세요.")
            return False
        try:
            manifest = inspect_backup(path)
            safety = restore_backup(path, self.context.paths, self.context.database, __version__)
        except (OSError, BackupError) as exc:
            self.message.setText(f"복원하지 못했습니다: {exc}")
            return False
        self.restored = True
        self.message.setText(f"복원했습니다(백업 {manifest['created_at']}). 복원 직전 상태는 {safety.name}에 있습니다. "
                             "앱을 다시 시작하세요.")
        return True
