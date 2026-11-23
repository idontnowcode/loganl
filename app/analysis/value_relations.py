"""숫자 관계 — 정상 로그에서 늘 같던 숫자 짝이 어긋남 (변경 요청 CR-17 VR-01~03, 결정론).

오류·경고 줄 없이 숫자만 어긋나는 비정상(예: 시작 줄 '계획 수'와 끝 줄 '처리 수'가 다름)을 잡는다. 이름·코드를 적어 넣지 않고
비슷한 정상 로그에서 관계를 배운다.

- 값 = 값: 로그에 몇 번만 나오는 이벤트의 숫자 칸 둘이, 둘 다 나온 정상 로그마다 나온 순서대로 모두 같음.
- 값 = 횟수: 그런 숫자 칸의 합이 어떤 이벤트가 나온 횟수와 늘 같음.
- 우연을 막는 조건: 둘 다 나온 정상 로그 5개 이상, 값이 3가지 이상, 하나도 어긋나지 않음.
- 세기: 늘 같았던 정상 로그가 10개 이상이면 강한 근거(SUPPORTS), 5~9개면 약한 후보(NEUTRAL, candidate) —
  약한 후보는 AI 확인(app/ai/candidate_check.py) 또는 엔지니어 답(판단 질문)을 기다린다.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from itertools import combinations
from typing import Any

from app.analysis.anomaly_detector import Finding

MAX_PER_LOG = 5  # 로그 하나에 이만큼 이하로 나오는 숫자 칸만(로트·실행 단위 값). 측정값 칸은 짝이 우연히 맞기 쉽다
MIN_FILES = 5
STRONG_FILES = 10
MIN_DISTINCT = 3
MAX_FINDINGS = 5
MAX_COUNT_TYPES = 3  # 값 = 횟수 어긋남 하나에 보여 줄 이벤트 수
# 근거 종류는 '값이 정상과 다름'(VALUE_OUT_OF_RANGE, DB 근거 종류 그대로)이고 세부 종류(kind)로 나눈다
RELATION_KINDS = frozenset({"value_equal", "value_count"})


def is_relation(finding: Any) -> bool:
    detail = getattr(finding, "detail", None) or {}
    return getattr(finding, "finding_type", None) == "VALUE_OUT_OF_RANGE" and detail.get("kind") in RELATION_KINDS

FieldKey = tuple[str, str]  # (이벤트 모양, 칸 이름)
Fields = Mapping[FieldKey, Sequence[tuple[float, Any]]]  # 칸 → [(값, 이벤트)] (reference_checks._numeric_fields)


def _few(fields: Fields) -> dict[FieldKey, list[float]]:
    return {key: [value for value, _e in items] for key, items in fields.items() if 1 <= len(items) <= MAX_PER_LOG}


def _num(value: float) -> str:
    return f"{value:g}"


def relation_findings(target_fields: Fields, target_counts: Counter, ref_fields: Sequence[Fields],
                      ref_counts: Sequence[Counter], log_file_id: int, detail: dict,
                      name: Callable[[str], str] = lambda t: t) -> list[Finding]:
    """target: 이 로그, ref: 비슷한 정상 로그들. name: 이벤트 모양 → 화면용 짧은 이름."""
    if len(ref_fields) < MIN_FILES:
        return []
    mine = _few(target_fields)
    if not mine:
        return []
    refs = [_few(f) for f in ref_fields]
    out: list[Finding] = []
    keys = sorted(mine)
    for a, b in combinations(keys, 2):  # 값 = 값
        support, distinct, broken = 0, set(), False
        for few in refs:
            if a in few and b in few:
                if few[a] != few[b]:
                    broken = True
                    break
                support += 1
                distinct.update(few[a])
        if broken or support < MIN_FILES or len(distinct) < MIN_DISTINCT or mine[a] == mine[b]:
            continue
        if list(target_fields[a])[0][1].line_no > list(target_fields[b])[0][1].line_no:
            a, b = b, a  # 먼저 나온 줄을 앞에
        out.append(_equal_finding(a, b, target_fields, support, len(distinct), log_file_id, detail, name))
        if len(out) >= MAX_FINDINGS:
            return out
    for a in keys:  # 값 = 횟수
        total = sum(mine[a])
        held: set[str] | None = None
        support, distinct = 0, set()
        for few, counts in zip(refs, ref_counts):
            if a not in few:
                continue
            s = sum(few[a])
            same = {event_type for event_type, n in counts.items() if n == s and event_type != a[0]}
            held = same if held is None else held & same
            support += 1
            distinct.add(s)
            if not held:
                break
        if not held or support < MIN_FILES or len(distinct) < MIN_DISTINCT:
            continue
        off = sorted(t for t in held if target_counts.get(t, 0) != total)
        if not off:
            continue
        out.append(_count_finding(a, off, total, target_fields, target_counts, support, len(distinct), log_file_id,
                                  detail, name))
        if len(out) >= MAX_FINDINGS:
            break
    return out


def _strength(support: int) -> dict:
    strong = support >= STRONG_FILES
    return {"stance": "SUPPORTS" if strong else "NEUTRAL", "candidate": not strong}


def _equal_finding(a: FieldKey, b: FieldKey, fields: Fields, support: int, distinct: int, log_file_id: int,
                   detail: dict, name) -> Finding:
    left, right = list(fields[a]), list(fields[b])
    index = next((i for i, (x, y) in enumerate(zip(left, right)) if x[0] != y[0]), min(len(left), len(right)) - 1)
    (lv, le), (rv, re_) = left[min(index, len(left) - 1)], right[min(index, len(right) - 1)]
    strength = _strength(support)
    summary = (f"'{a[1]}'({name(a[0])}, {le.line_no}행) {_num(lv)}과(와) '{b[1]}'({name(b[0])}, {re_.line_no}행) "
               f"{_num(rv)}이(가) 다름. 비슷한 정상 로그 {support}개에서는 두 값이 늘 같았음(값 {distinct}가지)")
    if strength["candidate"]:
        summary += " — 정상 로그가 아직 적어 후보로만 봄"
    return Finding(
        "DETERMINISTIC", "VALUE_OUT_OF_RANGE", summary, stance=strength["stance"], severity="SUSPECT",
        log_file_id=log_file_id, event_id=re_.id, line_start=re_.line_no,
        detail={**detail, "kind": "value_equal", "field": f"{a[1]}={name(b[0])}.{b[1]}", "event_type": a[0],
                "candidate": strength["candidate"], "support": support, "distinct": distinct,
                "left": {"event_type": a[0], "field": a[1], "value": lv, "line": le.line_no, "event_id": le.id},
                "right": {"event_type": b[0], "field": b[1], "value": rv, "line": re_.line_no, "event_id": re_.id}})


def _count_finding(a: FieldKey, off: Sequence[str], total: float, fields: Fields, counts: Counter, support: int,
                   distinct: int, log_file_id: int, detail: dict, name) -> Finding:
    _value, event = list(fields[a])[0]
    shown = list(off)[:MAX_COUNT_TYPES]
    strength = _strength(support)
    what = ", ".join(f"'{name(t)}' {counts.get(t, 0)}번" for t in shown) + (" 등" if len(off) > len(shown) else "")
    summary = (f"'{a[1]}'({name(a[0])}, {event.line_no}행) {_num(total)}과(와) 나온 횟수가 다름: {what}. "
               f"비슷한 정상 로그 {support}개에서는 늘 같았음(값 {distinct}가지)")
    if strength["candidate"]:
        summary += " — 정상 로그가 아직 적어 후보로만 봄"
    return Finding(
        "DETERMINISTIC", "VALUE_OUT_OF_RANGE", summary, stance=strength["stance"], severity="SUSPECT",
        log_file_id=log_file_id, event_id=event.id, line_start=event.line_no,
        detail={**detail, "kind": "value_count", "field": f"{a[1]}=count({name(shown[0])})", "event_type": a[0],
                "candidate": strength["candidate"], "support": support, "distinct": distinct,
                "left": {"event_type": a[0], "field": a[1], "value": total, "line": event.line_no, "event_id": event.id},
                "counts": {t: counts.get(t, 0) for t in shown}})
