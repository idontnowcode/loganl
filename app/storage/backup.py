"""백업 및 복원 (계획서 §9.1 "백업 및 복원 지원", §8.1 보조 메뉴).

- 백업: DB는 SQLite 백업 API로 일관된 사본을 만들고, 원본 사본 폴더(cases·attachments)와 분석 결과(analysis),
  설정을 하나의 zip으로 묶는다. manifest.json에 앱·스키마 버전과 파일별 SHA-256을 적는다.
- 복원: zip과 manifest를 검사하고(해시·스키마 버전), **복원 직전 상태를 먼저 백업**한 뒤 현재 폴더는 지우지 않고
  backups/pre-restore-…로 옮긴다. 원본 로그 사본은 다시 읽기 전용으로 만든다. 복원 후에는 앱을 다시 시작해야 한다.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.storage.database import Database, discover_migrations
from app.storage.paths import AppPaths

DATA_FOLDERS = ("cases", "attachments", "analysis")
READ_ONLY_FOLDERS = ("cases", "attachments")
MANIFEST = "manifest.json"
DB_NAME = "database/knowledge.db"


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    files: int
    schema_version: int
    created_at: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_backup(paths: AppPaths, database: Database, app_version: str, target: Path | None = None) -> BackupInfo:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = target or paths.backups / f"LLA-backup-{stamp}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=paths.backups) as tmp:
        db_copy = Path(tmp) / "knowledge.db"
        with closing(database.connect()) as source, closing(sqlite3.connect(db_copy)) as dest:
            source.backup(dest)
        schema_version = database.schema_version()
        entries: dict[str, str] = {DB_NAME: _sha256(db_copy)}
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(db_copy, DB_NAME)
            for folder in DATA_FOLDERS:
                base = paths.root / folder
                for file in sorted(p for p in base.rglob("*") if p.is_file()):
                    name = file.relative_to(paths.root).as_posix()
                    archive.write(file, name)
                    entries[name] = _sha256(file)
            settings_file = paths.config_dir / "settings.json"
            if settings_file.exists():
                archive.write(settings_file, "config/settings.json")
                entries["config/settings.json"] = _sha256(settings_file)
            manifest = {"app_version": app_version, "schema_version": schema_version, "created_at": stamp,
                        "files": entries}
            archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
    return BackupInfo(target, len(entries), schema_version, stamp)


def inspect_backup(zip_path: Path) -> dict:
    """복원 전에 zip을 검사한다. 문제가 있으면 BackupError."""
    try:
        with zipfile.ZipFile(zip_path) as archive:
            names = set(archive.namelist())
            if MANIFEST not in names:
                raise BackupError("백업 목록(manifest.json)이 없습니다. 이 앱이 만든 백업이 아닙니다.")
            manifest = json.loads(archive.read(MANIFEST).decode("utf-8"))
            for name, expected in manifest["files"].items():
                if name.startswith("/") or ".." in Path(name).parts:
                    raise BackupError(f"백업 안의 경로가 올바르지 않습니다: {name}")
                if name not in names:
                    raise BackupError(f"백업에 파일이 빠져 있습니다: {name}")
                if hashlib.sha256(archive.read(name)).hexdigest() != expected:
                    raise BackupError(f"백업 파일이 손상됐습니다(해시 불일치): {name}")
    except zipfile.BadZipFile as exc:
        raise BackupError("zip 파일이 아니거나 손상됐습니다.") from exc
    latest = max((m.version for m in discover_migrations()), default=0)
    if manifest["schema_version"] > latest:
        raise BackupError(f"이 앱보다 새 버전(스키마 v{manifest['schema_version']})에서 만든 백업입니다.")
    return manifest


def restore_backup(zip_path: Path, paths: AppPaths, database: Database, app_version: str) -> Path:
    """복원하고, 복원 직전 상태를 담은 안전 백업 경로를 돌려준다. 끝나면 앱을 다시 시작해야 한다."""
    manifest = inspect_backup(zip_path)
    safety = create_backup(paths, database, app_version,
                           paths.backups / f"pre-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip")
    aside = paths.backups / f"pre-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}-files"
    aside.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=paths.backups) as tmp:
        staging = Path(tmp)
        with zipfile.ZipFile(zip_path) as archive:
            for name in manifest["files"]:
                archive.extract(name, staging)
        # 현재 데이터는 지우지 않고 옆으로 옮긴다
        for folder in DATA_FOLDERS:
            current = paths.root / folder
            if current.exists():
                shutil.move(str(current), str(aside / folder))
        for suffix in ("", "-wal", "-shm"):
            current_db = Path(str(paths.db_file) + suffix)
            if current_db.exists():
                (aside / "database").mkdir(exist_ok=True)
                shutil.move(str(current_db), str(aside / "database" / current_db.name))
        paths.database_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staging / DB_NAME, paths.db_file)
        for folder in DATA_FOLDERS:
            source = staging / folder
            if source.exists():
                shutil.copytree(source, paths.root / folder)
        if (staging / "config/settings.json").exists():
            shutil.copy2(staging / "config/settings.json", paths.config_dir / "settings.json")
    for folder in READ_ONLY_FOLDERS:
        for file in (paths.root / folder).rglob("*"):
            if file.is_file():
                os.chmod(file, stat.S_IREAD)
    paths.ensure()
    return safety.path
