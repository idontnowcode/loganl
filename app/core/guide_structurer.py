"""진단 가이드·대응 매뉴얼 구조화 (결정론, 계획서 §3.2 권장 내용, CR-01).

제목 줄(마크다운 #, '1.'·'1)' 번호, '[...]', ':'로 끝나는 짧은 줄)을 찾아 권장 항목 이름과 맞춘다.
이건 문서의 **구성**을 읽는 것이지 장비의 뜻을 붙이는 것이 아니다. 항목 내용은 항상 원문 그대로이고
원문 위치(span)를 함께 남긴다. 제목을 못 찾으면 전체를 '참고(NOTE)' 하나로 두고 사용자·AI 구조화에 맡긴다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 순서가 중요하다: 더 구체적인 이름을 먼저 본다.
GUIDE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("VERIFICATION", ("조치 후 검증", "조치 후 확인", "검증 방법", "검증", "verification")),
    ("CRITERIA", ("비정상 판정 기준", "판정 기준", "판단 기준", "criteria")),
    ("CONDITION", ("발생 조건", "발생 상황", "condition")),
    ("CHECK_METHOD", ("원인 확인 방법", "확인 방법", "진단 방법", "점검 방법", "확인 절차", "check")),
    ("CAUSE", ("가능한 원인", "추정 원인", "원인", "cause")),
    ("RESOLUTION", ("해결 방법", "조치 방법", "해결", "대책", "resolution", "fix")),
    ("SYMPTOM", ("오류 증상", "증상", "현상", "symptom")),
    ("NOTE", ("추가 참고사항", "참고사항", "참고", "비고", "note")),
)
MANUAL_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("PRECAUTION", ("주의사항", "주의", "경고", "안전", "caution", "warning")),
    ("ESCALATION", ("에스컬레이션", "상위 지원", "기술 지원", "문의", "연락", "escalation")),
    ("VERIFICATION", ("조치 후 확인", "조치 후 검증", "검증", "verification")),
    ("RESPONSE_STEP", ("대응 절차", "조치 절차", "작업 절차", "대응 방법", "절차", "대응", "단계", "procedure", "step")),
    ("NOTE", ("참고", "비고", "note")),
)

_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s*(?P<md>.+?)\s*#*\s*$"
    r"|(?:\d{1,2}[.)]|[①-⑳]|[-*])\s*(?P<num>[^\n:：]{1,40})\s*[:：]?\s*$"
    r"|\[(?P<br>[^\]]{1,40})\]\s*$"
    r"|(?P<colon>[^\n:：]{1,30})[:：]\s*$)"
)
_STEP = re.compile(r"^\s*(?:\d{1,3}[.)]|[①-⑳]|[-*•])\s+\S")


@dataclass
class StructuredItem:
    section: str
    content: str
    span_start: int
    span_end: int
    sort_order: int = 0


def classify_heading(text: str, kind: str, strict: bool = False) -> str | None:
    """strict=True(번호·글머리·콜론 형태)면 제목 글자의 절반 이상이 항목 이름일 때만 제목으로 본다.
    '1. 안전 장갑을 착용한다' 같은 본문 단계가 제목으로 잘못 잡히지 않게 한다."""
    lowered = re.sub(r"\s+", " ", text.strip().lower())
    table = MANUAL_KEYWORDS if kind == "RESPONSE_MANUAL" else GUIDE_KEYWORDS
    for section, words in table:
        for word in words:
            if word.lower() in lowered and (not strict or len(word) / max(len(lowered), 1) >= 0.5):
                return section
    return None


def structure_text(text: str, kind: str = "DIAGNOSTIC_GUIDE") -> tuple[list[StructuredItem], bool]:
    """(항목 목록, 제목을 하나라도 찾았나)."""
    lines = text.split("\n")
    offsets = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line) + 1

    sections: list[tuple[str, int, int]] = []  # (section, 내용 시작 줄, 끝 줄(제외))
    current: str | None = None
    start = 0
    found = False
    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        section = None
        if match:
            groups = match.groupdict()
            if groups.get("md"):
                section = classify_heading(groups["md"], kind)
            else:
                heading = groups.get("num") or groups.get("br") or groups.get("colon")
                section = classify_heading(heading, kind, strict=True) if heading else None
        if section:
            if current is not None or index > start:
                sections.append((current or "NOTE", start, index))
            current, start, found = section, index + 1, True
    sections.append((current or "NOTE", start, len(lines)))

    items: list[StructuredItem] = []
    for section, first, last in sections:
        body = [(i, lines[i]) for i in range(first, last) if lines[i].strip()]
        if not body:
            continue
        if section == "RESPONSE_STEP":
            items.extend(_split_steps(body, offsets))
            continue
        span_start = offsets[body[0][0]]
        span_end = offsets[body[-1][0]] + len(body[-1][1])
        items.append(StructuredItem(section, text[span_start:span_end].strip(), span_start, span_end))
    if not found and items:
        items = [StructuredItem("NOTE", text.strip(), 0, len(text.rstrip()))]
    for order, item in enumerate(items):
        if item.section != "RESPONSE_STEP":
            item.sort_order = order
    return items, found


def _split_steps(body: list[tuple[int, str]], offsets: list[int]) -> list[StructuredItem]:
    """번호·글머리 줄마다 한 단계. 이어지는 들여쓴 줄은 앞 단계에 붙인다."""
    groups: list[list[tuple[int, str]]] = []
    for index, line in body:
        if _STEP.match(line) or not groups:
            groups.append([(index, line)])
        else:
            groups[-1].append((index, line))
    steps: list[StructuredItem] = []
    for order, group in enumerate(groups, start=1):
        start = offsets[group[0][0]]
        end = offsets[group[-1][0]] + len(group[-1][1])
        content = "\n".join(line for _, line in group).strip()
        steps.append(StructuredItem("RESPONSE_STEP", content, start, end, order))
    return steps


def mentioned_codes(text: str, codes: set[str]) -> set[str]:
    """가이드 원문에 로그의 오류 코드 문자열이 그대로 나오는지(대소문자 무시, 단어 경계)."""
    found = set()
    for code in codes:
        if code and re.search(rf"(?<![A-Za-z0-9_]){re.escape(code)}(?![A-Za-z0-9_])", text, re.IGNORECASE):
            found.add(code)
    return found
