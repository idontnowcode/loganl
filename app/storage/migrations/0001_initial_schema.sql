-- 0001_initial_schema.sql
-- Local Log Analyzer 초기 스키마 (LLA-DEV-PLAN-001 v0.2 R01 §4·§5, docs/DATABASE.md)
-- 한 번 적용된 마이그레이션 파일은 고치지 않는다(체크섬 검사). 바꿀 일은 새 번호 파일로 만든다.
-- 시각은 모두 UTC ISO-8601 문자열(예: 2026-11-16T03:00:00.000Z)이다.

-- ─────────────────────────────── 수집 세션 · 원본 파일 · 이벤트 ───────────────────────────────

CREATE TABLE collection_sessions (
    id                INTEGER PRIMARY KEY,
    label             TEXT    NOT NULL,
    time_sync_status  TEXT    NOT NULL DEFAULT 'UNKNOWN'
        CHECK (time_sync_status IN ('SYNCED', 'OFFSET_KNOWN', 'UNSYNCED', 'UNKNOWN')),
    started_at        TEXT,
    ended_at          TEXT,
    device_config     TEXT,
    note              TEXT,
    created_at        TEXT    NOT NULL
) STRICT;

CREATE TABLE log_files (
    id                  INTEGER PRIMARY KEY,
    session_id          INTEGER NOT NULL REFERENCES collection_sessions(id) ON DELETE RESTRICT,
    original_name       TEXT    NOT NULL,
    source_path         TEXT,
    stored_path         TEXT    NOT NULL,
    sha256              TEXT    NOT NULL UNIQUE
        CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    size_bytes          INTEGER NOT NULL CHECK (size_bytes >= 0),
    encoding            TEXT,
    line_count          INTEGER CHECK (line_count IS NULL OR line_count >= 0),
    log_role            TEXT,
    collected_at        TEXT,
    timestamp_basis     TEXT    NOT NULL DEFAULT 'UNKNOWN'
        CHECK (timestamp_basis IN ('ABSOLUTE', 'ABSOLUTE_NO_TZ', 'RELATIVE', 'NONE', 'UNKNOWN')),
    clock_offset_ms     INTEGER,
    offset_basis        TEXT,
    time_uncertainty_ms INTEGER CHECK (time_uncertainty_ms IS NULL OR time_uncertainty_ms >= 0),
    error_ranges_json   TEXT    CHECK (error_ranges_json IS NULL OR json_valid(error_ranges_json)),
    fw_version          TEXT,
    user_memo           TEXT,
    parser_profile      TEXT,
    origin_purpose      TEXT    NOT NULL
        CHECK (origin_purpose IN ('NORMAL_CASE', 'ABNORMAL_CASE', 'ANALYSIS')),
    imported_at         TEXT    NOT NULL,
    -- 시계 오프셋은 동기화 근거가 있을 때만 기록한다 (§4.3)
    CHECK (clock_offset_ms IS NULL OR trim(coalesce(offset_basis, '')) <> '')
) STRICT;

CREATE INDEX idx_log_files_session ON log_files(session_id);

-- 원본 파일을 식별하는 값은 가져온 뒤 바꿀 수 없다 (§1.3 원본 로그 수정 금지)
CREATE TRIGGER log_files_identity_immutable
BEFORE UPDATE ON log_files
WHEN NEW.original_name IS NOT OLD.original_name
  OR NEW.stored_path   IS NOT OLD.stored_path
  OR NEW.sha256        IS NOT OLD.sha256
  OR NEW.size_bytes    IS NOT OLD.size_bytes
  OR NEW.origin_purpose IS NOT OLD.origin_purpose
  OR NEW.imported_at   IS NOT OLD.imported_at
BEGIN
    SELECT RAISE(ABORT, 'log_files: 원본 파일 식별 정보(파일명·저장 경로·SHA-256·크기)는 바꿀 수 없습니다');
END;

