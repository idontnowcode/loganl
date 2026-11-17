"""테이블별 저장·조회. SQL은 이 파일에만 둔다 (docs/ARCHITECTURE.md §4.2).

모든 저장소는 호출자가 연 연결을 받는다. 트랜잭션은 호출자가 `transaction()`으로 묶는다.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from typing import Any

from app.models import utc_now_iso
from app.models.enums import Actor, AuditAction
from app.models.records import (
    CaseRecord,
    ClaimRecord,
    EventRecord,
    EvidenceRef,
    LogFileRecord,
    ModeRecord,
    NoteRecord,
    ParsedEvent,
    PatternCandidate,
    PatternRecord,
    SessionRecord,
)

EVENT_COLUMNS = (
    "id, log_file_id, seq, line_no, line_end, byte_offset, ts_raw, ts_value_us, ts_kind, level, component, "
    "event_type, error_code, message, raw_text"
)
LOG_FILE_COLUMNS = (
    "lf.id, lf.session_id, s.label AS session_label, lf.original_name, lf.stored_path, lf.sha256, lf.size_bytes, "
    "lf.encoding, lf.line_count, lf.log_role, lf.collected_at, lf.timestamp_basis, lf.clock_offset_ms, "
    "lf.offset_basis, lf.fw_version, lf.user_memo, lf.parser_profile, lf.origin_purpose, lf.imported_at"
)


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def placeholders(values: Sequence[Any]) -> str:
    return ", ".join("?" for _ in values)


class AuditRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add(
        self,
        entity_type: str,
        entity_id: int | None,
        action: AuditAction,
        actor: Actor = Actor.USER,
        *,
        before: Any = None,
        after: Any = None,
        note: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO audit_history (entity_type, entity_id, action, actor, before_json, after_json, note, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (entity_type, entity_id, action.value, actor.value, _json(before), _json(after), note, utc_now_iso()),
        )
        return int(cur.lastrowid)

    def for_entity(self, entity_type: str, entity_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM audit_history WHERE entity_type = ? AND entity_id = ? ORDER BY id", (entity_type, entity_id)
        ).fetchall()


class SessionRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(self, label: str, *, relation_confirmed: bool, time_sync_status: str = "UNKNOWN",
               note: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO collection_sessions (label, time_sync_status, relation_confirmed, note, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (label, time_sync_status, int(relation_confirmed), note, utc_now_iso()),
        )
        return int(cur.lastrowid)

    def get(self, session_id: int) -> SessionRecord | None:
        row = self.conn.execute("SELECT * FROM collection_sessions WHERE id = ?", (session_id,)).fetchone()
        return SessionRecord.model_validate(dict(row)) if row else None

    def update(self, session_id: int, **fields: Any) -> None:
        allowed = {"label", "time_sync_status", "relation_confirmed", "note", "device_config"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"바꿀 수 없는 세션 항목: {sorted(unknown)}")
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        values = [int(v) if isinstance(v, bool) else v for v in fields.values()]
        self.conn.execute(f"UPDATE collection_sessions SET {sets} WHERE id = ?", (*values, session_id))


class LogFileRepository:
    EDITABLE = {"log_role", "collected_at", "timestamp_basis", "clock_offset_ms", "offset_basis",
                "time_uncertainty_ms", "error_ranges_json", "fw_version", "user_memo", "session_id"}

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def find_by_sha256(self, sha256: str) -> LogFileRecord | None:
        row = self.conn.execute(
            f"SELECT {LOG_FILE_COLUMNS} FROM log_files lf JOIN collection_sessions s ON s.id = lf.session_id "
            "WHERE lf.sha256 = ?",
            (sha256,),
        ).fetchone()
        return LogFileRecord.model_validate(dict(row)) if row else None

    def add(self, **fields: Any) -> int:
        fields.setdefault("imported_at", utc_now_iso())
        columns = ", ".join(fields)
        cur = self.conn.execute(
            f"INSERT INTO log_files ({columns}) VALUES ({placeholders(list(fields))})", tuple(fields.values())
        )
        return int(cur.lastrowid)

    def get(self, log_file_id: int) -> LogFileRecord | None:
        found = self.get_many([log_file_id])
        return found[0] if found else None

    def get_many(self, ids: Sequence[int]) -> list[LogFileRecord]:
        if not ids:
            return []
        rows = self.conn.execute(
            f"SELECT {LOG_FILE_COLUMNS} FROM log_files lf JOIN collection_sessions s ON s.id = lf.session_id "
            f"WHERE lf.id IN ({placeholders(ids)}) ORDER BY lf.id",
            tuple(ids),
        ).fetchall()
        return [LogFileRecord.model_validate(dict(r)) for r in rows]

    def update_metadata(self, log_file_id: int, **fields: Any) -> None:
        unknown = set(fields) - self.EDITABLE
        if unknown:
            raise ValueError(f"바꿀 수 없는 파일 항목: {sorted(unknown)} (원본 식별 정보는 고칠 수 없음)")
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE log_files SET {sets} WHERE id = ?", (*fields.values(), log_file_id))


class EventRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add_many(self, log_file_id: int, events: Iterable[ParsedEvent], parser_profile: str | None) -> int:
        """넣은 이벤트 수를 돌려준다. total_changes는 FTS 색인 트리거의 변경까지 세므로 쓰지 않는다."""
        rows = [
            (
                log_file_id, e.seq, e.line_no, e.line_end, e.byte_offset, e.ts_raw, e.ts_value_us, e.ts_kind.value,
                e.level, e.component, e.event_type, e.error_code, e.message, e.raw_text, _json(e.attrs), parser_profile,
            )
            for e in events
        ]
        self.conn.executemany(
            "INSERT INTO events (log_file_id, seq, line_no, line_end, byte_offset, ts_raw, ts_value_us, ts_kind, level, "
            "component, event_type, error_code, message, raw_text, attrs_json, parser_profile) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        # 검색 색인은 파일 단위로 한 번에 넣는다(마이그레이션 0004). 같은 트랜잭션이라 원본과 어긋나지 않는다.
        self.conn.execute(
            "INSERT INTO events_fts (rowid, message, event_type, error_code) "
            "SELECT id, message, event_type, error_code FROM events WHERE log_file_id = ?",
            (log_file_id,),
        )
        return len(rows)

    def count(self, log_file_id: int, text_filter: str | None = None) -> int:
        where, params = self._where(log_file_id, text_filter)
        return int(self.conn.execute(f"SELECT COUNT(*) FROM events WHERE {where}", params).fetchone()[0])

    def list(self, log_file_id: int, *, offset: int = 0, limit: int | None = None,
             text_filter: str | None = None) -> list[EventRecord]:
        where, params = self._where(log_file_id, text_filter)
        sql = f"SELECT {EVENT_COLUMNS} FROM events WHERE {where} ORDER BY seq"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params = (*params, limit, offset)
        return [EventRecord.model_validate(dict(r)) for r in self.conn.execute(sql, params)]

    def for_files(self, log_file_ids: Sequence[int]) -> dict[int, list[EventRecord]]:
        result: dict[int, list[EventRecord]] = {i: [] for i in log_file_ids}
        if not log_file_ids:
            return result
        rows = self.conn.execute(
            f"SELECT {EVENT_COLUMNS} FROM events WHERE log_file_id IN ({placeholders(log_file_ids)}) "
            "ORDER BY log_file_id, seq",
            tuple(log_file_ids),
        )
        for row in rows:
            record = EventRecord.model_validate(dict(row))
            result[record.log_file_id].append(record)
        return result

    def error_codes_for_files(self, log_file_ids: Sequence[int]) -> set[str]:
        if not log_file_ids:
            return set()
        rows = self.conn.execute(
            f"SELECT DISTINCT error_code FROM events WHERE error_code IS NOT NULL "
            f"AND log_file_id IN ({placeholders(log_file_ids)})",
            tuple(log_file_ids),
        )
        return {r[0] for r in rows}

    def error_level_count(self, log_file_ids: Sequence[int], levels: Sequence[str]) -> int:
        if not log_file_ids:
            return 0
        return int(self.conn.execute(
            f"SELECT COUNT(*) FROM events WHERE log_file_id IN ({placeholders(log_file_ids)}) "
            f"AND UPPER(COALESCE(level, '')) IN ({placeholders(levels)})",
            (*log_file_ids, *levels),
        ).fetchone()[0])

    def get_many(self, event_ids: Sequence[int]) -> list[EventRecord]:
        if not event_ids:
            return []
        rows = self.conn.execute(
            f"SELECT {EVENT_COLUMNS} FROM events WHERE id IN ({placeholders(event_ids)}) ORDER BY log_file_id, seq",
            tuple(event_ids),
        )
        return [EventRecord.model_validate(dict(r)) for r in rows]

    def index_of_line(self, log_file_id: int, line_no: int, text_filter: str | None = None) -> int | None:
        """line_no가 들어 있는 이벤트가 (필터 적용 후) 몇 번째인지. 원본 행 이동용."""
        where, params = self._where(log_file_id, text_filter)
        row = self.conn.execute(
            f"SELECT seq FROM events WHERE {where} AND line_no <= ? AND COALESCE(line_end, line_no) >= ?",
            (*params, line_no, line_no),
        ).fetchone()
        if row is None:
            return None
        return int(self.conn.execute(
            f"SELECT COUNT(*) FROM events WHERE {where} AND seq < ?", (*params, row["seq"])
        ).fetchone()[0])

    @staticmethod
    def _where(log_file_id: int, text_filter: str | None) -> tuple[str, tuple[Any, ...]]:
        if text_filter and text_filter.strip():
            needle = "%" + text_filter.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            return ("log_file_id = ? AND (raw_text LIKE ? ESCAPE '\\' OR COALESCE(error_code, '') LIKE ? ESCAPE '\\')",
                    (log_file_id, needle, needle))
        return "log_file_id = ?", (log_file_id,)


# ── 운전 모드 · 사례 · 메모 · 패턴 · 주장 · 개정본 (Phase 3~) ──


class ModeRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add(self, name: str, *, description: str | None = None, conditions: str | None = None,
            source: str = "USER", status: str = "ACTIVE") -> int:
        now = utc_now_iso()
        approved = now if status == "ACTIVE" else None
        cur = self.conn.execute(
            "INSERT INTO mode_definitions (name, description, conditions, source, status, approved_at, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name.strip(), description, conditions, source, status, approved, now, now),
        )
        return int(cur.lastrowid)

    def update(self, mode_id: int, **fields: Any) -> None:
        allowed = {"name", "description", "conditions", "status", "approved_at"}
        if set(fields) - allowed:
            raise ValueError(f"바꿀 수 없는 모드 항목: {sorted(set(fields) - allowed)}")
        if not fields:
            return
        fields["updated_at"] = utc_now_iso()
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE mode_definitions SET {sets} WHERE id = ?", (*fields.values(), mode_id))

    def get(self, mode_id: int) -> ModeRecord | None:
        row = self.conn.execute("SELECT * FROM mode_definitions WHERE id = ?", (mode_id,)).fetchone()
        return ModeRecord.model_validate(dict(row)) if row else None

    def list(self, *, active_only: bool = False) -> list[ModeRecord]:
        sql = "SELECT * FROM mode_definitions"
        if active_only:
            sql += " WHERE status = 'ACTIVE'"
        return [ModeRecord.model_validate(dict(r)) for r in self.conn.execute(sql + " ORDER BY name")]


class CaseRepository:
    EDITABLE = {"title", "status", "mode_id", "mode_confirmed", "operating_conditions", "normal_rationale", "symptom",
                "abnormal_criteria", "cause_status", "device_config", "fw_version", "user_description",
                "current_revision", "approved_at"}

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(self, case_type: str, title: str, **fields: Any) -> int:
        now = utc_now_iso()
        values = {"case_type": case_type, "title": title, "created_at": now, "updated_at": now, **fields}
        cur = self.conn.execute(
            f"INSERT INTO cases ({', '.join(values)}) VALUES ({placeholders(list(values))})", tuple(values.values()))
        return int(cur.lastrowid)

    def update(self, case_id: int, **fields: Any) -> None:
        unknown = set(fields) - self.EDITABLE
        if unknown:
            raise ValueError(f"바꿀 수 없는 사례 항목: {sorted(unknown)}")
        if not fields:
            return
        fields["updated_at"] = utc_now_iso()
        values = [int(v) if isinstance(v, bool) else v for v in fields.values()]
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE cases SET {sets} WHERE id = ?", (*values, case_id))

    def get(self, case_id: int) -> CaseRecord | None:
        row = self.conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        return CaseRecord.model_validate(dict(row)) if row else None

    def list(self, *, case_type: str | None = None, statuses: Sequence[str] | None = None) -> list[CaseRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if case_type:
            clauses.append("case_type = ?")
            params.append(case_type)
        if statuses:
            clauses.append(f"status IN ({placeholders(statuses)})")
            params.extend(statuses)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(f"SELECT * FROM cases{where} ORDER BY id DESC", tuple(params))
        return [CaseRecord.model_validate(dict(r)) for r in rows]

    def approved(self, *, case_type: str | None = None, mode_id: int | None = None) -> list[CaseRecord]:
        """확정 지식으로 쓸 사례. approved_cases 뷰만 읽는다 (§5.3)."""
        clauses: list[str] = []
        params: list[Any] = []
        if case_type:
            clauses.append("case_type = ?")
            params.append(case_type)
        if mode_id is not None:
            clauses.append("mode_id = ?")
            params.append(mode_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(f"SELECT * FROM approved_cases{where} ORDER BY id", tuple(params))
        return [CaseRecord.model_validate(dict(r)) for r in rows]

    def delete(self, case_id: int) -> None:
        self.conn.execute("DELETE FROM cases WHERE id = ?", (case_id,))

    def log_file_ids(self, case_id: int) -> list[int]:
        rows = self.conn.execute("SELECT log_file_id FROM case_log_files WHERE case_id = ? ORDER BY log_file_id",
                                 (case_id,))
        return [int(r[0]) for r in rows]

    def single_log_cases(self, log_file_id: int | None = None) -> list[CaseRecord]:
        """로그 파일 하나만 걸린 사례(로그 입력, CR-06). 보관·반려는 뺀다. log_file_id를 주면 그 파일의 것만."""
        params: list[Any] = []
        where = ""
        if log_file_id is not None:
            where = " AND l.log_file_id = ?"
            params.append(log_file_id)
        rows = self.conn.execute(
            "SELECT c.*, l.log_file_id AS single_log_file_id FROM cases c JOIN case_log_files l ON l.case_id = c.id "
            "WHERE c.status NOT IN ('ARCHIVED', 'REJECTED') "
            "AND (SELECT COUNT(*) FROM case_log_files x WHERE x.case_id = c.id) = 1" + where +
            " ORDER BY c.id DESC", tuple(params))
        return [CaseRecord.model_validate({k: r[k] for k in r.keys() if k != "single_log_file_id"}) for r in rows]

    def link_logs(self, case_id: int, log_file_ids: Iterable[int]) -> None:
        now = utc_now_iso()
        self.conn.executemany(
            "INSERT OR IGNORE INTO case_log_files (case_id, log_file_id, added_at) VALUES (?, ?, ?)",
            [(case_id, i, now) for i in log_file_ids],
        )

    def unlink_logs(self, case_id: int, log_file_ids: Iterable[int]) -> None:
        self.conn.executemany("DELETE FROM case_log_files WHERE case_id = ? AND log_file_id = ?",
                              [(case_id, i) for i in log_file_ids])


class NoteRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add(self, case_id: int, body: str) -> int:
        now = utc_now_iso()
        cur = self.conn.execute("INSERT INTO case_notes (case_id, body, created_at, updated_at) VALUES (?, ?, ?, ?)",
                                (case_id, body, now, now))
        return int(cur.lastrowid)

    def update(self, note_id: int, body: str) -> None:
        self.conn.execute("UPDATE case_notes SET body = ?, updated_at = ? WHERE id = ?", (body, utc_now_iso(), note_id))

    def delete(self, note_id: int) -> None:
        self.conn.execute("DELETE FROM case_notes WHERE id = ?", (note_id,))

    def for_case(self, case_id: int) -> list[NoteRecord]:
        rows = self.conn.execute("SELECT * FROM case_notes WHERE case_id = ? ORDER BY id", (case_id,))
        return [NoteRecord.model_validate(dict(r)) for r in rows]


def _pattern(row: sqlite3.Row) -> PatternRecord:
    data = dict(row)
    data["definition"] = json.loads(data.pop("definition_json"))
    return PatternRecord.model_validate(data)


class PatternRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add(self, mode_id: int, candidate: PatternCandidate, *, source_case_id: int | None, source: str = "RULE",
            basis: str = "OBSERVED", basis_note: str | None = None, status: str = "PROPOSED") -> int:
        now = utc_now_iso()
        cur = self.conn.execute(
            "INSERT INTO event_patterns (mode_id, source_case_id, pattern_type, definition_json, basis, basis_note, "
            "source, status, observed_count, approved_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mode_id, source_case_id, candidate.pattern_type, _json(candidate.definition), basis,
             basis_note or candidate.note, source, status, candidate.observed_count,
             now if status == "APPROVED" else None, now, now),
        )
        return int(cur.lastrowid)

    def get(self, pattern_id: int) -> PatternRecord | None:
        row = self.conn.execute("SELECT * FROM event_patterns WHERE id = ?", (pattern_id,)).fetchone()
        return _pattern(row) if row else None

    def for_case(self, case_id: int) -> list[PatternRecord]:
        rows = self.conn.execute("SELECT * FROM event_patterns WHERE source_case_id = ? ORDER BY id", (case_id,))
        return [_pattern(r) for r in rows]

    def delete_proposed_for_case(self, case_id: int) -> int:
        cur = self.conn.execute("DELETE FROM event_patterns WHERE source_case_id = ? AND status = 'PROPOSED'",
                                (case_id,))
        return cur.rowcount

    def set_status(self, pattern_id: int, status: str) -> None:
        now = utc_now_iso()
        self.conn.execute(
            "UPDATE event_patterns SET status = ?, approved_at = CASE WHEN ? = 'APPROVED' THEN ? ELSE approved_at END, "
            "updated_at = ? WHERE id = ?",
            (status, status, now, now, pattern_id),
        )

    def approved_for_mode(self, mode_id: int) -> list[PatternRecord]:
        rows = self.conn.execute("SELECT * FROM approved_event_patterns WHERE mode_id = ? ORDER BY id", (mode_id,))
        return [_pattern(r) for r in rows]


EVIDENCE_FIELDS = ("evidence_type", "stance", "event_id", "log_file_id", "line_start", "line_end",
                   "guide_attachment_id", "span_start", "span_end", "answer_id", "ref_case_id", "note_id",
                   "analysis_evidence_id")


class ClaimRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add(self, *, claim_text: str, claim_kind: str, source: str, status: str = "PROPOSED",
            case_id: int | None = None, guide_item_id: int | None = None, pattern_id: int | None = None,
            analysis_run_id: int | None = None, evidence: Sequence[EvidenceRef] = ()) -> int:
        now = utc_now_iso()
        cur = self.conn.execute(
            "INSERT INTO knowledge_claims (case_id, guide_item_id, pattern_id, analysis_run_id, claim_text, claim_kind, "
            "source, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (case_id, guide_item_id, pattern_id, analysis_run_id, claim_text, claim_kind, source, status, now, now),
        )
        claim_id = int(cur.lastrowid)
        for ref in evidence:
            values = [getattr(ref, f) for f in EVIDENCE_FIELDS]
            self.conn.execute(
                f"INSERT INTO claim_evidence (claim_id, {', '.join(EVIDENCE_FIELDS)}, created_at) "
                f"VALUES (?, {placeholders(EVIDENCE_FIELDS)}, ?)",
                (claim_id, *values, now),
            )
        return claim_id

    def for_case(self, case_id: int, *, source: str | None = None) -> list[ClaimRecord]:
        sql = "SELECT * FROM knowledge_claims WHERE case_id = ?"
        params: list[Any] = [case_id]
        if source:
            sql += " AND source = ?"
            params.append(source)
        return self._with_evidence(self.conn.execute(sql + " ORDER BY id", params).fetchall())

    def for_run(self, run_id: int) -> list[ClaimRecord]:
        rows = self.conn.execute("SELECT * FROM knowledge_claims WHERE analysis_run_id = ? ORDER BY id", (run_id,))
        return self._with_evidence(rows.fetchall())

    def delete_proposed_for_case(self, case_id: int, source: str) -> None:
        self.conn.execute("DELETE FROM knowledge_claims WHERE case_id = ? AND source = ? AND status = 'PROPOSED'",
                          (case_id, source))

    def set_status(self, claim_id: int, status: str) -> None:
        self.conn.execute("UPDATE knowledge_claims SET status = ?, updated_at = ? WHERE id = ?",
                          (status, utc_now_iso(), claim_id))

    def _with_evidence(self, rows: list[sqlite3.Row]) -> list[ClaimRecord]:
        claims = [ClaimRecord.model_validate(dict(r)) for r in rows]
        if not claims:
            return claims
        ids = [c.id for c in claims]
        by_claim: dict[int, list[EvidenceRef]] = {i: [] for i in ids}
        for row in self.conn.execute(
            f"SELECT * FROM claim_evidence WHERE claim_id IN ({placeholders(ids)}) ORDER BY id", tuple(ids)
        ):
            by_claim[row["claim_id"]].append(EvidenceRef.model_validate(dict(row)))
        for claim in claims:
            claim.evidence = by_claim[claim.id]
        return claims


class GuideRepository:
    """진단 가이드·대응 매뉴얼 첨부와 구조화 항목 (Phase 4, CR-01)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add_attachment(self, case_id: int, kind: str, **fields: Any) -> int:
        fields.setdefault("imported_at", utc_now_iso())
        values = {"case_id": case_id, "attachment_kind": kind, **fields}
        cur = self.conn.execute(
            f"INSERT INTO guide_attachments ({', '.join(values)}) VALUES ({placeholders(list(values))})",
            tuple(values.values()),
        )
        return int(cur.lastrowid)

    def attachments(self, case_id: int, kind: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM guide_attachments WHERE case_id = ?"
        params: list[Any] = [case_id]
        if kind:
            sql += " AND attachment_kind = ?"
            params.append(kind)
        return self.conn.execute(sql + " ORDER BY id", params).fetchall()

    def attachment(self, attachment_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM guide_attachments WHERE id = ?", (attachment_id,)).fetchone()

    def add_item(self, case_id: int, *, section: str, content: str, source_kind: str, attachment_id: int | None,
                 span_start: int | None = None, span_end: int | None = None, sort_order: int = 0,
                 status: str = "PROPOSED") -> int:
        now = utc_now_iso()
        cur = self.conn.execute(
            "INSERT INTO diagnostic_guides (case_id, attachment_id, section, content, source_kind, status, span_start, "
            "span_end, sort_order, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (case_id, attachment_id, section, content, source_kind, status, span_start, span_end, sort_order, now, now),
        )
        return int(cur.lastrowid)

    def items(self, case_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT g.*, a.attachment_kind, a.original_name AS attachment_name FROM diagnostic_guides g "
            "LEFT JOIN guide_attachments a ON a.id = g.attachment_id WHERE g.case_id = ? "
            "ORDER BY COALESCE(a.attachment_kind, ''), g.section, g.sort_order, g.id",
            (case_id,),
        ).fetchall()

    def update_item(self, item_id: int, **fields: Any) -> None:
        allowed = {"section", "content", "status", "sort_order"}
        if set(fields) - allowed:
            raise ValueError(f"바꿀 수 없는 가이드 항목: {sorted(set(fields) - allowed)}")
        if not fields:
            return
        fields["updated_at"] = utc_now_iso()
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE diagnostic_guides SET {sets} WHERE id = ?", (*fields.values(), item_id))

    def delete_proposed_items(self, case_id: int, source_kinds: Sequence[str]) -> None:
        self.conn.execute(
            f"DELETE FROM diagnostic_guides WHERE case_id = ? AND status = 'PROPOSED' "
            f"AND source_kind IN ({placeholders(source_kinds)})",
            (case_id, *source_kinds),
        )

    def delete_proposed_items_for_attachment(self, attachment_id: int) -> None:
        self.conn.execute("DELETE FROM diagnostic_guides WHERE attachment_id = ? AND status = 'PROPOSED'",
                          (attachment_id,))

    def approved_items_for_cases(self, case_ids: Sequence[int]) -> list[sqlite3.Row]:
        """확정 지식으로 쓸 항목. approved_diagnostic_guides 뷰만 읽는다."""
        if not case_ids:
            return []
        return self.conn.execute(
            f"SELECT g.*, a.attachment_kind, a.original_name AS attachment_name FROM approved_diagnostic_guides g "
            f"LEFT JOIN guide_attachments a ON a.id = g.attachment_id WHERE g.case_id IN ({placeholders(case_ids)}) "
            "ORDER BY g.case_id, g.section, g.sort_order, g.id",
            tuple(case_ids),
        ).fetchall()


class QuestionRepository:
    """확인 질문·답변 (Phase 5)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert(self, case_id: int, *, trigger_type: str, question_text: str, reason: str, priority: int,
               is_required: bool, dedup_key: str, generated_by: str, target_ref: str | None = None) -> tuple[int, bool]:
        """같은 dedup_key가 있으면 새로 만들지 않는다(반복 방지). (id, 새로 만들었나)."""
        row = self.conn.execute("SELECT id, status FROM learning_questions WHERE case_id = ? AND dedup_key = ?",
                                (case_id, dedup_key)).fetchone()
        now = utc_now_iso()
        if row is not None:
            if row["status"] == "OBSOLETE":
                self.conn.execute(
                    "UPDATE learning_questions SET status = 'OPEN', question_text = ?, reason = ?, priority = ?, "
                    "is_required = ?, updated_at = ? WHERE id = ?",
                    (question_text, reason, priority, int(is_required), now, row["id"]),
                )
            return int(row["id"]), False
        cur = self.conn.execute(
            "INSERT INTO learning_questions (case_id, trigger_type, question_text, reason, priority, is_required, "
            "target_ref, dedup_key, generated_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (case_id, trigger_type, question_text, reason, priority, int(is_required), target_ref, dedup_key,
             generated_by, now, now),
        )
        return int(cur.lastrowid), True

    def for_case(self, case_id: int, statuses: Sequence[str] | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM learning_questions WHERE case_id = ?"
        params: list[Any] = [case_id]
        if statuses:
            sql += f" AND status IN ({placeholders(statuses)})"
            params.extend(statuses)
        return self.conn.execute(sql + " ORDER BY is_required DESC, priority DESC, id", params).fetchall()

    def get(self, question_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM learning_questions WHERE id = ?", (question_id,)).fetchone()

    def set_status(self, question_id: int, status: str) -> None:
        self.conn.execute("UPDATE learning_questions SET status = ?, updated_at = ? WHERE id = ?",
                          (status, utc_now_iso(), question_id))

    def add_answer(self, question_id: int, kind: str, text: str | None, structured: Any = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO learning_answers (question_id, answer_kind, answer_text, structured_json, answered_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (question_id, kind, text, _json(structured), utc_now_iso()),
        )
        return int(cur.lastrowid)

    def answers(self, question_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM learning_answers WHERE question_id = ? ORDER BY id",
                                 (question_id,)).fetchall()

    def open_required_count(self, case_id: int) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM learning_questions WHERE case_id = ? AND status = 'OPEN' AND is_required = 1",
            (case_id,),
        ).fetchone()[0])


class SearchRepository:
    """FTS5 이벤트 검색 (마이그레이션 0003)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @staticmethod
    def to_query(text: str) -> str:
        tokens = [t.replace('"', '""') for t in text.split() if t.strip()]
        return " OR ".join(f'"{t}"' for t in tokens)

    def search(self, text: str, log_file_ids: Sequence[int] | None = None, limit: int = 200) -> list[EventRecord]:
        query = self.to_query(text)
        if not query:
            return []
        sql = (f"SELECT {', '.join('e.' + c.strip() for c in EVENT_COLUMNS.split(','))} FROM events_fts f "
               "JOIN events e ON e.id = f.rowid WHERE events_fts MATCH ?")
        params: list[Any] = [query]
        if log_file_ids is not None:
            if not log_file_ids:
                return []
            sql += f" AND e.log_file_id IN ({placeholders(log_file_ids)})"
            params.extend(log_file_ids)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        return [EventRecord.model_validate(dict(r)) for r in self.conn.execute(sql, params)]

    def rebuild(self) -> None:
        self.conn.execute("INSERT INTO events_fts (events_fts) VALUES ('rebuild')")


class AnalysisRepository:
    """분석 실행 이력과 근거 (Phase 6~8)."""

    AXES = {"status", "mode_id", "mode_confirmed", "operating_conditions", "user_description", "normal_match",
            "anomaly_level", "similar_abnormal", "evidence_sufficiency", "response_guidance", "ai_status", "ai_model",
            "report_json_path", "report_html_path", "error_message", "finished_at"}

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(self, *, app_version: str, log_file_ids: Sequence[int], **fields: Any) -> int:
        values = {"app_version": app_version, "started_at": utc_now_iso(), **fields}
        values = {k: (int(v) if isinstance(v, bool) else v) for k, v in values.items()}
        cur = self.conn.execute(
            f"INSERT INTO analysis_runs ({', '.join(values)}) VALUES ({placeholders(list(values))})",
            tuple(values.values()),
        )
        run_id = int(cur.lastrowid)
        self.conn.executemany("INSERT INTO analysis_run_files (run_id, log_file_id) VALUES (?, ?)",
                              [(run_id, i) for i in log_file_ids])
        return run_id

    def update(self, run_id: int, **fields: Any) -> None:
        if set(fields) - self.AXES:
            raise ValueError(f"바꿀 수 없는 분석 항목: {sorted(set(fields) - self.AXES)}")
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        values = [int(v) if isinstance(v, bool) else v for v in fields.values()]
        self.conn.execute(f"UPDATE analysis_runs SET {sets} WHERE id = ?", (*values, run_id))

    def add_evidence(self, run_id: int, *, evidence_key: str, layer: str, finding_type: str, summary: str,
                     stance: str = "NEUTRAL", log_file_id: int | None = None, event_id: int | None = None,
                     line_start: int | None = None, line_end: int | None = None, case_id: int | None = None,
                     pattern_id: int | None = None, guide_item_id: int | None = None, similarity: float | None = None,
                     detail: Any = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO analysis_evidence (run_id, evidence_key, layer, finding_type, stance, log_file_id, event_id, "
            "line_start, line_end, case_id, pattern_id, guide_item_id, similarity, summary, detail_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, evidence_key, layer, finding_type, stance, log_file_id, event_id, line_start, line_end, case_id,
             pattern_id, guide_item_id, similarity, summary, _json(detail), utc_now_iso()),
        )
        return int(cur.lastrowid)

    def get(self, run_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM analysis_runs WHERE id = ?", (run_id,)).fetchone()

    def list(self, limit: int = 200) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM analysis_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def files(self, run_id: int) -> list[int]:
        return [int(r[0]) for r in self.conn.execute(
            "SELECT log_file_id FROM analysis_run_files WHERE run_id = ? ORDER BY log_file_id", (run_id,))]

    def evidence(self, run_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM analysis_evidence WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()


class RevisionRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add(self, case_id: int, revision_no: int, snapshot: dict[str, Any], change_summary: str | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO case_revisions (case_id, revision_no, snapshot_json, change_summary, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (case_id, revision_no, _json(snapshot), change_summary, utc_now_iso()),
        )
        return int(cur.lastrowid)

    def for_case(self, case_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM case_revisions WHERE case_id = ? ORDER BY revision_no",
                                 (case_id,)).fetchall()


REFERENCE_ENTRY_FIELDS = ("code", "title", "description", "cause", "action", "locator", "source_text")


class ReferenceRepository:
    """참고 자료(CR-04). 분석·AI는 approved_entries()(approved_reference_entries 뷰)만 쓴다."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add_document(self, *, title: str, original_name: str, source_path: str | None, stored_path: str, sha256: str,
                     size_bytes: int, fmt: str, column_map: Any = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO reference_documents (title, original_name, source_path, stored_path, sha256, size_bytes, format,"
            " column_map_json, imported_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (title, original_name, source_path, stored_path, sha256, size_bytes, fmt,
             json.dumps(column_map, ensure_ascii=False) if column_map else None, utc_now_iso()))
        return int(cur.lastrowid)

    def document(self, document_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT d.*, (SELECT COUNT(*) FROM reference_entries e WHERE e.document_id = d.id AND e.status = 'INCLUDED')"
            " AS included_count, (SELECT COUNT(*) FROM reference_passages p WHERE p.document_id = d.id) AS passage_count"
            " FROM reference_documents d WHERE d.id = ?", (document_id,)).fetchone()

    def by_sha256(self, sha256: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM reference_documents WHERE sha256 = ?", (sha256,)).fetchone()

    def documents(self, statuses: Sequence[str] | None = None) -> list[sqlite3.Row]:
        where = f" WHERE d.status IN ({placeholders(statuses)})" if statuses else ""
        return self.conn.execute(
            "SELECT d.*, (SELECT COUNT(*) FROM reference_entries e WHERE e.document_id = d.id AND e.status = 'INCLUDED')"
            f" AS included_count, (SELECT COUNT(*) FROM reference_passages p WHERE p.document_id = d.id) AS passage_count"
            f" FROM reference_documents d{where} ORDER BY d.id DESC", tuple(statuses or ())).fetchall()

    def approved_with_title(self, title: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM reference_documents WHERE status = 'APPROVED' AND title = ?",
                                 (title,)).fetchall()

    def update_document(self, document_id: int, **fields: Any) -> None:
        if "column_map" in fields:
            value = fields.pop("column_map")
            fields["column_map_json"] = json.dumps(value, ensure_ascii=False) if value else None
        if not fields:
            return
        sets = ", ".join(f"{name} = ?" for name in fields)
        self.conn.execute(f"UPDATE reference_documents SET {sets} WHERE id = ?", (*fields.values(), document_id))

    def delete_document(self, document_id: int) -> None:
        self.conn.execute("DELETE FROM reference_documents WHERE id = ?", (document_id,))

    def add_entry(self, document_id: int, *, code: str, code_norm: str, source_kind: str, sort_order: int = 0,
                  **fields: Any) -> int:
        values = {name: fields.get(name) for name in REFERENCE_ENTRY_FIELDS if name != "code"}
        now = utc_now_iso()
        cur = self.conn.execute(
            "INSERT INTO reference_entries (document_id, code, code_norm, title, description, cause, action, locator,"
            " source_text, source_kind, sort_order, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (document_id, code, code_norm, values["title"], values["description"], values["cause"], values["action"],
             values["locator"], values["source_text"], source_kind, sort_order, now, now))
        return int(cur.lastrowid)

    def entries(self, document_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM reference_entries WHERE document_id = ? ORDER BY sort_order, id",
                                 (document_id,)).fetchall()

    def entry(self, entry_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM reference_entries WHERE id = ?", (entry_id,)).fetchone()

    def update_entry(self, entry_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = utc_now_iso()
        sets = ", ".join(f"{name} = ?" for name in fields)
        self.conn.execute(f"UPDATE reference_entries SET {sets} WHERE id = ?", (*fields.values(), entry_id))

    def delete_entries(self, document_id: int, source_kind: str | None = None) -> None:
        if source_kind is None:
            self.conn.execute("DELETE FROM reference_entries WHERE document_id = ?", (document_id,))
        else:
            self.conn.execute("DELETE FROM reference_entries WHERE document_id = ? AND source_kind = ?",
                              (document_id, source_kind))

    def delete_entry(self, entry_id: int) -> None:
        self.conn.execute("DELETE FROM reference_entries WHERE id = ?", (entry_id,))

    def approved_entries(self, code_norms: Sequence[str] | None = None) -> list[sqlite3.Row]:
        """확정 지식으로 쓸 항목. approved_reference_entries 뷰만 읽는다."""
        if code_norms is None:
            return self.conn.execute("SELECT * FROM approved_reference_entries ORDER BY document_id, sort_order, id").fetchall()
        if not code_norms:
            return []
        return self.conn.execute(
            f"SELECT * FROM approved_reference_entries WHERE code_norm IN ({placeholders(code_norms)})"
            " ORDER BY code_norm, document_id, sort_order, id", tuple(code_norms)).fetchall()


class AiQuestionRepository:
    """LLM 질문(CR-07). 분석·AI가 지식으로 쓰는 답은 confirmed(): confirmed_ai_answers 뷰만 읽는다."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add(self, case_id: int, *, kind: str, question_text: str, reason: str | None, evidence: Any, model: str | None,
            dedup_key: str) -> int | None:
        """같은 사례·같은 질문(dedup_key)이 이미 답했거나 건너뛰었거나 열려 있으면 만들지 않고 None.
        예전에 OBSOLETE로 내린 같은 질문이면 다시 연다."""
        evidence_json = json.dumps(evidence or [], ensure_ascii=False)
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO ai_questions (case_id, kind, question_text, reason, evidence_json, model, dedup_key,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (case_id, kind, question_text, reason, evidence_json, model, dedup_key, utc_now_iso()))
        if cur.rowcount:
            return int(cur.lastrowid)
        row = self.conn.execute("SELECT id, status FROM ai_questions WHERE case_id = ? AND dedup_key = ?",
                                (case_id, dedup_key)).fetchone()
        if row is not None and row["status"] == "OBSOLETE":
            self.conn.execute("UPDATE ai_questions SET status = 'OPEN', kind = ?, question_text = ?, reason = ?,"
                              " evidence_json = ?, model = ? WHERE id = ?",
                              (kind, question_text, reason, evidence_json, model, row["id"]))
            return int(row["id"])
        return None

    def get(self, question_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM ai_questions WHERE id = ?", (question_id,)).fetchone()

    def for_cases(self, case_ids: Sequence[int] | None = None, statuses: Sequence[str] | None = None) -> list[sqlite3.Row]:
        clauses, params = [], []
        if case_ids is not None:
            if not case_ids:
                return []
            clauses.append(f"case_id IN ({placeholders(case_ids)})")
            params.extend(case_ids)
        if statuses:
            clauses.append(f"status IN ({placeholders(statuses)})")
            params.extend(statuses)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.conn.execute(f"SELECT * FROM ai_questions{where} ORDER BY id DESC", tuple(params)).fetchall()

    def obsolete_open(self, case_id: int) -> None:
        self.conn.execute("UPDATE ai_questions SET status = 'OBSOLETE' WHERE case_id = ? AND status = 'OPEN'", (case_id,))

    def set_answer(self, question_id: int, *, status: str, answer: str | None, answer_text: str | None) -> None:
        self.conn.execute("UPDATE ai_questions SET status = ?, answer = ?, answer_text = ?, answered_at = ? WHERE id = ?",
                          (status, answer, answer_text, utc_now_iso(), question_id))

    def confirmed(self, case_ids: Sequence[int]) -> list[sqlite3.Row]:
        if not case_ids:
            return []
        return self.conn.execute(
            f"SELECT * FROM confirmed_ai_answers WHERE case_id IN ({placeholders(case_ids)}) ORDER BY case_id, id",
            tuple(case_ids)).fetchall()


class ReferencePassageRepository:
    """참고 자료 일반 문서 문단(CR-07 REF-07). 분석·AI는 approved()(approved_reference_passages 뷰)만 쓴다."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add(self, document_id: int, *, body: str, locator: str | None, sort_order: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO reference_passages (document_id, sort_order, locator, body, created_at) VALUES (?, ?, ?, ?, ?)",
            (document_id, sort_order, locator, body, utc_now_iso()))
        return int(cur.lastrowid)

    def for_document(self, document_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM reference_passages WHERE document_id = ? ORDER BY sort_order, id",
                                 (document_id,)).fetchall()

    def count(self, document_id: int) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM reference_passages WHERE document_id = ?",
                                     (document_id,)).fetchone()[0])

    def approved(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM approved_reference_passages ORDER BY document_id, sort_order, id").fetchall()
