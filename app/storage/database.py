"""SQLite 연결과 마이그레이션 (계획서 §5, §13-7).

- 마이그레이션은 `migrations/NNNN_name.sql` 파일이다. 번호 순서대로 한 번씩 적용한다.
- 적용한 파일의 체크섬을 `schema_migrations`에 남긴다. 적용된 파일이 바뀌었거나,
  DB에 이 앱이 모르는 번호가 있으면(더 새 앱이 만든 DB) 시작을 거부한다.
- 마이그레이션 하나는 한 트랜잭션이다. 중간에 실패하면 전부 되돌린다.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.models import utc_now_iso

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
MIN_SQLITE_VERSION = (3, 37, 0)  # STRICT 테이블
_MIGRATION_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    checksum    TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL
) STRICT
"""


class DatabaseError(Exception):
    """DB를 준비하지 못함."""


class MigrationError(DatabaseError):
    """마이그레이션 파일 또는 적용 이력이 맞지 않음."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str
    checksum: str

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


@dataclass(frozen=True)
class SqliteCapabilities:
    version: str
    version_ok: bool
    fts5: bool
    json1: bool

    @property
    def problems(self) -> list[str]:
        found: list[str] = []
        if not self.version_ok:
            needed = ".".join(map(str, MIN_SQLITE_VERSION))
            found.append(f"SQLite {needed} 이상이 필요합니다 (현재 {self.version}).")
        if not self.json1:
            found.append("SQLite JSON 기능을 쓸 수 없습니다.")
        if not self.fts5:
            found.append("SQLite FTS5(텍스트 검색)를 쓸 수 없습니다. 사례 텍스트 검색(Phase 6)이 동작하지 않습니다.")
        return found


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    found: dict[int, Migration] = {}
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_FILE.match(path.name)
        if not match:
            raise MigrationError(f"마이그레이션 파일 이름이 규칙(0001_name.sql)에 맞지 않습니다: {path.name}")
        version = int(match.group(1))
        if version == 0:
            raise MigrationError(f"마이그레이션 번호는 0001부터 시작합니다: {path.name}")
        if version in found:
            raise MigrationError(f"마이그레이션 번호가 겹칩니다: {found[version].path.name}, {path.name}")
        # read_text는 줄바꿈을 \n으로 바꿔 읽는다. 체크섬이 Git 줄바꿈 설정에 흔들리지 않게 한 번 더 맞춘다.
        sql = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        found[version] = Migration(version, match.group(2), path, sql, checksum)
    return [found[v] for v in sorted(found)]


def probe_capabilities(conn: sqlite3.Connection) -> SqliteCapabilities:
    def works(*statements: str) -> bool:
        try:
            for statement in statements:
                conn.execute(statement)
            return True
        except sqlite3.Error:
            return False

    return SqliteCapabilities(
        version=sqlite3.sqlite_version,
        version_ok=sqlite3.sqlite_version_info >= MIN_SQLITE_VERSION,
        fts5=works(
            "CREATE VIRTUAL TABLE temp.lla_fts5_probe USING fts5(body)",
            "DROP TABLE temp.lla_fts5_probe",
        ),
        json1=works("SELECT json_valid('{}')"),
    )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """쓰기 트랜잭션. 예외가 나면 되돌린다. 연결은 isolation_level=None(자동 커밋)이어야 한다."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


class Database:
    def __init__(self, db_file: Path, migrations_dir: Path = MIGRATIONS_DIR) -> None:
        self.db_file = Path(db_file)
        self.migrations_dir = Path(migrations_dir)

    def connect(self) -> sqlite3.Connection:
        """스레드마다 따로 연다. 트랜잭션은 `transaction()`으로 직접 관리한다."""
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_file, isolation_level=None, timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA journal_mode = WAL").fetchone()
            conn.execute("PRAGMA synchronous = NORMAL")
        except sqlite3.Error:
            conn.close()
            raise
        return conn

    def migrate(self) -> list[int]:
        """적용 안 된 마이그레이션을 순서대로 적용하고, 새로 적용한 번호를 돌려준다."""
        migrations = discover_migrations(self.migrations_dir)
        known = {m.version: m for m in migrations}
        newly_applied: list[int] = []
        with closing(self.connect()) as conn:
            capabilities = probe_capabilities(conn)
            if not capabilities.version_ok:
                raise DatabaseError(capabilities.problems[0])
            conn.execute(_SCHEMA_MIGRATIONS_DDL)
            applied = {
                row["version"]: row
                for row in conn.execute("SELECT version, name, checksum FROM schema_migrations")
            }
            for version, row in sorted(applied.items()):
                migration = known.get(version)
                if migration is None:
                    raise MigrationError(
                        f"DB에 이 앱이 모르는 마이그레이션 {version:04d}({row['name']})이 적용돼 있습니다. "
                        "더 새 버전의 앱에서 만든 DB일 수 있습니다."
                    )
                if migration.checksum != row["checksum"]:
                    raise MigrationError(
                        f"마이그레이션 {migration.path.name}이 적용된 뒤 바뀌었습니다. "
                        "적용된 파일은 고치지 말고 새 번호 파일을 만드세요."
                    )
            for migration in migrations:
                if migration.version not in applied:
                    self._apply(conn, migration)
                    newly_applied.append(migration.version)
        return newly_applied

    def schema_version(self) -> int:
        with closing(self.connect()) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            if not exists:
                return 0
            row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations").fetchone()
            return int(row["v"])

    def capabilities(self) -> SqliteCapabilities:
        with closing(self.connect()) as conn:
            return probe_capabilities(conn)

    @staticmethod
    def _apply(conn: sqlite3.Connection, migration: Migration) -> None:
        try:
            # executescript는 스크립트 앞에서만 COMMIT하고 그 뒤 트랜잭션은 건드리지 않는다(3.11).
            conn.executescript("BEGIN IMMEDIATE;\n" + migration.sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.checksum, utc_now_iso()),
            )
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise MigrationError(f"마이그레이션 {migration.label} 적용에 실패해 되돌렸습니다: {exc}") from exc