CREATE TABLE events (
    id              INTEGER PRIMARY KEY,
    log_file_id     INTEGER NOT NULL REFERENCES log_files(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL CHECK (seq >= 0),
    line_no         INTEGER NOT NULL CHECK (line_no >= 1),
    line_end        INTEGER,
    byte_offset     INTEGER CHECK (byte_offset IS NULL OR byte_offset >= 0),
    ts_raw          TEXT,
    ts_value_us     INTEGER,
    ts_kind         TEXT    NOT NULL DEFAULT 'NONE'
        CHECK (ts_kind IN ('ABSOLUTE', 'ABSOLUTE_NO_TZ', 'RELATIVE', 'NONE')),
    level           TEXT,
    component       TEXT,
    event_type      TEXT,
    error_code      TEXT,
    message         TEXT    NOT NULL,
    raw_text        TEXT    NOT NULL,
    attrs_json      TEXT    CHECK (attrs_json IS NULL OR json_valid(attrs_json)),
    parser_profile  TEXT,
    UNIQUE (log_file_id, seq),
    CHECK (line_end IS NULL OR line_end >= line_no),
    CHECK (ts_kind = 'NONE' OR ts_value_us IS NOT NULL)
) STRICT;

CREATE INDEX idx_events_file_line ON events(log_file_id, line_no);
CREATE INDEX idx_events_event_type ON events(event_type);
CREATE INDEX idx_events_error_code ON events(error_code);

-- ─────────────────────────────── 운전 모드 · 학습 사례 ───────────────────────────────

CREATE TABLE mode_definitions (
    id           INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL UNIQUE,
    description  TEXT,
    conditions   TEXT,
    source       TEXT    NOT NULL DEFAULT 'USER' CHECK (source IN ('USER', 'AI_PROPOSED')),
    status       TEXT    NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('PROPOSED', 'ACTIVE', 'RETIRED')),
    approved_at  TEXT,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    -- AI가 제안한 모드는 사용자가 승인해야 쓸 수 있다 (§1.3)
    CHECK (source = 'USER' OR status <> 'ACTIVE' OR approved_at IS NOT NULL)
) STRICT;

CREATE TABLE cases (
    id                    INTEGER PRIMARY KEY,
    case_type             TEXT    NOT NULL CHECK (case_type IN ('NORMAL', 'ABNORMAL')),
    title                 TEXT    NOT NULL,
    status                TEXT    NOT NULL DEFAULT 'DRAFT'
        CHECK (status IN ('DRAFT', 'NEEDS_INFORMATION', 'READY_FOR_REVIEW', 'APPROVED', 'REJECTED', 'ARCHIVED')),
    mode_id               INTEGER REFERENCES mode_definitions(id) ON DELETE RESTRICT,
    mode_confirmed        INTEGER NOT NULL DEFAULT 0 CHECK (mode_confirmed IN (0, 1)),
    operating_conditions  TEXT,
    normal_rationale      TEXT,
    symptom               TEXT,
    abnormal_criteria     TEXT,
    cause_status          TEXT    CHECK (cause_status IS NULL OR cause_status IN ('CONFIRMED', 'SUSPECTED', 'UNKNOWN')),
    device_config         TEXT,
    fw_version            TEXT,
    user_description      TEXT,
    current_revision      INTEGER NOT NULL DEFAULT 0 CHECK (current_revision >= 0),
    approved_at           TEXT,
    created_at            TEXT    NOT NULL,
    updated_at            TEXT    NOT NULL,
    -- 증상·판정 기준·원인 확정 여부는 비정상 사례에만 있다
    CHECK (case_type = 'ABNORMAL' OR (symptom IS NULL AND abnormal_criteria IS NULL AND cause_status IS NULL)),
    -- 비정상 사례 승인에는 오류 증상과 비정상 판정 기준이 필수다 (§3.2)
    CHECK (case_type <> 'ABNORMAL' OR status <> 'APPROVED'
           OR (trim(coalesce(symptom, '')) <> '' AND trim(coalesce(abnormal_criteria, '')) <> '')),
    -- 승인 상태면 승인 시각과 개정본(1 이상)이 있어야 한다
    CHECK (status <> 'APPROVED' OR (approved_at IS NOT NULL AND current_revision >= 1))
) STRICT;

CREATE INDEX idx_cases_type_status ON cases(case_type, status);
CREATE INDEX idx_cases_mode ON cases(mode_id);

