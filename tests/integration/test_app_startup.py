"""시작 순서 전체 (STO-02·10, DOD-14 일부, UI-09, Phase 1 완료 기준 "앱이 실행되고 로컬 DB와 설정을 사용할 수 있음")."""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

import pytest

import main as entry
from app.ai.ollama_client import OllamaStatus
from app.bootstrap import StartupError, initialize

NOW = "2026-11-16T00:00:00.000Z"


def test_first_run_creates_folders_settings_and_db(tmp_path: Path) -> None:
    home = tmp_path / "home"
    ctx = initialize(home, configure_logging=False)
    try:
        assert ctx.settings_created
        assert ctx.settings_file == home / "config" / "settings.json"
        assert ctx.settings_file.exists()
        for directory in ctx.paths.all_dirs():
            assert directory.is_dir(), directory
        assert ctx.paths.db_file.exists()
        assert ctx.schema_version == 8
        assert ctx.capabilities.fts5
        assert ctx.warnings == []
    finally:
        ctx.close()


def test_restart_keeps_data_and_does_not_reapply(tmp_path: Path) -> None:
    home = tmp_path / "home"
    ctx = initialize(home, configure_logging=False)
    with closing(ctx.database.connect()) as conn:
        conn.execute(
            "INSERT INTO mode_definitions (name, created_at, updated_at) VALUES ('재시작 확인 모드', ?, ?)", (NOW, NOW)
        )
    ctx.close()

    again = initialize(home, configure_logging=False)
    try:
        assert not again.settings_created
        with closing(again.database.connect()) as conn:
            names = [r[0] for r in conn.execute("SELECT name FROM mode_definitions")]
            migrations = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        assert names == ["재시작 확인 모드"]
        assert migrations == 8
    finally:
        again.close()


def test_broken_settings_file_still_starts(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    (home / "config" / "settings.json").write_text("{깨진 파일", encoding="utf-8")
    ctx = initialize(home, configure_logging=False)
    try:
        assert any("기본값으로 시작" in w for w in ctx.warnings)
        assert list((home / "config").glob("settings.invalid-*.json"))
    finally:
        ctx.close()


def test_custom_data_root(tmp_path: Path) -> None:
    home, data = tmp_path / "home", tmp_path / "data"
    (home / "config").mkdir(parents=True)
    (home / "config" / "settings.json").write_text(json.dumps({"data_root": str(data)}), encoding="utf-8")
    ctx = initialize(home, configure_logging=False)
    try:
        assert ctx.paths.db_file == data / "database" / "knowledge.db"
        assert ctx.paths.db_file.exists()
        assert ctx.settings_file.parent == home / "config"
        assert not (home / "database").exists()
    finally:
        ctx.close()


def test_update_settings_reports_restart_needed(tmp_path: Path) -> None:
    ctx = initialize(tmp_path / "home", configure_logging=False)
    try:
        same_root = ctx.settings.model_copy(update={"ollama": ctx.settings.ollama.model_copy(update={"model": "m"})})
        assert ctx.update_settings(same_root) == []
        moved = ctx.settings.model_copy(update={"data_root": str(tmp_path / "elsewhere")})
        notes = ctx.update_settings(moved)
        assert notes and "다시 시작" in notes[0]
        saved = json.loads(ctx.settings_file.read_text(encoding="utf-8"))
        assert saved["ollama"]["model"] == "m" and saved["data_root"] == str(tmp_path / "elsewhere")
    finally:
        ctx.close()


def test_unusable_database_raises_startup_error(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "database" / "knowledge.db").mkdir(parents=True)  # 파일 자리에 폴더
    with pytest.raises(StartupError, match="데이터베이스"):
        initialize(home, configure_logging=False)


def test_app_log_is_written_and_released(tmp_path: Path) -> None:
    ctx = initialize(tmp_path / "home", configure_logging=True)
    log_file = ctx.paths.logs / "app.log"
    ctx.close()
    text = log_file.read_text(encoding="utf-8")
    assert "시작" in text and "스키마 v8" in text
    log_file.unlink()  # 핸들러가 파일을 놓았는지(Windows 잠금) 확인


class FakeClient:
    def __init__(self, base_url: str, **_: object) -> None:
        self.base_url = base_url

    def check_status(self, model: str | None = None) -> OllamaStatus:
        return OllamaStatus(base_url=self.base_url, reachable=False, configured_model=model,
                            error="Ollama에 연결할 수 없습니다. Ollama가 실행 중인지 확인하세요.")

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_check_command(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, isolated_home: Path) -> None:
    monkeypatch.setattr(entry, "OllamaClient", FakeClient)
    assert entry.main(["--check"]) == 0
    out = capsys.readouterr().out
    assert "점검" in out
    assert str(isolated_home) in out
    assert "스키마 v8" in out
    assert "Ollama: 연결 안 됨" in out
    assert "결과: 기반 정상" in out
