"""계층 2 — 학습 사례 검색 (계획서 §6.3: 모드·조건 필터, 이벤트 유형·오류 코드 비교, 부분 순서 비교, 텍스트 검색).

승인된 사례(approved_cases 뷰)만 본다. 유사도는 **순위를 매기기 위한 점수**이며 진단 확률이 아니다(§6.3).
점수 = 이벤트 유형 겹침(Jaccard) 0.5 + 오류 코드 겹침 0.3 + 첫 등장 순서 유사도(LCS) 0.2.
양쪽 모두 오류 코드가 없으면 유형 0.7 + 순서 0.3.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from typing import Sequence

from app.core.feature_extractor import FileFeatures, first_occurrence_order
from app.core.sequence_analyzer import lcs_ratio
from app.learning.normal_learner import case_file_features
from app.models.records import CaseRecord
from app.storage.database import Database
from app.storage.repositories import CaseRepository, SearchRepository

SIMILAR_THRESHOLD = 0.4
REFERENCE_LIMIT = 3  # 운전 모드 없이 분석할 때 기준으로 삼을 정상 로그 수 (CR-06 LI-06)


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

    @property
    def relevant(self) -> bool:
        return self.similarity >= SIMILAR_THRESHOLD or bool(self.shared_codes)

    def explain(self) -> str:
        parts = [f"점수 {self.similarity:.2f}(확률 아님)", f"유형 겹침 {self.type_overlap:.2f}",
                 f"순서 {self.order_similarity:.2f}"]
        if self.shared_codes:
            parts.append(f"같은 오류 코드 {', '.join(self.shared_codes)}")
        return ", ".join(parts)


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if (a or b) else 0.0


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
        score = 0.5 * type_overlap + 0.3 * code_overlap + 0.2 * order
    else:
        score = 0.7 * type_overlap + 0.3 * order
    return (round(score, 4), round(type_overlap, 4), round(code_overlap, 4), round(order, 4),
            sorted(in_types & case_types)[:20], sorted(in_codes & case_codes))


def find_similar(database: Database, input_files: Sequence[FileFeatures], case_type: str, mode_id: int | None,
                 limit: int = 5) -> list[SimilarCase]:
    with closing(database.connect()) as conn:
        candidates = CaseRepository(conn).approved(case_type=case_type, mode_id=mode_id)
    codes = sorted(set().union(*(f.error_codes for f in input_files))) if input_files else []
    results: list[SimilarCase] = []
    for case in candidates:
        files = case_file_features(database, case.id)
        score, types, code_overlap, order, shared_types, shared_codes = compare(input_files, files)
        text_hit = False
        if codes:
            with closing(database.connect()) as conn:
                text_hit = bool(SearchRepository(conn).search(" ".join(codes), [f.log_file_id for f in files], limit=1))
        results.append(SimilarCase(case, score, types, code_overlap, order, shared_types, shared_codes, text_hit))
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
