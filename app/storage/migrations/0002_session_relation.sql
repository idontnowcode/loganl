-- 0002_session_relation.sql
-- 수집 세션 관계를 사용자가 직접 정했는지 기록한다 (Phase 2).
-- 가져올 때 "모름"을 고르면 파일마다 세션을 따로 만들고 relation_confirmed = 0으로 둔다.
-- Phase 5 질문 엔진은 이 값이 0인 파일이 사례에 둘 이상 있으면 SESSION_RELATION_UNKNOWN 질문을 만든다.

ALTER TABLE collection_sessions
    ADD COLUMN relation_confirmed INTEGER NOT NULL DEFAULT 0 CHECK (relation_confirmed IN (0, 1));
