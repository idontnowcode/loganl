"""모든 테스트에 걸리는 장치 (docs/TEST_PLAN.md §1.1).

- 외부 네트워크 차단: loopback이 아닌 주소로 연결하거나 이름을 조회하면 예외.
- 데이터 폴더 격리: LLA_HOME·LOCALAPPDATA를 임시 폴더로 바꾼다.
- 화면 테스트: QT_QPA_PLATFORM=offscreen.
"""

from __future__ import annotations

import hashlib
import itertools
import os
import socket
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.security import is_loopback_host  # noqa: E402
from app.storage.database import Database  # noqa: E402

NOW = "2026-11-16T00:00:00.000Z"


class ExternalNetworkBlocked(RuntimeError):
    """테스트 중 PC 밖으로 나가려는 연결."""


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo


def _allowed(address: Any) -> bool:
    if isinstance(address, (str, bytes)):  # AF_UNIX
        return True
    if isinstance(address, tuple) and address:
        host = address[0]
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        return is_loopback_host(str(host))
    return False


def _guarded_connect(self: socket.socket, address: Any) -> None:
    if not _allowed(address):
        raise ExternalNetworkBlocked(f"테스트 중 외부 네트워크 연결 시도: {address!r}")
    return _real_connect(self, address)


def _guarded_connect_ex(self: socket.socket, address: Any) -> int:
    if not _allowed(address):
        raise ExternalNetworkBlocked(f"테스트 중 외부 네트워크 연결 시도: {address!r}")
    return _real_connect_ex(self, address)


def _guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any):
    if host not in (None, "", b""):
        name = host.decode("ascii", "replace") if isinstance(host, bytes) else str(host)
        if not is_loopback_host(name):
            raise ExternalNetworkBlocked(f"테스트 중 외부 이름 조회 시도: {name!r}")
    return _real_getaddrinfo(host, *args, **kwargs)


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "lla_home"
    monkeypatch.setenv("LLA_HOME", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    return home


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    database = Database(tmp_path / "db" / "knowledge.db")
    database.migrate()
    conn = database.connect()
    try:
        yield conn
    finally:
        conn.close()


class Factory:
    """스키마 테스트용 행 생성기. 값은 모두 가상 데이터다."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._counter = itertools.count(1)

    def insert(self, table: str, **values: Any) -> int:
        columns = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        cur = self.conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values()))
        return int(cur.lastrowid)

    def session(self, **overrides: Any) -> int:
        values = {"label": f"세션-{next(self._counter)}", "created_at": NOW}
        values.update(overrides)
        return self.insert("collection_sessions", **values)

    def log_file(self, session_id: int | None = None, **overrides: Any) -> int:
        n = next(self._counter)
        values = {
            "session_id": session_id if session_id is not None else self.session(),
            "original_name": f"test-{n}.log",
            "stored_path": f"cases/normal/test-{n}.log",
            "sha256": hashlib.sha256(f"file-{n}".encode()).hexdigest(),
            "size_bytes": 100,
            "origin_purpose": "NORMAL_CASE",
            "imported_at": NOW,
        }
        values.update(overrides)
        return self.insert("log_files", **values)

    def event(self, log_file_id: int, seq: int = 0, **overrides: Any) -> int:
        values = {
            "log_file_id": log_file_id,
            "seq": seq,
            "line_no": seq + 1,
            "message": f"TEST-EVENT {seq}",
            "raw_text": f"TEST-EVENT {seq}",
        }
        values.update(overrides)
        return self.insert("events", **values)

    def mode(self, **overrides: Any) -> int:
        values = {"name": f"테스트모드-{next(self._counter)}", "created_at": NOW, "updated_at": NOW}
        values.update(overrides)
        return self.insert("mode_definitions", **values)

    def case(self, case_type: str = "NORMAL", **overrides: Any) -> int:
        values = {
            "case_type": case_type,
            "title": f"사례-{next(self._counter)}",
            "created_at": NOW,
            "updated_at": NOW,
        }
        values.update(overrides)
        return self.insert("cases", **values)

    def approved_case(self, case_type: str = "NORMAL", **overrides: Any) -> int:
        values: dict[str, Any] = {"status": "APPROVED", "approved_at": NOW, "current_revision": 1}
        if case_type == "ABNORMAL":
            values.update(symptom="테스트 증상", abnormal_criteria="테스트 판정 기준", cause_status="UNKNOWN")
        values.update(overrides)
        return self.case(case_type, **values)

    def guide_attachment(self, case_id: int, kind: str = "DIAGNOSTIC_GUIDE", **overrides: Any) -> int:
        n = next(self._counter)
        values = {
            "case_id": case_id,
            "attachment_kind": kind,
            "original_name": f"guide-{n}.md",
            "stored_path": f"attachments/guides/guide-{n}.md",
            "sha256": hashlib.sha256(f"guide-{n}".encode()).hexdigest(),
            "size_bytes": 10,
            "format": "MD",
            "imported_at": NOW,
        }
        values.update(overrides)
        return self.insert("guide_attachments", **values)

    def guide_item(self, case_id: int, attachment_id: int | None, **overrides: Any) -> int:
        values = {
            "case_id": case_id,
            "attachment_id": attachment_id,
            "section": "SYMPTOM",
            "content": "테스트 항목",
            "source_kind": "GUIDE_TEXT" if attachment_id is not None else "USER",
            "created_at": NOW,
            "updated_at": NOW,
        }
        values.update(overrides)
        return self.insert("diagnostic_guides", **values)

    def pattern(self, mode_id: int, **overrides: Any) -> int:
        values = {
            "mode_id": mode_id,
            "pattern_type": "REQUIRED",
            "definition_json": '{"events": ["TEST-A"]}',
            "source": "RULE",
            "created_at": NOW,
            "updated_at": NOW,
        }
        values.update(overrides)
        return self.insert("event_patterns", **values)

    def analysis_run(self, **overrides: Any) -> int:
        values = {"app_version": "test", "started_at": NOW}
        values.update(overrides)
        return self.insert("analysis_runs", **values)


@pytest.fixture
def factory(db: sqlite3.Connection) -> Factory:
    return Factory(db)
