-- 0004_fts_bulk_index.sql
-- 행마다 도는 FTS 삽입 트리거를 없앤다. 성능 시험(T-PERF-01)에서 10MB × 10개 가져오기가 32초 → 145초로 느려졌다.
-- 대신 EventRepository.add_many가 파일 하나의 이벤트를 넣은 직후 한 문장으로 색인에 넣는다(같은 트랜잭션).
-- 삭제·수정 트리거는 그대로 둔다(드물고, 색인과 원본이 어긋나지 않게 하는 안전장치).

DROP TRIGGER IF EXISTS events_fts_insert;
