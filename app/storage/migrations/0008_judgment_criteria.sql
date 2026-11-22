-- 0008_judgment_criteria.sql
-- CR-15 JQ-03: 판단 질문에 엔지니어가 답한 '판단 기준'. 코드 판정(규칙 + 사례 기억)이 그대로 쓴다.
-- 특징(feature)은 CR-14 근거 지문의 특징 글자와 같은 열쇠다(위치·값을 뺀 근거 종류 + 이벤트 유형 등).
-- 종류: ABNORMAL_SIGN(나오면 비정상) / BENIGN(정상에서도 나옴, 문제없음) / VALUE_RANGE(값 범위) / HOLD(상황에 따라 다름 → 사람 확인).
-- 같은 특징에 기준은 하나다(새 답이 이전 것을 대신한다). 테이블을 더하기만 하므로 기존 데이터는 그대로다.
CREATE TABLE judgment_criteria (
    id               INTEGER PRIMARY KEY,
    feature          TEXT    NOT NULL CHECK (length(trim(feature)) > 0),
    kind             TEXT    NOT NULL CHECK (kind IN ('ABNORMAL_SIGN', 'BENIGN', 'VALUE_RANGE', 'HOLD')),
    event_type       TEXT,
    field            TEXT,
    low              REAL,
    high             REAL,
    question         TEXT    NOT NULL,
    example          TEXT,
    method_text      TEXT,
    source_log_ids   TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(source_log_ids)),
    source_run_id    INTEGER REFERENCES analysis_runs(id) ON DELETE SET NULL,
    status           TEXT    NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'DISABLED')),
    disabled_reason  TEXT,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL,
    CHECK (kind <> 'VALUE_RANGE' OR (field IS NOT NULL AND (low IS NOT NULL OR high IS NOT NULL)))
) STRICT;

CREATE UNIQUE INDEX idx_judgment_criteria_feature ON judgment_criteria(feature);
