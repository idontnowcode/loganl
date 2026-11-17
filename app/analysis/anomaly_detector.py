"""계층 1 — 결정론 이상 탐지 (계획서 §6.3: 명시적 오류·이벤트 누락·순서 제약·응답 지연·반복 패턴).

승인된 패턴(approved_event_patterns 뷰)만 기준으로 쓴다. 패턴은 같은 로그 역할의 파일에만 적용한다.
'정상 로그의 순서가 달라도 무조건 비정상으로 판단하지 않는다'(§1.3): 순서는 사용자가 승인한 순서 제약만 본다.
지연·반복은 '승인된 정상 사례에서 관찰된 범위'와 비교한 사실만 말한다(장비 사양을 지어내지 않음).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from app.core.feature_extractor import ERROR_LEVELS, FileFeatures
from app.core.sequence_analyzer import check_order
from app.models.records import PatternRecord

MAX_EXPLICIT = 50
MAX_EXTRA = 30


@dataclass
class Finding:
    layer: str
    finding_type: str
    summary: str
    stance: str = "NEUTRAL"  # SUPPORTS: 이상 쪽 근거, CONTRADICTS: 정상 쪽 근거, NEUTRAL: 참고
    severity: str = "INFO"  # INFO / SUSPECT / VIOLATION
    log_file_id: int | None = None
    event_id: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    case_id: int | None = None
    pattern_id: int | None = None
    guide_item_id: int | None = None
    similarity: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def applies(pattern: PatternRecord, features: FileFeatures) -> bool:
    return pattern.definition.get("log_role") == features.log_role


def detect(files: Sequence[FileFeatures], patterns: Sequence[PatternRecord],
           normal_types_by_role: dict[str | None, set[str]] | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for f in files:
        findings.extend(_explicit_errors(f))
        mine = [p for p in patterns if applies(p, f)]
        findings.extend(_missing(f, mine))
        findings.extend(_order(f, mine))
        findings.extend(_repetition(f, mine))
        findings.extend(_latency(f, mine))
        if normal_types_by_role is not None and f.log_role in normal_types_by_role:
            findings.extend(_extra(f, normal_types_by_role[f.log_role]))
    return findings


def _explicit_errors(f: FileFeatures) -> list[Finding]:
    out = []
    for key, event in f.first_event.items():
        level = (event.level or "").upper()
        if level in ERROR_LEVELS or event.error_code:
            what = f"오류 코드 {event.error_code}" if event.error_code else f"{level} 레벨"
            out.append(Finding(
                "DETERMINISTIC", "EXPLICIT_ERROR",
                f"{what} 이벤트가 {f.type_counts[key]}회 기록됨 (처음 {event.line_no}행): {event.message.splitlines()[0][:120]}",
                stance="SUPPORTS", severity="SUSPECT", log_file_id=f.log_file_id, event_id=event.id,
                line_start=event.line_no, line_end=event.line_end or event.line_no,
                detail={"event_type": key, "count": f.type_counts[key], "error_code": event.error_code, "level": level},
            ))
    return out[:MAX_EXPLICIT]


def _missing(f: FileFeatures, patterns: Sequence[PatternRecord]) -> list[Finding]:
    out = []
    for p in patterns:
        if p.pattern_type != "REQUIRED":
            continue
        for key in p.definition.get("events", []):
            if key not in f.type_counts:
                out.append(Finding(
                    "DETERMINISTIC", "MISSING_EVENT",
                    f"승인된 필수 이벤트 '{key}'가 이 로그에 없음", stance="SUPPORTS", severity="VIOLATION",
                    log_file_id=f.log_file_id, pattern_id=p.id, detail={"event_type": key},
                ))
    return out


def _order(f: FileFeatures, patterns: Sequence[PatternRecord]) -> list[Finding]:
    constraints = {(p.definition["before"], p.definition["after"]): p for p in patterns
                   if p.pattern_type == "ORDER_CONSTRAINT" and "before" in p.definition}
    out = []
    for violation in check_order(f.first_event, list(constraints)):
        p = constraints[(violation.before, violation.after)]
        out.append(Finding(
            "DETERMINISTIC", "ORDER_VIOLATION",
            f"승인된 순서 제약 위반: '{violation.after}'({violation.after_event.line_no}행)가 "
            f"'{violation.before}'({violation.before_event.line_no}행)보다 먼저 나옴",
            stance="SUPPORTS", severity="VIOLATION", log_file_id=f.log_file_id, event_id=violation.after_event.id,
            line_start=violation.after_event.line_no, pattern_id=p.id,
            detail={"before": violation.before, "after": violation.after},
        ))
    return out


def _repetition(f: FileFeatures, patterns: Sequence[PatternRecord]) -> list[Finding]:
    out = []
    for p in patterns:
        if p.pattern_type != "REPEATED":
            continue
        for key in p.definition.get("events", []):
            count = f.type_counts.get(key, 0)
            low, high = p.definition.get("min"), p.definition.get("max")
            if count and low is not None and high is not None and not (low <= count <= high):
                event = f.first_event[key]
                out.append(Finding(
                    "DETERMINISTIC", "REPETITION",
                    f"'{key}'가 {count}회 나옴. 승인된 정상 사례에서 관찰된 범위는 {low}~{high}회",
                    stance="SUPPORTS", severity="SUSPECT", log_file_id=f.log_file_id, event_id=event.id,
                    line_start=event.line_no, pattern_id=p.id, detail={"count": count, "min": low, "max": high},
                ))
    return out


def _latency(f: FileFeatures, patterns: Sequence[PatternRecord]) -> list[Finding]:
    out = []
    for p in patterns:
        limit = p.definition.get("observed_max_gap_us")
        if p.pattern_type != "ORDER_CONSTRAINT" or limit is None:
            continue
        a, b = f.first_event.get(p.definition["before"]), f.first_event.get(p.definition["after"])
        if not a or not b or a.ts_value_us is None or b.ts_value_us is None or a.ts_kind != b.ts_kind:
            continue
        gap = b.ts_value_us - a.ts_value_us
        if gap > limit:
            out.append(Finding(
                "DETERMINISTIC", "LATENCY",
                f"'{p.definition['before']}' → '{p.definition['after']}' 간격 {gap / 1000:.1f}ms. "
                f"승인된 정상 사례에서 관찰된 최대값은 {limit / 1000:.1f}ms",
                stance="SUPPORTS", severity="SUSPECT", log_file_id=f.log_file_id, event_id=b.id, line_start=b.line_no,
                pattern_id=p.id, detail={"gap_us": gap, "observed_max_gap_us": limit},
            ))
    return out


def _extra(f: FileFeatures, normal_types: set[str]) -> list[Finding]:
    out = []
    for key, event in f.first_event.items():
        if key not in normal_types:
            out.append(Finding(
                "DETERMINISTIC", "EXTRA_EVENT",
                f"승인된 정상 사례에서 본 적 없는 이벤트 ({event.line_no}행): {event.message.splitlines()[0][:120]}",
                stance="NEUTRAL", severity="INFO", log_file_id=f.log_file_id, event_id=event.id,
                line_start=event.line_no, detail={"event_type": key},
            ))
    return out[:MAX_EXTRA]
