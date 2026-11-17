"""스키마 v1 제약 (docs/DATABASE.md). 데이터는 모두 가상 값이다.

DB-01~05, PRN-03·06·10, AL-09·11, MLG-01·02·07, NA-12, QE-03·07, CR-01
"""

from __future__ import annotations

import re
import sqlite3
from enum import StrEnum

import pytest

from app.models import enums as E

NOW = "2026-11-16T00:00:00.000Z"

REQUIRED_TABLES = {  # 계획서 §5.1
    "log_files", "collection_sessions", "events", "cases", "case_log_files", "case_events", "case_notes",
    "guide_attachments", "diagnostic_guides", "mode_definitions", "event_patterns", "learning_questions",
    "learning_answers", "knowledge_claims", "claim_evidence", "analysis_runs", "analysis_evidence",
    "case_revisions", "audit_history",
}
ADDED_TABLES = {"analysis_run_files", "schema_migrations"}  # REQUIREMENTS G-5

ENUM_COLUMNS: list[tuple[type[StrEnum], str, str]] = [
    (E.TimeSyncStatus, "collection_sessions", "time_sync_status"),
    (E.TimestampBasis, "log_files", "timestamp_basis"),
    (E.OriginPurpose, "log_files", "origin_purpose"),
    (E.EventTimeKind, "events", "ts_kind"),
    (E.ModeSource, "mode_definitions", "source"),
    (E.ModeStatus, "mode_definitions", "status"),
    (E.CaseType, "cases", "case_type"),
    (E.CaseStatus, "cases", "status"),
    (E.CauseStatus, "cases", "cause_status"),
    (E.CaseEventRole, "case_events", "role"),
    (E.ProducedBy, "case_events", "source"),
    (E.AttachmentKind, "guide_attachments", "attachment_kind"),
    (E.GuideFormat, "guide_attachments", "format"),
    (E.GuideSection, "diagnostic_guides", "section"),
    (E.GuideSourceKind, "diagnostic_guides", "source_kind"),
    (E.ItemStatus, "diagnostic_guides", "status"),
    (E.PatternType, "event_patterns", "pattern_type"),
    (E.PatternBasis, "event_patterns", "basis"),
    (E.ProducedBy, "event_patterns", "source"),
    (E.ItemStatus, "event_patterns", "status"),
    (E.QuestionTrigger, "learning_questions", "trigger_type"),
    (E.QuestionGenerator, "learning_questions", "generated_by"),
    (E.QuestionStatus, "learning_questions", "status"),
    (E.AnswerKind, "learning_answers", "answer_kind"),
    (E.AnalysisStatus, "analysis_runs", "status"),
    (E.NormalMatch, "analysis_runs", "normal_match"),
    (E.AnomalyLevel, "analysis_runs", "anomaly_level"),
    (E.SimilarAbnormal, "analysis_runs", "similar_abnormal"),
    (E.EvidenceSufficiency, "analysis_runs", "evidence_sufficiency"),
    (E.ResponseGuidance, "analysis_runs", "response_guidance"),
    (E.AiStatus, "analysis_runs", "ai_status"),
    (E.AnalysisLayer, "analysis_evidence", "layer"),
    (E.FindingType, "analysis_evidence", "finding_type"),
    (E.FindingStance, "analysis_evidence", "stance"),
    (E.ClaimKind, "knowledge_claims", "claim_kind"),
    (E.ClaimSource, "knowledge_claims", "source"),
    (E.ClaimStatus, "knowledge_claims", "status"),
    (E.EvidenceType, "claim_evidence", "evidence_type"),
    (E.ClaimStance, "claim_evidence", "stance"),
    (E.AuditAction, "audit_history", "action"),
    (E.Actor, "audit_history", "actor"),
]


def check_values(conn: sqlite3.Connection, table: str, column: str) -> set[str]:
    """열 정의의 `column IN (...)` 목록을 읽는다(테이블 SQL에서 처음 나오는 것)."""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()[0]
    match = re.search(rf"\b{column}\s+IN\s*\(([^)]*)\)", sql)
    assert match, f"{table}.{column}에 CHECK IN 목록이 없다"
    return {value.strip().strip("'") for value in match.group(1).split(",")}


