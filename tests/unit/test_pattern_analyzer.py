"""정상 패턴 후보 (계획서 §3.1 정상 패턴 유형, §6.1, NL-10·16~19)."""

from __future__ import annotations

from collections import Counter

from app.core.feature_extractor import FileFeatures
from app.core.pattern_analyzer import propose_patterns
from app.models.enums import EventTimeKind
from app.models.records import EventRecord


def features(file_id: int, sequence: list[str], role: str | None = "TEST-ROLE", stamps: list[int] | None = None) -> FileFeatures:
    f = FileFeatures(file_id, 1, role, "ABSOLUTE", list(sequence))
    for index, key in enumerate(sequence):
        f.type_counts[key] += 1
        if key not in f.first_index:
            f.first_index[key] = index
            f.first_event[key] = EventRecord(
                id=file_id * 100 + index, log_file_id=file_id, seq=index, line_no=index + 1,
                ts_kind=EventTimeKind.ABSOLUTE if stamps else EventTimeKind.NONE,
                ts_value_us=stamps[index] if stamps else None, message=key, raw_text=key, event_type=key,
            )
    return f


def by_type(candidates):
    out: dict[str, list] = {}
    for c in candidates:
        out.setdefault(c.pattern_type, []).append(c)
    return out


def test_single_log_is_unclassified() -> None:
    result = by_type(propose_patterns([features(1, ["A", "B", "B"])]))
    assert {tuple(c.definition["events"]) for c in result["UNCLASSIFIED"]} == {("A",), ("B",)}
    assert "REQUIRED" not in result and "ORDER_CONSTRAINT" not in result
    assert result["REPEATED"][0].definition["min"] == 2  # 반복은 로그 1개로도 관찰값을 남긴다


def test_required_optional_and_repeated() -> None:
    result = by_type(propose_patterns([
        features(1, ["A", "B", "C", "C"]),
        features(2, ["A", "B", "C", "X"]),
    ]))
    assert {c.definition["events"][0] for c in result["REQUIRED"]} == {"A", "B", "C"}
    assert [c.definition["events"][0] for c in result["OPTIONAL"]] == ["X"]
    repeated = result["REPEATED"][0].definition
    assert repeated["events"] == ["C"] and (repeated["min"], repeated["max"]) == (1, 2)
    assert all(c.observed_count >= 1 and c.definition["sample_files"] == 2 for c in result["REQUIRED"])


def test_order_constraints_keep_only_direct_links() -> None:
    result = by_type(propose_patterns([
        features(1, ["A", "B", "C"], stamps=[0, 100, 300]),
        features(2, ["A", "B", "C"], stamps=[0, 200, 250]),
    ]))
    pairs = {(c.definition["before"], c.definition["after"]) for c in result["ORDER_CONSTRAINT"]}
    assert pairs == {("A", "B"), ("B", "C")}  # A→C는 A→B→C로 설명되므로 뺀다
    ab = next(c for c in result["ORDER_CONSTRAINT"] if c.definition["before"] == "A")
    assert (ab.definition["observed_min_gap_us"], ab.definition["observed_max_gap_us"]) == (100, 200)


def test_varying_order_becomes_independent_not_violation() -> None:
    """정상 로그의 순서가 달라도 비정상으로 보지 않는다(§1.3) → 순서 제약을 만들지 않고 독립 후보로."""
    result = by_type(propose_patterns([
        features(1, ["A", "B", "C"]),
        features(2, ["A", "C", "B"]),
    ]))
    pairs = {(c.definition["before"], c.definition["after"]) for c in result.get("ORDER_CONSTRAINT", [])}
    assert ("B", "C") not in pairs and ("C", "B") not in pairs
    assert ("A", "B") in pairs and ("A", "C") in pairs
    independent = {c.definition["events"][0]: c.definition["unordered_with"] for c in result["INDEPENDENT"]}
    assert independent == {"B": ["C"], "C": ["B"]}


def test_roles_are_compared_separately() -> None:
    result = propose_patterns([
        features(1, ["A", "B"], role="TEST-ROLE-1"),
        features(2, ["A", "B"], role="TEST-ROLE-1"),
        features(3, ["Z"], role="TEST-ROLE-2"),
    ])
    roles = {(c.definition["log_role"], c.pattern_type) for c in result}
    assert ("TEST-ROLE-1", "REQUIRED") in roles
    assert ("TEST-ROLE-2", "UNCLASSIFIED") in roles  # 다른 컴포넌트 로그 때문에 A·B가 선택으로 바뀌지 않는다
    assert ("TEST-ROLE-1", "OPTIONAL") not in roles


def test_all_candidates_are_observed_proposals() -> None:
    for c in propose_patterns([features(1, ["A"]), features(2, ["A"])]):
        assert c.note  # 무엇을 관찰했는지 설명이 있다
        assert isinstance(c.definition, dict)
    assert Counter(c.pattern_type for c in propose_patterns([features(1, ["A"]), features(2, ["A"])])) == {"REQUIRED": 1}
