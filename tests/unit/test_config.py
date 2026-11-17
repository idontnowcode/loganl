"""설정 관리: 기본값, 저장·읽기, 깨진 파일, 범위 검사, loopback, 사용자 지정 경로 (STO-11, SEC-01, ENV-03·04·06)."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import (
    SHIPPED_DEFAULTS_FILE,
    AppSettings,
    describe_validation_error,
    load_settings,
    resolve_data_root,
    save_settings,
)


def test_defaults_follow_plan() -> None:
    s = AppSettings()
    assert s.ollama.base_url == "http://127.0.0.1:11434"
    assert s.ollama.model is None
    assert s.import_.max_file_mb == 10
    assert s.import_.max_total_mb == 100
    assert s.import_.log_extensions == [".txt", ".log"]
    assert s.import_.guide_extensions == [".txt", ".md"]
    assert s.import_.manual_extensions == [".txt", ".md"]  # CR-01
    assert s.data_root is None


def test_shipped_defaults_file_is_valid_and_matches_code_defaults() -> None:
    data = json.loads(SHIPPED_DEFAULTS_FILE.read_text(encoding="utf-8"))
    assert AppSettings.model_validate(data) == AppSettings()


def test_first_load_creates_file(tmp_path: Path) -> None:
    settings_file = tmp_path / "config" / "settings.json"
    loaded = load_settings(settings_file)
    assert loaded.created is True
    assert loaded.warnings == []
    assert settings_file.exists()
    assert json.loads(settings_file.read_text(encoding="utf-8"))["import"]["max_file_mb"] == 10


def test_save_and_reload_roundtrip(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    s = AppSettings.model_validate({"ollama": {"model": "test-model:7b"}, "import": {"max_total_mb": 250}})
    save_settings(s, settings_file)
    loaded = load_settings(settings_file)
    assert loaded.created is False
    assert loaded.settings.ollama.model == "test-model:7b"
    assert loaded.settings.import_.max_total_mb == 250
    assert not list(tmp_path.glob(".settings-*.tmp")), "임시 파일이 남으면 안 된다"


def test_partial_user_file_is_merged_with_defaults(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"ollama": {"model": "m1"}}', encoding="utf-8")
    loaded = load_settings(settings_file)
    assert loaded.settings.ollama.model == "m1"
    assert loaded.settings.ollama.base_url == "http://127.0.0.1:11434"
    assert loaded.settings.import_.max_file_mb == 10


@pytest.mark.parametrize("content", ["{broken json", "[1, 2, 3]", '{"import": {"max_file_mb": 50}}'])
def test_invalid_file_is_backed_up_and_defaults_used(tmp_path: Path, content: str) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(content, encoding="utf-8")
    loaded = load_settings(settings_file)
    assert loaded.settings == AppSettings()
    assert len(loaded.warnings) == 1 and "기본값으로 시작" in loaded.warnings[0]
    backups = list(tmp_path.glob("settings.invalid-*.json"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == content
    assert json.loads(settings_file.read_text(encoding="utf-8"))  # 새 기본 파일이 다시 쓰였다


def test_file_size_limit_cannot_exceed_plan() -> None:
    with pytest.raises(ValidationError) as info:
        AppSettings.model_validate({"import": {"max_file_mb": 10.5}})
    assert "10 이하" in describe_validation_error(info.value)


def test_total_limit_not_below_file_limit() -> None:
    with pytest.raises(ValidationError) as info:
        AppSettings.model_validate({"import": {"max_file_mb": 10, "max_total_mb": 5}})
    assert "총 입력 한도" in describe_validation_error(info.value)


def test_extensions_are_normalized() -> None:
    s = AppSettings.model_validate({"import": {"log_extensions": ["LOG", ".Txt", "log", " "]}})
    assert s.import_.log_extensions == [".log", ".txt"]


@pytest.mark.parametrize(
    "url",
    ["http://192.168.0.10:11434", "http://example.com:11434", "ftp://127.0.0.1", "http://user:pw@127.0.0.1:11434"],
)
def test_ollama_url_must_be_loopback(url: str) -> None:
    with pytest.raises(ValidationError) as info:
        AppSettings.model_validate({"ollama": {"base_url": url}})
    assert "이 PC 안의 주소" in describe_validation_error(info.value)


@pytest.mark.parametrize("url", ["http://localhost:11434/", "http://127.0.0.1:11434", "http://[::1]:11434"])
def test_ollama_loopback_urls_accepted(url: str) -> None:
    s = AppSettings.model_validate({"ollama": {"base_url": url}})
    assert not s.ollama.base_url.endswith("/")


def test_blank_model_means_unset() -> None:
    assert AppSettings.model_validate({"ollama": {"model": "   "}}).ollama.model is None


def test_custom_data_root(tmp_path: Path) -> None:
    s = AppSettings.model_validate({"data_root": str(tmp_path / "data")})
    assert resolve_data_root(s, tmp_path / "boot") == tmp_path / "data"
    assert resolve_data_root(AppSettings(), tmp_path / "boot") == tmp_path / "boot"


def test_relative_data_root_rejected() -> None:
    with pytest.raises(ValidationError) as info:
        AppSettings.model_validate({"data_root": "relative\\folder"})
    assert "절대 경로" in describe_validation_error(info.value)


def test_newer_settings_version_warns(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"settings_version": 99, "future_key": true}', encoding="utf-8")
    loaded = load_settings(settings_file)
    assert any("새 버전" in w for w in loaded.warnings)


def _shipped_with_model(tmp_path: Path, model: str | None) -> Path:
    data = json.loads(SHIPPED_DEFAULTS_FILE.read_text(encoding="utf-8"))
    data["ollama"]["model"] = model
    path = tmp_path / "shipped.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_old_unset_model_uses_bundled_model(tmp_path: Path) -> None:
    """다른 PC에 예전 설정(모델 미지정)이 남아 있어도 휴대용 AI 배포본의 동봉 모델을 쓴다(실사용 보고 2026-11-17)."""
    settings_file = tmp_path / "settings.json"
    save_settings(AppSettings(), settings_file)  # model = None
    loaded = load_settings(settings_file, _shipped_with_model(tmp_path, "test-bundled:4b"))
    assert loaded.settings.ollama.model == "test-bundled:4b"
    assert json.loads(settings_file.read_text(encoding="utf-8"))["ollama"]["model"] == "test-bundled:4b"


def test_user_chosen_model_is_kept_over_bundled(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    save_settings(AppSettings.model_validate({"ollama": {"model": "user-pick:7b"}}), settings_file)
    loaded = load_settings(settings_file, _shipped_with_model(tmp_path, "test-bundled:4b"))
    assert loaded.settings.ollama.model == "user-pick:7b"


def test_no_bundled_model_keeps_unset(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    save_settings(AppSettings(), settings_file)
    assert load_settings(settings_file, _shipped_with_model(tmp_path, None)).settings.ollama.model is None