-- 승인·검토 중인 사례는 지우지 않고 ARCHIVED로 바꾼다. 한 번이라도 승인된 사례는
-- case_revisions의 RESTRICT 외래 키 때문에 상태와 관계없이 지울 수 없다.
CREATE TRIGGER cases_delete_only_draft_or_rejected
BEFORE DELETE ON cases
WHEN OLD.status NOT IN ('DRAFT', 'REJECTED')
BEGIN
    SELECT RAISE(ABORT, 'cases: DRAFT·REJECTED 사례만 삭제할 수 있습니다. 나머지는 ARCHIVED로 바꾸세요');
END;

CREATE TABLE case_log_files (
    case_id      INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    log_file_id  INTEGER NOT NULL REFERENCES log_files(id) ON DELETE RESTRICT,
    added_at     TEXT    NOT NULL,
    note         TEXT,
    PRIMARY KEY (case_id, log_file_id)
) STRICT;

CREATE INDEX idx_case_log_files_file ON case_log_files(log_file_id);

CREATE TABLE case_events (
    id          INTEGER PRIMARY KEY,
    case_id     INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    event_id    INTEGER NOT NULL REFERENCES events(id) ON DELETE RESTRICT,
    role        TEXT    NOT NULL CHECK (role IN ('SYMPTOM', 'ERROR', 'CONTEXT', 'PATTERN_MEMBER', 'REFERENCE')),
    source      TEXT    NOT NULL CHECK (source IN ('USER', 'RULE', 'AI')),
    note        TEXT,
    created_at  TEXT    NOT NULL,
    UNIQUE (case_id, event_id, role)
) STRICT;

CREATE TABLE case_notes (
    id          INTEGER PRIMARY KEY,
    case_id     INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    body        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
) STRICT;

-- ─────────────────────────────── 진단 가이드 · 대응 매뉴얼 ───────────────────────────────
-- 비정상 사례에는 진단 가이드(필수, §3.2)와 대응 매뉴얼(선택, 변경 요청 CR-01)을 첨부한다.

CREATE TABLE guide_attachments (
    id               INTEGER PRIMARY KEY,
    case_id          INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    attachment_kind  TEXT    NOT NULL CHECK (attachment_kind IN ('DIAGNOSTIC_GUIDE', 'RESPONSE_MANUAL')),
    original_name    TEXT    NOT NULL,
    source_path    TEXT,
    stored_path    TEXT    NOT NULL,
    sha256         TEXT    NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    size_bytes     INTEGER NOT NULL CHECK (size_bytes >= 0),
    encoding       TEXT,
    format         TEXT    NOT NULL CHECK (format IN ('TXT', 'MD')),
    imported_at    TEXT    NOT NULL,
    UNIQUE (case_id, attachment_kind, sha256)
) STRICT;

-- 가이드·매뉴얼 원본도 가져온 뒤 바꾸지 않는다. 고치면 새 첨부 행을 만든다.
CREATE TRIGGER guide_attachments_identity_immutable
BEFORE UPDATE ON guide_attachments
WHEN NEW.attachment_kind IS NOT OLD.attachment_kind
  OR NEW.original_name IS NOT OLD.original_name
  OR NEW.stored_path   IS NOT OLD.stored_path
  OR NEW.sha256        IS NOT OLD.sha256
  OR NEW.size_bytes    IS NOT OLD.size_bytes
  OR NEW.imported_at   IS NOT OLD.imported_at
BEGIN
    SELECT RAISE(ABORT, 'guide_attachments: 가이드·매뉴얼 원본 식별 정보는 바꿀 수 없습니다');
END;

