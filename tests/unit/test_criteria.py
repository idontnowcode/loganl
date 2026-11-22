"""CR-15 판단 질문·판단 기준. 데이터는 가상 값이다."""

from __future__ import annotations

from dataclasses import dataclass

from app.analysis.anomaly_detector import Finding
from app.learning.case_memory import Fingerprint, MemoryCase
from app.learning.criteria import (
    ABNORMAL_SIGN,
    BENIGN,
    HOLD,
    VALUE_RANGE,
    Criterion,
    apply_criteria,
    make_questions,
    self_check,
)

LINE = "EXTRA_EVENT:TEST-SYS|TEST-NOTICE flag=TEST-FK"
MISS = "MISSING_EVENT/component_absent:TEST-CELL-A"
VALUE = "VALUE_OUT_OF_RANGE/value_groups:temp@TEST-CELL|TEST-TEL temp=<N>"


@dataclass
class FakeCase:
    id: int
    case_type: str


@dataclass
class FakeEvent:
    event_type: str
    message: str


def crit(feature: str, kind: str, i: int = 1, **kw) -> Criterion:
    return Criterion(i, feature, kind, "질문", example=kw.pop("example", None), **kw)


def memory_case(i: int, abnormal: bool, strong=(), weak=()) -> MemoryCase:
    fp = Fingerprint(frozenset(strong), frozenset(weak), "SUSPECTED" if strong else "NONE_FOUND", 10)
    return MemoryCase(FakeCase(i, "ABNORMAL" if abnormal else "NORMAL"), 100 + i, fp)


def test_sign_benign_hold_and_rule_violation() -> None:
    sign = apply_criteria(frozenset(), frozenset({LINE}), [crit(LINE, ABNORMAL_SIGN, example="TEST-NOTICE flag=TEST-FK")],
                          [], "NONE_FOUND")
    assert (sign.rule_level, sign.level) == ("NONE_FOUND", "SUSPECTED") and "TEST-FK" in sign.reasons[0] and sign.signed
    calm = apply_criteria(frozenset({MISS}), frozenset({MISS}), [crit(MISS, BENIGN)], [], "SUSPECTED")
    assert calm.level == "NONE_FOUND" and "문제없음" in calm.reasons[0]
    mixed = apply_criteria(frozenset({MISS, "REPETITION:X"}), frozenset({MISS, "REPETITION:X"}), [crit(MISS, BENIGN)], [], "SUSPECTED")
    assert mixed.level == "SUSPECTED"  # 다른 강한 근거가 남아 있으면 내리지 않는다
    held = apply_criteria(frozenset({MISS}), frozenset({MISS}), [crit(MISS, HOLD)], [], "SUSPECTED")
    assert held.hold_decisive and held.hold and held.level == "SUSPECTED"  # 최종 판정에서 판단 보류가 된다
    rule = apply_criteria(frozenset(), frozenset({LINE}), [crit(LINE, ABNORMAL_SIGN)], [], "RULE_VIOLATION")
    assert rule.level == "RULE_VIOLATION"
    assert not apply_criteria(frozenset(), frozenset({"X"}), [crit(LINE, ABNORMAL_SIGN)], [], "NONE_FOUND").fired


def test_value_range_is_a_new_rule() -> None:
    temp = crit(VALUE, VALUE_RANGE, event_type="TEST-CELL|TEST-TEL temp=<N>", field="temp", low=None, high=360.0)
    events = [FakeEvent("TEST-CELL|TEST-TEL temp=<N>", f"TEST-TEL temp={v}") for v in (349.5, 351.0, 372.4)]
    hot = apply_criteria(frozenset(), frozenset(), [temp], events, "NONE_FOUND")  # 규칙은 못 잡았어도 엔지니어 범위로 잡는다
    assert hot.level == "SUSPECTED" and "372.4" in hot.reasons[0]
    fine = apply_criteria(frozenset({VALUE}), frozenset({VALUE}), [temp], events[:2], "SUSPECTED")
    assert fine.level == "NONE_FOUND"  # 값 근거만 있었는데 엔지니어 범위 안


def test_harmful_criteria_turn_themselves_off() -> None:
    cases = [memory_case(1, False, weak=[LINE]), memory_case(2, False, weak=[LINE]), memory_case(3, True, weak=[LINE]),
             memory_case(4, True, strong=[MISS]), memory_case(5, True, strong=[MISS]), memory_case(6, False, strong=[MISS])]
    off = dict((c.feature, reason) for c, reason in self_check([crit(LINE, ABNORMAL_SIGN), crit(MISS, BENIGN, 2)], cases))
    assert "정상 로그 2개" in off[LINE] and "비정상 로그가 2개" in off[MISS]
    assert self_check([crit(LINE, ABNORMAL_SIGN)], cases[:1]) == []  # 한 번 어긋난 것으로는 끄지 않는다


def finding(feature_type: str, **detail) -> Finding:
    return Finding("DETERMINISTIC", feature_type, "요약", stance="SUPPORTS" if feature_type != "EXTRA_EVENT" else "NEUTRAL",
                   event_id=detail.pop("event_id", None), line_start=12, log_file_id=1, detail=detail)


def test_questions_only_where_knowledge_is_missing() -> None:
    line = finding("EXTRA_EVENT", event_type="TEST-SYS|TEST-NOTICE flag=TEST-FK", event_id=7)
    miss = finding("MISSING_EVENT", kind="component_absent", component="TEST-CELL-A")
    value = finding("VALUE_OUT_OF_RANGE", kind="value_groups", field="temp", event_type="TEST-CELL|TEST-TEL temp=<N>", value=372.4)
    findings = [line, miss, value]
    strong, weak = frozenset({MISS, VALUE}), frozenset({LINE})
    raw = {7: "2026-11-22 10:00:00 [INFO] TEST-SYS TEST-NOTICE flag=TEST-FK"}
    asked = make_questions(findings, strong, weak, "ABNORMAL", [], [], raw)
    assert [q.kind for q in asked] == ["MISSING", "VALUE", "LINE"]  # 강한 근거 먼저, 처음 보는 줄도
    assert asked[2].example.endswith("flag=TEST-FK") and asked[2].line == 12 and asked[1].field == "temp"
    assert "372.4" in asked[1].text and any(kind == "BENIGN" for kind, _l in asked[0].answers)
    known = make_questions(findings, strong, weak, "ABNORMAL", [], [crit(MISS, BENIGN)], raw)
    assert MISS not in {q.feature for q in known}  # 이미 기준이 있으면 묻지 않음
    decisive = [memory_case(i, True, strong=[VALUE]) for i in range(3)]
    assert VALUE not in {q.feature for q in make_questions(findings, strong, weak, "ABNORMAL", decisive, [], raw)}
    seen_line = [memory_case(9, False, weak=[LINE])]  # 판정이 났으면, 이력이 있는 약한 근거는 묻지 않는다
    assert LINE not in {q.feature for q in make_questions(findings, strong, weak, "NORMAL", seen_line, [], raw)}
    assert LINE in {q.feature for q in make_questions(findings, strong, weak, "UNDECIDED", seen_line, [], raw)}
