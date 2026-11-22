"""사례 기억 (CR-14 CM-01~03). 확인된 로그(승인된 단일 로그 사례)가 쌓일수록 판정이 나아지게 한다.

- 근거 지문: 로그 하나의 결정론 근거를 위치·값을 뺀 특징 문자열 집합으로 바꾼 것. 데이터 폴더에 캐시(로그·사례로 다시 만들 수 있음).
- 사례 기억: 확인된 로그들의 지문. 특징마다 드문 정도로 무게(IDF)를 주고 가중 자카드로 비슷한 정도를 잰다.
- 사례 판단: 이 로그와 지문이 비슷한 확인 로그들의 판정 투표로 규칙 판정을 보정한다. 문턱은 확인 로그끼리
  '하나씩 빼고 판단'(leave-one-out)해서 스스로 고르고, 규칙만보다 나을 때만 쓴다 → 코드를 고치지 않아도 사례가 쌓이면 나아진다.
모델 재학습은 하지 않는다. 뜻도 지어내지 않는다(사례의 원인 글은 엔지니어가 쓴 그대로).
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

FP_VERSION = 1  # 지문 특징을 만드는 방식이 바뀌면 올린다(캐시를 다시 만듦)
RECOMPUTE_GROWTH = 2.0  # 지문을 만들 때보다 정상 사례가 2배 넘게(그리고 10개 넘게) 늘면 다시 만든다
RECOMPUTE_MIN_NEW = 10
MIN_PER_CLASS = 3  # 확인된 정상·비정상이 각각 이만큼 있어야 판정을 보정한다
TAUS = (0.3, 0.4, 0.5, 0.6, 0.7)  # 이웃으로 볼 유사도 문턱 후보(스스로 고름)
SHOW_TAU = 0.3  # 화면·AI에 보여 줄 이웃의 최소 유사도
NEIGHBORS = 5
MIN_VOTE_WEIGHT = 1.0  # 판정을 바꾸려면 이웃 유사도 합이 이만큼(예: 유사도 1.0 하나, 0.5 둘) 있어야 한다
ABNORMAL_SHARE = 2 / 3
NORMAL_SHARE = 1 / 3
DETECTED = frozenset({"SUSPECTED", "RULE_VIOLATION"})
FINGERPRINT_BUDGET_S = 5.0  # 분석 한 번에 사례 지문을 새로 만드는 데 쓸 시간(나머지는 prepare_case_memory·다음 분석)


# ---------------------------------------------------------------- 근거 지문

@dataclass(frozen=True)
class Fingerprint:
    strong: frozenset[str]  # 규칙이 '의심'으로 본 근거(SUPPORTS)
    weak: frozenset[str]  # 참고로만 본 근거
    rule_level: str  # 그때 규칙 판정(anomaly_level)
    normals: int  # 만들 때 저장돼 있던 정상 사례 수
    version: int = FP_VERSION
    ai_ran: bool = False  # 이 로그를 분석할 때 AI 의견이 나왔는지(CM-07)
    ai_likelihood: int | None = None  # 그때 AI가 낸 비정상 가능성(없으면 None)

    @property
    def features(self) -> frozenset[str]:
        return self.strong | self.weak

    def as_dict(self) -> dict[str, Any]:
        return {"version": self.version, "strong": sorted(self.strong), "weak": sorted(self.weak),
                "rule_level": self.rule_level, "normals": self.normals, "ai_ran": self.ai_ran,
                "ai_likelihood": self.ai_likelihood}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Fingerprint:
        likelihood = data.get("ai_likelihood")
        return cls(frozenset(data.get("strong", [])), frozenset(data.get("weak", [])), str(data.get("rule_level", "")),
                   int(data.get("normals", 0)), int(data.get("version", 0)), bool(data.get("ai_ran", False)),
                   int(likelihood) if likelihood is not None else None)


def feature_text(finding: Any) -> str:
    """결정론 근거 하나 → 위치·값·횟수를 뺀 특징 글자. 같은 종류의 차이는 로그가 달라도 같은 글자가 된다."""
    detail = getattr(finding, "detail", None) or {}
    kind = detail.get("kind")
    head = f"{finding.finding_type}/{kind}" if kind else finding.finding_type
    if finding.finding_type == "EXPLICIT_ERROR" and detail.get("error_code"):
        subject = str(detail["error_code"])
    elif detail.get("component"):
        subject = str(detail["component"])
    elif detail.get("field"):
        subject = f"{detail['field']}@{detail['event_type']}" if detail.get("event_type") else str(detail["field"])
    elif detail.get("event_type"):
        subject = str(detail["event_type"])
    elif detail.get("before") and detail.get("after"):
        subject = f"{detail['before']}>{detail['after']}"
    else:
        subject = ""
    return f"{head}:{subject}" if subject else head


def fingerprint_of(findings: Iterable[Any], rule_level: str, normals: int) -> Fingerprint:
    strong: set[str] = set()
    weak: set[str] = set()
    for finding in findings:
        if finding.layer != "DETERMINISTIC":
            continue
        (strong if finding.stance == "SUPPORTS" else weak).add(feature_text(finding))
    return Fingerprint(frozenset(strong), frozenset(weak - strong), rule_level, normals)


def unit_key(log_file_ids: Iterable[int]) -> str:
    """분석 단위(로그 하나 또는 같은 수집 세션의 로그 묶음)의 지문 열쇠: 로그 id를 작은 것부터 '-'로. 로그 하나면 그 id."""
    return "-".join(str(i) for i in sorted(set(log_file_ids)))


class FingerprintStore:
    """데이터 폴더 analysis/fingerprints/<단위 열쇠>.json. 지우면 다시 만든다(캐시)."""

    def __init__(self, analysis_dir: Path) -> None:
        self.root = analysis_dir / "fingerprints"

    def get(self, key: int | str) -> Fingerprint | None:
        path = self.root / f"{key}.json"
        try:
            fp = Fingerprint.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None
        return fp if fp.version == FP_VERSION else None

    def put(self, key: int | str, fp: Fingerprint) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{key}.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(fp.as_dict(), ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)


def stale(fp: Fingerprint, normals_now: int) -> bool:
    return normals_now > fp.normals * RECOMPUTE_GROWTH and normals_now - fp.normals >= RECOMPUTE_MIN_NEW


# ---------------------------------------------------------------- 사례 기억

@dataclass
class MemoryCase:
    case: Any  # CaseRecord
    log_file_id: int
    fp: Fingerprint

    @property
    def abnormal(self) -> bool:
        return self.case.case_type == "ABNORMAL"


def weights(cases: Sequence[MemoryCase]) -> dict[str, float]:
    """특징마다 무게: 확인 로그 중 그 특징이 있는 로그가 적을수록 크다(IDF)."""
    df: dict[str, int] = {}
    for c in cases:
        for f in c.fp.features:
            df[f] = df.get(f, 0) + 1
    n = len(cases)
    return {f: math.log((n + 1) / (k + 1)) + 1.0 for f, k in df.items()}


def similarity(a: frozenset[str], b: frozenset[str], w: dict[str, float]) -> float:
    union = a | b
    if not union:
        return 0.0
    total = sum(w.get(f, 1.0) for f in union)
    return sum(w.get(f, 1.0) for f in a & b) / total if total else 0.0


def _vote(scored: Sequence[tuple[float, MemoryCase]], tau: float) -> tuple[float | None, list[tuple[float, MemoryCase]]]:
    near = sorted((item for item in scored if item[0] >= tau), key=lambda item: -item[0])[:NEIGHBORS]
    if not near:
        return None, []
    total = sum(s for s, _c in near)
    return (sum(s for s, c in near if c.abnormal) / total if total else None), near


def _adjust(rule_level: str, strong: frozenset[str], share: float | None,
            near: Sequence[tuple[float, MemoryCase]]) -> str:
    if share is None or sum(s for s, _c in near) < MIN_VOTE_WEIGHT:  # 덜 비슷한 사례 하나로는 바꾸지 않는다
        return rule_level
    if rule_level == "SUSPECTED" and share <= NORMAL_SHARE:
        seen = frozenset().union(*(c.fp.features for _s, c in near if not c.abnormal))
        if strong and strong <= seen:  # 의심으로 본 차이가 모두 확인된 정상 로그에도 있었음
            return "NONE_FOUND"
    if rule_level in ("NONE_FOUND", "NOT_EVALUATED") and share >= ABNORMAL_SHARE:
        return "SUSPECTED"
    return rule_level


def _balanced(pairs: Sequence[tuple[bool, bool]]) -> float:
    """(정답이 비정상인가, 비정상이라고 했나) → 정상·비정상 각각 맞힌 비율의 평균."""
    rates = []
    for truth in (True, False):
        group = [said for t, said in pairs if t is truth]
        if group:
            rates.append(sum(said is truth for said in group) / len(group))
    return sum(rates) / len(rates) if rates else 0.0


@dataclass
class Calibration:
    tau: float | None  # 쓸 문턱(None이면 보정하지 않음)
    rule_score: float  # 확인 로그에서 규칙만의 균형 정확도
    memory_score: float  # 그 문턱으로 보정했을 때(하나씩 빼고 판단)
    normals: int
    abnormals: int
    reason: str = ""


def calibrate(cases: Sequence[MemoryCase], w: dict[str, float]) -> Calibration:
    """확인 로그마다 그 로그를 빼고 나머지로 판단해 문턱을 고른다. 규칙만보다 나을 때만 보정을 켠다."""
    normals = sum(not c.abnormal for c in cases)
    abnormals = len(cases) - normals
    rule_pairs = [(c.abnormal, c.fp.rule_level in DETECTED) for c in cases]
    rule_score = _balanced(rule_pairs)
    if normals < MIN_PER_CLASS or abnormals < MIN_PER_CLASS:
        return Calibration(None, rule_score, rule_score, normals, abnormals,
                           f"확인된 정상 {normals}·비정상 {abnormals}개라 아직 판정을 보정하지 않음(각 {MIN_PER_CLASS}개 이상 필요)")
    scored = [[(similarity(a.fp.features, b.fp.features, w), b) for j, b in enumerate(cases) if j != i]
              for i, a in enumerate(cases)]
    best_tau, best = None, rule_score
    for tau in sorted(TAUS, reverse=True):  # 같은 점수면 높은 문턱(보수적)
        pairs = []
        for a, others in zip(cases, scored):
            share, near = _vote(others, tau)
            pairs.append((a.abnormal, _adjust(a.fp.rule_level, a.fp.strong, share, near) in DETECTED))
        score = _balanced(pairs)
        if score > best + 1e-9:
            best_tau, best = tau, score
    if best_tau is None:
        return Calibration(None, rule_score, rule_score, normals, abnormals,
                           "확인 로그로 스스로 점검해 보니 사례로 보정해도 규칙만보다 낫지 않아 보정하지 않음")
    return Calibration(best_tau, rule_score, best, normals, abnormals)


# ---------------------------------------------------------------- 이 로그의 사례 판단

@dataclass
class MemoryDecision:
    rule_level: str
    level: str
    share: float | None
    neighbors: list[tuple[float, MemoryCase, list[str]]] = field(default_factory=list)  # (유사도, 사례, 같았던 특징)
    records: list[tuple[str, int, int]] = field(default_factory=list)  # (특징, 확인된 비정상 수, 정상 수)
    calibration: Calibration | None = None
    total: int = 0
    cases: list[MemoryCase] = field(default_factory=list, repr=False)  # AI 판정 보정(CM-07)에 다시 쓴다
    log_file_id: str | None = None  # 이 판단을 낸 분석 단위의 지문 열쇠(unit_key)
    own_case: bool = False  # 그 로그가 이미 사례인지(그러면 지문을 덮어쓰지 않음)

    @property
    def changed(self) -> bool:
        return self.level != self.rule_level

    def note(self) -> str:
        cal = self.calibration
        if not self.total:
            return "사례 판단: 지문이 있는 확인 로그가 아직 없습니다."
        head = f"사례 판단(확인된 로그 {self.total}개 기억): "
        if self.neighbors:
            abn = sum(c.abnormal for _s, c, _f in self.neighbors)
            head += (f"근거 지문이 비슷한 확인 로그 {len(self.neighbors)}개 중 비정상 {abn}·정상 {len(self.neighbors) - abn}"
                     f"(유사도 {self.neighbors[0][0]:.2f}~{self.neighbors[-1][0]:.2f}). ")
        else:
            head += "근거 지문이 비슷한 확인 로그가 없습니다. "
        if cal is not None and cal.tau is not None:
            head += (f"스스로 점검(확인 로그 하나씩 빼고 판단): 사례 보정 {cal.memory_score:.0%} > 규칙만 {cal.rule_score:.0%}, "
                     f"유사도 {cal.tau:.1f} 이상을 이웃으로 씀.")
        elif cal is not None:
            head += cal.reason + "."
        if self.changed:
            head += f" → 규칙 판정 {self.rule_level}을(를) {self.level}(으)로 바꿈."
        return head


def decide(fp: Fingerprint, cases: Sequence[MemoryCase]) -> MemoryDecision:
    decision = MemoryDecision(fp.rule_level, fp.rule_level, None, total=len(cases), cases=list(cases))
    if not cases:
        return decision
    w = weights(cases)
    scored = [(similarity(fp.features, c.fp.features, w), c) for c in cases]
    cal = calibrate(cases, w)
    decision.calibration = cal
    share, near = _vote(scored, cal.tau if cal.tau is not None else SHOW_TAU)
    shown = near if cal.tau is not None else _vote(scored, SHOW_TAU)[1]
    decision.neighbors = [(s, c, sorted(fp.features & c.fp.features)) for s, c in shown]
    if cal.tau is not None:
        decision.share = share
        decision.level = _adjust(fp.rule_level, fp.strong, share, near)
    for f in sorted(fp.features):
        abn = sum(1 for c in cases if c.abnormal and f in c.fp.features)
        nor = sum(1 for c in cases if not c.abnormal and f in c.fp.features)
        if abn or nor:
            decision.records.append((f, abn, nor))
    decision.records.sort(key=lambda r: -(r[1] + r[2]))
    return decision


def load_cases(approved: Sequence[Any], log_ids: Callable[[int], list[int]], store: FingerprintStore,
               normals_now: int, compute: Callable[[Any, list[int]], Fingerprint | None], budget_s: float,
               clock: Callable[[], float]) -> list[MemoryCase]:
    """확인 로그 목록(로그 묶음 사례도). 지문이 없거나 오래되면 시간 예산 안에서 다시 만든다(없는 것부터).
    못 만든 오래된 지문은 그대로 쓴다. 다시 만들어도 그때 AI가 낸 가능성(CM-07 이력)은 옮겨 둔다(리뷰 2026-11-22)."""
    out: list[MemoryCase] = []
    todo: list[tuple[int, Any, list[int], Fingerprint | None]] = []
    for case in approved:
        ids = sorted(log_ids(case.id))
        if not ids:
            continue
        fp = store.get(unit_key(ids))
        if fp is not None and not stale(fp, normals_now):
            out.append(MemoryCase(case, ids[0], fp))
        else:
            todo.append((0 if fp is None else 1, case, ids, fp))
    started = clock()
    for _order, case, ids, old in sorted(todo, key=lambda t: t[0]):
        fresh = compute(case, ids) if clock() - started < budget_s else None
        if fresh is not None:
            if old is not None and old.ai_ran:
                fresh = replace(fresh, ai_ran=True, ai_likelihood=old.ai_likelihood)
            store.put(unit_key(ids), fresh)
            out.append(MemoryCase(case, ids[0], fresh))
        elif old is not None:
            out.append(MemoryCase(case, ids[0], old))
    return out


# ---------------------------------------------------------------- AI 판정 보정 (CM-07)

AI_THRESHOLDS = tuple(range(50, 100, 5))


@dataclass
class AiJudgment:
    likelihood: int | None  # AI가 낸 비정상 가능성
    threshold: int  # 이 값 이상이면 비정상이라고 본다
    state: str  # ABNORMAL / NORMAL / UNDECIDED(AI가 가능성을 매기지 않았거나 AI 의견이 실패 → 판단 보류)
    history: int  # 문턱을 고르는 데 쓴 확인 로그 수(AI 의견이 있던 것)
    calibrated: bool
    ran: bool = True

    @property
    def abnormal(self) -> bool:
        return self.state == "ABNORMAL"

    @property
    def decided(self) -> bool:
        return self.state != "UNDECIDED"

    def note(self) -> str:
        if not self.ran:
            return "AI 판정: 판단 보류 — AI 의견이 나오지 않았습니다(결정론·사례 판단은 그대로)."
        if self.likelihood is None:
            return "AI 판정: 판단 보류 — AI가 비정상 가능성을 매기지 않았습니다."
        verdict = "비정상 쪽" if self.abnormal else "정상 쪽"
        if self.calibrated:
            return (f"AI 판정(확인 이력 보정): 가능성 {self.likelihood}% → {verdict}. AI 의견이 있던 확인 로그 {self.history}개에서 "
                    f"{self.threshold}% 이상일 때 실제 비정상과 가장 잘 맞았습니다.")
        return (f"AI 판정: 가능성 {self.likelihood}% → {verdict}(기본 문턱 {self.threshold}%). AI 의견이 있던 확인 로그가 정상·비정상 각 "
                f"{MIN_PER_CLASS}개 이상 쌓이면 문턱을 확인 이력으로 고릅니다(지금 {self.history}개).")


def ai_judgment(likelihood: int | None, cases: Sequence[MemoryCase], ran: bool = True) -> AiJudgment:
    """확인된 로그에서 그때 AI가 낸 가능성과 실제 판정을 견줘 '몇 % 이상이면 비정상'인지 고른다(균형 정확도 최대,
    같으면 50%에 가까운 쪽). AI 모델이 가능성을 한쪽으로 쏠리게 내도 쌓인 확인 결과로 맞춘다 → 앱을 고치지 않아도 나아진다.
    가능성을 매기지 않았거나 AI 의견이 실패하면 '판단 보류'(정상 쪽으로 세지 않음, 리뷰 2026-11-22)."""
    pairs = [(c.abnormal, c.fp.ai_likelihood) for c in cases if c.fp.ai_ran and c.fp.ai_likelihood is not None]
    abnormals = sum(truth for truth, _v in pairs)
    threshold, calibrated = 50, False
    if abnormals >= MIN_PER_CLASS and len(pairs) - abnormals >= MIN_PER_CLASS:
        best: tuple[tuple[float, int], int] | None = None
        for tau in AI_THRESHOLDS:
            key = (_balanced([(t, v >= tau) for t, v in pairs]), -abs(tau - 50))
            if best is None or key > best[0]:
                best = (key, tau)
        threshold, calibrated = best[1], True
    if not ran or likelihood is None:
        state = "UNDECIDED"
    else:
        state = "ABNORMAL" if likelihood >= threshold else "NORMAL"
    return AiJudgment(likelihood, threshold, state, len(pairs), calibrated, ran)
