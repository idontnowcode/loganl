"""CR-14 사례 기억(근거 지문·사례 판단·스스로 문턱 고르기). 데이터는 가상 값이다."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.analysis.anomaly_detector import Finding
from app.learning.case_memory import (
    MIN_PER_CLASS,
    Fingerprint,
    FingerprintStore,
    MemoryCase,
    calibrate,
    decide,
    feature_text,
    fingerprint_of,
    load_cases,
    similarity,
    stale,
    weights,
)


@dataclass
class FakeCase:
    id: int
    case_type: str
    label: str = ""


def fp(strong=(), weak=(), level: str | None = None, normals: int = 10) -> Fingerprint:
    level = level or ("SUSPECTED" if strong else "NONE_FOUND")
    return Fingerprint(frozenset(strong), frozenset(weak), level, normals)


def case(i: int, abnormal: bool, fingerprint: Fingerprint) -> MemoryCase:
    return MemoryCase(FakeCase(i, "ABNORMAL" if abnormal else "NORMAL", f"C-{i:04d}"), 100 + i, fingerprint)


def test_feature_text_drops_position_and_value() -> None:
    a = Finding("DETERMINISTIC", "VALUE_OUT_OF_RANGE", "'temp' 값 441(10행)", stance="SUPPORTS", line_start=10,
                detail={"kind": "value_groups", "field": "temp", "event_type": "TEST-CELL|TEST-TEL temp=<N>", "value": 441})
    b = Finding("DETERMINISTIC", "VALUE_OUT_OF_RANGE", "'temp' 값 377(90행)", stance="SUPPORTS", line_start=90,
                detail={"kind": "value_groups", "field": "temp", "event_type": "TEST-CELL|TEST-TEL temp=<N>", "value": 377})
    assert feature_text(a) == feature_text(b) == "VALUE_OUT_OF_RANGE/value_groups:temp@TEST-CELL|TEST-TEL temp=<N>"
    code = Finding("DETERMINISTIC", "EXPLICIT_ERROR", "x", detail={"error_code": "TEST-E710", "event_type": "y"})
    assert feature_text(code) == "EXPLICIT_ERROR:TEST-E710"
    flow = Finding("DETERMINISTIC", "ORDER_VIOLATION", "x", detail={"kind": "flow", "field": "sub", "values": ["W1"]})
    assert feature_text(flow) == "ORDER_VIOLATION/flow:sub"


def test_fingerprint_splits_strong_and_weak_and_skips_retrieval() -> None:
    findings = [Finding("DETERMINISTIC", "EXPLICIT_ERROR", "x", stance="SUPPORTS", detail={"error_code": "TEST-E1"}),
                Finding("DETERMINISTIC", "EXTRA_EVENT", "x", detail={"event_type": "TEST-AUX"}),
                Finding("RETRIEVAL", "SIMILAR_NORMAL_CASE", "x", case_id=3)]
    made = fingerprint_of(findings, "SUSPECTED", 12)
    assert made.strong == {"EXPLICIT_ERROR:TEST-E1"} and made.weak == {"EXTRA_EVENT:TEST-AUX"} and made.normals == 12


def test_store_round_trip_and_staleness(tmp_path: Path) -> None:
    store = FingerprintStore(tmp_path)
    store.put(7, fp(["A"], ["B"], normals=10))
    assert store.get(7) == fp(["A"], ["B"], normals=10) and store.get(8) is None
    assert not stale(fp(normals=10), 19) and stale(fp(normals=10), 21)  # 2배 넘고 10개 넘게 늘면 다시 만든다


def test_rare_features_weigh_more() -> None:
    cases = [case(i, False, fp(weak=["COMMON"])) for i in range(9)] + [case(9, True, fp(["RARE", "COMMON"]))]
    w = weights(cases)
    assert w["RARE"] > w["COMMON"]
    assert similarity(frozenset({"RARE"}), frozenset({"RARE", "COMMON"}), w) > 0.5


def memory_set() -> list[MemoryCase]:
    """확인된 비정상: 같은 오류 코드 3개 + 값 이탈 3개. 확인된 정상: 정비 알림 때문에 '빠짐'이 늘 나오는 3개 + 깨끗한 3개."""
    out = [case(i, True, fp(["EXPLICIT_ERROR:TEST-E7", "MISSING:LOT_END"])) for i in range(3)]
    out += [case(3 + i, True, fp(weak=["VALUE_GROUPS:temp"], level="NONE_FOUND")) for i in range(3)]  # 규칙은 놓침
    out += [case(6 + i, False, fp(["MISSING:CELL_A"], ["EXTRA_EVENT:CELL_OFF"])) for i in range(3)]  # 규칙은 오탐
    out += [case(9 + i, False, fp(weak=["EXTRA_EVENT:RARE_INFO"])) for i in range(3)]
    return out


def test_needs_both_kinds_before_changing_verdict() -> None:
    only_normals = [c for c in memory_set() if not c.abnormal]
    cal = calibrate(only_normals, weights(only_normals))
    assert cal.tau is None and "보정하지 않음" in cal.reason and cal.abnormals == 0 < MIN_PER_CLASS
    decision = decide(fp(["MISSING:CELL_A"], ["EXTRA_EVENT:CELL_OFF"]), only_normals)
    assert decision.level == "SUSPECTED" and decision.neighbors  # 판정은 그대로, 이웃은 AI·화면에 보인다


def test_learned_benign_difference_and_learned_miss() -> None:
    cases = memory_set()
    cal = calibrate(cases, weights(cases))
    assert cal.tau is not None and cal.memory_score > cal.rule_score  # 하나씩 빼고 판단해도 규칙만보다 낫다
    benign = decide(fp(["MISSING:CELL_A"], ["EXTRA_EVENT:CELL_OFF"]), cases)  # 확인된 정상과 같은 차이
    assert (benign.rule_level, benign.level) == ("SUSPECTED", "NONE_FOUND") and "확인된 로그 12개" in benign.note()
    miss = decide(fp(weak=["VALUE_GROUPS:temp"], level="NONE_FOUND"), cases)  # 확인된 비정상과 같은 약한 근거
    assert miss.level == "SUSPECTED" and all(c.abnormal for _s, c, _f in miss.neighbors)
    assert ("VALUE_GROUPS:temp", 3, 0) in miss.records
    error = decide(fp(["EXPLICIT_ERROR:TEST-E7", "MISSING:LOT_END"]), cases)
    assert error.level == "SUSPECTED" and not error.changed


def test_suspect_with_new_difference_is_not_downgraded() -> None:
    cases = memory_set()
    # 확인된 정상과 비슷하지만 정상 로그에 없던 강한 근거가 하나 더 있으면 내리지 않는다
    mixed = decide(fp(["MISSING:CELL_A", "REPETITION:NEW"], ["EXTRA_EVENT:CELL_OFF"]), cases)
    assert mixed.level == "SUSPECTED"


def test_load_cases_computes_missing_within_budget(tmp_path: Path) -> None:
    store = FingerprintStore(tmp_path)
    store.put(201, fp(["A"]))
    approved = [FakeCase(1, "ABNORMAL"), FakeCase(2, "NORMAL"), FakeCase(3, "NORMAL"), FakeCase(4, "NORMAL")]
    links = {1: [201], 2: [202], 3: [203], 4: [205, 204]}  # 4번은 같은 수집 세션 로그 묶음 사례
    made = []
    ticks = iter([0.0, 0.0, 99.0, 99.0])  # 시작 시각, 첫 계산 앞(예산 안), 그다음은 예산 넘김

    def compute(c, ids):
        made.append(ids)
        return fp(weak=[f"W{'-'.join(map(str, ids))}"])

    cases = load_cases(approved, links.__getitem__, store, 10, compute, 5.0, lambda: next(ticks))
    assert [c.log_file_id for c in cases] == [201, 202] and made == [[202]]
    assert store.get(202) == fp(weak=["W202"]) and store.get(203) is None  # 못 만든 것은 다음 분석 때
    ticks = iter([0.0] * 5)
    cases = load_cases(approved, links.__getitem__, store, 10, compute, 5.0, lambda: next(ticks))
    assert made[-1] == [204, 205] and store.get("204-205") == fp(weak=["W204-205"])  # 묶음 사례도 지문(열쇠는 작은 id부터)


def test_recomputed_fingerprint_keeps_ai_history(tmp_path: Path) -> None:
    """정상 사례가 늘어 지문을 다시 만들어도 그때 AI가 낸 가능성은 남는다(리뷰 2026-11-22)."""
    from dataclasses import replace

    store = FingerprintStore(tmp_path)
    store.put(301, replace(fp(["A"], normals=5), ai_ran=True, ai_likelihood=80))
    ticks = iter([0.0, 0.0])
    cases = load_cases([FakeCase(1, "ABNORMAL")], {1: [301]}.__getitem__, store, 40,
                       lambda c, ids: fp(["A", "B"], normals=40), 5.0, lambda: next(ticks))
    assert cases[0].fp.strong == {"A", "B"} and cases[0].fp.ai_ran and cases[0].fp.ai_likelihood == 80
    assert store.get(301).ai_likelihood == 80

def test_ai_threshold_is_learned_from_confirmed_history() -> None:  # CM-07
    from dataclasses import replace

    from app.learning.case_memory import ai_judgment

    def with_ai(c: MemoryCase, likelihood: int | None) -> MemoryCase:
        return MemoryCase(c.case, c.log_file_id, replace(c.fp, ai_ran=True, ai_likelihood=likelihood))

    few = [with_ai(case(i, True, fp(["X"])), 85) for i in range(3)]
    first = ai_judgment(80, few)  # 확인된 정상 쪽 AI 이력이 없으면 기본 50%
    assert (first.threshold, first.calibrated, first.abnormal) == (50, False, True) and "지금 3개" in first.note()
    # AI가 정상 로그에도 70~80%를 내는 모델: 확인 이력으로 문턱이 85로 올라간다
    history = few + [with_ai(case(10 + i, False, fp()), v) for i, v in enumerate((70, 80, 75, None))]
    learned = ai_judgment(80, history)  # 가능성을 매기지 않은 확인 로그(None)는 문턱 이력에 넣지 않는다
    assert learned.calibrated and learned.threshold == 85 and learned.state == "NORMAL" and learned.history == 6
    held = ai_judgment(None, history)  # 가능성을 내지 않으면 정상 쪽이 아니라 판단 보류
    assert held.state == "UNDECIDED" and not held.decided and "판단 보류" in held.note()
    assert ai_judgment(85, history, ran=False).state == "UNDECIDED"  # AI 의견 실패도 보류
    untouched = MemoryCase(FakeCase(99, "NORMAL"), 199, fp())  # AI 의견이 없던 확인 로그는 이력에 안 들어간다
    assert ai_judgment(80, history + [untouched]).history == 6


def test_one_weakly_similar_case_does_not_flip_verdict() -> None:
    """유사도 0.5짜리 비정상 사례 하나만으로는 '찾은 이상 없음'을 '의심'으로 바꾸지 않는다(2026-11-22 애매한 세트 MC_031).
    유사도 1.0 하나나 0.5 둘(합 1.0 이상)이면 바꾼다."""
    from app.learning.case_memory import _adjust

    odd = case(20, True, fp(weak=["EXTRA_EVENT:ODD"], level="NONE_FOUND"))
    twin = case(21, True, fp(weak=["EXTRA_EVENT:ODD"], level="NONE_FOUND"))
    empty = frozenset()
    assert _adjust("NONE_FOUND", empty, 1.0, [(0.5, odd)]) == "NONE_FOUND"
    assert _adjust("NONE_FOUND", empty, 1.0, [(0.5, odd), (0.5, twin)]) == "SUSPECTED"
    assert _adjust("NONE_FOUND", empty, 1.0, [(1.0, odd)]) == "SUSPECTED"