-- 구조화된 진단·대응 지식. 항목 하나가 행 하나다.
-- SYMPTOM~NOTE는 진단 가이드 권장 8항목(§3.2), RESPONSE_STEP~ESCALATION은 대응 매뉴얼 항목(CR-01).
CREATE TABLE diagnostic_guides (
    id             INTEGER PRIMARY KEY,
    case_id        INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    attachment_id  INTEGER REFERENCES guide_attachments(id) ON DELETE CASCADE,
    section        TEXT    NOT NULL CHECK (section IN (
        'SYMPTOM', 'CONDITION', 'CRITERIA', 'CAUSE', 'CHECK_METHOD', 'RESOLUTION', 'VERIFICATION', 'NOTE',
        'RESPONSE_STEP', 'PRECAUTION', 'ESCALATION')),
    content        TEXT    NOT NULL,
    source_kind    TEXT    NOT NULL CHECK (source_kind IN ('GUIDE_TEXT', 'USER', 'AI_PROPOSED')),
    status         TEXT    NOT NULL DEFAULT 'PROPOSED' CHECK (status IN ('PROPOSED', 'APPROVED', 'REJECTED', 'RETIRED')),
    span_start     INTEGER,
    span_end       INTEGER,
    sort_order     INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL,
    CHECK (source_kind <> 'GUIDE_TEXT' OR attachment_id IS NOT NULL),
    CHECK (span_end IS NULL OR span_start IS NULL OR span_end >= span_start)
) STRICT;

CREATE INDEX idx_diagnostic_guides_case ON diagnostic_guides(case_id, section);

-- ─────────────────────────────── 이벤트 패턴 ───────────────────────────────

CREATE TABLE event_patterns (
    id               INTEGER PRIMARY KEY,
    mode_id          INTEGER NOT NULL REFERENCES mode_definitions(id) ON DELETE RESTRICT,
    source_case_id   INTEGER REFERENCES cases(id) ON DELETE CASCADE,
    pattern_type     TEXT    NOT NULL CHECK (pattern_type IN (
        'REQUIRED', 'OPTIONAL', 'ORDER_CONSTRAINT', 'INDEPENDENT', 'REPEATED', 'UNCLASSIFIED')),
    definition_json  TEXT    NOT NULL CHECK (json_valid(definition_json)),
    conditions       TEXT,
    basis            TEXT    NOT NULL DEFAULT 'OBSERVED' CHECK (basis IN ('OBSERVED', 'SPECIFICATION', 'USER_DEFINED')),
    basis_note       TEXT,
    source           TEXT    NOT NULL CHECK (source IN ('RULE', 'AI', 'USER')),
    status           TEXT    NOT NULL DEFAULT 'PROPOSED' CHECK (status IN ('PROPOSED', 'APPROVED', 'REJECTED', 'RETIRED')),
    observed_count   INTEGER NOT NULL DEFAULT 0 CHECK (observed_count >= 0),
    approved_at      TEXT,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL,
    -- "장비 사양상 필수" 근거는 출처 설명 없이 둘 수 없다 (§6.1, §13-11)
    CHECK (basis <> 'SPECIFICATION' OR trim(coalesce(basis_note, '')) <> ''),
    CHECK (status <> 'APPROVED' OR approved_at IS NOT NULL)
) STRICT;

CREATE INDEX idx_event_patterns_mode ON event_patterns(mode_id, status);

-- ─────────────────────────────── 확인 질문 ───────────────────────────────

CREATE TABLE learning_questions (
    id            INTEGER PRIMARY KEY,
    case_id       INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    trigger_type  TEXT    NOT NULL CHECK (trigger_type IN (
        'MODE_UNKNOWN', 'NORMAL_RATIONALE_MISSING', 'SESSION_RELATION_UNKNOWN', 'TIMESTAMP_BASIS_UNKNOWN',
        'GUIDE_INFO_MISSING', 'CAUSE_STATUS_UNKNOWN', 'CRITERIA_DETAIL_UNKNOWN', 'USER_LOG_CONFLICT',
        'CONFLICTING_KNOWLEDGE')),
    question_text TEXT    NOT NULL,
    reason        TEXT    NOT NULL CHECK (trim(reason) <> ''),
    priority      INTEGER NOT NULL DEFAULT 0,
    is_required   INTEGER NOT NULL DEFAULT 0 CHECK (is_required IN (0, 1)),
    target_ref    TEXT,
    dedup_key     TEXT    NOT NULL,
    generated_by  TEXT    NOT NULL CHECK (generated_by IN ('RULE', 'AI')),
    status        TEXT    NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'ANSWERED', 'SKIPPED', 'UNKNOWN', 'OBSOLETE')),
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    UNIQUE (case_id, dedup_key)
) STRICT;

