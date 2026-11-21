"""계층 1 보강 — 비슷한 정상 로그와 비교 (CR-08 DT-01~10, 결정론).

운전 모드 없이 분석할 때(CR-09: 늘 그렇다) 점수 0.4 이상인 비슷한 승인 정상 로그(최대 5개)와 비교해,
'정상 로그에서 관찰된 것과 다른 점'만 말한다. 장비 사양·코드 뜻은 쓰지 않는다.
사용자가 승인한 규칙이 아니므로 모두 의심(SUSPECT)이고 '규칙 위반'(VIOLATION)으로 올리지 않는다.

- 누락: 기준 정상 로그(4개 이상) 모두에 같은 횟수로 있던 이벤트가 없음
- 횟수 초과: 정상 로그마다 딱 1번이던 이벤트가 3번 이상(재시작·재작업 반복)
- 순서: 정상 로그 모두에서 늘 앞서던 이벤트가 뒤에 나옴(딱 1번 나오는 이벤트가 낀 쌍만)
- 비율: 비율이 일정하던 이벤트가 거의 사라지거나 몇 배로 늘어남
- 번호별 흐름: job=·blk=·cycle= 같은 번호마다 늘 따라 나오던 단계가 빠지거나 겹치거나 뒤바뀜
- 연속 반복: 같은 이벤트가 연달아 4번 이상이고 정상 로그 최대 연속의 2배 초과
- 간격: 이웃한 이벤트 사이 간격이 정상 로그 최대 간격의 5배 초과(시각을 읽은 경우만)
- 값 범위: 같은 이벤트의 key=숫자 값이 정상 로그 관찰 범위를 크게 벗어남(관찰 5번 이상, 카운터 제외)
- 값 멈춤: 늘 변하던 값이 여러 번 연속 똑같음
- 상태값 바뀜: 정상 로그 모두에서 늘 같던 낱말 값(예: result=OK)이 다름
- 통째로 없음: 정상 로그 10개 이상 모두에 2번 이상씩 나오던 이벤트가 한 번도 없음
- 짝 횟수 어긋남: 정상 로그마다 늘 서로 같은 횟수로 나오던 이벤트들(채널·반복 단계)의 횟수가 다름
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from app.analysis.anomaly_detector import Finding
from app.core.event_normalizer import message_template
from app.core.feature_extractor import ERROR_LEVELS, WARNING_LEVELS, FileFeatures
from app.models.records import EventRecord

MIN_REFERENCE_FILES = 2
# '늘 있음·늘 같은 횟수'(누락·횟수 초과·순서·비율)는 기준 로그가 적으면 우연히 겹친 이벤트를 규칙으로 착각하기 쉽다.
# 비슷한 정상 로그가 4개 이상일 때만 쓴다(정상 로그가 쌓일수록 살아남).
MISSING_MIN_REFERENCE_FILES = 4
CATEGORY_MIN_REFERENCE_FILES = 8  # 상태값 바뀜(DT-10)은 기준이 더 많아야 쓴다
ABSENT_MIN_REFERENCE_FILES = 10  # 횟수가 들쭉날쭉한 이벤트의 '통째로 없음'(DT-13)은 기준 10개 이상일 때만
EQUAL_MIN_COUNT = 3  # 짝 횟수(DT-14)는 정상에서 3번 이상 나오던 이벤트끼리만
MAX_PER_KIND = 10
BURST_MIN = 4  # 연달아 이만큼 이상
BURST_FACTOR = 2  # 정상 로그 최대 연속의 이 배수를 넘으면
GAP_FACTOR = 5  # 정상 로그 최대 간격의 이 배수를 넘으면
VALUE_MIN_OBSERVATIONS = 5
VALUE_MIN_OBSERVATIONS_CONSTANT = 8  # 정상 로그에서 값이 늘 같았던 경우는 더 많이 봐야 쓴다
VALUE_SPREAD_FACTOR = 3
VALUE_RELATIVE_MARGIN = 0.5
COUNT_EXCESS = 2  # 늘 같은 횟수였던 이벤트가 이만큼 이상 많으면
ORDER_MAX_TYPES = 60  # 순서를 비교할 이벤트 수(처음 나오는 순서로 앞에서부터)
SHARE_MIN = 0.02  # 비율 비교는 정상 로그에서 전체의 2% 이상 나오던 이벤트만
SHARE_STABLE = 3.0  # 정상 로그 사이 비율 차이가 3배 안이면 '일정'
SHARE_FACTOR = 3.0  # 정상 비율의 1/3 미만이거나 3배 초과면
SHARE_MIN_EXPECTED = 8  # 정상 비율대로라면 8번 이상 나왔어야 '거의 안 나옴'으로 본다(기대가 2~3번이면 우연히 0번일 수 있음)
SHARE_CORE = 0.2  # 정상 로그에서 전체의 20% 이상인 핵심 이벤트는 짧은 로그에서도 '아예 없음'을 본다
FLOW_MIN_IDS = 3  # 번호 칸으로 보려면 한 파일에 서로 다른 값이 3개 이상
FLOW_MIN_EVENTS_PER_ID = 1.5  # 값 하나에 이벤트가 평균 1.5개 이상(번호마다 여러 단계가 이어짐)
STUCK_MIN_RUN = 5  # 같은 값이 이만큼 연속이면
STUCK_MIN_DISTINCT = 10  # 정상 로그에서 값이 10가지 이상으로 늘 변하던 칸만(몇 가지 값만 쓰는 칸은 우연히 이어질 수 있음)
BASIS = "REFERENCE_NORMAL"

# key=값 또는 'key: 값'(콜론 뒤 빈칸, 시각 10:00:00과 구분)
_NUMERIC_FIELD = re.compile(r"(?<![\w.])([A-Za-z_][\w.]*)\s*(?:=|:\s)\s*([-+]?\d+(?:\.\d+)?)(?![\w.:])")
_ANY_FIELD = re.compile(r"(?<![\w.])([A-Za-z_][\w.]*)(?:=|:\s)\s*([^\s,;]+)")
_WORD_FIELD = re.compile(r"(?<![\w.])([A-Za-z_][\w.]*)(?:=|:\s)\s*([A-Za-z][A-Za-z_\-]*)(?![\w.\-])")


@dataclass
class ReferenceFile:
    features: FileFeatures
    events: list[EventRecord]
    case_id: int | None = None


def compare_with_normals(target: FileFeatures, events: Sequence[EventRecord],
                         references: Sequence[ReferenceFile]) -> list[Finding]:
    refs = [r for r in references if r.features.log_role == target.log_role]
    if len(refs) < MIN_REFERENCE_FILES:
        return []
    detail = {"basis": BASIS, "reference_files": len(refs),
              "reference_case_ids": sorted({r.case_id for r in refs if r.case_id is not None})}
    stats = [head_stats(r.features) for r in refs]
    tstats = head_stats(target)
    findings: list[Finding] = []
    findings += _missing(target, tstats, refs, stats, detail)
    findings += _count_excess(target, tstats, refs, stats, detail)
    findings += _order(target, tstats, refs, stats, detail)
    shares = _shares(target, tstats, refs, stats, detail)
    findings += shares
    findings += _absent(target, tstats, refs, stats, detail, {f.detail["event_type"] for f in shares})
    findings += _equal_counts(target, tstats, refs, stats, detail)
    findings += _category_change(events, refs, detail, target.log_file_id)
    findings += _id_flows(events, refs, detail, target.log_file_id)
    findings += _bursts(target, events, refs, detail)
    findings += _gaps(target, events, refs, detail)
    findings += _values(target, events, refs, detail)
    findings += _stuck(events, refs, detail, target.log_file_id)
    return findings


# ---------------------------------------------------------------- 이름 단위 통계

def _parsed_types(features: FileFeatures) -> set[str]:
    """칸 이름·날짜 머리 줄·형식 밖 줄처럼 시각·레벨·구성요소가 모두 없는 이벤트는 비교에서 뺀다.
    원문 파서로 읽은 파일(모든 이벤트가 그렇다)은 그대로 다 쓴다."""
    parsed = {key for key, e in features.first_event.items() if e.ts_raw or e.level or e.component}
    return parsed or set(features.first_event)


@dataclass
class HeadStats:
    """파일 하나의 이벤트 '이름'(구성요소 + 메시지 첫 낱말) 단위 횟수·처음 위치·예시.
    값만 다른 이벤트(예: mode=A·mode=C, result=OK)를 한 이름으로 본다."""

    counts: Counter
    first: dict[str, int]
    sample: dict[str, EventRecord]
    total: int


def head_stats(features: FileFeatures) -> HeadStats:
    parsed = _parsed_types(features)
    counts: Counter = Counter()
    first: dict[str, int] = {}
    sample: dict[str, EventRecord] = {}
    for index, key in enumerate(features.sequence):
        if key not in parsed:
            continue
        head = _head(key)
        counts[head] += 1
        if head not in first:
            first[head] = index
            sample[head] = features.first_event[key]
    return HeadStats(counts, first, sample, sum(counts.values()))


def _always(refs: Sequence[ReferenceFile], stats: Sequence[HeadStats]) -> set[str]:
    """기준 정상 로그 모두에 나온 이벤트 이름. 경고·오류와 같은 이름의 이벤트 무리(예: 대기 경고와 그 해제)는
    조건부로 나오는 것이라 뺀다(없거나 순서가 달라도 이상은 아니다)."""
    if len(refs) < MISSING_MIN_REFERENCE_FILES:
        return set()
    always = set.intersection(*(set(st.counts) for st in stats))
    warned = {_head(key) for r in refs for key, e in r.features.first_event.items()
              if (e.level or "").upper() in WARNING_LEVELS | ERROR_LEVELS}
    return always - warned


def _steady(refs: Sequence[ReferenceFile], stats: Sequence[HeadStats]) -> dict[str, int]:
    """기준 정상 로그 모두에 **같은 횟수로** 나온 이벤트 이름과 그 횟수(시작·정지·요약처럼 실행마다 정해진 만큼)."""
    return {head: stats[0].counts[head] for head in _always(refs, stats)
            if len({st.counts[head] for st in stats}) == 1}


# ---------------------------------------------------------------- 누락

def _missing(target: FileFeatures, tstats: HeadStats, refs: Sequence[ReferenceFile], stats: Sequence[HeadStats],
             detail: dict) -> list[Finding]:
    """기준 정상 로그 모두에 **같은 횟수로** 나온 이벤트 이름만 '빠지면 안 되는 이벤트'로 본다.
    가끔 나오는 이벤트(대기·상태 알림 등)는 기준 로그 몇 개에 우연히 모두 있을 수 있지만 횟수는 대개 다르다."""
    steady = _steady(refs, stats)
    if not steady:
        return []
    # 구성요소·레벨을 못 읽은 줄(머리 줄·형식 밖 줄)에 같은 내용이 들어 있는지 볼 때만 쓴다
    texts = [message_template(e.message) for e in target.first_event.values() if not (e.component or e.level)]
    missing = []
    for head in sorted(set(steady) - set(tstats.counts), key=lambda h: stats[0].first.get(h, 0)):
        sample = message_template(stats[0].sample[head].message)
        if sample and any(sample in text for text in texts):
            continue  # 같은 내용이 다른 줄 모양으로 들어 있다(예: 머리 줄 안의 문장)
        missing.append(head)
    out = []
    for head in missing[:MAX_PER_KIND]:
        out.append(Finding(
            "DETERMINISTIC", "MISSING_EVENT",
            f"비슷한 정상 로그 {len(refs)}개 모두에 있던 이벤트가 이 로그에 없음: {_first_line(stats[0].sample[head].message)}",
            stance="SUPPORTS", severity="SUSPECT", log_file_id=target.log_file_id,
            detail={**detail, "event_type": head},
        ))
    return out


def _head(event_type: str) -> str:
    """이벤트 종류의 '이름'(구성요소 + 메시지 첫 낱말). 'TEST-CTRL|TEST-START mode=A …' → 'TEST-CTRL|TEST-START'."""
    return event_type.split(" ", 1)[0]


def _name(event_type: str) -> str:
    """구성요소를 뺀 이벤트 이름. 'TEST-WORKER-2|TEST-JOB-DONE job=<N>' → 'TEST-JOB-DONE'."""
    return event_type.split("|", 1)[-1].split(" ", 1)[0]


# ---------------------------------------------------------------- 횟수 초과

def _count_excess(target: FileFeatures, tstats: HeadStats, refs: Sequence[ReferenceFile],
                  stats: Sequence[HeadStats], detail: dict) -> list[Finding]:
    """정상 로그마다 딱 한 번이던 이벤트(시작·단계 진입 등)가 COUNT_EXCESS번 이상 더 나옴(재시작·재작업 반복).
    여러 번 나오는 이벤트는 비슷한 로그끼리 횟수가 우연히 같을 수 있어 보지 않는다."""
    out = []
    for head, count in sorted(_steady(refs, stats).items(), key=lambda item: tstats.first.get(item[0], 0)):
        seen = tstats.counts.get(head, 0)
        if count != 1 or seen < count + COUNT_EXCESS:
            continue
        event = tstats.sample[head]
        out.append(Finding(
            "DETERMINISTIC", "REPETITION",
            f"비슷한 정상 로그마다 {count}번이던 이벤트가 {seen}번 나옴({event.line_no}행부터): {_first_line(event.message)}",
            stance="SUPPORTS", severity="SUSPECT", log_file_id=target.log_file_id, event_id=event.id,
            line_start=event.line_no, detail={**detail, "kind": "count", "event_type": head, "count": seen,
                                              "reference_count": count},
        ))
    return out[:MAX_PER_KIND]


# ---------------------------------------------------------------- 순서

def _order(target: FileFeatures, tstats: HeadStats, refs: Sequence[ReferenceFile], stats: Sequence[HeadStats],
           detail: dict) -> list[Finding]:
    """기준 정상 로그 모두에서 a가 처음 나온 뒤에 b가 처음 나왔는데, 이 로그에서는 b가 먼저 나옴.
    늦게 나온 이벤트마다 한 번만, 바로 앞에 와야 할 이벤트와 함께 보인다."""
    keys = [h for h in _always(refs, stats) if h in tstats.first]
    mean = {h: sum(st.first[h] for st in stats) / len(stats) for h in keys}
    keys = sorted(keys, key=mean.__getitem__)[:ORDER_MAX_TYPES]
    # 실행마다 딱 한 번 나오는 이벤트(시작·인증·단계 진입 등)가 낀 쌍만 본다. 여러 번 나오는 이벤트끼리의
    # '처음 나온 순서'는 작업이 섞여 진행되는 로그에서 우연히 정해지므로 규칙으로 삼지 않는다.
    once = {head for head, count in _steady(refs, stats).items() if count == 1}
    late: dict[str, str] = {}
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if (a in once or b in once) and tstats.first[b] < tstats.first[a] and all(
                    st.first[a] < st.first[b] for st in stats):
                if b not in late or mean[a] > mean[late[b]]:
                    late[b] = a
    out = []
    for b, a in sorted(late.items(), key=lambda item: tstats.first[item[0]]):
        early, after = tstats.sample[b], tstats.sample[a]
        out.append(Finding(
            "DETERMINISTIC", "ORDER_VIOLATION",
            f"비슷한 정상 로그에서는 늘 '{_first_line(after.message)}' 다음에 나오던 "
            f"'{_first_line(early.message)}'가 먼저 나옴({early.line_no}행, 앞서야 할 줄은 {after.line_no}행)",
            stance="SUPPORTS", severity="SUSPECT", log_file_id=target.log_file_id, event_id=early.id,
            line_start=early.line_no, detail={**detail, "before": a, "after": b},
        ))
    return out[:MAX_PER_KIND]


# ---------------------------------------------------------------- 비율

def _shares(target: FileFeatures, tstats: HeadStats, refs: Sequence[ReferenceFile], stats: Sequence[HeadStats],
            detail: dict) -> list[Finding]:
    """정상 로그에서 전체 이벤트 중 비율이 일정하던 이벤트(측정 채널·작업자별 처리 등)가 거의 사라지거나 몇 배로 늘어남.
    로그 길이와 무관하게 비교하려고 횟수 대신 비율을 쓴다. 횟수가 늘 같은 이벤트는 누락·횟수 초과에서 본다."""
    total = tstats.total
    out = []
    for head in sorted(_always(refs, stats) - set(_steady(refs, stats))):
        shares = [st.counts[head] / max(1, st.total) for st in stats]
        low, high = min(shares), max(shares)
        if low < SHARE_MIN or high > SHARE_STABLE * low:
            continue
        seen = tstats.counts.get(head, 0)
        share = seen / max(1, total)
        core_absent = seen == 0 and low >= SHARE_CORE and low * total >= 2  # 핵심 이벤트(정상에서 20% 이상)가 아예 없음
        if (share < low / SHARE_FACTOR and low * total >= SHARE_MIN_EXPECTED) or core_absent:
            text, kind = "거의 나오지 않음", "MISSING_EVENT"
        elif share > high * SHARE_FACTOR and seen - high * total >= BURST_MIN:
            text, kind = "몇 배로 많이 나옴", "REPETITION"
        else:
            continue
        out.append(Finding(
            "DETERMINISTIC", kind,
            f"비슷한 정상 로그에서 전체의 {low:.0%}~{high:.0%}이던 이벤트가 {text}(이 로그 {seen}번, {share:.0%}): "
            f"{_first_line(stats[0].sample[head].message)}",
            stance="SUPPORTS", severity="SUSPECT", log_file_id=target.log_file_id,
            detail={**detail, "kind": "share", "event_type": head, "count": seen, "share": round(share, 4),
                    "reference_share_min": round(low, 4), "reference_share_max": round(high, 4)},
        ))
    return out[:MAX_PER_KIND]


# ---------------------------------------------------------------- 통째로 없음 · 짝 횟수 어긋남

def _absent(target: FileFeatures, tstats: HeadStats, refs: Sequence[ReferenceFile], stats: Sequence[HeadStats],
            detail: dict, reported: set[str]) -> list[Finding]:
    """비슷한 정상 로그 10개 이상 모두에 2번 이상씩 나오던 이벤트 이름이 이 로그에 한 번도 없음(DT-13).
    횟수는 로그마다 달라도(작업자별 처리·측정 채널 등) 늘 있던 것이 통째로 빠진 경우다."""
    if len(refs) < ABSENT_MIN_REFERENCE_FILES:
        return []
    texts = [message_template(e.message) for e in target.first_event.values() if not (e.component or e.level)]
    out = []
    for head in sorted(_always(refs, stats) - set(_steady(refs, stats)) - set(tstats.counts) - reported):
        if min(st.counts[head] for st in stats) < 2:
            continue
        sample = message_template(stats[0].sample[head].message)
        if sample and any(sample in text for text in texts):
            continue
        out.append(Finding(
            "DETERMINISTIC", "MISSING_EVENT",
            f"비슷한 정상 로그 {len(refs)}개 모두에 2번 이상씩 나오던 이벤트가 이 로그에 한 번도 없음: "
            f"{_first_line(stats[0].sample[head].message)}",
            stance="SUPPORTS", severity="SUSPECT", log_file_id=target.log_file_id,
            detail={**detail, "kind": "absent", "event_type": head,
                    "reference_min_count": min(st.counts[head] for st in stats)},
        ))
    return out[:MAX_PER_KIND]


def _equal_counts(target: FileFeatures, tstats: HeadStats, refs: Sequence[ReferenceFile], stats: Sequence[HeadStats],
                  detail: dict) -> list[Finding]:
    """정상 로그마다 늘 **서로 같은 횟수**로 나오던 이벤트 묶음(측정 채널들·반복 단계들·작업의 단계들)의 횟수가
    이 로그에서 어긋남(DT-14). 묶음은 정상 로그의 횟수 모양이 모든 기준 로그에서 똑같은 이름끼리로 정한다."""
    if len(refs) < MISSING_MIN_REFERENCE_FILES:
        return []
    groups: dict[tuple[int, ...], list[str]] = defaultdict(list)
    for head in _always(refs, stats):
        vector = tuple(st.counts[head] for st in stats)
        if min(vector) >= EQUAL_MIN_COUNT and len(set(vector)) > 1:  # 늘 같은 횟수(시작·정지)는 누락·횟수 초과에서 본다
            groups[vector].append(head)
    out = []
    for heads in groups.values():
        if len(heads) < 2:
            continue
        counts = {head: tstats.counts.get(head, 0) for head in heads}
        usual = Counter(counts.values()).most_common(1)[0][0]
        odd = {head: n for head, n in counts.items() if n != usual}
        if not odd or usual == 0:
            continue
        head, seen = min(odd.items(), key=lambda item: item[1])
        sample = tstats.sample.get(head) or stats[0].sample[head]
        names = ", ".join(f"{_first_line(tstats.sample.get(h, stats[0].sample[h]).message).split(' ', 1)[0]} {n}번"
                          for h, n in sorted(counts.items(), key=lambda item: -item[1])[:6])
        out.append(Finding(
            "DETERMINISTIC", "MISSING_EVENT" if seen < usual else "REPETITION",
            f"비슷한 정상 로그에서 늘 같은 횟수로 나오던 이벤트 {len(heads)}개의 횟수가 어긋남({names})",
            stance="SUPPORTS", severity="SUSPECT", log_file_id=target.log_file_id,
            event_id=sample.id if head in tstats.sample else None,
            line_start=sample.line_no if head in tstats.sample else None,
            detail={**detail, "kind": "equal_counts", "event_type": head, "counts": counts, "usual": usual},
        ))
    return out[:MAX_PER_KIND]


# ---------------------------------------------------------------- 상태값 바뀜

def _categories(events: Sequence[EventRecord]) -> dict[tuple[str, str], list[tuple[str, EventRecord]]]:
    """(이벤트 이름, key) → 낱말 값 목록. 숫자·코드(TEST-E101)·번호가 섞인 값은 뺀다."""
    out: dict[tuple[str, str], list[tuple[str, EventRecord]]] = defaultdict(list)
    for event in events:
        head = _head(event.event_type or event.message)
        for key, value in _WORD_FIELD.findall(_first_line(event.message)):
            out[(head, key)].append((value, event))
    return out


def _category_change(events: Sequence[EventRecord], refs: Sequence[ReferenceFile], detail: dict,
                     log_file_id: int) -> list[Finding]:
    """기준 정상 로그(8개 이상) 모두에서 늘 같은 낱말이던 상태값(예: result=OK)이 이 로그에서 다른 낱말임(DT-10).
    레벨이 INFO여도 잡는다. 정상에서 여러 값이 나오던 칸(모드 등)은 보지 않는다.
    기준이 적으면 여러 값이 나오는 칸도 우연히 늘 같아 보이므로 8개 이상일 때만 쓴다."""
    if len(refs) < CATEGORY_MIN_REFERENCE_FILES:
        return []
    per_ref = [_categories(r.events) for r in refs]
    constant: dict[tuple[str, str], str] = {}
    for key in set.intersection(*(set(c) for c in per_ref)):
        values = {value for c in per_ref for value, _e in c[key]}
        if len(values) == 1:
            constant[key] = next(iter(values))
    out = []
    for key, items in sorted(_categories(events).items()):
        odd = [(value, event) for value, event in items if key in constant and value != constant[key]]
        if not odd:
            continue
        value, event = odd[0]
        out.append(Finding(
            "DETERMINISTIC", "VALUE_OUT_OF_RANGE",
            f"'{key[1]}' 값이 {value}({event.line_no}행): {_first_line(event.message)}. "
            f"비슷한 정상 로그 {len(refs)}개에서는 늘 {constant[key]}",
            stance="SUPPORTS", severity="SUSPECT", log_file_id=log_file_id, event_id=event.id,
            line_start=event.line_no,
            detail={**detail, "kind": "category", "event_type": key[0], "field": key[1], "value": value,
                    "expected": constant[key], "count": len(odd)},
        ))
    return out[:MAX_PER_KIND]


# ---------------------------------------------------------------- 같은 번호로 이어지는 흐름(작업·블록·사이클)

def _flows(events: Sequence[EventRecord]) -> dict[str, dict[str, list[tuple[str, EventRecord]]]]:
    """key=값 묶음별 이벤트 이름 흐름. 예: job=4211 → [ACCEPT, START, DONE]."""
    out: dict[str, dict[str, list[tuple[str, EventRecord]]]] = defaultdict(lambda: defaultdict(list))
    for event in events:
        name = _name(event.event_type or event.message)
        for key, value in _ANY_FIELD.findall(_first_line(event.message)):
            out[key][value].append((name, event))
    return out


def _flow_start(required: set[str], order: list[tuple[str, str]]) -> str | None:
    """흐름의 시작 이벤트: 정상 로그에서 늘 다른 필수 이벤트보다 먼저 나온 것(없으면 None)."""
    pairs = set(order)
    return next((r for r in sorted(required) if all((r, b) in pairs for b in required if b != r)), None)


def _episodes(items: list[tuple[str, EventRecord]], start: str | None) -> list[list[tuple[str, EventRecord]]]:
    """번호 하나의 이벤트를 '한 번의 흐름'으로 나눈다. 시작 이벤트가 다시 나오면 새 흐름이다.
    같은 값을 다시 쓰는 설정 칸(kv=100을 워밍업과 촬영에서 또 씀), 묶음마다 1부터 다시 세는 칸(촬영마다 step=1…)을
    한 흐름으로 합쳐 '겹침'으로 오해하지 않게 한다(2026-11-22 CT 유사 세트 오탐). 마지막 단계만 두 번 나오는 진짜 겹침은
    시작 이벤트가 아니므로 나뉘지 않는다."""
    if start is None:
        return [items]
    out: list[list[tuple[str, EventRecord]]] = []
    current: list[tuple[str, EventRecord]] = []
    for name, event in items:
        if name == start and any(n == start for n, _e in current):
            out.append(current)
            current = []
        current.append((name, event))
    if current:
        out.append(current)
    return out


def _learn_flow(files: list[dict[str, list[tuple[str, EventRecord]]]]) -> dict | None:
    raw = [[name for name, _e in items] for flows in files for items in flows.values()]
    required = set.intersection(*(set(seq) for seq in raw))
    order = [(a, b) for a in required for b in required if a != b and all(seq.index(a) < seq.index(b) for seq in raw)]
    start = _flow_start(required, order)
    sequences = [[name for name, _e in episode] for flows in files for items in flows.values()
                 for episode in _episodes(items, start)]
    required = set.intersection(*(set(seq) for seq in sequences))
    if len(required) < 2:
        return None
    order = [(a, b) for a in required for b in required if a != b
             and all(seq.index(a) < seq.index(b) for seq in sequences)]
    # 흐름 하나에 같은 이름이 나온 최대 횟수(가끔 나오는 재시도 같은 이벤트도 포함). 이보다 많으면 겹침.
    # 정상 로그에서 횟수가 세 가지 이상으로 늘 달랐던 이벤트(회전마다 한 줄 등 길이에 따라 느는 것)는 겹침으로 보지 않는다.
    names = set().union(*sequences)
    most = {name: max(seq.count(name) for seq in sequences) for name in names}
    variable = {name for name in names if len({seq.count(name) for seq in sequences if name in seq}) >= 3}
    return {"required": required, "most": most, "order": order, "start": start, "variable": variable}


def _id_flows(events: Sequence[EventRecord], refs: Sequence[ReferenceFile], detail: dict,
              log_file_id: int) -> list[Finding]:
    """정상 로그에서 같은 번호(job=·blk=·cycle= 등)마다 늘 따라 나온 이벤트 묶음이 있으면, 이 로그의 번호마다
    빠진 단계·겹친 단계·뒤바뀐 순서를 찾는다. 번호 칸 이름은 정하지 않고 정상 로그의 모양으로 고른다."""
    learned: dict[str, dict] = {}
    ref_flows = [_flows(r.events) for r in refs]
    for key in set().union(*ref_flows):
        files = [flows[key] for flows in ref_flows
                 if len(flows.get(key, {})) >= FLOW_MIN_IDS
                 and sum(len(v) for v in flows[key].values()) >= FLOW_MIN_EVENTS_PER_ID * len(flows[key])]
        if len(files) < MIN_REFERENCE_FILES:
            continue
        rule = _learn_flow(files)
        if rule is not None:
            learned[key] = rule
    out = []
    target = _flows(events)
    for key, rule in sorted(learned.items()):
        problems: dict[str, list[tuple[str, EventRecord, str]]] = {"MISSING_EVENT": [], "REPETITION": [],
                                                                    "ORDER_VIOLATION": []}
        for value, all_items in target.get(key, {}).items():
            for items in _episodes(all_items, rule["start"]):
                names = [name for name, _e in items]
                first = items[0][1]
                lacking = sorted(rule["required"] - set(names))
                if lacking:
                    problems["MISSING_EVENT"].append((value, first, f"{', '.join(lacking)} 없음"))
                extra = sorted(n for n in set(names) if n in rule["most"] and n not in rule["variable"]
                               and names.count(n) > max(rule["most"][n], 1))
                if extra:
                    problems["REPETITION"].append((value, first, f"{', '.join(extra)} 겹침"))
                swapped = [(a, b) for a, b in rule["order"] if a in names and b in names
                           and names.index(b) < names.index(a)]
                if swapped:
                    problems["ORDER_VIOLATION"].append((value, first, f"{swapped[0][1]}이(가) {swapped[0][0]}보다 먼저"))
        for kind, items in problems.items():
            if not items:
                continue
            examples = ", ".join(f"{key}={value}({event.line_no}행, {what})" for value, event, what in items[:3])
            required = " → ".join(sorted(rule["required"], key=lambda n: sum(1 for a, b in rule["order"] if b == n)))
            out.append(Finding(
                "DETERMINISTIC", kind,
                f"'{key}' 번호마다 비슷한 정상 로그에서는 늘 {required}이(가) 한 번씩 나왔는데, "
                f"이 로그에서 {len(items)}개 번호가 다름: {examples}",
                stance="SUPPORTS", severity="SUSPECT", log_file_id=log_file_id, event_id=items[0][1].id,
                line_start=items[0][1].line_no,
                detail={**detail, "kind": "flow", "field": key, "count": len(items),
                        "values": [value for value, _e, _w in items[:20]], "required": sorted(rule["required"])},
            ))
    return out[:MAX_PER_KIND]


# ---------------------------------------------------------------- 연속 반복

def _runs(sequence: Sequence[str]) -> list[tuple[str, int, int]]:
    """(이벤트 종류, 시작 위치, 길이) — 같은 종류가 연달아 나온 구간."""
    out: list[tuple[str, int, int]] = []
    start = 0
    for index in range(1, len(sequence) + 1):
        if index == len(sequence) or sequence[index] != sequence[start]:
            out.append((sequence[start], start, index - start))
            start = index
    return out


def _bursts(target: FileFeatures, events: Sequence[EventRecord], refs: Sequence[ReferenceFile],
            detail: dict) -> list[Finding]:
    ref_max: dict[str, int] = defaultdict(int)
    for r in refs:
        for key, _start, length in _runs(r.features.sequence):
            ref_max[key] = max(ref_max[key], length)
    out = []
    seen: set[str] = set()
    for key, start, length in sorted(_runs(target.sequence), key=lambda run: -run[2]):
        if key in seen or length < BURST_MIN or length <= BURST_FACTOR * max(ref_max.get(key, 0), 1):
            continue
        seen.add(key)
        event = events[start] if start < len(events) else target.first_event[key]
        out.append(Finding(
            "DETERMINISTIC", "REPETITION",
            f"같은 이벤트가 연달아 {length}번 나옴({event.line_no}행부터): {_first_line(event.message)}. "
            f"비슷한 정상 로그에서는 연달아 최대 {ref_max.get(key, 0)}번",
            stance="SUPPORTS", severity="SUSPECT", log_file_id=target.log_file_id, event_id=event.id,
            line_start=event.line_no, detail={**detail, "event_type": key, "run": length,
                                              "reference_max_run": ref_max.get(key, 0)},
        ))
        if len(out) >= MAX_PER_KIND:
            break
    return out


# ---------------------------------------------------------------- 간격

def _max_gap(events: Sequence[EventRecord]) -> tuple[int, EventRecord, EventRecord] | None:
    best = None
    previous = None
    for event in events:
        if event.ts_value_us is None:
            continue
        if previous is not None and previous.ts_kind == event.ts_kind:
            gap = event.ts_value_us - previous.ts_value_us
            if best is None or gap > best[0]:
                best = (gap, previous, event)
        previous = event
    return best


def _gaps(target: FileFeatures, events: Sequence[EventRecord], refs: Sequence[ReferenceFile],
          detail: dict) -> list[Finding]:
    ref_gaps = [g[0] for g in (_max_gap(r.events) for r in refs) if g is not None]
    found = _max_gap(events)
    if len(ref_gaps) < MIN_REFERENCE_FILES or found is None:
        return []
    limit = max(ref_gaps)
    gap, before, after = found
    if limit <= 0 or gap <= GAP_FACTOR * limit:
        return []
    return [Finding(
        "DETERMINISTIC", "LATENCY",
        f"이벤트 사이 간격 {_duration(gap)}({before.line_no}행 → {after.line_no}행): {_first_line(after.message)}. "
        f"비슷한 정상 로그에서 가장 긴 간격은 {_duration(limit)}",
        stance="SUPPORTS", severity="SUSPECT", log_file_id=target.log_file_id, event_id=after.id,
        line_start=before.line_no, line_end=after.line_end or after.line_no,
        detail={**detail, "gap_us": gap, "reference_max_gap_us": limit},
    )]


# ---------------------------------------------------------------- 값 범위

def _numeric_fields(events: Sequence[EventRecord]) -> dict[tuple[str, str], list[tuple[float, EventRecord]]]:
    out: dict[tuple[str, str], list[tuple[float, EventRecord]]] = defaultdict(list)
    for event in events:
        key = event.event_type or event.message
        for name, value in _NUMERIC_FIELD.findall(_first_line(event.message)):
            out[(key, name)].append((float(value), event))
    return out


def _steps_by_one(values: Sequence[float]) -> bool:
    return len(values) >= 2 and all(b - a == 1 for a, b in zip(values, values[1:]))


def _increasing(values: Sequence[float]) -> bool:
    return len(values) >= 2 and all(b > a for a, b in zip(values, values[1:]))


def _values(target: FileFeatures, events: Sequence[EventRecord], refs: Sequence[ReferenceFile],
            detail: dict) -> list[Finding]:
    observed: dict[tuple[str, str], list[float]] = defaultdict(list)
    files: dict[tuple[str, str], int] = defaultdict(int)
    # 값이 2개 이상인 기준 파일마다 (1씩 오름, 계속 커짐, 값 개수)
    shapes: dict[tuple[str, str], list[tuple[bool, bool, int]]] = defaultdict(list)
    for r in refs:
        for field_key, items in _numeric_fields(r.events).items():
            values = [v for v, _e in items]
            observed[field_key].extend(values)
            files[field_key] += 1
            if len(values) >= 2:
                shapes[field_key].append((_steps_by_one(values), _increasing(values), len(values)))
    # 카운터(사이클 번호·줄 번호 등)는 범위로 비교하지 않는다: 값이 여러 개인 기준 파일에서 모두 1씩 올랐거나,
    # 모두 계속 커졌고 그중 값이 3개 이상인 파일이 있을 때(값 2개가 우연히 커지는 순서인 경우와 구분). 이 로그에서 1씩 오르는 값도 뺀다.
    counters = {key for key, items in shapes.items()
                if all(step for step, _inc, _n in items)
                or (all(inc for _step, inc, _n in items) and any(n >= 3 for _s, _i, n in items))}
    out = []
    for field_key, items in _numeric_fields(events).items():
        values = observed.get(field_key)
        if not values or field_key in counters or files[field_key] < MIN_REFERENCE_FILES:
            continue
        # 실행 한 번에 한 번만 나오는 값(합계·총 시간 등)은 기준 표본이 기준 로그 수만큼뿐이고 동작 길이에 따라 달라 비교하지 않는다
        if not any(n >= 2 for _s, _i, n in shapes.get(field_key, [])):
            continue
        mine = [v for v, _e in items]
        if _steps_by_one(mine):
            continue
        # 이 로그에서 4번 넘게 계속 커지고 기준 로그에서도 계속 커지기만 했다면 줄 번호 같은 카운터다
        if len(mine) >= 4 and _increasing(mine) and all(inc for _s, inc, _n in shapes.get(field_key, [])):
            continue
        low, high = min(values), max(values)
        spread = high - low
        if len(values) < (VALUE_MIN_OBSERVATIONS_CONSTANT if spread == 0 else VALUE_MIN_OBSERVATIONS):
            continue
        margin = max(VALUE_SPREAD_FACTOR * spread, VALUE_RELATIVE_MARGIN * max(abs(low), abs(high)))
        if spread == 0:
            margin = max(margin, 1.0)  # 늘 같던 값(예: 늘 0)은 1을 넘게 벗어날 때만 본다
        outside = [(v, e) for v, e in items if v < low - margin or v > high + margin]
        if not outside:
            continue
        value, event = max(outside, key=lambda item: abs(item[0] - (low + high) / 2))
        name = field_key[1]
        out.append(Finding(
            "DETERMINISTIC", "VALUE_OUT_OF_RANGE",
            f"'{name}' 값 {_number(value)}({event.line_no}행): {_first_line(event.message)}. "
            f"비슷한 정상 로그에서 같은 이벤트의 값은 {_number(low)}~{_number(high)}({len(values)}번 관찰)",
            stance="SUPPORTS", severity="SUSPECT", log_file_id=target.log_file_id, event_id=event.id,
            line_start=event.line_no,
            detail={**detail, "event_type": field_key[0], "field": name, "value": value, "observed_min": low,
                    "observed_max": high, "observations": len(values), "outside_count": len(outside)},
        ))
        if len(out) >= MAX_PER_KIND:
            break
    return out


# ---------------------------------------------------------------- 값 멈춤

def _longest_same(values: Sequence[float]) -> tuple[int, int]:
    """(가장 긴 같은 값 연속 길이, 시작 위치)."""
    best, best_at, run, start = 0, 0, 0, 0
    for index, value in enumerate(values):
        if index and value == values[index - 1]:
            run += 1
        else:
            run, start = 1, index
        if run > best:
            best, best_at = run, start
    return best, best_at


def _stuck(events: Sequence[EventRecord], refs: Sequence[ReferenceFile], detail: dict,
           log_file_id: int) -> list[Finding]:
    """정상 로그에서 늘 조금씩 변하던 값(측정값 등)이 이 로그에서 여러 번 연속 똑같음(멈춘 값)."""
    ref_values: dict[tuple[str, str], list[list[float]]] = defaultdict(list)
    for r in refs:
        for field_key, items in _numeric_fields(r.events).items():
            ref_values[field_key].append([v for v, _e in items])
    out = []
    for field_key, items in _numeric_fields(events).items():
        series = ref_values.get(field_key)
        if not series or len({v for values in series for v in values}) < STUCK_MIN_DISTINCT:
            continue
        reference_run = max(_longest_same(values)[0] for values in series)
        mine = [v for v, _e in items]
        run, start = _longest_same(mine)
        if run < STUCK_MIN_RUN or run <= 2 * max(reference_run, 1):
            continue
        event = items[start][1]
        out.append(Finding(
            "DETERMINISTIC", "VALUE_STUCK",
            f"'{field_key[1]}' 값이 {run}번 연속 {_number(mine[start])}로 같음({event.line_no}행부터): "
            f"{_first_line(event.message)}. 비슷한 정상 로그에서는 같은 값이 연속 최대 {reference_run}번",
            stance="SUPPORTS", severity="SUSPECT", log_file_id=log_file_id, event_id=event.id,
            line_start=event.line_no,
            detail={**detail, "event_type": field_key[0], "field": field_key[1], "value": mine[start], "run": run,
                    "reference_max_run": reference_run},
        ))
    return out[:MAX_PER_KIND]


# ---------------------------------------------------------------- 표기

def _first_line(text: str) -> str:
    return text.split("\n", 1)[0][:120]


def _duration(us: int) -> str:
    return f"{us / 1_000_000:.1f}초" if us >= 1_000_000 else f"{us / 1000:.0f}ms"


def _number(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:g}"
