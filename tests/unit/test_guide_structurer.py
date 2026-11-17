"""가이드·매뉴얼 구조화 (결정론, AL-03·17). 문서 내용은 가상이다."""

from __future__ import annotations

from app.ai.guide_labeler import split_segments
from app.core.guide_structurer import classify_heading, mentioned_codes, structure_text

GUIDE = """# 가상 진단 가이드 TEST-E001

## 오류 증상
TEST-SUB가 TEST-FAULT를 반복해서 기록한다.

## 발생 조건
가상 운전 모드 A에서 시작 직후.

## 비정상 판정 기준
TEST-FAULT가 10초 안에 2회 이상.

## 가능한 원인
가상 원인 1
가상 원인 2

## 원인 확인 방법
TEST 점검 절차를 따른다.

## 조치 후 검증 방법
TEST-RUN이 다시 기록되는지 본다.
"""

MANUAL = """대응 절차:
1. 가상 장비를 대기 상태로 둔다.
2. 1. 안전 확인 후 TEST 커넥터를 다시 꽂는다.
   (들여쓴 설명은 앞 단계에 붙는다)
3. TEST-RUN이 나오는지 확인한다.

주의사항:
- 가상 주의 문장

에스컬레이션:
3회 반복되면 상위 지원에 문의한다.
"""


def test_guide_sections_and_spans() -> None:
    items, found = structure_text(GUIDE, "DIAGNOSTIC_GUIDE")
    assert found
    sections = [i.section for i in items]
    assert sections[:1] == ["NOTE"]  # 첫 제목('가상 진단 가이드')은 항목 이름이 아니라 참고로 남는다
    assert {"SYMPTOM", "CONDITION", "CRITERIA", "CAUSE", "CHECK_METHOD", "VERIFICATION"} <= set(sections)
    for item in items:
        assert GUIDE[item.span_start:item.span_end].strip() == item.content  # 내용은 원문 그대로
    cause = next(i for i in items if i.section == "CAUSE")
    assert cause.content == "가상 원인 1\n가상 원인 2"
    assert "RESOLUTION" not in sections  # 해결 방법이 없으면 만들지 않는다(빈 값 허용)


def test_manual_steps_split_and_ordered() -> None:
    items, found = structure_text(MANUAL, "RESPONSE_MANUAL")
    assert found
    steps = [i for i in items if i.section == "RESPONSE_STEP"]
    assert [s.sort_order for s in steps] == [1, 2, 3]
    assert "들여쓴 설명" in steps[1].content
    assert {i.section for i in items} == {"RESPONSE_STEP", "PRECAUTION", "ESCALATION"}
    for item in items:
        assert MANUAL[item.span_start:item.span_end].strip() == item.content


def test_body_step_with_keyword_is_not_heading() -> None:
    assert classify_heading("안전 장갑을 착용한 뒤 TEST 커넥터를 분리한다", "RESPONSE_MANUAL", strict=True) is None
    assert classify_heading("주의사항", "RESPONSE_MANUAL", strict=True) == "PRECAUTION"


def test_no_headings_becomes_single_note() -> None:
    text = "제목 없이 쓴 가상 가이드 문장.\n둘째 줄."
    items, found = structure_text(text)
    assert not found and len(items) == 1 and items[0].section == "NOTE" and items[0].content == text


def test_code_mentions_are_exact_tokens() -> None:
    text = "가이드에는 TEST-E001과 test-e002가 나온다. TEST-E0010은 다른 코드다."
    assert mentioned_codes(text, {"TEST-E001", "TEST-E002", "TEST-E003"}) == {"TEST-E001", "TEST-E002"}


def test_segments_keep_original_text() -> None:
    for segment in split_segments(MANUAL):
        assert MANUAL[segment.span_start:segment.span_end].strip() == segment.text
    assert len(split_segments(MANUAL)) >= 6
