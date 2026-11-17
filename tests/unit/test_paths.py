"""STO-01·02: 데이터 폴더 구조와 자동 생성."""

from pathlib import Path

from app.config import bootstrap_dir
from app.storage.paths import AppPaths

PLAN_DIRS = (
    "database",
    "cases/normal",
    "cases/abnormal",
    "attachments/guides",
    "indexes",
    "analysis",
    "backups",
    "logs",
    "config",
)


def test_ensure_creates_plan_structure(tmp_path: Path) -> None:
    root = tmp_path / "LocalLogAnalyzer"
    paths = AppPaths(root=root, config_dir=root / "config")
    paths.ensure()
    for relative in PLAN_DIRS:
        assert (root / relative).is_dir(), relative
    assert paths.db_file == root / "database" / "knowledge.db"


def test_ensure_is_idempotent(tmp_path: Path) -> None:
    paths = AppPaths(root=tmp_path / "r", config_dir=tmp_path / "r" / "config")
    paths.ensure()
    marker = paths.logs / "keep.txt"
    marker.write_text("x", encoding="utf-8")
    paths.ensure()
    assert marker.read_text(encoding="utf-8") == "x"


def test_custom_root_keeps_config_in_bootstrap(tmp_path: Path) -> None:
    paths = AppPaths(root=tmp_path / "data", config_dir=tmp_path / "boot" / "config")
    paths.ensure()
    assert (tmp_path / "data" / "database").is_dir()
    assert (tmp_path / "boot" / "config").is_dir()
    assert not (tmp_path / "data" / "config").exists()


def test_bootstrap_dir_uses_lla_home(isolated_home: Path) -> None:
    assert bootstrap_dir() == isolated_home.resolve()


def test_bootstrap_dir_defaults_to_localappdata(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LLA_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    assert bootstrap_dir() == tmp_path / "lad" / "LocalLogAnalyzer"
