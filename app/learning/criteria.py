"""판단 질문·판단 기준 (CR-15 JQ-01~06). 설계: docs/CR15_판단_질문_설계.md

애매한 분석에서 엔지니어에게 짧게 묻고, 답을 코드 판정이 그대로 쓸 수 있는 '판단 기준'으로 저장한다.
- 질문 틀은 코드가 근거 지문 특징(CR-14)에서 만든다(AI 없이도 동작). 뜻은 묻기만 하고 지어내지 않는다.
- 기준 종류: ABNORMAL_SIGN(나오면 비정상) / BENIGN(정상에서도 나옴) / VALUE_RANGE(값 범위) / HOLD(상황에 따라 다름 → 사람 확인).
- 판정: 사례 기억 뒤에 기준을 덮어 적용한다(엔지니어가 직접 정한 기준이 가장 강함). 이유는 결과 메모에.
- 잘못된 답 막기: 확인된 로그에서 기준과 어긋난 일이 더 많으면 스스로 끈다(이유를 남김).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any

from app.learning.case_memory import feature_text
from app.storage.database import Database, transaction
from app.storage.repositories import CriteriaRepository

ABNORMAL_SIGN, BENIGN, VALUE_RANGE, HOLD = "ABNORMAL_SIGN", "BENIGN", "VALUE_RANGE", "HOLD"
KIND_TEXT = {ABNORMAL_SIGN: "나오면 비정상", BENIGN: "정상에서도 나옴(문제없음)", VALUE_RANGE: "값 범위", HOLD: "상황에 따라 다름(사람 확인)"}
MAX_QUESTIONS = 3
DECISIVE_HISTORY = 3  # 확인된 로그에서 한쪽 판정으로만 3번 이상 나온 근거는 묻지 않는다
DISABLE_MIN = 2  # 기준과 어긋난 확인 로그가 이만큼 이상이고 맞은 것보다 많으면 끈다
_NUMBER = re.compile(r"(?<![\w.])([A-Za-z_][\w.]*)\s*(?:=|:\s)\s*([-+]?\d+(?:\.\d+)?)(?![\w.:])")


# ---------------------------------------------------------------- 기준

@dataclass(frozen=True)
class Criterion:
    id: int
    feature: str
    kind: str
    question: str
    event_type: str | None = None
    field: str | None = None
    low: float | None = None
    high: float | None = None
    example: str | None = None
    method_text: str | None = None
    status: str = "ACTIVE"
    disabled_reason: str | None = None
    source_log_ids: tuple[int, ...] = ()

    @classmethod
    def from_row(cls, row: Any) -> Criterion:
        return cls(int(row["id"]), row["feature"], row["kind"], row["question"], row["event_type"], row["field"],
                   row["low"], row["high"], row["example"], row["method_text"], row["status"], row["disabled_reason"],
                   tuple(json.loads(row["source_log_ids"] or "[]")))

    def describe(self) -> str:
        """화면·메모용 한 줄: 무엇이 어떤 기준인지(원문 줄이 있으면 그것으로)."""
        what = f"'{self.example}'" if self.example else self.feature
        if self.kind == VALUE_RANGE:
            low = "-∞" if self.low is None else f"{self.low:g}"
            high = "+∞" if self.high is None else f"{self.high:g}"
            return f"'{self.field}' 값은 {low}~{high}이면 정상({what})"
        return f"{what}: {KIND_TEXT.get(self.kind, self.kind)}"


class CriteriaService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def all(self, status: str | None = None) -> list[Criterion]:
        with closing(self.database.connect()) as conn:
            return [Criterion.from_row(r) for r in CriteriaRepository(conn).all(status)]

    def active(self) -> list[Criterion]:
        return self.all("ACTIVE")

    def save_answer(self, question: Question, kind: str, *, low: float | None = None, high: float | None = None,
                    method_text: str = "", log_ids: Sequence[int] = (), run_id: int | None = None) -> int:
        """질문에 답한 것을 기준으로 저장(답 = 승인). 값 범위는 하한·상한 중 하나는 있어야 한다."""
        if kind not in KIND_TEXT:
            raise ValueError(f"기준 종류가 아닙니다: {kind}")
        if kind == VALUE_RANGE and (question.field is None or (low is None and high is None)):
            raise ValueError("값 범위는 값 칸과 하한·상한 중 하나가 있어야 합니다.")
        with closing(self.database.connect()) as conn, transaction(conn):
            return CriteriaRepository(conn).save(
                feature=question.feature, kind=kind, question=question.text, event_type=question.event_type,
                field=question.field, low=low, high=high, example=question.example, method_text=method_text.strip() or None,
                source_log_ids=log_ids, source_run_id=run_id)

    def set_status(self, criterion_id: int, status: str, reason: str | None = None) -> None:
        with closing(self.database.connect()) as conn, transaction(conn):
            CriteriaRepository(conn).set_status(criterion_id, status, reason)

    def delete(self, criterion_id: int) -> None:
        with closing(self.database.connect()) as conn, transaction(conn):
            CriteriaRepository(conn).delete(criterion_id)

    def checked(self, cases: Sequence[Any]) -> list[Criterion]:
        """켜진 기준을 확인된 로그(사례 기억)로 점검해 해로운 것은 끄고(DB에 이유), 남은 켜진 기준을 돌려준다(JQ-06)."""
        active = self.active()
        harmful = self_check(active, cases)
        for criterion, reason in harmful:
            self.set_status(criterion.id, "DISABLED", reason)
        gone = {c.id for c, _r in harmful}
        return [c for c in active if c.id not in gone]


def self_check(criteria: Sequence[Criterion], cases: Sequence[Any]) -> list[tuple[Criterion, str]]:
    """확인된 로그에서 기준과 어긋난 일이 맞은 일보다 많고 2번 이상이면 끈다.
    - 나오면 비정상: 그 특징이 있던 확인된 정상 로그 수 vs 비정상 로그 수.
    - 문제없음: 그 특징이 유일한 강한 근거였던 확인된 비정상 로그 수 vs 그 특징이 있던 정상 로그 수.
    값 범위·사람 확인은 지문만으로 점검할 수 없어 끄지 않는다."""
    out = []
    for c in criteria:
        holding = [case for case in cases if c.feature in case.fp.features]
        normals = sum(not case.abnormal for case in holding)
        abnormals = len(holding) - normals
        if c.kind == ABNORMAL_SIGN and normals >= DISABLE_MIN and normals > abnormals:
            out.append((c, f"확인된 정상 로그 {normals}개에도 나와(비정상 로그 {abnormals}개) 스스로 껐습니다."))
        if c.kind == BENIGN:
            wrong = sum(1 for case in holding if case.abnormal and case.fp.strong <= {c.feature})
            if wrong >= DISABLE_MIN and wrong > normals:
                out.append((c, f"이 근거만 있던 확인된 비정상 로그가 {wrong}개라(정상 로그 {normals}개) 스스로 껐습니다."))
    return out


# ---------------------------------------------------------------- 기준 적용 (JQ-04)

@dataclass
class CriteriaDecision:
    rule_level: str  # 기준을 보기 전 판정(규칙 + 사례 기억)
    level: str
    reasons: list[str] = field(default_factory=list)
    hold: list[str] = field(default_factory=list)  # 판단 보류 사유(사람 확인 기준)
    fired: list[Criterion] = field(default_factory=list)
    hold_decisive: bool = False  # 사람 확인 기준이 판정을 좌우함(다른 이상 근거 없음) → 최종 판정 판단 보류

    @property
    def changed(self) -> bool:
        return self.level != self.rule_level

    @property
    def signed(self) -> bool:
        return any(c.kind in (ABNORMAL_SIGN, VALUE_RANGE) and self.level == "SUSPECTED" for c in self.fired)

    def note(self) -> str:
        if not self.fired:
            return ""
        head = "판단 기준(엔지니어 답): " + " / ".join(self.reasons + self.hold)
        if self.changed:
            head += f" → 판정 {self.rule_level}을(를) {self.level}(으)로 바꿈."
        return head


def numeric_values(events: Iterable[Any], event_type: str, field_name: str) -> list[float]:
    out = []
    for event in events:
        if (event.event_type or event.message) != event_type:
            continue
        for name, value in _NUMBER.findall((event.message or "").splitlines()[0] if event.message else ""):
            if name == field_name:
                out.append(float(value))
    return out


def apply_criteria(strong: frozenset[str], features: frozenset[str], criteria: Sequence[Criterion],
                   events: Sequence[Any], level: str) -> CriteriaDecision:
    """나오면 비정상·값 범위 벗어남 → 비정상 의심. 규칙이 의심한 강한 근거가 모두 '문제없음'(또는 값 범위 안)이면 → 정상.
    사람 확인 기준이 나오면 판단 보류 사유. 승인된 규칙 위반(RULE_VIOLATION)은 바꾸지 않는다."""
    decision = CriteriaDecision(level, level)
    if not criteria:
        return decision
    benign: set[str] = set()
    holding: set[str] = set()
    signs: list[str] = []
    for c in criteria:
        if c.kind == VALUE_RANGE and c.event_type and c.field:
            values = numeric_values(events, c.event_type, c.field)
            if not values:
                continue
            outside = [v for v in values if (c.low is not None and v < c.low) or (c.high is not None and v > c.high)]
            decision.fired.append(c)
            if outside:
                worst = max(outside, key=lambda v, c=c: (v - c.high) if c.high is not None and v > c.high else (c.low - v))
                signs.append(f"'{c.field}' 값 {worst:g}이(가) 엔지니어가 정한 정상 범위 밖({c.describe()})")
            else:
                benign.add(c.feature)
            continue
        if c.feature not in features:
            continue
        decision.fired.append(c)
        if c.kind == ABNORMAL_SIGN:
            signs.append(f"{c.describe()}")
        elif c.kind == BENIGN:
            benign.add(c.feature)
        elif c.kind == HOLD:
            holding.add(c.feature)
            decision.hold.append(f"{c.describe()} — 사람이 확인해야 함")
    if level == "RULE_VIOLATION":
        return decision
    decision.hold_decisive = bool(holding) and not signs and strong <= (holding | benign)
    if signs:
        decision.level = "SUSPECTED"
        decision.reasons = signs
    elif level == "SUSPECTED" and strong and strong <= benign:
        decision.level = "NONE_FOUND"
        decision.reasons = ["규칙이 의심한 근거가 모두 엔지니어가 '문제없음'으로 답한 것"]
    elif benign:
        decision.reasons = [f"'문제없음' 기준 {len(benign)}개 해당"]
    return decision


# ---------------------------------------------------------------- 판단 질문 (JQ-01·02)

ANSWERS_DEFAULT = ((ABNORMAL_SIGN, "문제의 표시예요"), (BENIGN, "정상에서도 나와요"), (HOLD, "상황에 따라 달라요"))


@dataclass
class Question:
    feature: str
    kind: str  # LINE / CODE / VALUE / MISSING / ORDER / REPEAT / OTHER
    text: str
    example: str | None = None  # 원문 줄
    line: int | None = None
    log_file_id: int | None = None
    event_type: str | None = None
    field: str | None = None
    value: float | None = None
    answers: tuple[tuple[str, str], ...] = ANSWERS_DEFAULT


def _template(finding: Any, message: str | None) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    """(질문 종류, 질문 글, 답 단추). 뜻을 말하지 않고 '문제인지'만 묻는다. 질문에는 메시지 부분만(원문 줄은 아래에 따로)."""
    detail = finding.detail or {}
    kind = detail.get("kind")
    line = f"'{message[:120]}'" if message else "이 줄"
    if finding.finding_type == "EXPLICIT_ERROR" and detail.get("error_code"):
        return ("CODE", f"오류 코드 {detail['error_code']}가 찍힌 {line}은(는) 실제 문제의 표시인가요?",
                ((ABNORMAL_SIGN, "문제의 표시예요"), (BENIGN, "무해한 코드예요"), (HOLD, "상황에 따라 달라요")))
    if finding.finding_type == "VALUE_OUT_OF_RANGE" and kind in ("value_equal", "value_count"):  # CR-17 숫자 관계
        left, right = detail.get("left") or {}, detail.get("right") or {}
        counts = detail.get("counts") or {}
        if right.get("value") is not None:
            other = f"'{right.get('field')}'({right.get('value'):g})"
        elif counts:
            event_type, n = next(iter(counts.items()))
            other = f"'{event_type.split('|', 1)[-1].split(' ', 1)[0]}' 줄 수({n})"
        else:
            other = "같이 나온 줄 수"
        value = left.get("value")
        mine = f"'{left.get('field')}'({value:g})" if isinstance(value, (int, float)) else f"'{left.get('field')}'"
        return ("RELATION", f"정상 로그에서는 늘 같던 {mine}과(와) {other}이(가) 이 로그에서 다릅니다. 이것은 문제인가요?",
                ((ABNORMAL_SIGN, "문제예요"), (BENIGN, "달라도 돼요"), (HOLD, "상황에 따라 달라요")))
    if finding.finding_type == "VALUE_OUT_OF_RANGE":
        value = detail.get("value")
        return ("VALUE", f"'{detail.get('field')}' 값 {value:g}({line})은(는) 정상 범위를 벗어난 것인가요? 정상 범위를 알면 적어 주세요."
                if isinstance(value, (int, float)) else f"'{detail.get('field')}' 값({line})은(는) 정상 범위를 벗어난 것인가요?",
                ((ABNORMAL_SIGN, "벗어났어요(문제)"), (BENIGN, "정상 범위예요"), (HOLD, "상황에 따라 달라요")))
    if finding.finding_type == "MISSING_EVENT":
        what = detail.get("component") or message or detail.get("event_type") or "이 단계"
        return ("MISSING", f"{what}이(가) 없는 것은 문제인가요?",
                ((ABNORMAL_SIGN, "문제예요"), (BENIGN, "정상에서도 빠질 수 있어요"), (HOLD, "상황에 따라 달라요")))
    if finding.finding_type == "ORDER_VIOLATION":
        return ("ORDER", f"순서가 평소와 다릅니다({finding.summary[:120]}). 이 순서는 문제인가요?",
                ((ABNORMAL_SIGN, "문제예요"), (BENIGN, "바뀌어도 돼요"), (HOLD, "상황에 따라 달라요")))
    if finding.finding_type in ("REPETITION", "LATENCY"):
        return ("REPEAT", f"평소와 다른 반복·간격입니다({finding.summary[:120]}). 이것은 문제인가요?",
                ((ABNORMAL_SIGN, "문제예요"), (BENIGN, "문제없어요"), (HOLD, "상황에 따라 달라요")))
    if finding.finding_type == "EXTRA_EVENT" or kind in ("new_warning",):
        return ("LINE", f"{line}이(가) 나오면 문제인가요?", ANSWERS_DEFAULT)
    return ("OTHER", f"{finding.summary[:140]} — 이것은 문제인가요?", ANSWERS_DEFAULT)


def make_questions(findings: Sequence[Any], strong: frozenset[str], weak: frozenset[str], verdict_state: str,
                   cases: Sequence[Any], criteria: Sequence[Criterion], raw: dict[int, str],
                   messages: dict[int, str] | None = None) -> list[Question]:
    """애매한 분석에서만(판단 보류, 또는 확인 이력 없는 근거가 있음) 최대 3개. 이미 기준이 있거나(켜짐·꺼짐 모두)
    확인 이력이 한쪽으로 뚜렷한(같은 판정 3번 이상) 근거는 묻지 않는다. 순서: 강한 근거 → 약한 근거 중 처음 보는 줄."""
    known = {c.feature for c in criteria}
    history: dict[str, tuple[int, int]] = {}
    for f in strong | weak:
        holding = [case for case in cases if f in case.fp.features]
        history[f] = (sum(case.abnormal for case in holding), sum(not case.abnormal for case in holding))

    def open_question(f: str) -> bool:
        abn, nor = history.get(f, (0, 0))
        decisive = (abn >= DECISIVE_HISTORY and nor == 0) or (nor >= DECISIVE_HISTORY and abn == 0)
        return f not in known and not decisive

    def unseen(f: str) -> bool:
        return history.get(f, (0, 0)) == (0, 0)

    candidates = [f for f in sorted(strong) if open_question(f)]
    if verdict_state == "UNDECIDED":
        candidates += [f for f in sorted(weak) if open_question(f)]
    else:  # 판정이 났어도 처음 보는 줄(이력 없음)은 뜻을 모르므로 묻는다
        candidates += [f for f in sorted(weak) if open_question(f) and unseen(f)
                       and f.startswith(("EXTRA_EVENT", "VALUE_OUT_OF_RANGE/value_equal", "VALUE_OUT_OF_RANGE/value_count"))]
    first: dict[str, Any] = {}
    for finding in findings:
        if finding.layer != "DETERMINISTIC":
            continue
        first.setdefault(feature_text(finding), finding)
    out: list[Question] = []
    for f in candidates:
        finding = first.get(f)
        if finding is None:
            continue
        example = (raw.get(finding.event_id) or "").splitlines()[0][:200] if finding.event_id in raw else None
        message = ((messages or {}).get(finding.event_id) or "").splitlines()[0] if finding.event_id in (messages or {}) else None
        kind, text, answers = _template(finding, message or example)
        detail = finding.detail or {}
        out.append(Question(f, kind, text, example, finding.line_start, finding.log_file_id,
                            detail.get("event_type"), detail.get("field") if kind == "VALUE" else None,
                            detail.get("value") if kind == "VALUE" else None, answers))
        if len(out) >= MAX_QUESTIONS:
            break
    return out