CREATE TABLE learning_answers (
    id               INTEGER PRIMARY KEY,
    question_id      INTEGER NOT NULL REFERENCES learning_questions(id) ON DELETE CASCADE,
    answer_kind      TEXT    NOT NULL CHECK (answer_kind IN ('TEXT', 'CHOICE', 'UNKNOWN', 'SKIPPED')),
    answer_text      TEXT,
    structured_json  TEXT    CHECK (structured_json IS NULL OR json_valid(structured_json)),
    answered_at      TEXT    NOT NULL,
    CHECK (answer_kind IN ('UNKNOWN', 'SKIPPED') OR answer_text IS NOT NULL OR structured_json IS NOT NULL)
) STRICT;

-- ─────────────────────────────── 분석 실행 ───────────────────────────────

CREATE TABLE analysis_runs (
    id                    INTEGER PRIMARY KEY,
    status                TEXT    NOT NULL DEFAULT 'RUNNING'
        CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')),
    mode_id               INTEGER REFERENCES mode_definitions(id) ON DELETE RESTRICT,
    mode_confirmed        INTEGER NOT NULL DEFAULT 0 CHECK (mode_confirmed IN (0, 1)),
    operating_conditions  TEXT,
    user_description      TEXT,
    -- 결과 분류는 배타적이지 않다. 네 축을 따로 기록한다 (§3.3)
    normal_match          TEXT    NOT NULL DEFAULT 'NOT_EVALUATED'
        CHECK (normal_match IN ('MATCH', 'PARTIAL', 'NO_MATCH', 'NOT_EVALUATED')),
    anomaly_level         TEXT    NOT NULL DEFAULT 'NOT_EVALUATED'
        CHECK (anomaly_level IN ('NONE_FOUND', 'SUSPECTED', 'RULE_VIOLATION', 'NOT_EVALUATED')),
    similar_abnormal      TEXT    NOT NULL DEFAULT 'NOT_EVALUATED'
        CHECK (similar_abnormal IN ('FOUND', 'NOT_FOUND', 'NOT_EVALUATED')),
    evidence_sufficiency  TEXT    NOT NULL DEFAULT 'NOT_EVALUATED'
        CHECK (evidence_sufficiency IN ('SUFFICIENT', 'PARTIAL', 'INSUFFICIENT', 'NOT_EVALUATED')),
    -- 사용자(CS 엔지니어 등)에게 보여 줄 권장 대응의 근거 (CR-01).
    -- MANUAL: 승인된 대응 매뉴얼 항목, GUIDE_ONLY: 매뉴얼은 없고 진단 가이드의 해결 방법만, NONE: 등록된 대응 없음
    response_guidance     TEXT    NOT NULL DEFAULT 'NOT_EVALUATED'
        CHECK (response_guidance IN ('MANUAL', 'GUIDE_ONLY', 'NONE', 'NOT_EVALUATED')),
    ai_status             TEXT    NOT NULL DEFAULT 'NOT_RUN'
        CHECK (ai_status IN ('NOT_RUN', 'OK', 'UNAVAILABLE', 'VALIDATION_FAILED', 'CANCELLED')),
    ai_model              TEXT,
    app_version           TEXT    NOT NULL,
    report_json_path      TEXT,
    report_html_path      TEXT,
    error_message         TEXT,
    started_at            TEXT    NOT NULL,
    finished_at           TEXT
) STRICT;

CREATE TABLE analysis_run_files (
    run_id       INTEGER NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    log_file_id  INTEGER NOT NULL REFERENCES log_files(id) ON DELETE RESTRICT,
    PRIMARY KEY (run_id, log_file_id)
) STRICT;

