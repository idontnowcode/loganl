"""시간축과 부분 순서 (계획서 §4.1·§4.3, §6.3 계층 1, 결정론).

- 서로 다른 수집 세션의 로그는 하나의 시간축으로 합치지 않는다 → SessionMismatchError (MLG-03).
- 같은 세션 안에서도 모든 파일이 시간대 있는 절대시각이거나, 확인된 오프셋(근거 있음)이 있을 때만
  파일 사이를 시각으로 섞는다. 그 밖에는 파일마다 내부 순서만 유지한다(MLG-05~07).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from app.models.records import EventRecord, LogFileRecord


class SessionMismatchError(ValueError):
    """세션이 다른 로그를 한 시간축으로 합치려 함."""


@dataclass
class TimelineEntry:
    event: EventRecord
    log_file_id: int
    corrected_us: int | None


@dataclass
class Timeline:
    entries: list[TimelineEntry]
    merged_by_time: bool
    notes: list[str] = field(default_factory=list)


def build_timeline(files: Sequence[tuple[LogFileRecord, Sequence[EventRecord]]], *, session_synced: bool = False) -> Timeline:
    if len({record.session_id for record, _ in files}) > 1:
        raise SessionMismatchError("서로 다른 수집 세션의 로그는 하나의 시간축으로 합치지 않습니다(계획서 §4.1).")
    entries: list[TimelineEntry] = []
    for record, events in files:
        use_offset = session_synced and record.clock_offset_ms is not None and bool(record.offset_basis)
        offset_us = record.clock_offset_ms * 1000 if use_offset else 0
        for event in events:
            corrected = event.ts_value_us + offset_us if event.ts_value_us is not None else None
            entries.append(TimelineEntry(event, record.id, corrected))
    if len(files) <= 1:
        return Timeline(entries, False, [])

    notes: list[str] = []
    can_merge = True
    for record, _ in files:
        basis = str(record.timestamp_basis)
        offset_ok = session_synced and record.clock_offset_ms is not None and bool(record.offset_basis)
        if basis != "ABSOLUTE" and not (offset_ok and basis in ("ABSOLUTE", "ABSOLUTE_NO_TZ")):
            can_merge = False
            notes.append(f"'{record.original_name}'은(는) 시각 기준이 {basis}이고 확인된 오프셋이 없어 "
                         "다른 파일과 시각으로 섞지 않습니다.")
    if can_merge and any(e.corrected_us is None for e in entries):
        can_merge = False
        notes.append("시각이 없는 이벤트가 있어 파일 사이 순서를 정하지 않습니다.")
    if can_merge:
        entries.sort(key=lambda e: (e.corrected_us, e.log_file_id, e.event.seq))
    return Timeline(entries, can_merge, notes)


@dataclass
class OrderViolation:
    before: str
    after: str
    before_event: EventRecord
    after_event: EventRecord


def check_order(first_events: dict[str, EventRecord], constraints: Sequence[tuple[str, str]]) -> list[OrderViolation]:
    """같은 파일 안에서 a가 b보다 먼저여야 하는데 b의 첫 등장이 a보다 앞서면 위반. 둘 중 하나가 없으면 판단하지 않는다
    (없는 것은 '이벤트 누락'에서 다룬다)."""
    out = []
    for before, after in constraints:
        a, b = first_events.get(before), first_events.get(after)
        if a is not None and b is not None and b.seq < a.seq:
            out.append(OrderViolation(before, after, a, b))
    return out


def lcs_ratio(a: Sequence[str], b: Sequence[str], limit: int = 200) -> float:
    """두 순서의 최장 공통 부분열 비율(0~1). 순서 유사도 점수용이며 확률이 아니다."""
    a, b = list(a[:limit]), list(b[:limit])
    if not a or not b:
        return 0.0
    previous = [0] * (len(b) + 1)
    for x in a:
        current = [0]
        for j, y in enumerate(b, start=1):
            current.append(previous[j - 1] + 1 if x == y else max(previous[j], current[j - 1]))
        previous = current
    return previous[-1] / max(len(a), len(b))
