"""예전 버전 앱이 읽어 저장한 로그를 지금 파서로 다시 읽기 — 비교에 쓸 때 메모리에서만 (CR-08 DT-16).

v0.4.0(마이그레이션 0007)에서 파서가 바뀌었다(대괄호 `[구성요소]`, `<레벨>`, 자동 형식 인식, 새 줄 형식).
그 전에 가져온 로그는 이벤트 이름이 지금 가져오는 로그와 다르게 저장돼 있어서, 업그레이드한 PC에서는
예전에 저장한 정상 로그와 새 로그를 비교하지 못했다(예: `[TEST-CTRL] TEST-RUN` 과 `TEST-CTRL|TEST-RUN`).

그래서 비슷한 사례 찾기·정상 로그 대비 비교에 쓸 때만, 보관한 원본 사본을 지금 파서로 다시 읽어 쓴다.
- DB의 이벤트와 원본 사본은 바꾸지 않는다(절대 규칙 5). 다시 읽은 결과는 이 프로세스 안에서만 캐시한다.
- 다시 읽은 이벤트의 id는 그 줄을 담은 DB 이벤트의 id다. 그래서 근거로 저장해도 외래 키가 맞는다.
- 지금 가져오기와 같은 방식(파서 자동 선택)으로 읽는다. 원본 사본을 못 읽으면 저장된 이벤트를 그대로 쓴다.
"""

from __future__ import annotations

import bisect
import logging
from collections.abc import Mapping, Sequence
from contextlib import closing

from app.core.decoding import decode_log_bytes
from app.core.importer import LogImporter
from app.models.records import EventRecord, LogFileRecord
from app.parsers.parser_registry import ParserRegistry
from app.storage.database import Database
from app.storage.repositories import placeholders

logger = logging.getLogger(__name__)

# 이 번호의 마이그레이션이 적용되기 전에 가져온 로그는 예전 파서로 읽은 것이다.
# 파서 규칙이 다시 크게 바뀌면 그때 추가한 마이그레이션 번호로 올린다.
PARSER_CHANGED_MIGRATION = 7
_CACHE_LIMIT = 64

_registry: ParserRegistry | None = None
_cache: dict[tuple[str, int], tuple[LogFileRecord, list[EventRecord]]] = {}


def _default_registry() -> ParserRegistry:
    global _registry
    if _registry is None:
        _registry = ParserRegistry.default()
    return _registry


def stale_log_ids(database: Database, log_file_ids: Sequence[int]) -> set[int]:
    """지금 파서 규칙이 들어오기 전(마이그레이션 0007 적용 전)에 가져온 로그 id."""
    if not log_file_ids:
        return set()
    with closing(database.connect()) as conn:
        row = conn.execute("SELECT applied_at FROM schema_migrations WHERE version = ?",
                           (PARSER_CHANGED_MIGRATION,)).fetchone()
        if row is None:
            return set()
        rows = conn.execute(
            f"SELECT id FROM log_files WHERE id IN ({placeholders(log_file_ids)}) AND imported_at < ?",
            (*log_file_ids, row[0]),
        )
        return {int(r[0]) for r in rows}


def _reparse(database: Database, record: LogFileRecord,
             stored: Sequence[EventRecord]) -> tuple[LogFileRecord, list[EventRecord]] | None:
    if not stored:
        return None
    root = database.db_file.parent.parent  # AppPaths: <데이터>/database/knowledge.db, 사본은 <데이터>/<stored_path>
    try:
        decoded = decode_log_bytes((root / record.stored_path).read_bytes())
    except (OSError, ValueError) as exc:
        logger.warning("예전 로그 %s를 다시 읽지 못해 저장된 이벤트를 씁니다: %s", record.id, exc)
        return None
    registry = _default_registry()
    try:
        parser = registry.select([line.text for line in decoded.lines[:200]])
        parsed = list(parser.parse(decoded.lines))
    except Exception:  # 다시 읽기는 보조 수단이다. 실패해도 분석은 저장된 이벤트로 계속한다.
        logger.exception("예전 로그 %s를 지금 파서로 다시 읽지 못해 저장된 이벤트를 씁니다", record.id)
        return None
    if not parsed:
        return None
    lines = [e.line_no for e in stored]  # DB 이벤트는 seq(=줄) 순서다
    out: list[EventRecord] = []
    for e in parsed:
        owner = stored[max(bisect.bisect_right(lines, e.line_no) - 1, 0)]
        out.append(EventRecord(
            id=owner.id, log_file_id=record.id, seq=e.seq, line_no=e.line_no, line_end=e.line_end,
            byte_offset=e.byte_offset, ts_raw=e.ts_raw, ts_value_us=e.ts_value_us, ts_kind=e.ts_kind,
            level=e.level, component=e.component, event_type=e.event_type, error_code=e.error_code,
            message=e.message, raw_text=e.raw_text,
        ))
    basis = LogImporter._timestamp_basis(parser.timestamp_basis, parsed)
    return record.model_copy(update={"timestamp_basis": basis, "parser_profile": parser.name}), out


def current_parse(database: Database, records: Sequence[LogFileRecord],
                  events: Mapping[int, list[EventRecord]]) -> tuple[list[LogFileRecord], dict[int, list[EventRecord]]]:
    """records 가운데 예전 파서로 읽은 로그만 지금 파서로 다시 읽은 것으로 바꿔 돌려준다."""
    out_records = list(records)
    out_events = dict(events)
    stale = stale_log_ids(database, [r.id for r in records])
    if not stale:
        return out_records, out_events
    for index, record in enumerate(out_records):
        if record.id not in stale:
            continue
        key = (str(database.db_file), record.id)
        hit = _cache.get(key)
        if hit is None:
            hit = _reparse(database, record, events.get(record.id, []))
            if hit is None:
                continue
            if len(_cache) >= _CACHE_LIMIT:
                _cache.pop(next(iter(_cache)))
            _cache[key] = hit
        out_records[index], out_events[record.id] = hit
    return out_records, out_events
