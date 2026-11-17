"""앱 시작 순서를 한곳에서 조립한다. Qt를 쓰지 않으므로 GUI 없는 점검(`main.py --check`)과 테스트에서도 쓴다.

순서: 설정 읽기 → 데이터 폴더 결정·생성 → 앱 로그 설정 → DB 마이그레이션 → SQLite 기능 검사
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app import APP_NAME, __version__
from app.config import (
    AppSettings,
    bootstrap_dir,
    load_settings,
    resolve_data_root,
    save_settings,
    settings_file_for,
)
from app.storage.database import Database, DatabaseError, SqliteCapabilities
from app.storage.paths import AppPaths

logger = logging.getLogger("app")


class StartupError(Exception):
    """앱을 시작할 수 없음. 메시지는 사용자에게 그대로 보여 준다."""


@dataclass
class AppContext:
    bootstrap_dir: Path
    settings_file: Path
    settings: AppSettings
    paths: AppPaths
    database: Database
    capabilities: SqliteCapabilities
    schema_version: int
    warnings: list[str] = field(default_factory=list)
    settings_created: bool = False
    _log_handler: logging.Handler | None = field(default=None, repr=False)

    def update_settings(self, new_settings: AppSettings) -> list[str]:
        """설정을 저장하고 이번 실행에 반영한다. 다시 시작해야 적용되는 항목은 안내 문장으로 돌려준다."""
        save_settings(new_settings, self.settings_file)
        notes: list[str] = []
        if resolve_data_root(new_settings, self.bootstrap_dir) != self.paths.root:
            notes.append("데이터 폴더 변경은 앱을 다시 시작한 뒤 적용됩니다. 기존 데이터는 옮겨지지 않습니다.")
        self.settings = new_settings
        logger.info("설정을 저장했습니다: %s", self.settings_file)
        return notes

    def close(self) -> None:
        if self._log_handler is not None:
            logger.removeHandler(self._log_handler)
            self._log_handler.close()
            self._log_handler = None


def initialize(home: Path | None = None, *, configure_logging: bool = True) -> AppContext:
    bootstrap = Path(home) if home is not None else bootstrap_dir()
    settings_file = settings_file_for(bootstrap)

    try:
        loaded = load_settings(settings_file)
    except OSError as exc:
        raise StartupError(f"설정 파일을 읽거나 만들 수 없습니다: {settings_file} ({exc})") from exc

    root = resolve_data_root(loaded.settings, bootstrap)
    paths = AppPaths(root=root, config_dir=settings_file.parent)
    try:
        paths.ensure()
    except OSError as exc:
        raise StartupError(
            f"데이터 폴더를 만들 수 없습니다: {root}\n"
            f"설정 파일({settings_file})의 data_root를 확인하세요. ({exc})"
        ) from exc

    handler = _configure_logging(paths.logs) if configure_logging else None
    database = Database(paths.db_file)
    try:
        applied = database.migrate()
        capabilities = database.capabilities()
        schema_version = database.schema_version()
    except (DatabaseError, sqlite3.Error, OSError) as exc:
        if handler is not None:
            logger.error("DB 준비 실패: %s", exc)
            logger.removeHandler(handler)
            handler.close()
        raise StartupError(f"데이터베이스를 준비하지 못했습니다: {paths.db_file}\n{exc}") from exc

    warnings = list(loaded.warnings) + capabilities.problems
    logger.info(
        "%s %s 시작 (Python %s, SQLite %s, 스키마 v%d, 새로 적용한 마이그레이션 %s)",
        APP_NAME,
        __version__,
        sys.version.split()[0],
        capabilities.version,
        schema_version,
        applied or "없음",
    )
    for warning in warnings:
        logger.warning(warning)

    return AppContext(
        bootstrap_dir=bootstrap,
        settings_file=settings_file,
        settings=loaded.settings,
        paths=paths,
        database=database,
        capabilities=capabilities,
        schema_version=schema_version,
        warnings=warnings,
        settings_created=loaded.created,
        _log_handler=handler,
    )


def _configure_logging(log_dir: Path) -> logging.Handler:
    """앱 동작 기록용. 분석 대상 로그의 원문은 남기지 않는다."""
    handler = RotatingFileHandler(log_dir / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return handler
