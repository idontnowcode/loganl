"""최종 판정 하나: 정상 / 비정상 / 판단 보류 (CR-14 리뷰 2026-11-22). 데이터는 가상 값이다."""

from __future__ import annotations

from dataclasses import dataclass

from app.analysis.verdict import final_verdict
from app.learning.case_memory import AiJudgment, Calibration, MemoryDecision


@dataclass
class FakeCase:
    id: int
    case_type: str
    label: str = "C-0001"


@dataclass
class FakeItem:
    case: FakeCase

    @property
    def abnormal(self) -> bool:
        return self.case.case_type == "ABNORMAL"


def memory(share: float | None, sims=(0.8, 0.7), tau: float | None = 0.5) -> MemoryDecision:
    decision = MemoryDecision("NONE_FOUND", "NONE_FOUND", share, total=10)
    decision.calibration = Calibration(tau, 0.8, 0.9, 5, 5)
    decision.neighbors = [(s, FakeItem(FakeCase(i, "ABNORMAL" if i % 2 else "NORMAL")), []) for i, s in enumerate(sims)]
    return decision


def test_abnormal_normal_and_undecided() -> None:
    assert final_verdict({"anomaly_level": "SUSPECTED"}).state == "ABNORMAL"
    assert final_verdict({"anomaly_level": "RULE_VIOLATION"}).tone == "bad"
    ok = final_verdict({"anomaly_level": "NONE_FOUND", "normal_match": "MATCH"})
    assert (ok.state, ok.short, ok.tone) == ("NORMAL", "정상", "ok")
    held = final_verdict({"anomaly_level": "NOT_EVALUATED"})
    assert (held.state, held.short, held.tone) == ("UNDECIDED", "판단 보류", "info") and "정상 로그" in held.reasons[0]


def test_undecided_when_evidence_is_thin_or_split() -> None:
    axes = {"anomaly_level": "NONE_FOUND", "normal_match": "MATCH"}
    assert final_verdict({**axes, "normal_match": "NO_MATCH"}).state == "UNDECIDED"
    assert final_verdict(axes, short_log=True).state == "UNDECIDED"
    split = final_verdict(axes, memory(0.5))
    assert split.state == "UNDECIDED" and "엇갈림" in split.reasons[0]
    assert final_verdict(axes, memory(0.5, sims=(0.4,))).state == "NORMAL"  # 비슷한 정도 합이 1 미만이면 엇갈림으로 안 봄
    assert final_verdict(axes, memory(0.5, tau=None)).state == "NORMAL"  # 사례 보정이 꺼져 있으면


def test_ai_opinion_never_changes_the_verdict() -> None:
    """AI(LLM) 의견은 판정에 넣지 않는다(2026-11-22 사용자 결정: 설명 보조). 확인 이력으로 보정된 AI라도 마찬가지."""
    axes = {"anomaly_level": "NONE_FOUND", "normal_match": "MATCH"}
    assert final_verdict(axes, ai_judgment=AiJudgment(90, 85, "ABNORMAL", 12, True)).state == "NORMAL"
    assert final_verdict({"anomaly_level": "SUSPECTED"}, ai_judgment=AiJudgment(10, 85, "NORMAL", 12, True)).state == "ABNORMAL"
