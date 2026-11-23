"""CR-17 숫자 관계(값 = 값, 값 = 횟수). 이벤트 이름·값은 가상이다."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from app.analysis.value_relations import MIN_FILES, STRONG_FILES, is_relation, relation_findings

START, END, ITEM = "TEST-SCHED|LOT_START count=<N>", "TEST-SCHED|LOT_END processed=<N>", "TEST-ROBOT|ITEM_DONE"


@dataclass
class Ev:
    id: int
    line_no: int


def log(count: float, processed: float, items: int | None = None):
    """(숫자 칸, 이벤트 횟수). items: ITEM_DONE 줄 수(기본은 processed와 같음)."""
    fields = {(START, "count"): [(count, Ev(1, 3))], (END, "processed"): [(processed, Ev(2, 90))]}
    counts = Counter({START: 1, END: 1, ITEM: int(processed if items is None else items)})
    return fields, counts


def normals(n: int):
    logs = [log(v, v) for v in [12 + (i % 6) for i in range(n)]]
    return [f for f, _c in logs], [c for _f, c in logs]


def run(target, n: int):
    ref_fields, ref_counts = normals(n)
    return relation_findings(target[0], target[1], ref_fields, ref_counts, 7, {"basis": "T"})


def test_mismatch_found_and_strength_grows_with_normals() -> None:
    strong = run(log(17, 16), STRONG_FILES)
    equal = next(f for f in strong if f.detail["kind"] == "value_equal")
    assert equal.stance == "SUPPORTS" and not equal.detail["candidate"] and is_relation(equal)
    assert equal.line_start == 90 and "17" in equal.summary and "16" in equal.summary  # 먼저 나온 줄(count)이 앞
    assert equal.summary.index("count") < equal.summary.index("processed")
    count = next(f for f in strong if f.detail["kind"] == "value_count")
    assert "ITEM_DONE" in count.summary and count.detail["counts"] == {ITEM: 16}
    weak = run(log(17, 16), MIN_FILES)
    assert weak and all(f.stance == "NEUTRAL" and f.detail["candidate"] for f in weak)  # 정상 로그가 적으면 후보만
    assert run(log(17, 16), MIN_FILES - 1) == []


def test_no_finding_when_relation_holds_or_is_not_a_relation() -> None:
    assert run(log(15, 15), STRONG_FILES) == []  # 관계가 지켜짐
    constant = [log(10, 10) for _ in range(STRONG_FILES)]  # 늘 같은 값이면 관계가 아님(값 3가지 이상)
    assert relation_findings(log(17, 16)[0], log(17, 16)[1], [f for f, _c in constant], [c for _f, c in constant],
                             7, {}) == []
    ref_fields, ref_counts = normals(STRONG_FILES)
    broken = log(14, 13)  # 정상 로그 하나라도 어긋나면 관계가 아님(count=processed, count=ITEM 모두 깨짐)
    assert relation_findings(log(17, 16)[0], log(17, 16)[1], ref_fields + [broken[0]], ref_counts + [broken[1]],
                             7, {}) == []
