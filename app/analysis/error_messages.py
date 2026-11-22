"""비정상 로그의 '예상 에러 메시지' (변경 요청 CR-11, EM-01~04).

문제로 보이는 **원문 줄**을 골라 묶는다. 결정론만 쓴다(AI 없음). 뜻은 만들지 않는다(절대 규칙 2):
줄은 로그 원문 그대로, 고른 이유는 결정론 근거의 요약, 뜻은 승인된 참고 자료에 있을 때만 그 글 그대로.

- presumed_errors: 분석 결과의 근거(Finding 또는 저장된 근거 행)에서 고른다(신규 분석·보고서).
- file_error_candidates: 정상 로그와 비교하지 않고 한 파일의 이벤트에서 고른다(로그 입력 탭).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any

from app.core.feature_extractor import ERROR_LEVELS, STAGE_LOOKBACK, is_stage, pick_stage
from app.models.records import EventRecord
from app.storage.database import Database
from app.storage.repositories import EventRepository

ERROR, DIFFERENT, NEW_WARNING, MISSING = "ERROR", "DIFFERENT", "NEW_WARNING", "MISSING"
WARNING = "WARNING"  # 로그 입력 탭: 정상 로그와 비교하지 않은 경고·실패 낱말 줄
GROUP_TITLES = {
    ERROR: "에러로 기록된 줄",
    DIFFERENT: "비슷한 정상 로그와 다른 줄",
    NEW_WARNING: "정상 로그에 없던 경고·실패 줄",
    MISSING: "평소 있던 메시지가 없음",
    WARNING: "경고·실패 낱말이 있는 줄",
}
GROUP_ORDER = {ERROR: 0, DIFFERENT: 1, NEW_WARNING: 2, WARNING: 2, MISSING: 3}
DIFFERENT_TYPES = frozenset({"VALUE_OUT_OF_RANGE", "VALUE_STUCK", "REPETITION", "ORDER_VIOLATION", "LATENCY",
                             "MISSING_EVENT"})
WARN_LEVELS = frozenset({"WARN", "WARNING"})
# 일반 낱말만(장비별 뜻이 아님). '이상'은 '이상 없음'처럼 정상에도 쓰여 뺐다.
FAIL_WORDS = re.compile(
    r"(?i)(?<![a-z])(fail(?:ed|ure|s)?|errors?|err|fault(?:s|y)?|abort(?:ed)?|alarm|timeout|timed\s+out|ng|"
    r"reject(?:ed)?|denied|crash(?:ed)?|panic|exception|warn(?:ing)?)(?![a-z])|실패|오류|에러|고장|중단|경보|경고")
SHOW_LEVELS = frozenset({"SUSPECTED", "RULE_VIOLATION"})
MAX_TEXT = 300


@dataclass
class ErrorMessage:
    group: str
    text: str  # 로그 원문 한 줄(빠진 메시지는 근거 요약)
    reason: str  # 고른 이유(결정론 근거 요약)
    log_file_id: int | None = None
    line: int | None = None
    count: int | None = None
    key: str | None = None  # 근거 키(EV-…)
    code: str | None = None
    reference: str | None = None  # 승인된 참고 자료 글 그대로
    reasons: list[str] = field(default_factory=list)  # 같은 줄을 가리킨 다른 근거
    stage: str | None = None  # 직전 단계 줄 원문(CR-13 SQ-01)
    stage_line: int | None = None
    context: list[str] = field(default_factory=list)  # 앞 3줄 원문(가까운 것이 아래)

    @property
    def group_title(self) -> str:
        return GROUP_TITLES[self.group]

    def as_dict(self) -> dict[str, Any]:
        return {"group": self.group, "group_title": self.group_title, "text": self.text, "reason": self.reason,
                "file_id": self.log_file_id, "line": self.line, "count": self.count, "key": self.key,
                "code": self.code, "reference": self.reference, "more_reasons": self.reasons,
                "stage": self.stage, "stage_line": self.stage_line, "context": self.context}


def should_show(anomaly_level: str | None, ai_likelihood: int | None) -> bool:
    """비정상으로 보이는 로그만: 이상 수준이 의심·규칙 위반이거나 AI 가능성 50% 이상(EM-01)."""
    return anomaly_level in SHOW_LEVELS or (ai_likelihood is not None and ai_likelihood >= 50)


def _first_line(text: str | None) -> str:
    return (text or "").strip().splitlines()[0][:MAX_TEXT] if (text or "").strip() else ""


def _get(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    try:
        return item[name]  # sqlite3.Row
    except (TypeError, KeyError, IndexError):
        return getattr(item, name, None)


def presumed_errors(findings: Sequence[Any], keys: Mapping[int, str] | None = None,
                    raw_lines: Mapping[int, str] | None = None, details: Sequence[dict] | None = None,
                    references: Mapping[str, str] | None = None, limit: int = 20) -> list[ErrorMessage]:
    """근거에서 예상 에러 메시지를 고른다.

    findings: Finding 또는 근거 행(finding_type·layer·stance·summary·log_file_id·event_id·line_start).
    keys: 근거 순번 → 근거 키, raw_lines: event_id → 원문, details: 근거마다 detail(행에서 읽은 경우),
    references: 오류 코드 → 승인된 참고 자료 글."""
    keys, raw_lines, references = keys or {}, raw_lines or {}, references or {}
    picked: dict[object, ErrorMessage] = {}
    for index, finding in enumerate(findings):
        if _get(finding, "layer") != "DETERMINISTIC":
            continue
        kind = _get(finding, "finding_type")
        detail = details[index] if details is not None else (_get(finding, "detail") or {})
        event_id = _get(finding, "event_id")
        summary = _get(finding, "summary") or ""
        raw = _first_line(raw_lines.get(event_id)) if event_id is not None else ""
        if kind == "EXPLICIT_ERROR":
            group = ERROR
        elif kind in DIFFERENT_TYPES and _get(finding, "stance") == "SUPPORTS":
            group = DIFFERENT if event_id is not None else MISSING
        elif kind == "EXTRA_EVENT" and (_get(finding, "stance") == "SUPPORTS" or FAIL_WORDS.search(raw or summary)):
            group = NEW_WARNING
        else:
            continue
        if group == MISSING:
            text = summary.split(": ", 1)[1] if ": " in summary else summary
        else:
            text = raw or (summary.split(": ", 1)[1] if ": " in summary else summary)
        slot: object = event_id if event_id is not None else (group, text)
        if slot in picked:  # 같은 줄을 다른 근거도 가리킴(예: 에러 + 연속 반복)
            picked[slot].reasons.append(summary)
            continue
        code = detail.get("error_code") if isinstance(detail, dict) else None
        picked[slot] = ErrorMessage(
            group=group, text=text[:MAX_TEXT], reason=summary, log_file_id=_get(finding, "log_file_id"),
            line=_get(finding, "line_start"), count=detail.get("count") if isinstance(detail, dict) else None,
            key=keys.get(index), code=code, reference=references.get(code) if code else None)
    ordered = sorted(picked.values(), key=lambda m: (GROUP_ORDER[m.group], m.line if m.line is not None else 1 << 30))
    return ordered[:limit]


CONTEXT_LINES = 3


def _component(event: EventRecord) -> str | None:
    return event.component


def _event_is_stage(event: EventRecord) -> bool:
    return is_stage(event.event_type) or is_stage(event.message)


def attach_context(database: Database, messages: Sequence[ErrorMessage]) -> None:
    """원본 행이 있는 메시지마다 직전 단계(가장 가까운 앞쪽 시작 표시 줄)와 앞 3줄을 원문 그대로 붙인다(SQ-01)."""
    with closing(database.connect()) as conn:
        repo = EventRepository(conn)
        for message in messages:
            if message.log_file_id is None or message.line is None:
                continue
            rows = repo.before(message.log_file_id, message.line + 1, STAGE_LOOKBACK + 1)  # 오류 줄 자신 + 앞 줄들
            own = rows[0] if rows and rows[0].line_no == message.line else None
            before = rows[1:] if own is not None else rows[:STAGE_LOOKBACK]
            message.context = [_first_line(e.raw_text) or _first_line(e.message) for e in before[:CONTEXT_LINES]][::-1]
            stage = pick_stage(before, own.component if own else None, _component, _event_is_stage)
            if stage is not None:
                message.stage = _first_line(stage.raw_text) or _first_line(stage.message)
                message.stage_line = stage.line_no


def reference_texts(matches: Iterable[Any]) -> dict[str, str]:
    """참고 자료 일치(ReferenceMatch 또는 보고서 dict) → 코드별 글. 첫 출처의 명칭·설명만, 글 그대로(EM-02)."""
    out: dict[str, str] = {}
    for match in matches:
        code = _get(match, "code")
        entries = _get(match, "entries") or []
        if not code or not entries:
            continue
        first = entries[0] if isinstance(entries[0], Mapping) else entries[0].as_dict()
        parts = [f"{label}: {first[name]}" for name, label in (("title", "명칭"), ("description", "설명")) if first.get(name)]
        if parts:
            out[code] = " / ".join(parts) + f" [출처 {first.get('document_title', '-')}]"
    return out


def file_error_candidates(database: Database, log_file_id: int, limit: int = 15) -> list[ErrorMessage]:
    """정상 로그와 비교하지 않은 후보(EM-04): 에러 레벨·오류 코드 줄, 그다음 경고 레벨·실패 낱말 줄.
    같은 이벤트 이름은 처음 한 줄만(횟수를 함께 센다)."""
    with closing(database.connect()) as conn:
        events = EventRepository(conn).for_files([log_file_id]).get(log_file_id, [])
    picked: dict[str, ErrorMessage] = {}
    stage = None  # 가장 가까운 시작 줄(SQ-01)
    stage_by_component: dict[str, EventRecord] = {}  # 구성요소마다 가장 가까운 시작 줄(SQ-03)
    for index, event in enumerate(events):
        previous = events[index - 1] if index else None
        if previous is not None and _event_is_stage(previous):
            stage = previous
            if previous.component:
                stage_by_component[previous.component] = previous
        level = (event.level or "").upper()
        raw = _first_line(event.raw_text) or _first_line(event.message)
        if level in ERROR_LEVELS or event.error_code:
            group = ERROR
            reason = f"오류 코드 {event.error_code}" if event.error_code else f"{level} 레벨"
        elif level in WARN_LEVELS or FAIL_WORDS.search(event.message or ""):
            group = WARNING
            reason = f"{level} 레벨" if level in WARN_LEVELS else "경고·실패 낱말"
        else:
            continue
        name = event.event_type or event.message
        if name in picked:
            picked[name].count = (picked[name].count or 1) + 1
            continue
        chosen = (stage_by_component.get(event.component) if event.component else None) or stage
        picked[name] = ErrorMessage(
            group=group, text=raw, reason=reason, log_file_id=log_file_id, line=event.line_no, count=1,
            code=event.error_code, context=[_first_line(e.raw_text) or _first_line(e.message)
                                            for e in events[max(0, index - CONTEXT_LINES):index]],
            stage=(_first_line(chosen.raw_text) or _first_line(chosen.message)) if chosen else None,
            stage_line=chosen.line_no if chosen else None)
    ordered = sorted(picked.values(), key=lambda m: (GROUP_ORDER[m.group], m.line or 0))
    return ordered[:limit]


def message_line(message: ErrorMessage) -> str:
    """화면 목록·보고서 글 한 줄(+이유 줄)."""
    where = f"{message.line}행" if message.line is not None else "—"
    times = f" ×{message.count}" if message.count and message.count > 1 else ""
    head = f"[{message.group_title}] {where}{times}  {message.text}"
    tail = (f"\n    직전 단계: {message.stage_line}행 {message.stage}" if message.stage else "") + \
        f"\n    이유: {message.reason}" + (f"\n    참고 자료: {message.reference}" if message.reference else "")
    return head + tail