def raises_integrity(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> str:
    with pytest.raises(sqlite3.IntegrityError) as info:
        conn.execute(sql, params)
    return str(info.value)


# ── 구조 ──
def test_required_tables_exist(db: sqlite3.Connection) -> None:
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert REQUIRED_TABLES <= tables, REQUIRED_TABLES - tables
    assert ADDED_TABLES <= tables
    assert len(REQUIRED_TABLES) == 19


def test_all_tables_are_strict(db: sqlite3.Connection) -> None:
    rows = db.execute("SELECT name, strict FROM pragma_table_list WHERE schema = 'main' AND type = 'table'")
    loose = [r["name"] for r in rows if not r["strict"] and not r["name"].startswith("sqlite_")]
    assert loose == []


@pytest.mark.parametrize("enum_cls, table, column", ENUM_COLUMNS, ids=lambda v: getattr(v, "__name__", str(v)))
def test_enum_matches_db_check(db: sqlite3.Connection, enum_cls: type[StrEnum], table: str, column: str) -> None:
    assert {m.value for m in enum_cls} == check_values(db, table, column)


def test_case_status_has_plan_values() -> None:  # 계획서 §5.3
    assert [s.value for s in E.CaseStatus] == [
        "DRAFT", "NEEDS_INFORMATION", "READY_FOR_REVIEW", "APPROVED", "REJECTED", "ARCHIVED",
    ]


def test_question_triggers_cover_plan_conditions() -> None:  # 계획서 §7.2
    assert len(E.QuestionTrigger) == 9


def test_foreign_keys_are_enforced(db: sqlite3.Connection) -> None:
    message = raises_integrity(
        db, "INSERT INTO case_notes (case_id, body, created_at, updated_at) VALUES (999, 'x', ?, ?)", (NOW, NOW)
    )
    assert "FOREIGN KEY" in message


# ── 복수 로그 · 세션 (§4, §5.2) ──
def test_case_with_logs_from_two_sessions(db: sqlite3.Connection, factory) -> None:
    s1, s2 = factory.session(label="같은 시점 수집"), factory.session(label="재현 수집")
    f1, f2, f3 = factory.log_file(s1), factory.log_file(s1), factory.log_file(s2)
    case_id = factory.case("ABNORMAL")
    for f in (f1, f2, f3):
        factory.insert("case_log_files", case_id=case_id, log_file_id=f, added_at=NOW)
    sessions = db.execute(
        """SELECT lf.session_id, COUNT(*) AS n FROM case_log_files c JOIN log_files lf ON lf.id = c.log_file_id
           WHERE c.case_id = ? GROUP BY lf.session_id ORDER BY lf.session_id""",
        (case_id,),
    ).fetchall()
    assert [(r["session_id"], r["n"]) for r in sessions] == [(s1, 2), (s2, 1)]


def test_log_file_requires_session(db: sqlite3.Connection, factory) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        factory.log_file(session_id=999)


def test_clock_offset_requires_basis(factory) -> None:  # §4.3
    with pytest.raises(sqlite3.IntegrityError):
        factory.log_file(clock_offset_ms=120)
    factory.log_file(clock_offset_ms=120, offset_basis="동기화 이벤트 TEST-SYNC 기준")


def test_event_timestamp_kind_needs_value(factory) -> None:
    f = factory.log_file()
    with pytest.raises(sqlite3.IntegrityError):
        factory.event(f, seq=0, ts_kind="ABSOLUTE")
    factory.event(f, seq=0, ts_kind="ABSOLUTE", ts_value_us=1_700_000_000_000_000, ts_raw="raw")
    factory.event(f, seq=1)  # 타임스탬프 없음 → 파일 안 순서만


def test_event_seq_unique_per_file(factory) -> None:
    f = factory.log_file()
    factory.event(f, seq=0)
    with pytest.raises(sqlite3.IntegrityError):
        factory.event(f, seq=0)


# ── 원본 보존 (§1.3, §9.1) ──
def test_duplicate_file_hash_rejected(factory) -> None:
    factory.log_file(sha256="a" * 64)
    with pytest.raises(sqlite3.IntegrityError):
        factory.log_file(sha256="a" * 64)


@pytest.mark.parametrize("bad", ["A" * 64, "g" * 64, "a" * 63])
def test_sha256_format(factory, bad: str) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        factory.log_file(sha256=bad)


@pytest.mark.parametrize(
    "column, value",
    [("sha256", "b" * 64), ("stored_path", "elsewhere.log"), ("size_bytes", 1), ("original_name", "x.log")],
)
def test_log_file_identity_is_immutable(db: sqlite3.Connection, factory, column: str, value) -> None:
    f = factory.log_file()
    with pytest.raises(sqlite3.IntegrityError, match="바꿀 수 없습니다"):
        db.execute(f"UPDATE log_files SET {column} = ? WHERE id = ?", (value, f))


def test_log_file_metadata_is_editable(db: sqlite3.Connection, factory) -> None:
    f = factory.log_file()
    db.execute("UPDATE log_files SET user_memo = '메모', log_role = 'TEST-ROLE', fw_version = 'T1' WHERE id = ?", (f,))
    assert db.execute("SELECT user_memo FROM log_files WHERE id = ?", (f,)).fetchone()[0] == "메모"


def test_log_file_linked_to_case_cannot_be_deleted(db: sqlite3.Connection, factory) -> None:
    f = factory.log_file()
    c = factory.case()
    factory.insert("case_log_files", case_id=c, log_file_id=f, added_at=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM log_files WHERE id = ?", (f,))


# ── 사례 상태 · 승인 (§3.2, §5.3) ──
def test_invalid_case_status_rejected(factory) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        factory.case(status="DONE")


def test_abnormal_approval_requires_symptom_and_criteria(factory) -> None:  # AL-09
    with pytest.raises(sqlite3.IntegrityError):
        factory.case("ABNORMAL", status="APPROVED", approved_at=NOW, current_revision=1, symptom="증상만")
    with pytest.raises(sqlite3.IntegrityError):
        factory.case("ABNORMAL", status="APPROVED", approved_at=NOW, current_revision=1,
                     symptom="  ", abnormal_criteria="기준")
    factory.approved_case("ABNORMAL")


def test_unconfirmed_cause_can_be_approved(factory) -> None:  # 원인 미확정 사례도 등록 가능
    factory.approved_case("ABNORMAL", cause_status="UNKNOWN")


def test_normal_case_has_no_abnormal_fields(factory) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        factory.case("NORMAL", symptom="정상 사례에 증상")


def test_approved_requires_timestamp_and_revision(factory) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        factory.case(status="APPROVED")
    with pytest.raises(sqlite3.IntegrityError):
        factory.case(status="APPROVED", approved_at=NOW, current_revision=0)


@pytest.mark.parametrize("status", ["NEEDS_INFORMATION", "READY_FOR_REVIEW", "ARCHIVED"])
def test_only_draft_or_rejected_cases_can_be_deleted(db: sqlite3.Connection, factory, status: str) -> None:
    c = factory.case(status=status)
    with pytest.raises(sqlite3.IntegrityError, match="ARCHIVED"):
        db.execute("DELETE FROM cases WHERE id = ?", (c,))
    draft = factory.case()
    db.execute("DELETE FROM cases WHERE id = ?", (draft,))


def test_case_with_revision_cannot_be_deleted_even_as_draft(db: sqlite3.Connection, factory) -> None:
    c = factory.case()
    factory.insert("case_revisions", case_id=c, revision_no=1, snapshot_json="{}", created_at=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM cases WHERE id = ?", (c,))


def test_revisions_are_append_only(db: sqlite3.Connection, factory) -> None:  # PRN-10, DB-05
    c = factory.case()
    r = factory.insert("case_revisions", case_id=c, revision_no=1, snapshot_json='{"v": 1}', created_at=NOW)
    factory.insert("case_revisions", case_id=c, revision_no=2, snapshot_json='{"v": 2}', created_at=NOW)
    with pytest.raises(sqlite3.IntegrityError, match="고칠 수 없습니다"):
        db.execute("UPDATE case_revisions SET snapshot_json = '{}' WHERE id = ?", (r,))
    with pytest.raises(sqlite3.IntegrityError, match="지울 수 없습니다"):
        db.execute("DELETE FROM case_revisions WHERE id = ?", (r,))
    with pytest.raises(sqlite3.IntegrityError):
        factory.insert("case_revisions", case_id=c, revision_no=2, snapshot_json="{}", created_at=NOW)


def test_audit_history_is_append_only(db: sqlite3.Connection, factory) -> None:
    a = factory.insert("audit_history", entity_type="case", entity_id=1, action="APPROVE", actor="USER", occurred_at=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE audit_history SET note = 'x' WHERE id = ?", (a,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM audit_history WHERE id = ?", (a,))


def test_json_columns_must_be_valid(factory) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        factory.insert("audit_history", entity_type="case", action="UPDATE", actor="USER",
                       after_json="{not json", occurred_at=NOW)


# ── 승인된 지식만 분석에 (§5.3, §12.5) ──
def test_approved_views_exclude_unapproved(db: sqlite3.Connection, factory) -> None:  # PRN-06, DB-04
    mode = factory.mode()
    draft = factory.case()
    approved = factory.approved_case()
    factory.pattern(mode, source_case_id=draft, status="APPROVED", approved_at=NOW)  # 사례가 초안
    keep = factory.pattern(mode, source_case_id=approved, status="APPROVED", approved_at=NOW)
    factory.pattern(mode, source_case_id=approved)  # 패턴이 제안 상태
    user_defined = factory.pattern(mode, status="APPROVED", approved_at=NOW, basis="USER_DEFINED", source="USER")

    assert [r[0] for r in db.execute("SELECT id FROM approved_cases")] == [approved]
    assert sorted(r[0] for r in db.execute("SELECT id FROM approved_event_patterns")) == sorted([keep, user_defined])


def test_approved_guides_view(db: sqlite3.Connection, factory) -> None:
    draft = factory.case("ABNORMAL")
    approved = factory.approved_case("ABNORMAL")
    g_draft = factory.guide_attachment(draft)
    g_ok = factory.guide_attachment(approved)
    factory.guide_item(draft, g_draft, status="APPROVED")
    ok = factory.guide_item(approved, g_ok, status="APPROVED")
    factory.guide_item(approved, g_ok, status="PROPOSED")
    assert [r[0] for r in db.execute("SELECT id FROM approved_diagnostic_guides")] == [ok]


# ── 패턴 · 모드 (§3.1, §6.1) ──
def test_pattern_requires_mode(factory) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        factory.pattern(mode_id=None)


def test_specification_basis_needs_note(factory) -> None:  # 관찰 패턴과 사양 조건 구분
    mode = factory.mode()
    with pytest.raises(sqlite3.IntegrityError):
        factory.pattern(mode, basis="SPECIFICATION")
    factory.pattern(mode, basis="SPECIFICATION", basis_note="사용자가 제공한 사양 문서 3장")


def test_ai_proposed_mode_needs_approval_to_be_active(factory) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        factory.mode(source="AI_PROPOSED", status="ACTIVE")
    factory.mode(source="AI_PROPOSED", status="PROPOSED")
    factory.mode(source="AI_PROPOSED", status="ACTIVE", approved_at=NOW)


# ── 진단 가이드 · 대응 매뉴얼 (§3.2, CR-01) ──
def test_guide_and_optional_manual_on_abnormal_case(db: sqlite3.Connection, factory) -> None:
    case_id = factory.approved_case("ABNORMAL")
    guide = factory.guide_attachment(case_id, "DIAGNOSTIC_GUIDE")
    manual = factory.guide_attachment(case_id, "RESPONSE_MANUAL")
    factory.guide_item(case_id, guide, section="RESOLUTION", status="APPROVED")
    for order, section in enumerate(["RESPONSE_STEP", "RESPONSE_STEP", "PRECAUTION", "ESCALATION"]):
        factory.guide_item(case_id, manual, section=section, sort_order=order, status="APPROVED")
    rows = db.execute(
        """SELECT a.attachment_kind, COUNT(*) FROM approved_diagnostic_guides g
           JOIN guide_attachments a ON a.id = g.attachment_id GROUP BY a.attachment_kind ORDER BY 1"""
    ).fetchall()
    assert [tuple(r) for r in rows] == [("DIAGNOSTIC_GUIDE", 1), ("RESPONSE_MANUAL", 4)]


def test_attachment_kind_is_required_and_immutable(db: sqlite3.Connection, factory) -> None:
    case_id = factory.case("ABNORMAL")
    with pytest.raises(sqlite3.IntegrityError):
        factory.guide_attachment(case_id, kind="MANUAL")
    a = factory.guide_attachment(case_id, "RESPONSE_MANUAL")
    with pytest.raises(sqlite3.IntegrityError, match="바꿀 수 없습니다"):
        db.execute("UPDATE guide_attachments SET attachment_kind = 'DIAGNOSTIC_GUIDE' WHERE id = ?", (a,))


def test_guide_text_item_needs_attachment(factory) -> None:
    case_id = factory.case("ABNORMAL")
    with pytest.raises(sqlite3.IntegrityError):
        factory.guide_item(case_id, None, source_kind="GUIDE_TEXT")
    factory.guide_item(case_id, None, source_kind="USER", section="RESOLUTION")


def test_no_error_code_to_solution_column() -> None:
    """오류 코드와 해결책을 일대일로 묶는 열이 없어야 한다 (AL-11)."""
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[2] / "app/storage/migrations/0001_initial_schema.sql").read_text("utf-8")
    assert not re.search(r"error_code\w*\s+TEXT[^\n]*\n\s*(solution|resolution|fix)", sql, re.IGNORECASE)
    assert "error_meaning" not in sql and "code_meaning" not in sql


# ── 질문 (§7) ──
def test_question_requires_reason_and_is_not_repeated(factory) -> None:  # QE-03, QE-07
    case_id = factory.case()
    base = dict(case_id=case_id, trigger_type="MODE_UNKNOWN", question_text="운전 모드는?",
                dedup_key="mode", generated_by="RULE", created_at=NOW, updated_at=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        factory.insert("learning_questions", **{**base, "reason": "  "})
    q = factory.insert("learning_questions", **{**base, "reason": "모드가 정해지지 않았다"})
    with pytest.raises(sqlite3.IntegrityError):
        factory.insert("learning_questions", **{**base, "reason": "같은 질문"})
    factory.insert("learning_answers", question_id=q, answer_kind="UNKNOWN", answered_at=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        factory.insert("learning_answers", question_id=q, answer_kind="TEXT", answered_at=NOW)


# ── 지식 주장 · 근거 ──
def test_claim_belongs_to_exactly_one_subject(factory) -> None:
    case_id = factory.case()
    run_id = factory.analysis_run()
    base = dict(claim_text="TEST 주장", claim_kind="HYPOTHESIS", source="AI", created_at=NOW, updated_at=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        factory.insert("knowledge_claims", **base)
    with pytest.raises(sqlite3.IntegrityError):
        factory.insert("knowledge_claims", case_id=case_id, analysis_run_id=run_id, **base)
    factory.insert("knowledge_claims", case_id=case_id, **base)


def test_claim_evidence_needs_matching_reference(factory) -> None:
    case_id = factory.case()
    claim = factory.insert("knowledge_claims", case_id=case_id, claim_text="관측", claim_kind="FACT",
                           source="LOG_OBSERVATION", created_at=NOW, updated_at=NOW)
    f = factory.log_file()
    e = factory.event(f)
    with pytest.raises(sqlite3.IntegrityError):
        factory.insert("claim_evidence", claim_id=claim, evidence_type="EVENT", created_at=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        factory.insert("claim_evidence", claim_id=claim, evidence_type="LOG_LINES", log_file_id=f, created_at=NOW)
    factory.insert("claim_evidence", claim_id=claim, evidence_type="EVENT", event_id=e, created_at=NOW)
    factory.insert("claim_evidence", claim_id=claim, evidence_type="LOG_LINES", log_file_id=f,
                   line_start=1, line_end=3, stance="CONTRADICTS", created_at=NOW)


# ── 분석 결과 (§3.3, CR-01) ──
def test_analysis_result_axes_are_independent(db: sqlite3.Connection, factory) -> None:  # NA-12
    run = factory.analysis_run()
    row = db.execute(
        "SELECT normal_match, anomaly_level, similar_abnormal, evidence_sufficiency, response_guidance, ai_status "
        "FROM analysis_runs WHERE id = ?", (run,)
    ).fetchone()
    assert tuple(row) == ("NOT_EVALUATED",) * 5 + ("NOT_RUN",)
    db.execute(
        "UPDATE analysis_runs SET anomaly_level = 'RULE_VIOLATION', similar_abnormal = 'FOUND', "
        "normal_match = 'PARTIAL', evidence_sufficiency = 'PARTIAL', response_guidance = 'MANUAL' WHERE id = ?",
        (run,),
    )


def test_response_reference_points_to_guide_item(db: sqlite3.Connection, factory) -> None:  # CR-01
    case_id = factory.approved_case("ABNORMAL")
    manual = factory.guide_attachment(case_id, "RESPONSE_MANUAL")
    step = factory.guide_item(case_id, manual, section="RESPONSE_STEP", status="APPROVED")
    run = factory.analysis_run(response_guidance="MANUAL")
    factory.insert("analysis_evidence", run_id=run, evidence_key="EV-0001", layer="RETRIEVAL",
                   finding_type="RESPONSE_REFERENCE", case_id=case_id, guide_item_id=step,
                   summary="승인된 대응 절차 1단계", created_at=NOW)
    with pytest.raises(sqlite3.IntegrityError):  # 실행마다 근거 키는 고유
        factory.insert("analysis_evidence", run_id=run, evidence_key="EV-0001", layer="AI",
                       finding_type="AI_HYPOTHESIS", summary="중복 키", created_at=NOW)
