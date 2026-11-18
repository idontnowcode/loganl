"""예전 버전 앱이 읽어 저장한 정상 로그를 지금 파서로 다시 읽어 비교 (CR-08 DT-16).

v0.3.0은 `[TEST-CTRL] TEST-RUN ...`의 대괄호 구성요소를 메시지에 남겨 이벤트 이름이 `[TEST-CTRL] TEST-RUN ...`
이었고, v0.4.0은 `TEST-CTRL|TEST-RUN ...`으로 읽는다. 업그레이드한 DB를 흉내 내려고 저장된 이벤트를 v0.3.0 모양으로
바꾸고, 마이그레이션 0007 적용 시각을 그 로그들을 가져온 뒤로 옮긴다. 데이터는 모두 가상 값(TEST- 이름)이다.
"""

from __future__ import annotations

import time
from contextlib import closing
from pathlib import Path

import pytest

from app.analysis.diagnostic_engine import AnalysisRequest, DiagnosticEngine
from app.bootstrap import initialize
from app.core.importer import LogImporter
from app.core.reparse import PARSER_CHANGED_MIGRATION, _reparse, current_parse, stale_log_ids
from app.learning.log_entry import LogEntryService
from app.models.enums import OriginPurpose
from app.models.records import ImportRequest, SessionMode
from app.storage.repositories import EventRepository, LogFileRepository


def bracket_lines(values: tuple[int, ...], start: int) -> str:
    events = [("INFO", "TEST-CTRL", "TEST-START step=1"), ("INFO", "TEST-SUB", "TEST-READY slot=2")]
    for cycle, value in enumerate(values, start=1):
        events += [("INFO", "TEST-CTRL", f"TEST-RUN cycle={cycle} value={value}"),
                   ("INFO", "TEST-SUB", f"TEST-CHECK cycle={cycle} result=OK")]
    events.append(("INFO", "TEST-CTRL", "TEST-STOP step=9"))
    out = []
    for index, (level, component, message) in enumerate(events):
        seconds = start + index
        out.append(f"2026-11-16 10:{seconds // 60:02d}:{seconds % 60:02d}.000 {level} [{component}] {message}")
    return "\n".join(out) + "\n"


@pytest.fixture
def env(tmp_path: Path):
    ctx = initialize(tmp_path / "home", configure_logging=False)
    importer = LogImporter(ctx.database, ctx.paths, ctx.settings.import_)

    def add(name: str, values: tuple[int, ...], start: int) -> int:
        path = tmp_path / name
        path.write_text(bracket_lines(values, start), encoding="utf-8")
        return importer.import_logs(ImportRequest(paths=[path], purpose=OriginPurpose.NORMAL_CASE,
                                                  session_mode=SessionMode.SEPARATE)).log_file_ids[0]

    entries = LogEntryService(ctx.database)
    old_ids = []
    for index, values in enumerate([(41, 43, 42), (44, 40, 42), (42, 41, 43), (43, 44, 40)]):
        old_ids.append(add(f"normal_{index}.log", values, start=index * 30))
        entries.save(old_ids[-1], "NORMAL", "")
    with closing(ctx.database.connect()) as conn:  # v0.3.0이 저장한 모양으로 되돌린다
        conn.execute(
            "UPDATE events SET message = '[' || component || '] ' || message, "
            "event_type = '[' || component || '] ' || substr(event_type, instr(event_type, '|') + 1), "
            f"component = NULL WHERE component IS NOT NULL AND log_file_id IN ({', '.join('?' * len(old_ids))})",
            old_ids,
        )
        time.sleep(0.01)
        conn.execute("UPDATE schema_migrations SET applied_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE version = ?",
                     (PARSER_CHANGED_MIGRATION,))
    time.sleep(0.01)
    yield ctx, add, DiagnosticEngine(ctx.database, ctx.paths, ctx.settings), old_ids
    ctx.close()


def deterministic_types(result) -> set[str]:
    return {f.finding_type for f in result.findings if f.layer == "DETERMINISTIC" and f.stance == "SUPPORTS"}


def test_only_logs_from_before_the_parser_change_are_stale(env) -> None:
    ctx, add, _engine, old_ids = env
    new_id = add("new.log", (42, 43, 41), start=300)
    assert stale_log_ids(ctx.database, [*old_ids, new_id]) == set(old_ids)


def test_old_normals_are_reread_and_compared(env) -> None:
    _ctx, add, engine, _old = env
    result = engine.run(AnalysisRequest([add("high.log", (42, 418, 41), start=300)], use_ai=False))
    assert result.axes["anomaly_level"] == "SUSPECTED"
    assert "VALUE_OUT_OF_RANGE" in deterministic_types(result)


def test_new_normal_log_is_not_flagged_against_old_normals(env) -> None:
    _ctx, add, engine, _old = env
    result = engine.run(AnalysisRequest([add("ok.log", (43, 42, 41, 44), start=300)], use_ai=False))
    assert result.axes["anomaly_level"] == "NONE_FOUND"
    assert deterministic_types(result) == set()


def test_rereading_does_not_change_stored_events(env) -> None:
    ctx, add, engine, old_ids = env
    engine.run(AnalysisRequest([add("high.log", (42, 418, 41), start=300)], use_ai=False))
    with closing(ctx.database.connect()) as conn:
        stored = EventRepository(conn).for_files(old_ids)
    assert all(e.component is None for events in stored.values() for e in events)
    assert stored[old_ids[0]][0].event_type.startswith("[TEST-CTRL] ")


def test_reread_events_point_to_real_event_ids(env) -> None:
    ctx, _add, _engine, old_ids = env
    with closing(ctx.database.connect()) as conn:
        records = LogFileRepository(conn).get_many(old_ids)
        stored = EventRepository(conn).for_files(old_ids)
    _records, events = current_parse(ctx.database, records, stored)
    first = events[old_ids[0]]
    assert first[0].component == "TEST-CTRL" and first[0].event_type.startswith("TEST-CTRL|")
    assert {e.id for e in first} <= {e.id for e in stored[old_ids[0]]}


def test_missing_original_falls_back_to_stored_events(env) -> None:
    ctx, _add, _engine, old_ids = env
    with closing(ctx.database.connect()) as conn:
        record = LogFileRepository(conn).get_many(old_ids[:1])[0]
        stored = EventRepository(conn).for_files(old_ids[:1])[old_ids[0]]
    assert _reparse(ctx.database, record.model_copy(update={"stored_path": "no/such/file.log"}), stored) is None
