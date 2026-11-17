"""로그별 특징 추출 (계획서 §6.1 "로그별 특징 추출", 결정론)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Sequence

from app.models.records import EventRecord, LogFileRecord

ERROR_LEVELS = frozenset({"ERROR", "ERR", "FATAL", "CRIT", "CRITICAL", "ALERT", "EMERG", "SEVERE"})
WARNING_LEVELS = frozenset({"WARN", "WARNING"})


@dataclass
class FileFeatures:
    log_file_id: int
    session_id: int
    log_role: str | None
    timestamp_basis: str
    sequence: list[str]  # 이벤트 유형의 등장 순서(전체)
    type_counts: Counter = field(default_factory=Counter)
    first_index: dict[str, int] = field(default_factory=dict)
    first_event: dict[str, EventRecord] = field(default_factory=dict)
    error_codes: Counter = field(default_factory=Counter)
    level_counts: Counter = field(default_factory=Counter)
    duration_us: int | None = None

    @property
    def role_key(self) -> str:
        return self.log_role or "(역할 없음)"

    def error_events(self) -> list[EventRecord]:
        return [e for e in self.first_event.values() if (e.level or "").upper() in ERROR_LEVELS or e.error_code]


def extract(record: LogFileRecord, events: Sequence[EventRecord]) -> FileFeatures:
    features = FileFeatures(record.id, record.session_id, record.log_role, str(record.timestamp_basis), [])
    stamps: list[int] = []
    for index, event in enumerate(events):
        key = event.event_type or event.message
        features.sequence.append(key)
        features.type_counts[key] += 1
        if key not in features.first_index:
            features.first_index[key] = index
            features.first_event[key] = event
        if event.error_code:
            features.error_codes[event.error_code] += 1
        if event.level:
            features.level_counts[event.level.upper()] += 1
        if event.ts_value_us is not None:
            stamps.append(event.ts_value_us)
    if len(stamps) >= 2:
        features.duration_us = max(stamps) - min(stamps)
    return features


def first_occurrence_order(features: FileFeatures) -> list[str]:
    return sorted(features.first_index, key=features.first_index.__getitem__)
