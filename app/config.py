"""설정 관리.

- 설정 파일은 항상 부트스트랩 폴더(`%LOCALAPPDATA%\\LocalLogAnalyzer\\config\\settings.json`)에 둔다.
  그 안의 `data_root`가 나머지 데이터 폴더 위치를 정한다 (REQUIREMENTS D-1).
- 값의 우선순위: 사용자 설정 파일 > 배포 기본값(`configs/settings.json`) > 코드 기본값 (D-2).
- 테스트와 개발용으로 `LLA_HOME` 환경 변수가 부트스트랩 폴더를 바꾼다.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app import APP_DIR_NAME
from app.security import require_loopback_url

ENV_HOME = "LLA_HOME"
SETTINGS_VERSION = 1
SHIPPED_DEFAULTS_FILE = Path(__file__).resolve().parent.parent / "configs" / "settings.json"
MAX_FILE_MB_LIMIT = 10  # 계획서 §2.1 "개별 파일 10MB 이하"


class _Section(BaseModel):
    model_config = ConfigDict(extra="ignore")


class OllamaSettings(_Section):
    base_url: str = "http://127.0.0.1:11434"
    model: str | None = None  # 사내 PC에서 평가 후 확정 (§2.2). 미지정이면 AI 단계를 건너뛴다.
    connect_timeout_s: float = Field(default=3.0, gt=0, le=60)
    request_timeout_s: float = Field(default=300.0, gt=0, le=3600)

    @field_validator("base_url")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        return require_loopback_url(value)

    @field_validator("model")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class ImportSettings(_Section):
    max_file_mb: float = Field(default=10, gt=0, le=MAX_FILE_MB_LIMIT)
    max_total_mb: float = Field(default=100, gt=0, le=100_000)
    log_extensions: list[str] = Field(default_factory=lambda: [".txt", ".log"])
    guide_extensions: list[str] = Field(default_factory=lambda: [".txt", ".md"])
    # 대응 매뉴얼(선택, CR-01). 형식이 정해지기 전까지 가이드와 같은 TXT/MD (REQUIREMENTS Q5)
    manual_extensions: list[str] = Field(default_factory=lambda: [".txt", ".md"])

    @field_validator("log_extensions", "guide_extensions", "manual_extensions")
    @classmethod
    def _normalize_extensions(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in values:
            ext = raw.strip().lower()
            if not ext:
                continue
            if not ext.startswith("."):
                ext = "." + ext
            if ext not in normalized:
                normalized.append(ext)
        if not normalized:
            raise ValueError("확장자를 하나 이상 지정해야 합니다")
        return normalized

    @model_validator(mode="after")
    def _total_covers_one_file(self) -> "ImportSettings":
        if self.max_total_mb < self.max_file_mb:
            raise ValueError("총 입력 한도는 파일당 한도보다 작을 수 없습니다")
        return self


class UiSettings(_Section):
    language: Literal["ko"] = "ko"


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    settings_version: int = SETTINGS_VERSION
    data_root: str | None = None  # None이면 부트스트랩 폴더를 그대로 쓴다
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    import_: ImportSettings = Field(default_factory=ImportSettings, alias="import")
    ui: UiSettings = Field(default_factory=UiSettings)

    @field_validator("data_root")
    @classmethod
    def _absolute_or_none(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        expanded = os.path.expandvars(os.path.expanduser(value.strip()))
        if not Path(expanded).is_absolute():
            raise ValueError("데이터 폴더는 절대 경로여야 합니다 (예: D:\\LocalLogAnalyzerData)")
        return str(Path(expanded))

    def to_json_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


@dataclass
class LoadedSettings:
    settings: AppSettings
    warnings: list[str] = field(default_factory=list)
    created: bool = False


def bootstrap_dir() -> Path:
    """설정 파일이 사는 폴더. `LLA_HOME`이 있으면 그것, 없으면 `%LOCALAPPDATA%\\LocalLogAnalyzer`."""
    override = os.environ.get(ENV_HOME, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    base = os.environ.get("LOCALAPPDATA", "").strip() or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_DIR_NAME


def settings_file_for(bootstrap: Path) -> Path:
    return bootstrap / "config" / "settings.json"


def resolve_data_root(settings: AppSettings, bootstrap: Path) -> Path:
    return Path(settings.data_root) if settings.data_root else bootstrap


def load_settings(settings_file: Path, defaults_file: Path = SHIPPED_DEFAULTS_FILE) -> LoadedSettings:
    """설정을 읽는다. 파일이 없으면 기본값으로 만들고, 깨져 있으면 보관한 뒤 기본값으로 시작한다."""
    warnings: list[str] = []
    shipped = _read_shipped_defaults(defaults_file, warnings)

    if not settings_file.exists():
        settings = _validated_defaults(shipped, warnings)
        save_settings(settings, settings_file)
        return LoadedSettings(settings, warnings, created=True)

    try:
        user = json.loads(settings_file.read_text(encoding="utf-8-sig"))
        if not isinstance(user, dict):
            raise ValueError("설정 파일의 최상위가 JSON 객체가 아닙니다")
        settings = AppSettings.model_validate(_deep_merge(shipped, user))
    except (OSError, ValueError) as exc:  # json.JSONDecodeError, ValidationError 모두 ValueError
        backup = _backup_invalid(settings_file)
        reason = describe_validation_error(exc) if isinstance(exc, ValidationError) else str(exc)
        warnings.append(
            f"설정 파일을 읽지 못해 기본값으로 시작했습니다. 원래 파일은 {backup.name}(으)로 보관했습니다. 사유: {reason}"
        )
        settings = _validated_defaults(shipped, warnings)
        save_settings(settings, settings_file)
        return LoadedSettings(settings, warnings, created=False)

    shipped_model = _shipped_model(shipped)
    if not settings.ollama.model and shipped_model:
        # 휴대용 AI를 동봉한 배포본(CR-03)인데, 그 PC에 예전 설정 파일(모델 미지정)이 남아 있던 경우.
        # 미지정은 "아직 고르지 않음"으로 보고 동봉한 모델을 쓴다. 설정 창에서 다른 모델로 바꿀 수 있다.
        settings = settings.model_copy(update={"ollama": settings.ollama.model_copy(update={"model": shipped_model})})
        save_settings(settings, settings_file)

    if settings.settings_version > SETTINGS_VERSION:
        warnings.append("설정 파일이 이 앱보다 새 버전에서 만들어졌습니다. 이 버전이 모르는 항목은 무시합니다.")
    return LoadedSettings(settings, warnings, created=False)


def _shipped_model(shipped: dict[str, Any]) -> str | None:
    ollama = shipped.get("ollama")
    model = ollama.get("model") if isinstance(ollama, dict) else None
    return model.strip() if isinstance(model, str) and model.strip() else None


def shipped_default_model(defaults_file: Path = SHIPPED_DEFAULTS_FILE) -> str | None:
    """배포 기본 설정에 들어 있는 모델(휴대용 AI 동봉 시 그 모델). 없으면 None."""
    return _shipped_model(_read_shipped_defaults(defaults_file, []))


def save_settings(settings: AppSettings, settings_file: Path) -> None:
    """임시 파일에 쓴 뒤 교체한다. 쓰는 도중 꺼져도 이전 파일이 남는다."""
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(settings.to_json_dict(), ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=settings_file.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, settings_file)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


FIELD_LABELS = {
    "data_root": "데이터 폴더",
    "ollama.base_url": "Ollama 주소",
    "ollama.model": "모델",
    "ollama.connect_timeout_s": "연결 확인 시간",
    "ollama.request_timeout_s": "생성 요청 시간",
    "import": "가져오기",
    "import.max_file_mb": "파일당 최대 크기",
    "import.max_total_mb": "총 입력 한도",
    "import.log_extensions": "로그 확장자",
    "import.guide_extensions": "가이드 확장자",
    "import.manual_extensions": "대응 매뉴얼 확장자",
}


def describe_validation_error(exc: ValidationError) -> str:
    """Pydantic 검증 오류를 화면에 보여 줄 한국어 문장으로 바꾼다."""
    lines: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", ()))
        label = FIELD_LABELS.get(loc, loc or "설정")
        lines.append(f"{label}: {_korean_message(err)}")
    return "\n".join(lines)


def _number(value: Any) -> str:
    """10.0 → "10", 0.5 → "0.5"."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _korean_message(err: dict[str, Any]) -> str:
    kind = err.get("type", "")
    ctx = err.get("ctx") or {}
    if kind == "value_error":
        return str(ctx.get("error", err.get("msg", "")))
    if kind == "less_than_equal":
        return f"{_number(ctx.get('le'))} 이하여야 합니다"
    if kind == "less_than":
        return f"{_number(ctx.get('lt'))}보다 작아야 합니다"
    if kind == "greater_than":
        return f"{_number(ctx.get('gt'))}보다 커야 합니다"
    if kind == "greater_than_equal":
        return f"{_number(ctx.get('ge'))} 이상이어야 합니다"
    if kind in {"float_parsing", "int_parsing", "float_type", "int_type"}:
        return "숫자여야 합니다"
    if kind in {"string_type", "list_type", "dict_type", "model_type"}:
        return "형식이 맞지 않습니다"
    if kind == "literal_error":
        return f"허용되지 않는 값입니다 ({ctx.get('expected', '')})"
    return str(err.get("msg", "올바르지 않은 값입니다"))


def _read_shipped_defaults(defaults_file: Path, warnings: list[str]) -> dict[str, Any]:
    if not defaults_file.exists():
        return {}
    try:
        data = json.loads(defaults_file.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("최상위가 JSON 객체가 아닙니다")
        AppSettings.model_validate(data)
        return data
    except (OSError, ValueError) as exc:
        warnings.append(f"배포 기본 설정({defaults_file.name})이 잘못돼 내장 기본값을 씁니다: {exc}")
        return {}


def _validated_defaults(shipped: dict[str, Any], warnings: list[str]) -> AppSettings:
    try:
        return AppSettings.model_validate(shipped)
    except ValidationError as exc:  # _read_shipped_defaults에서 이미 걸러지지만 방어적으로
        warnings.append(f"배포 기본 설정이 잘못돼 내장 기본값을 씁니다: {describe_validation_error(exc)}")
        return AppSettings()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _backup_invalid(settings_file: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = settings_file.with_name(f"{settings_file.stem}.invalid-{stamp}{settings_file.suffix}")
    counter = 1
    while backup.exists():
        backup = settings_file.with_name(f"{settings_file.stem}.invalid-{stamp}-{counter}{settings_file.suffix}")
        counter += 1
    os.replace(settings_file, backup)
    return backup
