"""계층 2 — 학습 사례 검색 (계획서 §6.3: 모드·조건 필터, 이벤트 유형·오류 코드 비교, 부분 순서 비교, 텍스트 검색).

승인된 사례(approved_cases 뷰)만 본다. 유사도는 **순위를 매기기 위한 점수**이며 진단 확률이 아니다(§6.3).
점수 = 이벤트 유형 겹침(Jaccard) 0.5 + 오류 코드 겹침 0.3 + 첫 등장 순서 유사도(LCS) 0.2.
양쪽 모두 오류 코드가 없으면 유형 0.7 + 순서 0.3.
CR-13 SQ-02: 같은 오류 코드라도 직전 단계(시퀀스 위치)가 다르면 코드 몫을 절반으로 본다(같은 증상, 다른 원인일 수 있음).
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from typing import Sequence

from app.core.feature_extractor import FileFeatures, code_stages, first_occurrence_order
from app.core.sequence_analyzer import lcs_ratio
from app.learning.normal_learner import case_file_features
from app.models.records import CaseRecord
from app.storage.database import Database
from app.storage.repositories import CaseRepository, SearchRepository

SIMILAR_THRESHOLD = 0.4
REFERENCE_LIMIT = 3  # 운전 모드 없이 분석할 때 기준으로 삼을 정상 로그 수 (CR-06 LI-06)
REFERENCE_POOL = 20  # 정상 로그와 비교하는 통계(CR-08 DT-01~10)에 쓸 비슷한 정상 로그 최대 수
CONTAINMENT_THRESHOLD = 0.8  # 점수는 낮아도 입력 로그의 이벤트 종류가 거의 다 들어 있는 정상 로그(끊긴 로그의 비교 기준)
CONTAINMENT_MIN_SCORE = 0.2


def comparison_pool(similar_normal: Sequence[SimilarCase]) -> list[SimilarCase]:
    """정상 로그 대비 비교(CR-08)에 쓸 기준: 점수 0.4 이상, 또는 점수 0.2 이상이면서 입력 로그 이벤트 종류의 80% 이상을 담은 것."""
    return [s for s in similar_normal if s.similarity >= SIMILAR_THRESHOLD
            or (s.containment >= CONTAINMENT_THRESHOLD and s.similarity >= CONTAINMENT_MIN_SCORE)]


@dataclass
class SimilarCase:
    case: CaseRecord
    similarity: float
    type_overlap: float
    code_overlap: float
    order_similarity: float
    shared_types: list[str] = field(default_factory=list)
    shared_codes: list[str] = field(default_factory=list)
    text_hit: bool = False
    containment: float = 0.0  # 입력 로그의 이벤트 종류 중 이 사례에도 있는 비율(끊긴 로그도 비교 기준을 고르려고, CR-08)
    same_stage_codes: list[str] = field(default_factory=list)  # 같은 오류 코드가 같은 직전 단계에서 남(SQ-02)
    other_stage_codes: list[str] = field(default_factory=list)  # 같은 오류 코드지만 다른 단계에서 남

    @property
    def relevant(self) -> bool:
        return self.similarity >= SIMILAR_THRESHOLD or bool(self.shared_codes)

    def explain(self) -> str:
        parts = [f"점수 {self.similarity:.2f}(확률 아님)", f"유형 겹침 {self.type_overlap:.2f}",
                 f"순서 {self.order_similarity:.2f}"]
        if self.shared_codes:
            parts.append(f"같은 오류 코드 {', '.join(self.shared_codes)}")
        if self.same_stage_codes:
            parts.append(f"같은 단계에서 남 {', '.join(self.same_stage_codes)}")
        if self.other_stage_codes:
            parts.append(f"다른 단계에서 남 {', '.join(self.other_stage_codes)}")
        return ", ".join(parts)


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if (a or b) else 0.0


def stage_match(input_files: Sequence[FileFeatures], case_files: Sequence[FileFeatures],
                codes: Sequence[str]) -> tuple[list[str], list[str]]:
    """같은 오류 코드마다 직전 단계가 같은지: (같은 단계 코드, 다른 단계 코드). 어느 쪽이든 단계를 못 찾으면 넣지 않는다."""
    mine: dict[str, set[str]] = {}
    theirs: dict[str, set[str]] = {}
    for files, target in ((input_files, mine), (case_files, theirs)):
        for f in files:
            for code, stage in code_stages(f).items():
                if stage:
                    target.setdefault(code, set()).add(stage)
    same = [c for c in codes if mine.get(c) and theirs.get(c) and mine[c] & theirs[c]]
    other = [c for c in codes if mine.get(c) and theirs.get(c) and not mine[c] & theirs[c]]
    return same, other


def compare(input_files: Sequence[FileFeatures], case_files: Sequence[FileFeatures]) -> tuple[float, float, float, float, list[str], list[str]]:
    in_types = set().union(*(f.type_counts for f in input_files)) if input_files else set()
    case_types = set().union(*(f.type_counts for f in case_files)) if case_files else set()
    in_codes = set().union(*(f.error_codes for f in input_files)) if input_files else set()
    case_codes = set().union(*(f.error_codes for f in case_files)) if case_files else set()
    type_overlap = _jaccard(in_types, case_types)
    code_overlap = _jaccard(in_codes, case_codes)
    order = 0.0
    for a in input_files:
        for b in case_files:
            if a.log_role == b.log_role:
                order = max(order, lcs_ratio(first_occurrence_order(a), first_occurrence_order(b)))
    if in_codes or case_codes:
        shared = sorted(in_codes & case_codes)
        _same, other = stage_match(input_files, case_files, shared)
        code_part = code_overlap * (1 - 0.5 * len(other) / len(shared)) if shared else code_overlap
        score = 0.5 * type_overlap + 0.3 * code_part + 0.2 * order
    else:
        score = 0.7 * type_overlap + 0.3 * order
    return (round(score, 4), round(type_overlap, 4), round(code_overlap, 4), round(order, 4),
            sorted(in_types & case_types)[:20], sorted(in_codes & case_codes))


def find_similar(database: Database, input_files: Sequence[FileFeatures], case_type: str, mode_id: int | None,
                 limit: int = 5, seen_types: set[str] | None = None,
                 exclude_case_ids: frozenset[int] = frozenset()) -> list[SimilarCase]:
    """seen_types를 주면 살펴본 모든 사례 로그의 이벤트 유형을 모아 둔다(정상 로그 전체에 있던 경고인지 보려고, DT-18).
    exclude_case_ids의 사례는 보지 않는다(사례 로그의 지문을 다시 만들 때 자기 자신, CR-14)."""
    with closing(database.connect()) as conn:
        candidates = [c for c in CaseRepository(conn).approved(case_type=case_type, mode_id=mode_id)
                      if c.id not in exclude_case_ids]
    codes = sorted(set().union(*(f.error_codes for f in input_files))) if input_files else []
    results: list[SimilarCase] = []
    for case in candidates:
        files = case_file_features(database, case.id)
        if seen_types is not None:
            for f in files:
                seen_types.update(f.type_counts)
        score, types, code_overlap, order, shared_types, shared_codes = compare(input_files, files)
        text_hit = False
        if codes:
            with closing(database.connect()) as conn:
                text_hit = bool(SearchRepository(conn).search(" ".join(codes), [f.log_file_id for f in files], limit=1))
        in_types = set().union(*(f.type_counts for f in input_files)) if input_files else set()
        case_types = set().union(*(f.type_counts for f in files)) if files else set()
        containment = len(in_types & case_types) / len(in_types) if in_types else 0.0
        same, other = stage_match(input_files, files, shared_codes)
        results.append(SimilarCase(case, score, types, code_overlap, order, shared_types, shared_codes, text_hit,
                                   round(containment, 4), same, other))
    results.sort(key=lambda s: (s.relevant, s.similarity), reverse=True)
    return results[:limit]


def reference_normals(similar_normal: Sequence[SimilarCase]) -> list[SimilarCase]:
    """운전 모드를 고르지 않았을 때 비교 기준으로 삼을 정상 사례: 점수 0.4 이상 중 위에서 최대 3개."""
    return [s for s in similar_normal if s.similarity >= SIMILAR_THRESHOLD][:REFERENCE_LIMIT]


def normal_types_for_cases(database: Database, case_ids: Sequence[int]) -> dict[str | None, set[str]] | None:
    """고른 정상 사례들에서 관찰된 이벤트 유형(로그 역할별). 사례가 없으면 None."""
    if not case_ids:
        return None
    by_role: dict[str | None, set[str]] = {}
    for case_id in case_ids:
        for f in case_file_features(database, case_id):
            by_role.setdefault(f.log_role, set()).update(f.type_counts)
    return by_role


def approved_normal_types(database: Database, mode_id: int | None) -> dict[str | None, set[str]] | None:
    """같은 모드의 승인된 정상 사례에서 관찰된 이벤트 유형(로그 역할별). 승인된 정상 사례가 없으면 None."""
    if mode_id is None:
        return None
    with closing(database.connect()) as conn:
        cases = CaseRepository(conn).approved(case_type="NORMAL", mode_id=mode_id)
    if not cases:
        return None
    by_role: dict[str | None, set[str]] = {}
    for case in cases:
        for f in case_file_features(database, case.id):
            by_role.setdefault(f.log_role, set()).update(f.type_counts)
    return by_role
