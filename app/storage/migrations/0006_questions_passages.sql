-- 0006_questions_passages.sql
-- CR-07: LLM 질문(ai_questions), 참고 자료 일반 문서 문단(reference_passages),
-- 비정상 판정 기준을 선택으로(0001의 CHECK 하나 제거).

-- ── 1. cases: '승인된 비정상은 증상·판정 기준 필수' CHECK 제거 (CR-07 LI-09) ──
-- SQLite는 CHECK를 ALTER로 지울 수 없다. 표를 다시 만들면 외래 키(ON DELETE CASCADE)가 걸린 표가 많아
-- 위험하므로, SQLite 공식 문서의 '제약 제거' 절차대로 CREATE TABLE 문에서 해당 문구만 지운다.
-- 제약을 느슨하게만 하므로 기존 데이터는 그대로 맞는다. 아래 CREATE TABLE이 스키마 번호를 올려
-- 다른 연결도 새 정의를 읽는다. 줄바꿈이 LF·CRLF인 경우를 모두 처리한다.
PRAGMA writable_schema = ON;
UPDATE sqlite_schema
SET sql = replace(replace(sql,
    ',
    -- 비정상 사례 승인에는 오류 증상과 비정상 판정 기준이 필수다 (§3.2)
    CHECK (case_type <> ''ABNORMAL'' OR status <> ''APPROVED''
           OR (trim(coalesce(symptom, '''')) <> '''' AND trim(coalesce(abnormal_criteria, '''')) <> ''''))', ''),
    ',' || char(13, 10) ||
    '    -- 비정상 사례 승인에는 오류 증상과 비정상 판정 기준이 필수다 (§3.2)' || char(13, 10) ||
    '    CHECK (case_type <> ''ABNORMAL'' OR status <> ''APPROVED''' || char(13, 10) ||
    '           OR (trim(coalesce(symptom, '''')) <> '''' AND trim(coalesce(abnormal_criteria, '''')) <> ''''))', '')
WHERE type = 'table' AND name = 'cases';
PRAGMA writable_schema = OFF;

-- ── 2. LLM 질문 (CR-07 QA-01~03) ──
CREATE TABLE ai_questions (
    id             INTEGER PRIMARY KEY,
    case_id        INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    kind           TEXT    NOT NULL CHECK (kind IN ('GUESS', 'ASK')),
    question_text  TEXT    NOT NULL CHECK (length(trim(question_text)) > 0),
    reason         TEXT,
    evidence_json  TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(evidence_json)),
    status         TEXT    NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'ANSWERED', 'SKIPPED', 'OBSOLETE')),
    answer         TEXT    CHECK (answer IS NULL OR answer IN ('YES', 'NO', 'UNKNOWN', 'TEXT')),
    answer_text    TEXT,
    model          TEXT,
    dedup_key      TEXT    NOT NULL,
    created_at     TEXT    NOT NULL,
    answered_at    TEXT,
    UNIQUE (case_id, dedup_key),
    CHECK (status <> 'ANSWERED' OR answer IS NOT NULL)
) STRICT;

CREATE INDEX idx_ai_questions_case ON ai_questions(case_id, status);

-- 분석·AI가 지식으로 쓰는 답: 승인된 사례의 맞음·틀림·글 답만('모름'은 지식이 아님).
CREATE VIEW confirmed_ai_answers AS
    SELECT q.*
    FROM ai_questions AS q
    JOIN cases AS c ON c.id = q.case_id
    WHERE q.status = 'ANSWERED' AND q.answer IN ('YES', 'NO', 'TEXT') AND c.status = 'APPROVED';

-- ── 3. 참고 자료 일반 문서 문단 (CR-07 REF-07) ──
CREATE TABLE reference_passages (
    id           INTEGER PRIMARY KEY,
    document_id  INTEGER NOT NULL REFERENCES reference_documents(id) ON DELETE CASCADE,
    sort_order   INTEGER NOT NULL DEFAULT 0,
    locator      TEXT,
    body         TEXT    NOT NULL CHECK (length(trim(body)) > 0),
    created_at   TEXT    NOT NULL
) STRICT;

CREATE INDEX idx_reference_passages_document ON reference_passages(document_id, sort_order);

CREATE TRIGGER reference_passages_locked_insert
BEFORE INSERT ON reference_passages
WHEN (SELECT status FROM reference_documents WHERE id = NEW.document_id) <> 'DRAFT'
BEGIN
    SELECT RAISE(ABORT, 'reference_passages: 승인·보관된 문서에는 문단을 더할 수 없습니다');
END;

CREATE TRIGGER reference_passages_locked_update
BEFORE UPDATE ON reference_passages
WHEN (SELECT status FROM reference_documents WHERE id = OLD.document_id) <> 'DRAFT'
BEGIN
    SELECT RAISE(ABORT, 'reference_passages: 승인·보관된 문서의 문단은 고칠 수 없습니다');
END;

CREATE TRIGGER reference_passages_locked_delete
BEFORE DELETE ON reference_passages
WHEN (SELECT status FROM reference_documents WHERE id = OLD.document_id) <> 'DRAFT'
BEGIN
    SELECT RAISE(ABORT, 'reference_passages: 승인·보관된 문서의 문단은 지울 수 없습니다');
END;

CREATE VIEW approved_reference_passages AS
    SELECT p.*, d.title AS document_title, d.original_name AS document_name
    FROM reference_passages AS p
    JOIN reference_documents AS d ON d.id = p.document_id
    WHERE d.status = 'APPROVED';
