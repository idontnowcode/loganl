"""AI 가이드·매뉴얼 구조화 (계획서 §3.2 8단계, CR-01).

AI는 **이미 있는 원문 조각에 항목 이름만 붙인다.** 새 문장을 쓰지 않으므로 가이드에 없는 원인·조치가
생길 수 없다(AI_POLICY §1 "가이드에 없는 원인·조치 추가 금지"). 조각 키(S-0001)는 묶음에 있는 것만 인정한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, Field

from app.ai.context_builder import DATA_RULES, EvidencePackage
from app.ai.ollama_client import OllamaClient
from app.ai.response_validator import StructuredResult, ask_structured
from app.core.cancel import NEVER_CANCEL, CancelToken

_ITEM = re.compile(r"^(?:\d{1,3}[.)]|[-*•]|[①-⑳])\s*\S")
GUIDE_SECTIONS = ("SYMPTOM", "CONDITION", "CRITERIA", "CAUSE", "CHECK_METHOD", "RESOLUTION", "VERIFICATION", "NOTE")
MANUAL_SECTIONS = ("RESPONSE_STEP", "PRECAUTION", "ESCALATION", "VERIFICATION", "NOTE")
SECTION_HELP = {
    "SYMPTOM": "오류 증상", "CONDITION": "발생 조건", "CRITERIA": "비정상 판정 기준", "CAUSE": "가능한 원인",
    "CHECK_METHOD": "원인 확인 방법", "RESOLUTION": "해결 방법", "VERIFICATION": "조치 후 검증 방법",
    "NOTE": "추가 참고사항", "RESPONSE_STEP": "대응 절차의 한 단계", "PRECAUTION": "주의·안전 사항",
    "ESCALATION": "상위 지원 요청 기준",
}


@dataclass
class Segment:
    text: str
    span_start: int
    span_end: int


class SegmentLabel(BaseModel):
    segment: str
    section: str


class SegmentLabels(BaseModel):
    labels: list[SegmentLabel] = Field(default_factory=list)


def split_segments(text: str) -> list[Segment]:
    """빈 줄로 나눈 문단, 문단 안의 번호·글머리 줄은 따로 나눈다."""
    segments: list[Segment] = []
    position = 0
    for block in text.split("\n\n"):
        block_start = text.find(block, position)
        position = block_start + len(block)
        line_offset = block_start
        current: list[tuple[int, str]] = []
        for line in block.split("\n"):
            stripped = line.strip()
            is_item = bool(_ITEM.match(stripped))
            if stripped and (is_item and current):
                segments.append(_segment(text, current))
                current = []
            if stripped:
                current.append((line_offset, line))
            line_offset += len(line) + 1
        if current:
            segments.append(_segment(text, current))
    return segments


def _segment(text: str, lines: list[tuple[int, str]]) -> Segment:
    start = lines[0][0]
    end = lines[-1][0] + len(lines[-1][1])
    return Segment(text[start:end].strip(), start, end)


def label_segments(client: OllamaClient, model: str | None, segments: list[Segment], kind: str,
                   cancel: CancelToken = NEVER_CANCEL) -> tuple[StructuredResult[SegmentLabels], EvidencePackage]:
    allowed = MANUAL_SECTIONS if kind == "RESPONSE_MANUAL" else GUIDE_SECTIONS
    package = EvidencePackage(prefix="S")
    for segment in segments:
        package.add("GUIDE", segment.text, span_start=segment.span_start, span_end=segment.span_end)
    sections_help = "\n".join(f"- {s}: {SECTION_HELP[s]}" for s in allowed)
    system = ("너는 문서 조각마다 항목 이름을 붙이는 보조자다. " + DATA_RULES +
              " 새 문장을 쓰지 말고, 조각 키와 항목 이름만 답하라. 어느 항목인지 모르면 NOTE로 둔다.")
    document = "대응 매뉴얼" if kind == "RESPONSE_MANUAL" else "진단 가이드"
    prompt = f"문서 종류: {document}\n쓸 수 있는 항목:\n{sections_help}\n\n조각 목록:\n{package.render(16_000)}"

    def keys_and_sections(value: SegmentLabels) -> list[str]:
        errors = []
        for label in value.labels:
            if label.segment not in package:
                errors.append(f"V-3: 없는 조각 키 {label.segment}")
            if label.section not in allowed:
                errors.append(f"쓸 수 없는 항목 이름 {label.section}")
        return errors

    result = ask_structured(client, model, system=system, prompt=prompt, schema=SegmentLabels, package=package,
                            extra_checks=[keys_and_sections], cancel=cancel)
    return result, package
