"""DB-06·T-MIG-01: 마이그레이션 적용·재실행·체크섬·버전·롤백, 연결 설정, SQLite 기능."""

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.storage.database import (
    MIGRATIONS_DIR,
    Database,
    MigrationError,
    discover_migrations,
    transaction,
)


def _copy_migrations(tmp_path: Path) -> Path:
    target = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, target)
    return target


def test_migrate_applies_initial_schema(tmp_path: Path) -> None:
    database = Database(tmp_path / "k.db")
    assert database.schema_version() == 0
    assert database.migrate() == [1, 2, 3, 4, 5, 6]
    assert database.schema_version() == 6


def test_migrate_twice_is_noop(tmp_path: Path) -> None:
    database = Database(tmp_path / "k.db")
    database.migrate()
    assert database.migrate() == []
    with closing(database.connect()) as conn:
        rows = conn.execute("SELECT version, name, length(checksum) AS n FROM schema_migrations").fetchall()
    assert [(r["version"], r["name"], r["n"]) for r in rows] == [(1, "initial_schema", 64), (2, "session_relation", 64), (3, "search_index", 64), (4, "fts_bulk_index", 64),
                                                                (5, "reference_library", 64), (6, "questions_passages", 64)]


def test_changed_applied_migration_is_rejected(tmp_path: Path) -> None:
    migrations = _copy_migrations(tmp_path)
    database = Database(tmp_path / "k.db", migrations)
    database.migrate()
    first = migrations / "0001_initial_schema.sql"
    first.write_text(first.read_text(encoding="utf-8") + "\n-- 수정\n", encoding="utf-8")
    with pytest.raises(MigrationError, match="바뀌었습니다"):
        database.migrate()


def test_checksum_ignores_line_endings(tmp_path: Path) -> None:
    migrations = _copy_migrations(tmp_path)
    database = Database(tmp_path / "k.db", migrations)
    database.migrate()
    first = migrations / "0001_initial_schema.sql"
    text = first.read_text(encoding="utf-8")
    first.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    assert database.migrate() == []


def test_db_from_newer_app_is_rejected(tmp_path: Path) -> None:
    database = Database(tmp_path / "k.db")
    database.migrate()
    with closing(database.connect()) as conn:
        conn.execute(
            "INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (99, 'future', ?, 'x')",
            ("0" * 64,),
        )
    with pytest.raises(MigrationError, match="모르는 마이그레이션"):
        database.migrate()


def test_failed_migration_rolls_back(tmp_path: Path) -> None:
    migrations = _copy_migrations(tmp_path)
    (migrations / "0099_broken.sql").write_text(
        "CREATE TABLE half_done (id INTEGER PRIMARY KEY);\nTHIS IS NOT SQL;\n", encoding="utf-8"
    )
    database = Database(tmp_path / "k.db", migrations)
    with pytest.raises(MigrationError, match="0099_broken"):
        database.migrate()
    assert database.schema_version() == 6  # 0001~0006은 적용, 0099는 전부 되돌림
    with closing(database.connect()) as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'half_done'").fetchone()
    assert exists is None


@pytest.mark.parametrize("name", ["1_short.sql", "0001-dash.sql", "0001_UPPER.sql"])
def test_bad_migration_file_name(tmp_path: Path, name: str) -> None:
    folder = tmp_path / "m"
    folder.mkdir()
    (folder / name).write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="이름"):
        discover_migrations(folder)


def test_duplicate_migration_version(tmp_path: Path) -> None:
    folder = tmp_path / "m"
    folder.mkdir()
    (folder / "0001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (folder / "0001_b.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="겹칩니다"):
        discover_migrations(folder)


def test_connection_pragmas(db: sqlite3.Connection) -> None:
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_capabilities(tmp_path: Path) -> None:
    caps = Database(tmp_path / "k.db").capabilities()
    assert caps.version_ok
    assert caps.json1
    assert caps.fts5, "FTS5가 없으면 Phase 6 텍스트 검색이 불가능하다"
    assert caps.problems == []


def test_transaction_rolls_back_on_error(db: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError):
        with transaction(db):
            db.execute(
                "INSERT INTO collection_sessions (label, created_at) VALUES ('롤백 대상', '2026-11-16T00:00:00.000Z')"
            )
            raise RuntimeError("중단")
    assert db.execute("SELECT COUNT(*) FROM collection_sessions").fetchone()[0] == 0
