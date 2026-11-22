"""참고 자료의 판정 설명을 판정에 쓰기 (변경 요청 CR-16 RJ-04·05). 결정론.

승인된 참고 자료 항목에 '판정' 칸(예: 에러·정상)이 있고 그 항목이 로그에 나오면:
- 비정상 쪽 낱말 → 비정상 의심(승인된 규칙 위반은 그대로). 정상 로그를 아직 학습하지 않았어도 쓴다.
- 정상 쪽 낱말 → 그 줄에서 나온 결정론 근거는 '문제없음'. 규칙이 의심한 강한 근거가 모두 그런 줄이면 정상.
적용 순서: 규칙 → 사례 기억 → **참고 자료 판정** → 엔지니어 기준(CR-15, 가장 강함).

스스로 점검(RJ-05): 확인된 사례 로그와 자주 어긋나는 항목(비정상이라 적혔는데 확인된 정상 로그에 2번 이상, 비정상 로그보다
많이 나옴 — 정상이라 적힌 항목은 반대)은 이번 판정에 쓰지 않는다. 승인된 문서는 바꿀 수 없으므로 끄지 않고 건너뛴다.
뜻은 만들지 않는다: 이유에는 문서의 코드·판정 낱말·위치를 글자 그대로 인용한다.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.learning.case_memory import feature_text

ABNORMAL, NORMAL = "ABNORMAL", "NORMAL"
MIN_CONFLICTS = 2  # 확인 로그와 이만큼 이상 어긋나고, 맞은 것보다 많으면 쓰지 않는다
SIGN_PREFIX = "REFERENCE:"  # 근거(결정론 근거)가 없는 줄이 비정상으로 적혀 있을 때의 특징 표시


@dataclass
class JudgedMatch:
    match: Any  # app.analysis.reference_matcher.ReferenceMatch
    hit: Any  # app.analysis.reference_matcher.ReferenceHit
    side: str  # ABNORMAL / NORMAL

    def quote(self) -> str:
        return (f"참고 자료 「{self.hit.source()}」에 '{self.hit.code}'이(가) '{self.hit.judgment}'(으)로 적혀 있음"
                f"(로그 {self.match.first_file_name} {self.match.first_line}행)")


@dataclass
class ReferenceDecision:
    rule_level: str  # 참고 자료를 보기 전(규칙 + 사례 기억)
    level: str
    reasons: list[str] = field(default_factory=list)
    used: list[JudgedMatch] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # 쓰지 않은 항목과 이유
    sign_features: set[str] = field(default_factory=set)  # 비정상으로 적힌 줄의 특징(질문에서 묻지 않음, 엔지니어 기준이 덮을 수 있음)
    benign_features: set[str] = field(default_factory=set)  # 정상으로 적힌 줄의 특징

    @property
    def changed(self) -> bool:
        return self.level != self.rule_level

    @property
    def signed(self) -> bool:
        """비정상으로 적힌 항목이 나와 '비정상 의심'이 됨(원래 의심이었어도)."""
        return self.level == "SUSPECTED" and any(i.side == ABNORMAL for i in self.used)

    def note(self) -> str:
        if not (self.used or self.skipped):
            return ""
        parts = []
        if self.used:
            parts.append("참고 자료 판정: " + " / ".join(self.reasons or [i.quote() for i in self.used[:3]]))
            if self.changed:
                parts[-1] += f" → 판정 {self.rule_level}을(를) {self.level}(으)로 바꿈."
        if self.skipped:
            parts.append("판정에 쓰지 않은 참고 자료: " + " / ".join(self.skipped[:3]))
        return " ".join(parts)


def judged_matches(references: Sequence[Any], skipped: list[str]) -> list[JudgedMatch]:
    """판정 칸이 판정 쪽으로 읽히는 항목만. 같은 코드에 문서끼리 판정이 엇갈리면 쓰지 않는다."""
    out: list[JudgedMatch] = []
    for match in references:
        sides: dict[str, Any] = {}
        for hit in match.entries:
            side = hit.side
            if side is not None:
                sides.setdefault(side, hit)
        if len(sides) > 1:
            skipped.append(f"'{match.code}' — 참고 자료끼리 판정이 엇갈림("
                           + ", ".join(f"{h.document_title}: {h.judgment}" for h in sides.values()) + ")")
            continue
        for side, hit in sides.items():
            out.append(JudgedMatch(match, hit, side))
    return out


def apply_reference(references: Sequence[Any], findings: Sequence[Any], strong: frozenset[str], level: str,
                    counts: Callable[[JudgedMatch], tuple[int, int]] | None = None) -> ReferenceDecision:
    """counts(항목) → (그 항목이 나온 확인된 정상 사례 수, 비정상 사례 수). 없으면 스스로 점검을 하지 않는다."""
    decision = ReferenceDecision(level, level)
    for item in judged_matches(references, decision.skipped):
        if counts is not None:
            normal_n, abnormal_n = counts(item)
            wrong, right = (normal_n, abnormal_n) if item.side == ABNORMAL else (abnormal_n, normal_n)
            if wrong >= MIN_CONFLICTS and wrong > right:
                other = "정상" if item.side == ABNORMAL else "비정상"
                decision.skipped.append(f"'{item.hit.code}'('{item.hit.judgment}') — 확인된 {other} 로그 {wrong}개에도 나와 "
                                        "이번 판정에 쓰지 않음")
                continue
        decision.used.append(item)
        ids = set(item.match.event_ids)
        features = {feature_text(f) for f in findings if getattr(f, "layer", None) == "DETERMINISTIC" and f.event_id in ids}
        if item.side == ABNORMAL:
            decision.sign_features |= features | {SIGN_PREFIX + item.match.code_norm}
        else:
            decision.benign_features |= features
    signs = [i for i in decision.used if i.side == ABNORMAL]
    calm = [i for i in decision.used if i.side == NORMAL]
    if signs:
        decision.reasons = [i.quote() for i in signs[:3]]
        if level != "RULE_VIOLATION":  # 승인된 규칙 위반은 그대로
            decision.level = "SUSPECTED"
    elif level == "SUSPECTED" and strong and strong <= decision.benign_features:
        decision.level = "NONE_FOUND"
        decision.reasons = ["규칙이 의심한 근거가 모두 참고 자료에 정상으로 적힌 줄 — " + "; ".join(i.quote() for i in calm[:2])]
    elif calm:
        decision.reasons = [i.quote() for i in calm[:2]]
    return decision