CREATE TABLE analysis_evidence (
    id             INTEGER PRIMARY KEY,
    run_id         INTEGER NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    evidence_key   TEXT    NOT NULL,
    layer          TEXT    NOT NULL CHECK (layer IN ('DETERMINISTIC', 'RETRIEVAL', 'AI')),
    finding_type   TEXT    NOT NULL CHECK (finding_type IN (
        'EXPLICIT_ERROR', 'MISSING_EVENT', 'EXTRA_EVENT', 'ORDER_VIOLATION', 'LATENCY', 'REPETITION',
        'SIMILAR_NORMAL_CASE', 'SIMILAR_ABNORMAL_CASE', 'GUIDE_REFERENCE', 'RESPONSE_REFERENCE', 'TEXT_MATCH',
        'AI_OBSERVATION', 'AI_HYPOTHESIS', 'AI_COUNTER_EVIDENCE', 'AI_VERIFICATION_STEP', 'AI_UNKNOWN')),
    stance         TEXT    NOT NULL DEFAULT 'NEUTRAL' CHECK (stance IN ('SUPPORTS', 'CONTRADICTS', 'NEUTRAL')),
    log_file_id    INTEGER REFERENCES log_files(id) ON DELETE RESTRICT,
    event_id       INTEGER REFERENCES events(id) ON DELETE RESTRICT,
    line_start     INTEGER,
    line_end       INTEGER,
    case_id        INTEGER REFERENCES cases(id) ON DELETE RESTRICT,
    pattern_id     INTEGER REFERENCES event_patterns(id) ON DELETE RESTRICT,
    guide_item_id  INTEGER REFERENCES diagnostic_guides(id) ON DELETE RESTRICT,
    similarity     REAL,
    summary        TEXT    NOT NULL,
    detail_json    TEXT    CHECK (detail_json IS NULL OR json_valid(detail_json)),
    created_at     TEXT    NOT NULL,
    UNIQUE (run_id, evidence_key),
    CHECK (line_end IS NULL OR line_start IS NULL OR line_end >= line_start)
) STRICT;

-- ─────────────────────────────── 지식 주장 · 근거 ───────────────────────────────

CREATE TABLE knowledge_claims (
    id               INTEGER PRIMARY KEY,
    case_id          INTEGER REFERENCES cases(id) ON DELETE CASCADE,
    guide_item_id    INTEGER REFERENCES diagnostic_guides(id) ON DELETE CASCADE,
    pattern_id       INTEGER REFERENCES event_patterns(id) ON DELETE CASCADE,
    analysis_run_id  INTEGER REFERENCES analysis_runs(id) ON DELETE CASCADE,
    claim_text       TEXT    NOT NULL,
    claim_kind       TEXT    NOT NULL CHECK (claim_kind IN ('FACT', 'HYPOTHESIS', 'UNKNOWN')),
    source           TEXT    NOT NULL CHECK (source IN ('LOG_OBSERVATION', 'USER', 'GUIDE', 'AI')),
    status           TEXT    NOT NULL DEFAULT 'PROPOSED' CHECK (status IN ('PROPOSED', 'CONFIRMED', 'REJECTED', 'SUPERSEDED')),
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL,
    -- 주장은 정확히 하나의 대상(사례·가이드 항목·패턴·분석 실행)에 속한다
    CHECK ((case_id IS NOT NULL) + (guide_item_id IS NOT NULL)
         + (pattern_id IS NOT NULL) + (analysis_run_id IS NOT NULL) = 1)
) STRICT;

CREATE TABLE claim_evidence (
    id                    INTEGER PRIMARY KEY,
    claim_id              INTEGER NOT NULL REFERENCES knowledge_claims(id) ON DELETE CASCADE,
    evidence_type         TEXT    NOT NULL CHECK (evidence_type IN (
        'EVENT', 'LOG_LINES', 'GUIDE_SPAN', 'USER_ANSWER', 'CASE', 'NOTE', 'ANALYSIS_EVIDENCE')),
    stance                TEXT    NOT NULL DEFAULT 'SUPPORTS' CHECK (stance IN ('SUPPORTS', 'CONTRADICTS')),
    event_id              INTEGER REFERENCES events(id) ON DELETE RESTRICT,
    log_file_id           INTEGER REFERENCES log_files(id) ON DELETE RESTRICT,
    line_start            INTEGER,
    line_end              INTEGER,
    guide_attachment_id   INTEGER REFERENCES guide_attachments(id) ON DELETE CASCADE,
    span_start            INTEGER,
    span_end              INTEGER,
    answer_id             INTEGER REFERENCES learning_answers(id) ON DELETE CASCADE,
    ref_case_id           INTEGER REFERENCES cases(id) ON DELETE RESTRICT,
    note_id               INTEGER REFERENCES case_notes(id) ON DELETE CASCADE,
    analysis_evidence_id  INTEGER REFERENCES analysis_evidence(id) ON DELETE CASCADE,
    created_at            TEXT    NOT NULL,
    -- 근거 종류에 맞는 참조가 반드시 있어야 한다
    CHECK (
        (evidence_type = 'EVENT'             AND event_id IS NOT NULL) OR
        (evidence_type = 'LOG_LINES'         AND log_file_id IS NOT NULL AND line_start IS NOT NULL) OR
        (evidence_type = 'GUIDE_SPAN'        AND guide_attachment_id IS NOT NULL) OR
        (evidence_type = 'USER_ANSWER'       AND answer_id IS NOT NULL) OR
        (evidence_type = 'CASE'              AND ref_case_id IS NOT NULL) OR
        (evidence_type = 'NOTE'              AND note_id IS NOT NULL) OR
        (evidence_type = 'ANALYSIS_EVIDENCE' AND analysis_evidence_id IS NOT NULL)
    )
) STRICT;

