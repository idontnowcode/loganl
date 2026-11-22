-- 변경 요청 CR-16 RJ-01: 참고 자료 항목의 '판정' 칸(문서에 적힌 낱말 그대로, 예: 에러·정상).
-- 분석은 app/core/reference_structurer.judgment_side로 비정상·정상·안 씀으로만 읽는다. 승인된 문서의 항목은 0005 트리거로 그대로 잠긴다.
ALTER TABLE reference_entries ADD COLUMN judgment TEXT;

-- 뷰는 e.*를 쓰지만 새 칸이 확실히 보이도록 다시 만든다.
DROP VIEW approved_reference_entries;
CREATE VIEW approved_reference_entries AS
    SELECT e.*, d.title AS document_title, d.original_name AS document_name
    FROM reference_entries AS e
    JOIN reference_documents AS d ON d.id = e.document_id
    WHERE d.status = 'APPROVED' AND e.status = 'INCLUDED';
