"""정상 패턴 후보 만들기 (계획서 §3.1 정상 패턴 유형, §6.1 처리 알고리즘 7~8, 결정론).

같은 운전 모드의 정상 로그들을 **로그 역할별로** 묶어 비교한다(역할이 다른 로그는 서로 다른 컴포넌트).
- 필수(REQUIRED): 그 역할의 모든 로그에 나옴 (로그 2개 이상일 때만)
- 선택(OPTIONAL): 일부 로그에만 나옴
- 반복(REPEATED): 한 로그 안에서 2번 이상 나옴. 관찰된 최소~최대 횟수
- 순서 제약(ORDER_CONSTRAINT): 필수 이벤트 a의 첫 등장이 모든 로그에서 b보다 앞섬(바로 이웃한 관계만 남김)
- 독립(INDEPENDENT): 필수 이벤트인데 다른 필수 이벤트와의 순서가 로그마다 다름
- 미분류(UNCLASSIFIED): 로그가 1개뿐이라 필수·선택을 가를 수 없음

모두 '관찰된' 후보(basis=OBSERVED, status=PROPOSED)다. 확정은 사용자가 승인해야 한다(§3.1 마지막 문단).
장비 사양상 필수 조건은 여기서 만들지 않는다(§6.1 "관찰된 공통 패턴과 실제 장비 사양상 필수 조건을 구분").
"""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations
from typing import Sequence

from app.core.feature_extractor import FileFeatures
from app.models.records import PatternCandidate

MAX_TYPES_PER_ROLE = 150


def propose_patterns(files: Sequence[FileFeatures]) -> list[PatternCandidate]:
    by_role: dict[str, list[FileFeatures]] = defaultdict(list)
    for features in files:
        by_role[features.role_key].append(features)
    candidates: list[PatternCandidate] = []
    for role, group in sorted(by_role.items()):
        candidates.extend(_role_candidates(role, group))
    return candidates


def _role_candidates(role: str, group: list[FileFeatures]) -> list[PatternCandidate]:
    n = len(group)
    role_value = None if role == "(역할 없음)" else role
    totals: dict[str, int] = defaultdict(int)
    present: dict[str, int] = defaultdict(int)
    for features in group:
        for key, count in features.type_counts.items():
            totals[key] += count
            present[key] += 1
    keys = sorted(totals, key=lambda k: (-totals[k], k))[:MAX_TYPES_PER_ROLE]
    skipped = max(0, len(totals) - len(keys))

    def definition(**extra) -> dict:
        base = {"log_role": role_value, "sample_files": n}
        base.update(extra)
        return base

    out: list[PatternCandidate] = []
    required = [k for k in keys if n >= 2 and present[k] == n]
    for key in keys:
        if n < 2:
            out.append(PatternCandidate(pattern_type="UNCLASSIFIED", definition=definition(events=[key]),
                                        observed_count=present[key],
                                        note="로그가 1개뿐이라 필수·선택을 구분할 수 없음"))
        elif present[key] == n:
            out.append(PatternCandidate(pattern_type="REQUIRED", definition=definition(events=[key]),
                                        observed_count=present[key], note=f"로그 {n}개 모두에서 관찰"))
        else:
            out.append(PatternCandidate(pattern_type="OPTIONAL", definition=definition(events=[key]),
                                        observed_count=present[key], note=f"로그 {n}개 중 {present[key]}개에서 관찰"))
        counts = [f.type_counts[key] for f in group if key in f.type_counts]
        if max(counts) >= 2:
            out.append(PatternCandidate(pattern_type="REPEATED",
                                        definition=definition(events=[key], min=min(counts), max=max(counts)),
                                        observed_count=len(counts), note=f"한 로그에서 {min(counts)}~{max(counts)}회 관찰"))

    if len(required) >= 2:
        out.extend(_order_candidates(group, required, definition))
    if skipped:
        out.append(PatternCandidate(pattern_type="UNCLASSIFIED", definition=definition(events=[], skipped_types=skipped),
                                    observed_count=0, note=f"이벤트 유형이 많아 빈도 낮은 {skipped}개는 후보에서 뺌"))
    return out


def _order_candidates(group: list[FileFeatures], required: list[str], definition) -> list[PatternCandidate]:
    before: set[tuple[str, str]] = set()
    inconsistent: dict[str, set[str]] = defaultdict(set)
    for a, b in combinations(required, 2):
        a_first = [f.first_index[a] < f.first_index[b] for f in group]
        if all(a_first):
            before.add((a, b))
        elif not any(a_first):
            before.add((b, a))
        else:
            inconsistent[a].add(b)
            inconsistent[b].add(a)
    # 바로 이웃한 관계만 남긴다: a<c<b가 모두 성립하면 a<b는 뺀다
    direct = {
        (a, b) for (a, b) in before
        if not any((a, c) in before and (c, b) in before for c in required if c not in (a, b))
    }
    out: list[PatternCandidate] = []
    for a, b in sorted(direct, key=lambda p: (min(f.first_index[p[0]] for f in group), p)):
        gaps = _observed_gaps(group, a, b)
        extra = {"before": a, "after": b, "events": [a, b]}
        if gaps:
            extra["observed_max_gap_us"] = max(gaps)
            extra["observed_min_gap_us"] = min(gaps)
        out.append(PatternCandidate(pattern_type="ORDER_CONSTRAINT", definition=definition(**extra),
                                    observed_count=len(group), note=f"로그 {len(group)}개 모두에서 {a} 다음 {b}"))
    for key, others in sorted(inconsistent.items()):
        out.append(PatternCandidate(pattern_type="INDEPENDENT",
                                    definition=definition(events=[key], unordered_with=sorted(others)),
                                    observed_count=len(group), note="다른 필수 이벤트와의 순서가 로그마다 다름"))
    return out


def _observed_gaps(group: list[FileFeatures], a: str, b: str) -> list[int]:
    gaps = []
    for features in group:
        ea, eb = features.first_event.get(a), features.first_event.get(b)
        if ea and eb and ea.ts_value_us is not None and eb.ts_value_us is not None and ea.ts_kind == eb.ts_kind:
            gaps.append(eb.ts_value_us - ea.ts_value_us)
    return gaps
