-- 0005_reference_library.sql
-- CR-04 참고 자료(사용자 코드 사전). 사용자가 정한 오류 코드 목록·설명 문서를 올리고, 승인한 항목만
-- 분석과 AI가 참고한다. 원본 사본은 attachments/references/에 읽기 전용으로 둔다.

CREATE TABLE reference_documents (
    id               INTEGER PRIMARY KEY,
    title            TEXT    NOT NULL CHECK (length(trim(title)) > 0),
    original_name    TEXT    NOT NULL,
    source_path      TEXT,
    stored_path      TEXT    NOT NULL,
    sha256           TEXT    NOT NULL UNIQUE CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    size_bytes       INTEGER NOT NULL CHECK (size_bytes >= 0),
    format           TEXT    NOT NULL CHECK (format IN ('TXT', 'MD', 'XLSX', 'PDF', 'DOCX')),
    status           TEXT    NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT', 'APPROVED', 'ARCHIVED')),
    column_map_json  TEXT    CHECK (column_map_json IS NULL OR json_valid(column_map_json)),
    replaces_id      INTEGER REFERENCES reference_documents(id) ON DELETE SET NULL,
    note             TEXT,
    imported_at      TEXT    NOT NULL,
    approved_at      TEXT,
    archived_at      TEXT,
    CHECK (status <> 'APPROVED' OR approved_at IS NOT NULL)
) STRICT;

-- 원본 식별 정보는 바꾸지 않는다(원본 무결성). 고치려면 새 문서로 올린다.
CREATE TRIGGER reference_documents_identity_immutable
BEFORE UPDATE ON reference_documents
WHEN NEW.original_name IS NOT OLD.original_name
  OR NEW.stored_path   IS NOT OLD.stored_path
  OR NEW.sha256        IS NOT OLD.sha256
  OR NEW.size_bytes    IS NOT OLD.size_bytes
  OR NEW.format        IS NOT OLD.format
BEGIN
    SELECT RAISE(ABORT, 'reference_documents: 원본 식별 정보는 바꿀 수 없습니다');
END;

-- 상태는 DRAFT → APPROVED/ARCHIVED, APPROVED → ARCHIVED만. 보관은 마지막 상태다.
CREATE TRIGGER reference_documents_status_flow
BEFORE UPDATE OF status ON reference_documents
WHEN NEW.status IS NOT OLD.status
 AND NOT ((OLD.status = 'DRAFT' AND NEW.status IN ('APPROVED', 'ARCHIVED'))
       OR (OLD.status = 'APPROVED' AND NEW.status = 'ARCHIVED'))
BEGIN
    SELECT RAISE(ABORT, 'reference_documents: 허용하지 않는 상태 변경입니다');
END;

-- 승인·보관된 문서는 제목·열 역할도 바꾸지 않는다(무엇을 근거로 썼는지 보존).
CREATE TRIGGER reference_documents_locked_content
BEFORE UPDATE OF title, column_map_json ON reference_documents
WHEN OLD.status <> 'DRAFT'
 AND (NEW.title IS NOT OLD.title OR NEW.column_map_json IS NOT OLD.column_map_json)
BEGIN
    SELECT RAISE(ABORT, 'reference_documents: 승인·보관된 문서는 고칠 수 없습니다. 새 버전을 올리세요');
END;

CREATE TRIGGER reference_documents_delete_only_draft
BEFORE DELETE ON reference_documents
WHEN OLD.status <> 'DRAFT'
BEGIN
    SELECT RAISE(ABORT, 'reference_documents: 초안만 지울 수 있습니다');
END;

CREATE TABLE reference_entries (
    id            INTEGER PRIMARY KEY,
    document_id   INTEGER NOT NULL REFERENCES reference_documents(id) ON DELETE CASCADE,
    code          TEXT    NOT NULL CHECK (length(trim(code)) > 0),
    code_norm     TEXT    NOT NULL CHECK (length(code_norm) > 0),
    title         TEXT,
    description   TEXT,
    cause         TEXT,
    action        TEXT,
    locator       TEXT,
    source_text   TEXT,
    source_kind   TEXT    NOT NULL CHECK (source_kind IN ('EXTRACTED', 'USER')),
    status        TEXT    NOT NULL DEFAULT 'INCLUDED' CHECK (status IN ('INCLUDED', 'EXCLUDED')),
    sort_order    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
) STRICT;

CREATE INDEX idx_reference_entries_code ON reference_entries(code_norm);
CREATE INDEX idx_reference_entries_document ON reference_entries(document_id, sort_order);

-- 승인·보관된 문서의 항목은 넣기·고치기·지우기를 막는다.
CREATE TRIGGER reference_entries_locked_insert
BEFORE INSERT ON reference_entries
WHEN (SELECT status FROM reference_documents WHERE id = NEW.document_id) <> 'DRAFT'
BEGIN
    SELECT RAISE(ABORT, 'reference_entries: 승인·보관된 문서에는 항목을 더할 수 없습니다');
END;

CREATE TRIGGER reference_entries_locked_update
BEFORE UPDATE ON reference_entries
WHEN (SELECT status FROM reference_documents WHERE id = OLD.document_id) <> 'DRAFT'
BEGIN
    SELECT RAISE(ABORT, 'reference_entries: 승인·보관된 문서의 항목은 고칠 수 없습니다');
END;

CREATE TRIGGER reference_entries_locked_delete
BEFORE DELETE ON reference_entries
WHEN (SELECT status FROM reference_documents WHERE id = OLD.document_id) <> 'DRAFT'
BEGIN
    SELECT RAISE(ABORT, 'reference_entries: 승인·보관된 문서의 항목은 지울 수 없습니다');
END;

-- 분석·AI는 이 뷰로만 읽는다. 승인 전 문서와 제외한 항목은 나타나지 않는다.
CREATE VIEW approved_reference_entries AS
    SELECT e.*, d.title AS document_title, d.original_name AS document_name
    FROM reference_entries AS e
    JOIN reference_documents AS d ON d.id = e.document_id
    WHERE d.status = 'APPROVED' AND e.status = 'INCLUDED';
