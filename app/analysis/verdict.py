"""최종 판정 하나: 정상 / 비정상 / 판단 보류 (CR-14 리뷰 2026-11-22).

규칙 판정과 사례 기억 보정이 낸 이상 수준(anomaly_level)을 바탕으로, 근거가 부족하거나 판단이 엇갈리면 '판단 보류'로 둔다.
- 비정상: 규칙·사례 기억이 이상 근거를 찾음(의심·승인된 규칙 위반).
- 판단 보류: 비교할 정상 로그가 없음 / 비슷한 정상 로그가 없어 제대로 비교 못 함 / 로그가 짧음 /
  근거 지문이 비슷한 확인 로그의 판정이 엇갈림.
- 정상: 이상을 못 찾았고 위 보류 사유가 없음.
AI(LLM) 의견은 판정에 넣지 않고 설명 보조로 화면에 따로 보인다(2026-11-22 사용자 결정). 측정에서 gemma3:4b의 판정은
규칙·사례 기억보다 30%p 넘게 낮았고, AI만으로 건 '판단 보류' 6건 중 5건이 정상이었다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.learning.case_memory import ABNORMAL_SHARE, MIN_VOTE_WEIGHT, NORMAL_SHARE

ABNORMAL, NORMAL, UNDECIDED = "ABNORMAL", "NORMAL", "UNDECIDED"


@dataclass
class Verdict:
    state: str  # ABNORMAL / NORMAL / UNDECIDED
    title: str  # 화면 큰 글자
    tone: str  # bad(규칙 위반) / warn(비정상 의심) / info(판단 보류) / ok(정상) — 화면 색조
    reasons: list[str] = field(default_factory=list)
    short: str = ""  # 목록 칸의 짧은 글

    @property
    def decided(self) -> bool:
        return self.state != UNDECIDED


def memory_split(memory: Any) -> bool:
    """사례 보정이 켜져 있고, 충분히 비슷한 확인 로그들의 판정이 엇갈림(비정상 비율 1/3~2/3)."""
    if memory is None or memory.calibration is None or memory.calibration.tau is None or memory.share is None:
        return False
    weight = sum(s for s, _c, _f in memory.neighbors)
    return weight >= MIN_VOTE_WEIGHT and NORMAL_SHARE < memory.share < ABNORMAL_SHARE


def final_verdict(axes: dict[str, str], memory: Any = None, ai_judgment: Any = None, short_log: bool = False) -> Verdict:
    """ai_judgment는 받기만 하고 판정에는 쓰지 않는다(설명 보조)."""
    level = axes.get("anomaly_level")
    if level == "RULE_VIOLATION":
        return Verdict(ABNORMAL, "비정상 — 승인된 규칙 위반", "bad", ["승인된 규칙을 어긴 근거가 있음"], "규칙 위반")
    if level == "SUSPECTED":
        reasons = ["정상 로그·규칙과 다른 근거가 있음"]
        if memory is not None and memory.changed:
            reasons = ["근거 지문이 비슷한 확인된 비정상 로그가 있음(사례 판단)"]
        return Verdict(ABNORMAL, "비정상 의심", "warn", reasons, "비정상 의심")
    if level in (None, "NOT_EVALUATED"):
        return Verdict(UNDECIDED, "판단 보류", "info", ["비교할 정상 로그가 없음 — 정상 로그를 먼저 학습하세요"], "판단 보류")
    reasons = []
    if axes.get("normal_match") == "NO_MATCH":
        reasons.append("비슷한 정상 로그가 없어 제대로 비교하지 못함")
    if short_log:
        reasons.append("로그가 짧아 비교 근거가 적음")
    if memory_split(memory):
        reasons.append("근거 지문이 비슷한 확인 로그들의 판정이 엇갈림")
    if reasons:
        return Verdict(UNDECIDED, "판단 보류", "info", reasons, "판단 보류")
    normal_reasons = ["정상 로그와 비교해 이상 근거를 찾지 못함"]
    if memory is not None and memory.changed:
        normal_reasons = ["규칙이 본 차이가 모두 확인된 정상 로그에도 있었음(사례 판단)"]
    return Verdict(NORMAL, "정상 — 찾은 이상 없음", "ok", normal_reasons, "정상")


def verdict_of(result: Any) -> Verdict:
    return final_verdict(result.axes, getattr(result, "memory", None), getattr(result, "ai_judgment", None),
                         getattr(result, "short_log", False))
