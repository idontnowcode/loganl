-- 0007_value_out_of_range.sql
-- CR-08 DT-04·09: 분석 근거 종류에 VALUE_OUT_OF_RANGE(정상 로그 관찰 범위를 벗어난 값)와
-- VALUE_STUCK(정상 로그에서 늘 변하던 값이 연속으로 같음)을 더한다.
-- SQLite는 CHECK를 ALTER로 바꿀 수 없다. 0006과 같은 절차(writable_schema로 CREATE TABLE 문의 값 목록만 고침)를 쓴다.
-- 허용 값을 하나 늘리기만 하므로 기존 데이터는 그대로 맞는다. 아래 CREATE INDEX가 스키마 번호를 올려
-- 다른 연결도 새 정의를 읽는다.
PRAGMA writable_schema = ON;
UPDATE sqlite_schema
SET sql = replace(sql, '''AI_UNKNOWN''))', '''AI_UNKNOWN'', ''VALUE_OUT_OF_RANGE'', ''VALUE_STUCK''))')
WHERE type = 'table' AND name = 'analysis_evidence' AND sql NOT LIKE '%VALUE_OUT_OF_RANGE%';
PRAGMA writable_schema = OFF;

CREATE INDEX IF NOT EXISTS idx_analysis_evidence_type ON analysis_evidence(finding_type);
