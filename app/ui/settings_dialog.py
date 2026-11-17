"""설정 대화상자 (보조 메뉴 "설정"). 검증은 app.config의 Pydantic 모델이 한다."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.config import MAX_FILE_MB_LIMIT, AppSettings, describe_validation_error, shipped_default_model
from app.ui.theme import set_role


class SettingsDialog(QDialog):
    def __init__(
        self,
        settings: AppSettings,
        *,
        default_data_root: Path,
        known_models: Sequence[str] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("설정")
        self.setMinimumWidth(560)
        self._base = settings
        self._result: AppSettings | None = None

        # Ollama
        self.base_url_edit = QLineEdit(settings.ollama.base_url)
        self.base_url_edit.setPlaceholderText("http://127.0.0.1:11434")
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.addItem("")
        bundled = shipped_default_model()
        # AI 서버가 꺼져 있으면 목록이 비므로, 휴대용 AI 배포본의 동봉 모델은 늘 고를 수 있게 둔다(CR-03)
        for name in dict.fromkeys([*known_models, *([bundled] if bundled else [])]):
            self.model_combo.addItem(name)
        self.model_combo.setCurrentText(settings.ollama.model or "")
        self.model_combo.lineEdit().setPlaceholderText("미지정 (AI 단계 건너뜀)")
        self.connect_timeout_spin = QDoubleSpinBox()
        self.connect_timeout_spin.setRange(0.5, 60.0)
        self.connect_timeout_spin.setSingleStep(0.5)
        self.connect_timeout_spin.setSuffix(" 초")
        self.connect_timeout_spin.setValue(settings.ollama.connect_timeout_s)
        self.request_timeout_spin = QSpinBox()
        self.request_timeout_spin.setRange(10, 3600)
        self.request_timeout_spin.setSuffix(" 초")
        self.request_timeout_spin.setValue(int(settings.ollama.request_timeout_s))

        ollama_box = QGroupBox("로컬 AI (Ollama)")
        ollama_form = QFormLayout(ollama_box)
        ollama_form.addRow("주소", self.base_url_edit)
        ollama_form.addRow("모델", self.model_combo)
        ollama_form.addRow("연결 확인 시간", self.connect_timeout_spin)
        ollama_form.addRow("생성 요청 시간", self.request_timeout_spin)
        ollama_form.addRow("", _note("이 PC 안의 주소(127.0.0.1, localhost)만 쓸 수 있습니다."))
        if not known_models:
            ollama_form.addRow("", _note("AI 서버에 연결되지 않아 설치된 모델 목록을 못 불러왔습니다. "
                                         "앱 폴더의 'AI와 함께 시작'으로 켠 뒤 다시 여세요."))

        # 가져오기 한도
        self.max_file_spin = QDoubleSpinBox()
        self.max_file_spin.setRange(0.1, float(MAX_FILE_MB_LIMIT))
        self.max_file_spin.setSuffix(" MB")
        self.max_file_spin.setValue(settings.import_.max_file_mb)
        self.max_total_spin = QDoubleSpinBox()
        self.max_total_spin.setRange(1.0, 100_000.0)
        self.max_total_spin.setSuffix(" MB")
        self.max_total_spin.setValue(settings.import_.max_total_mb)

        import_box = QGroupBox("로그 가져오기")
        import_form = QFormLayout(import_box)
        import_form.addRow("파일당 최대 크기", self.max_file_spin)
        import_form.addRow("한 번에 가져올 총 용량", self.max_total_spin)
        import_form.addRow("", _note(f"파일당 최대 크기는 {MAX_FILE_MB_LIMIT}MB를 넘을 수 없습니다(계획서 §2.1)."))

        # 데이터 폴더
        self.data_root_edit = QLineEdit(settings.data_root or "")
        self.data_root_edit.setPlaceholderText(f"기본값: {default_data_root}")
        browse = QPushButton("찾아보기…")
        browse.clicked.connect(self._browse_data_root)
        root_row = QHBoxLayout()
        root_row.addWidget(self.data_root_edit, 1)
        root_row.addWidget(browse)
        root_holder = QWidget()
        root_holder.setLayout(root_row)
        root_row.setContentsMargins(0, 0, 0, 0)

        data_box = QGroupBox("데이터 폴더")
        data_form = QFormLayout(data_box)
        data_form.addRow("위치", root_holder)
        data_form.addRow(
            "",
            _note("바꾸면 앱을 다시 시작한 뒤 새 폴더를 씁니다. 기존 데이터는 옮기지 않습니다. 비워 두면 기본 위치를 씁니다."),
        )

        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        set_role(self.error_label, "warning")
        self.error_label.hide()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(ollama_box)
        layout.addWidget(import_box)
        layout.addWidget(data_box)
        layout.addWidget(self.error_label)
        layout.addWidget(buttons)

    def build_settings(self) -> AppSettings:
        """입력값으로 새 설정을 만든다. 값이 틀리면 ValidationError."""
        data = self._base.to_json_dict()
        data["ollama"].update(
            base_url=self.base_url_edit.text(),
            model=self.model_combo.currentText(),
            connect_timeout_s=self.connect_timeout_spin.value(),
            request_timeout_s=float(self.request_timeout_spin.value()),
        )
        data["import"].update(
            max_file_mb=self.max_file_spin.value(),
            max_total_mb=self.max_total_spin.value(),
        )
        data["data_root"] = self.data_root_edit.text().strip() or None
        return AppSettings.model_validate(data)

    def accept(self) -> None:
        try:
            self._result = self.build_settings()
        except ValidationError as exc:
            self.error_label.setText("저장할 수 없습니다.\n" + describe_validation_error(exc))
            self.error_label.show()
            return
        super().accept()

    def result_settings(self) -> AppSettings | None:
        return self._result

    def _browse_data_root(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "데이터 폴더 선택", self.data_root_edit.text())
        if chosen:
            self.data_root_edit.setText(str(Path(chosen)))


def _note(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    set_role(label, "muted")
    return label
