"""계층 1 결정론: 시간축(세션 병합 거부·시각 기준), 부분 순서, 이상 탐지, 유사도 (T-ML-01, T-NA-02·04·06·07, PRN-07)."""

from __future__ import annotations

import pytest

from app.analysis.anomaly_detector import detect
from app.analysis.case_retriever import compare
from app.core.sequence_analyzer import SessionMismatchError, build_timeline, check_order, lcs_ratio
from app.models.enums import EventTimeKind, TimestampBasis
from app.models.records import EventRecord, LogFileRecord, PatternRecord
from tests.unit.test_pattern_analyzer import features

NOW = "2026-11-16T00:00:00.000Z"


def record(file_id: int, session_id: int, basis: str = "ABSOLUTE", **kw) -> LogFileRecord:
    return LogFileRecord(id=file_id, session_id=session_id, original_name=f"f{file_id}.log", stored_path="x",
                         sha256="0" * 64, size_bytes=1, timestamp_basis=TimestampBasis(basis),
                         origin_purpose="ANALYSIS", imported_at=NOW, **kw)


def event(file_id: int, seq: int, ts: int | None, kind: str = "ABSOLUTE") -> EventRecord:
    return EventRecord(id=file_id * 100 + seq, log_file_id=file_id, seq=seq, line_no=seq + 1, ts_value_us=ts,
                       ts_kind=EventTimeKind(kind if ts is not None else "NONE"), message=f"e{seq}", raw_text=f"e{seq}")


def test_different_sessions_never_share_a_timeline() -> None:  # T-ML-01
    with pytest.raises(SessionMismatchError):
        build_timeline([(record(1, 1), [event(1, 0, 10)]), (record(2, 2), [event(2, 0, 5)])])


def test_same_session_absolute_times_are_merged() -> None:
    t = build_timeline([(record(1, 1), [event(1, 0, 10), event(1, 1, 30)]), (record(2, 1), [event(2, 0, 20)])])
    assert t.merged_by_time and [e.corrected_us for e in t.entries] == [10, 20, 30]


def test_unsynced_clocks_keep_file_order_only() -> None:  # T-NA-07
    t = build_timeline([(record(1, 1, "RELATIVE"), [event(1, 0, 10, "RELATIVE")]),
                        (record(2, 1), [event(2, 0, 5)])])
    assert not t.merged_by_time and t.notes
    assert [e.log_file_id for e in t.entries] == [1, 2]  # 파일 내부 순서만, 섞지 않음


def test_confirmed_offset_allows_merge() -> None:
    a = record(1, 1, "ABSOLUTE_NO_TZ", clock_offset_ms=100, offset_basis="TEST 동기화 이벤트")
    b = record(2, 1, "ABSOLUTE")
    t = build_timeline([(a, [event(1, 0, 0, "ABSOLUTE_NO_TZ")]), (b, [event(2, 0, 50_000)])], session_synced=True)
    assert t.merged_by_time and [e.log_file_id for e in t.entries] == [2, 1]  # 0 + 100ms 보정 > 50ms
    assert not build_timeline([(a, [event(1, 0, 0)]), (b, [event(2, 0, 50_000)])], session_synced=False).merged_by_time


def test_missing_timestamps_prevent_merge() -> None:  # T-NA-06
    t = build_timeline([(record(1, 1), [event(1, 0, None)]), (record(2, 1), [event(2, 0, 5)])])
    assert not t.merged_by_time


def test_check_order_and_lcs() -> None:
    f = features(1, ["A", "C", "B"])
    violations = check_order(f.first_event, [("A", "B"), ("B", "C"), ("A", "X")])
    assert [(v.before, v.after) for v in violations] == [("B", "C")]  # 없는 X는 판단하지 않음
    assert lcs_ratio(["A", "B", "C"], ["A", "C"]) == pytest.approx(2 / 3)


def pattern(pid: int, ptype: str, **definition) -> PatternRecord:
    definition.setdefault("log_role", "TEST-ROLE")
    return PatternRecord(id=pid, mode_id=1, pattern_type=ptype, definition=definition, basis="OBSERVED",
                         source="RULE", status="APPROVED")


def test_detector_finds_each_kind() -> None:  # T-NA-04
    f = features(1, ["A", "C", "B", "C", "C", "NEW"], stamps=[0, 100, 900_000, 950_000, 960_000, 970_000])
    f.first_event["NEW"] = f.first_event["NEW"].model_copy(update={"level": "ERROR", "error_code": "TEST-E001"})
    found = detect([f], [
        pattern(1, "REQUIRED", events=["A"]),
        pattern(2, "REQUIRED", events=["Z"]),
        pattern(3, "ORDER_CONSTRAINT", before="B", after="C", events=["B", "C"]),
        pattern(4, "REPEATED", events=["C"], min=1, max=2),
        pattern(5, "ORDER_CONSTRAINT", before="A", after="B", events=["A", "B"], observed_max_gap_us=500_000),
        pattern(6, "REQUIRED", events=["Q"], log_role="OTHER-ROLE"),  # 다른 역할 → 적용 안 함
    ], {"TEST-ROLE": {"A", "B", "C"}})
    kinds = {(x.finding_type, x.pattern_id) for x in found}
    assert ("MISSING_EVENT", 2) in kinds and ("MISSING_EVENT", 1) not in kinds and ("MISSING_EVENT", 6) not in kinds
    assert ("ORDER_VIOLATION", 3) in kinds and ("REPETITION", 4) in kinds and ("LATENCY", 5) in kinds
    assert any(x.finding_type == "EXPLICIT_ERROR" and "TEST-E001" in x.summary for x in found)
    assert any(x.finding_type == "EXTRA_EVENT" and x.detail["event_type"] == "NEW" for x in found)
    latency = next(x for x in found if x.finding_type == "LATENCY")
    assert "관찰된 최대값" in latency.summary  # 사양이 아니라 관찰값과 비교했다고 말한다


def test_reordered_normal_without_constraint_is_not_violation() -> None:  # PRN-07, T-NA-02
    f = features(1, ["A", "C", "B"])
    found = detect([f], [pattern(1, "REQUIRED", events=["A"]), pattern(2, "INDEPENDENT", events=["B"], unordered_with=["C"])],
                   {"TEST-ROLE": {"A", "B", "C"}})
    assert found == []


def test_similarity_is_bounded_score() -> None:
    a = [features(1, ["A", "B", "C"])]
    same = compare(a, [features(2, ["A", "B", "C"])])
    other = compare(a, [features(3, ["X", "Y"])])
    assert same[0] == pytest.approx(1.0) and other[0] == 0.0
