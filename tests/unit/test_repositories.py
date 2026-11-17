"""저장소: 이벤트 페이지·필터·원본 행 위치, 파일 메타데이터 수정 규칙."""

from __future__ import annotations

import sqlite3

import pytest

from app.models.enums import EventTimeKind
from app.models.records import ParsedEvent
from app.storage.repositories import AuditRepository, EventRepository, LogFileRepository, SessionRepository


def events(n: int) -> list[ParsedEvent]:
    out = []
    line = 1
    for i in range(n):
        multi = i == 3
        text = f"TEST-EVENT {i} 100%_done" if i % 2 else f"TEST-EVENT {i}"
        raw = text + ("\n  continued" if multi else "")
        out.append(ParsedEvent(seq=i, line_no=line, line_end=line + 1 if multi else None, ts_kind=EventTimeKind.NONE,
                               message=raw, raw_text=raw, error_code="TEST-E001" if i == 5 else None))
        line += 2 if multi else 1
    return out


@pytest.fixture
def file_with_events(db: sqlite3.Connection, factory) -> int:
    f = factory.log_file()
    assert EventRepository(db).add_many(f, events(10), "raw") == 10
    return f


def test_paging_and_count(db: sqlite3.Connection, file_with_events: int) -> None:
    repo = EventRepository(db)
    assert repo.count(file_with_events) == 10
    page = repo.list(file_with_events, offset=4, limit=3)
    assert [e.seq for e in page] == [4, 5, 6]


def test_filter_escapes_wildcards(db: sqlite3.Connection, file_with_events: int) -> None:
    repo = EventRepository(db)
    assert repo.count(file_with_events, "100%_done") == 5
    assert repo.count(file_with_events, "%") == 5  # % 자체를 찾는다
    assert repo.count(file_with_events, "TEST-E001") == 1  # 오류 코드도 찾는다
    assert repo.count(file_with_events, "  ") == 10  # 빈 필터


def test_index_of_line_handles_multiline(db: sqlite3.Connection, file_with_events: int) -> None:
    repo = EventRepository(db)
    assert repo.index_of_line(file_with_events, 4) == 3
    assert repo.index_of_line(file_with_events, 5) == 3  # 여러 줄 이벤트의 둘째 줄
    assert repo.index_of_line(file_with_events, 6) == 4
    assert repo.index_of_line(file_with_events, 99) is None
    assert repo.index_of_line(file_with_events, 6, "100%_done") is None  # 필터에 걸러진 행
    assert repo.index_of_line(file_with_events, 7, "100%_done") == 2


def test_metadata_update_rules(db: sqlite3.Connection, factory) -> None:
    f = factory.log_file()
    repo = LogFileRepository(db)
    repo.update_metadata(f, log_role="TEST-ROLE", user_memo="메모", timestamp_basis="RELATIVE")
    record = repo.get(f)
    assert record.log_role == "TEST-ROLE" and record.timestamp_basis == "RELATIVE"
    with pytest.raises(ValueError, match="원본 식별 정보"):
        repo.update_metadata(f, sha256="0" * 64)


def test_session_update(db: sqlite3.Connection) -> None:
    repo = SessionRepository(db)
    sid = repo.create("TEST 세션", relation_confirmed=False)
    repo.update(sid, relation_confirmed=True, time_sync_status="SYNCED")
    session = repo.get(sid)
    assert session.relation_confirmed is True and session.time_sync_status == "SYNCED"
    with pytest.raises(ValueError):
        repo.update(sid, created_at="x")


def test_audit_round_trip(db: sqlite3.Connection) -> None:
    from app.models.enums import AuditAction

    AuditRepository(db).add("case", 7, AuditAction.UPDATE, before={"a": 1}, after={"a": 2}, note="TEST")
    rows = AuditRepository(db).for_entity("case", 7)
    assert len(rows) == 1 and rows[0]["after_json"] == '{"a": 2}'