CREATE INDEX idx_claim_evidence_claim ON claim_evidence(claim_id);

-- ─────────────────────────────── 개정 이력 · 감사 이력 (추가 전용) ───────────────────────────────

CREATE TABLE case_revisions (
    id              INTEGER PRIMARY KEY,
    case_id         INTEGER NOT NULL REFERENCES cases(id) ON DELETE RESTRICT,
    revision_no     INTEGER NOT NULL CHECK (revision_no >= 1),
    snapshot_json   TEXT    NOT NULL CHECK (json_valid(snapshot_json)),
    change_summary  TEXT,
    created_at      TEXT    NOT NULL,
    UNIQUE (case_id, revision_no)
) STRICT;

CREATE TRIGGER case_revisions_no_update
BEFORE UPDATE ON case_revisions
BEGIN
    SELECT RAISE(ABORT, 'case_revisions: 개정 이력은 고칠 수 없습니다');
END;

CREATE TRIGGER case_revisions_no_delete
BEFORE DELETE ON case_revisions
BEGIN
    SELECT RAISE(ABORT, 'case_revisions: 개정 이력은 지울 수 없습니다');
END;

CREATE TABLE audit_history (
    id           INTEGER PRIMARY KEY,
    entity_type  TEXT    NOT NULL,
    entity_id    INTEGER,
    action       TEXT    NOT NULL CHECK (action IN (
        'CREATE', 'UPDATE', 'STATUS_CHANGE', 'APPROVE', 'REJECT', 'ARCHIVE', 'RESTORE', 'DELETE', 'IMPORT')),
    actor        TEXT    NOT NULL CHECK (actor IN ('USER', 'AI', 'SYSTEM')),
    before_json  TEXT    CHECK (before_json IS NULL OR json_valid(before_json)),
    after_json   TEXT    CHECK (after_json IS NULL OR json_valid(after_json)),
    note         TEXT,
    occurred_at  TEXT    NOT NULL
) STRICT;

CREATE INDEX idx_audit_history_entity ON audit_history(entity_type, entity_id);

CREATE TRIGGER audit_history_no_update
BEFORE UPDATE ON audit_history
BEGIN
    SELECT RAISE(ABORT, 'audit_history: 감사 이력은 고칠 수 없습니다');
END;

CREATE TRIGGER audit_history_no_delete
BEFORE DELETE ON audit_history
BEGIN
    SELECT RAISE(ABORT, 'audit_history: 감사 이력은 지울 수 없습니다');
END;

-- ─────────────────────────────── 승인된 지식 뷰 (§5.3) ───────────────────────────────
-- 신규 분석의 확정 지식은 이 뷰로만 읽는다. 승인 전 사례·항목은 여기 나타나지 않는다.

CREATE VIEW approved_cases AS
    SELECT * FROM cases WHERE status = 'APPROVED';

CREATE VIEW approved_event_patterns AS
    SELECT p.*
    FROM event_patterns AS p
    LEFT JOIN cases AS c ON c.id = p.source_case_id
    WHERE p.status = 'APPROVED'
      AND (p.source_case_id IS NULL OR c.status = 'APPROVED');

CREATE VIEW approved_diagnostic_guides AS
    SELECT g.*
    FROM diagnostic_guides AS g
    JOIN cases AS c ON c.id = g.case_id
    WHERE g.status = 'APPROVED' AND c.status = 'APPROVED';
