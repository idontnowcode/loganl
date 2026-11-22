"""로그별 특징 추출 (계획서 §6.1 "로그별 특징 추출", 결정론)."""

from __future__ import annotations

import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Sequence

from app.models.records import EventRecord, LogFileRecord

ERROR_LEVELS = frozenset({"ERROR", "ERR", "FATAL", "CRIT", "CRITICAL", "ALERT", "EMERG", "SEVERE"})
WARNING_LEVELS = frozenset({"WARN", "WARNING"})
# 단계가 시작됨을 나타내는 일반 낱말(장비별 뜻이 아님, CR-13 SQ-01). 오류 줄 앞에서 가장 가까운 이런 줄이 '직전 단계'.
STAGE_WORDS = re.compile(r"(?i)(?<![a-z])(start(?:ed|ing|s)?|begin(?:s|ning)?|enter(?:ed|ing|s)?)(?![a-z])|시작|진입")
STAGE_LOOKBACK = 300  # 직전 단계를 찾을 때 거슬러 올라가는 이벤트 수


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
        key = sys.intern(event.event_type or event.message)  # 같은 유형 글자는 하나만(특징을 기억해 둘 때 메모리, PF-01)
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


def is_stage(text: str | None) -> bool:
    return bool(text and STAGE_WORDS.search(text))


def code_stages(features: FileFeatures) -> dict[str, str | None]:
    """오류 코드마다 처음 나온 줄의 직전 단계(가장 가까운 앞쪽 시작 표시 이벤트 유형). 같은 코드라도 시퀀스 위치를 구분하려고."""
    out: dict[str, str | None] = {}
    for event_type, event in features.first_event.items():
        if not event.error_code or event.error_code in out:
            continue
        index = features.first_index.get(event_type, 0)
        stage = next((features.sequence[i] for i in range(index - 1, max(-1, index - 1 - STAGE_LOOKBACK), -1)
                      if is_stage(features.sequence[i])), None)
        out[event.error_code] = stage
    return out


def first_occurrence_order(features: FileFeatures) -> list[str]:
    return sorted(features.first_index, key=features.first_index.__getitem__)
