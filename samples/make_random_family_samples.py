"""무작위 제품군 로그 세트 — 씨앗마다 처음 보는 동작 구조·어휘·로그 양식·고장으로 (CR-08 DT-05 반복 평가).

- 실제 장비의 로그 형식·오류 코드의 뜻·동작 사양이 아니다(CLAUDE.md 절대 규칙 2).
  이벤트·구성요소 이름은 뜻 없는 TEST-음절 조합, 코드는 TEST-E###.
- 제품군 하나 = 무작위 '설계도': 시작부(2~4단계) + 본체 블록 2~3개(반복·단계·동시 작업·주기 측정·분기 중) + 마무리부.
  값 항목(측정값·정수·늘 같은 상태값·바뀌는 상태값·카운터·작업 번호), 시간 간격, 가끔 나오는 경고·디버그도 무작위.
- 로그 양식도 무작위(앱이 아는 양식과 모르는 양식을 섞음): 시각(ISO·쉼표 밀리초·슬래시·일-월이름-연·붙여 쓴 날짜·
  ctime·미국식 AM/PM·한국어 오전/오후·유닉스 시간·대괄호 ISO·시각만+날짜 머리 줄), 레벨(대문자·소문자·[대괄호]·
  한 글자·<꺾쇠>·세 글자·[한국어]·Windows 낱말·없음), 칸 배치, JSON·logfmt·CSV(무작위 키 이름).
- 비정상은 정상 실행을 만든 뒤 일반 고장을 하나 넣어 만든다(오류 후 중단·끊김·단계 빠짐·재시작·순서 바뀜·공백·값 튐·
  값 오름·값 멈춤·경고 폭주·작업 미완료·작업 단계 겹침·작업 순서·채널 끊김·상태값 바뀜·반복 단계 빠짐 등).
- 제품군 폴더마다 정상 40 + 비정상 10과 정답표.txt, 회차 폴더에 제품군설계.txt(시퀀스 설명).

실행: uv run python samples/make_random_family_samples.py --seed R1 --families 6 --out <폴더>
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
NOTE = "가상 테스트 로그, 실제 장비 형식과 의미 아님"
SYL = "ka ro mi ze tu pa lo ni vo se da ri ku fe jo ha bi no ta we zu mo ga ne pi so re du yo ki".split()
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
GOOD = ["OK", "PASS", "GOOD", "READY", "DONE"]
BAD = ["FAIL", "NG", "BAD", "LOST", "HALT"]
LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")


# ============================================================ 설계도

class Names:
    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.used: set[str] = set()

    def event(self) -> str:
        return self._unique(lambda: "TEST-" + self._syl(2, 3).upper())

    def comp(self) -> str:
        return self._unique(lambda: "TEST-" + self._syl(1, 2).upper() + self.rng.choice(["", "-A", "-1", "-X", "D"]))

    def key(self) -> str:
        return self._unique(lambda: self._syl(1, 2) + self.rng.choice(["", "_n", "_v", "_ms", "_lv", "x"]))

    def _syl(self, lo: int, hi: int) -> str:
        return "".join(self.rng.choice(SYL) for _ in range(self.rng.randint(lo, hi)))

    def _unique(self, make: Callable[[], str]) -> str:
        while True:
            value = make()
            if value not in self.used and len(value) >= 3 and value not in ("code", "err", "error", "msg", "level"):
                self.used.add(value)
                return value


@dataclass
class Field:
    key: str
    kind: str  # gauss · int · const · var · counter · id
    center: float = 0.0
    rel: float = 0.03
    lo: int = 0
    hi: int = 0
    words: tuple[str, ...] = ()
    decimals: int = 1

    def draw(self, rng: random.Random, ctx: dict) -> str:
        if self.kind == "gauss":
            return f"{self.center * (1 + rng.gauss(0, self.rel)):.{self.decimals}f}"
        if self.kind == "int":
            return str(rng.randint(self.lo, self.hi))
        if self.kind == "const":
            return self.words[0]
        if self.kind == "var":
            return rng.choice(self.words)
        if self.kind == "counter":
            return str(ctx.get("counter", 1))
        return str(ctx.get("id", 0))  # id


@dataclass
class Step:
    name: str
    comp: str
    role: str  # 사람에게 보일 역할 이름(시작·초기화·…)
    fields: list[Field] = field(default_factory=list)
    gap: tuple[int, int] = (50, 400)
    level: str = "INFO"


@dataclass
class Ev:
    at: datetime
    level: str
    comp: str
    name: str
    fields: dict[str, str]
    block: str = ""
    txn: str | None = None
    channel: str | None = None


class Run:
    def __init__(self, rng: random.Random, start: datetime) -> None:
        self.rng = rng
        self.now = start
        self.events: list[Ev] = []

    def emit(self, step: Step, ctx: dict | None = None, *, block: str = "", comp: str | None = None,
             gap: int | None = None) -> Ev:
        self.now += timedelta(milliseconds=self.rng.randint(*step.gap) if gap is None else gap,
                              microseconds=self.rng.randint(0, 999))
        values = {f.key: f.draw(self.rng, ctx or {}) for f in step.fields}
        event = Ev(self.now, step.level, comp or step.comp, step.name, values, block)
        self.events.append(event)
        return event


def gauss_field(names: Names, rng: random.Random) -> Field:
    center = rng.choice([rng.uniform(1, 9), rng.uniform(10, 99), rng.uniform(100, 999), rng.uniform(1000, 5000)])
    return Field(names.key(), "gauss", center=center, rel=rng.uniform(0.01, 0.05), decimals=rng.choice([1, 2]))


def int_field(names: Names, rng: random.Random) -> Field:
    lo = rng.randint(0, 50)
    return Field(names.key(), "int", lo=lo, hi=lo + rng.randint(3, 40))


def const_field(names: Names, rng: random.Random) -> Field:
    return Field(names.key(), "const", words=(rng.choice(GOOD),))


def var_field(names: Names, rng: random.Random) -> Field:
    return Field(names.key(), "var", words=tuple(rng.sample(["A", "B", "C", "D", "E"], rng.randint(2, 3))))


@dataclass
class LoopBlock:
    steps: list[Step]
    count: tuple[int, int]
    blip: Step | None
    blip_p: float
    debug: Step | None
    debug_p: float

    def run(self, r: Run) -> None:
        for i in range(1, r.rng.randint(*self.count) + 1):
            for step in self.steps:
                r.emit(step, {"counter": i}, block="loop")
            if self.debug and r.rng.random() < self.debug_p:
                r.emit(self.debug, {"counter": i}, block="loop-debug")
            if self.blip and r.rng.random() < self.blip_p:
                r.emit(self.blip, {"counter": i}, block="blip")

    def describe(self) -> str:
        body = " → ".join(s.role for s in self.steps)
        return f"({body}) {self.count[0]}~{self.count[1]}회 반복"


@dataclass
class Stage:
    enter: Step
    measure: Step
    leave: Step
    samples: tuple[int, int]


@dataclass
class StagesBlock:
    stages: list[Stage]
    hold: Step | None
    hold_p: float

    def run(self, r: Run) -> None:
        for stage in self.stages:
            r.emit(stage.enter, block="stage")
            for _ in range(r.rng.randint(*stage.samples)):
                r.emit(stage.measure, block="stage-measure")
            if self.hold and r.rng.random() < self.hold_p:
                r.emit(self.hold, comp=stage.enter.comp, block="blip")
            r.emit(stage.leave, block="stage")

    def describe(self) -> str:
        return f"단계 {len(self.stages)}개(단계마다 진입 → 측정 → 나감)"


@dataclass
class TxnBlock:
    accept: Step
    work: list[Step]
    id_key: str
    workers: list[str]
    count: tuple[int, int]
    retry: Step | None
    retry_p: float

    def run(self, r: Run) -> None:
        rng = r.rng
        clock = r.now
        busy = {w: clock for w in self.workers}
        planned: list[Ev] = []
        ids = rng.sample(range(1000, 99999), rng.randint(*self.count))
        for txn in ids:
            clock += timedelta(milliseconds=rng.randint(40, 300))
            planned.append(self._ev(rng, self.accept, clock, self.accept.comp, txn))
            worker = min(self.workers, key=busy.__getitem__)
            at = max(clock + timedelta(milliseconds=rng.randint(10, 80)), busy[worker])
            for index, step in enumerate(self.work):
                if index:
                    at += timedelta(milliseconds=rng.randint(*step.gap))
                planned.append(self._ev(rng, step, at, worker, txn))
                if self.retry and index == 0 and rng.random() < self.retry_p:
                    at += timedelta(milliseconds=rng.randint(20, 60))
                    planned.append(self._ev(rng, self.retry, at, worker, txn, level="WARN"))
            busy[worker] = at
        planned.sort(key=lambda e: e.at)
        r.events.extend(planned)
        r.now = planned[-1].at

    @staticmethod
    def _ev(rng: random.Random, step: Step, at: datetime, comp: str, txn: int, level: str | None = None) -> Ev:
        values = {f.key: f.draw(rng, {"id": txn}) for f in step.fields}
        return Ev(at + timedelta(microseconds=rng.randint(0, 999)), level or step.level, comp, step.name, values,
                  "txn", txn=str(txn))

    def describe(self) -> str:
        work = " → ".join(s.role for s in self.work)
        return (f"작업 {self.count[0]}~{self.count[1]}개가 작업자 {len(self.workers)}명에게 섞여 진행"
                f"(작업마다 {self.accept.role} → {work})")


@dataclass
class PeriodicBlock:
    channels: list[tuple[str, float]]
    sample: str
    value_key: str
    decimals: int
    cycles: tuple[int, int]
    period: tuple[int, int]
    report: Step
    report_every: int
    noise: Step | None
    noise_p: float

    def run(self, r: Run) -> None:
        rng = r.rng
        for cycle in range(1, rng.randint(*self.cycles) + 1):
            for index, (comp, center) in enumerate(self.channels):
                step = Step(self.sample, comp, "측정", [], gap=self.period if index == 0 else (3, 30))
                event = r.emit(step, block="periodic")
                event.fields[self.value_key] = f"{center * (1 + rng.gauss(0, 0.02)):.{self.decimals}f}"
                event.channel = comp
            if self.noise and rng.random() < self.noise_p:
                r.emit(self.noise, comp=rng.choice(self.channels)[0], block="blip")
            if cycle % self.report_every == 0:
                r.emit(self.report, {"counter": cycle // self.report_every}, block="report")

    def describe(self) -> str:
        return (f"채널 {len(self.channels)}개를 {self.period[0] // 1000 or 1}초 안팎마다 측정 "
                f"{self.cycles[0]}~{self.cycles[1]}주기, {self.report_every}주기마다 {self.report.role}")


@dataclass
class ChoiceBlock:
    options: list[list[Step]]

    def run(self, r: Run) -> None:
        for step in r.rng.choice(self.options):
            r.emit(step, block="choice")

    def describe(self) -> str:
        return "분기(" + " / ".join(" → ".join(s.role for s in option) for option in self.options) + " 중 하나)"


@dataclass
class Family:
    code: str
    slug: str
    comps: list[str]
    prologue: list[Step]
    blocks: list
    epilogue: list[Step]
    fmt: "LineFormat"
    names: Names
    code_key: str
    fault: str
    warn: str
    kv_style: str

    def normal(self, rng: random.Random, start: datetime) -> list[Ev]:
        r = Run(rng, start)
        r.events.append(Ev(start, "INFO", "TEST-NOTE", NOTE, {}, "note"))
        for step in self.prologue:
            r.emit(step, block="prologue")
        for block in self.blocks:
            block.run(r)
        for step in self.epilogue:
            r.emit(step, block="epilogue")
        return r.events

    def sequence(self) -> str:
        parts = [s.role for s in self.prologue] + [b.describe() for b in self.blocks] + [s.role for s in self.epilogue]
        return "[" + " - ".join(parts) + "]"

    def once_steps(self) -> list[str]:
        names = [s.name for s in self.prologue + self.epilogue]
        for block in self.blocks:
            if isinstance(block, StagesBlock):
                names += [n for st in block.stages for n in (st.enter.name, st.leave.name)]
        return names


def make_family(rng: random.Random, index: int, fmt: "LineFormat") -> Family:
    names = Names(rng)
    comps = [names.comp() for _ in range(rng.randint(2, 4))]

    def step(role: str, fields: list[Field] | None = None, gap=(50, 400), comp: str | None = None) -> Step:
        return Step(names.event(), comp or rng.choice(comps), role, fields or [], gap)

    prologue = [step("시작", [var_field(names, rng)] if rng.random() < 0.5 else [], gap=(500, 1500)),
                step("초기화", [int_field(names, rng)])]
    if rng.random() < 0.7:
        prologue.append(step("자체 점검", [const_field(names, rng)]))
    if rng.random() < 0.6:
        prologue.append(step("준비", [int_field(names, rng)]))
    kinds = rng.sample(["loop", "stages", "txn", "periodic", "choice"], rng.randint(2, 3))
    if "choice" in kinds and kinds[0] == "choice":
        kinds.append(kinds.pop(0))
    blocks: list = []
    for kind in kinds:
        if kind == "loop":
            counter = Field(names.key(), "counter")
            body = [step("동작", [counter, gauss_field(names, rng)])]
            body += [step(rng.choice(["확인", "기록", "이동", "적재"]),
                          rng.sample([counter, gauss_field(names, rng), const_field(names, rng), int_field(names, rng)],
                                     rng.randint(1, 2))) for _ in range(rng.randint(1, 3))]
            if counter not in body[-1].fields:
                body[-1].fields.insert(0, counter)
            blip = Step(names.event(), rng.choice(comps), "가끔 경고", [Field(names.key(), "int", lo=1, hi=1)],
                        (20, 120), "WARN")
            debug = Step(names.event(), rng.choice(comps), "가끔 디버그", [int_field(names, rng)], (5, 60), "DEBUG")
            lo = rng.randint(1, 5)
            blocks.append(LoopBlock(body, (lo, lo + rng.randint(5, 30)), blip, rng.uniform(0.03, 0.1), debug,
                                    rng.uniform(0.1, 0.4)))
        elif kind == "stages":
            stages = []
            for _ in range(rng.randint(3, 6)):
                comp = names.comp()
                comps.append(comp)
                stages.append(Stage(Step(names.event(), comp, "진입", [], (1500, 5000)),
                                    Step(names.event(), comp, "측정", [gauss_field(names, rng)], (2000, 8000)),
                                    Step(names.event(), comp, "나감", [int_field(names, rng)], (800, 3000)),
                                    (2, rng.randint(3, 6))))
            hold = Step(names.event(), stages[0].enter.comp, "가끔 보류 경고", [int_field(names, rng)], (500, 2000), "WARN")
            blocks.append(StagesBlock(stages, hold, rng.uniform(0.05, 0.15)))
        elif kind == "txn":
            id_key = names.key()
            ident = Field(id_key, "id")
            queue = names.comp()
            workers = [names.comp() for _ in range(rng.randint(2, 4))]
            comps.extend([queue, *workers])
            accept = Step(names.event(), queue, "접수", [ident, int_field(names, rng)])
            work = [Step(names.event(), "", "처리 시작", [ident], (10, 80)),
                    Step(names.event(), "", "처리 완료", [ident, int_field(names, rng)], (60, 400))]
            if rng.random() < 0.5:
                work.insert(1, Step(names.event(), "", "중간 처리", [ident], (20, 150)))
            retry = Step(names.event(), "", "가끔 재시도 경고", [ident], (20, 60), "WARN")
            lo = rng.randint(6, 15)
            blocks.append(TxnBlock(accept, work, id_key, workers, (lo, lo + rng.randint(5, 25)), retry,
                                   rng.uniform(0.03, 0.1)))
        elif kind == "periodic":
            channels = [(names.comp(), rng.choice([rng.uniform(2, 9), rng.uniform(20, 90), rng.uniform(200, 900)]))
                        for _ in range(rng.randint(3, 5))]
            comps.extend(c for c, _ in channels)
            report = Step(names.event(), rng.choice(comps), "보고", [Field(names.key(), "counter")], (5, 40))
            noise = Step(names.event(), "", "가끔 잡음 경고", [], (5, 30), "WARN")
            lo = rng.randint(15, 40)
            blocks.append(PeriodicBlock(channels, names.event(), names.key(), rng.choice([1, 2]), (lo, lo + rng.randint(10, 60)),
                                        (rng.choice([500, 900, 1000]), rng.choice([1100, 1500, 2000])), report,
                                        rng.randint(3, 8), noise, rng.uniform(0.02, 0.06)))
        else:  # choice
            blocks.append(ChoiceBlock([[step(f"분기{n + 1}-{i + 1}", [int_field(names, rng)]) for i in range(rng.randint(1, 2))]
                                       for n in range(2)]))
    epilogue = [step("정지", [const_field(names, rng)] if rng.random() < 0.5 else []),
                step("요약", [int_field(names, rng)])]
    return Family(f"R{index}", "", comps, prologue, blocks, epilogue, fmt, names,
                  rng.choice(["code", "err", "error"]), names.event(), names.event(), rng.choice(["eq", "eq", "colon"]))


# ============================================================ 고장 넣기

def shift(events: list[Ev], start: int, delta: timedelta) -> None:
    for event in events[start:]:
        event.at += delta


def middle(rng: random.Random, n: int, lo: float = 0.3, hi: float = 0.7) -> int:
    return max(2, min(n - 2, rng.randint(int(n * lo), int(n * hi))))


def gauss_series(fam: Family, events: list[Ev]) -> dict[tuple[str, str], list[int]]:
    keys = set()
    for block in fam.blocks:
        if isinstance(block, LoopBlock):
            keys |= {(s.name, f.key) for s in block.steps for f in s.fields if f.kind == "gauss"}
        elif isinstance(block, StagesBlock):
            keys |= {(st.measure.name, f.key) for st in block.stages for f in st.measure.fields}
        elif isinstance(block, PeriodicBlock):
            keys |= {(block.sample, block.value_key)}
    out: dict[tuple[str, str], list[int]] = {}
    for i, e in enumerate(events):
        for name, key in keys:
            if e.name == name and key in e.fields:
                channel = e.channel or ""
                out.setdefault((name + "|" + channel, key), []).append(i)
    return out


def m_error_stop(fam, rng, ev):
    i = middle(rng, len(ev))
    code = f"TEST-E{rng.randint(100, 999)}"
    fault = Ev(ev[i].at + timedelta(milliseconds=7), "ERROR", rng.choice(fam.comps), fam.fault, {fam.code_key: code})
    return ev[:i + 1] + [fault], f"중간에 ERROR {fam.fault}({fam.code_key}={code}) 뒤 기록이 끝남(마무리 없음)."


def m_error_continue(fam, rng, ev):
    i = middle(rng, len(ev))
    code = f"TEST-E{rng.randint(100, 999)}"
    fault = Ev(ev[i].at + timedelta(milliseconds=3), "ERROR", rng.choice(fam.comps), fam.fault, {fam.code_key: code})
    return ev[:i + 1] + [fault] + ev[i + 1:], f"중간에 ERROR {fam.fault}({fam.code_key}={code})가 한 번 나오고 계속 진행."


def m_truncate(fam, rng, ev):
    i = middle(rng, len(ev), 0.4, 0.8)
    return ev[:i], f"{ev[i - 1].name} 뒤로 기록이 끊김(마무리 {fam.epilogue[-1].name} 없음)."


def m_skip_once(fam, rng, ev):
    candidates = [n for n in fam.once_steps()[1:] if any(e.name == n for e in ev)]
    name = rng.choice(candidates)
    return [e for e in ev if e.name != name], f"늘 한 번 나오던 {name}가 빠짐."


def m_restart(fam, rng, ev):
    pro = [e for e in ev if e.block == "prologue"]
    end = ev.index(pro[-1]) + 1
    copies: list[Ev] = []
    t = pro[-1].at
    for _ in range(2):
        for e in pro:
            t += timedelta(milliseconds=rng.randint(200, 900))
            copies.append(Ev(t, e.level, e.comp, e.name, dict(e.fields), "prologue"))
    rest = ev[end:]
    shift(rest, 0, t - pro[-1].at + timedelta(milliseconds=300))
    return ev[:end] + copies + rest, f"시작부({pro[0].name} 등)가 3번 반복된 뒤 진행(재시작 반복)."


def m_swap(fam, rng, ev):
    once = [n for n in fam.once_steps() if sum(e.name == n for e in ev) == 1]
    pairs = [(a, b) for a, b in zip(once, once[1:])]
    a, b = rng.choice(pairs)
    ea = next(e for e in ev if e.name == a)
    eb = next(e for e in ev if e.name == b)
    ea.at, eb.at = eb.at, ea.at
    return sorted(ev, key=lambda e: e.at), f"늘 {a} 다음이던 {b}가 먼저 나옴."


def m_gap(fam, rng, ev):
    i = middle(rng, len(ev))
    seconds = rng.randint(60, 180)
    shift(ev, i, timedelta(seconds=seconds))
    return ev, f"{ev[i - 1].name}와 {ev[i].name} 사이가 약 {seconds}초 비어 있음."


def m_spike(fam, rng, ev):
    series = {k: v for k, v in gauss_series(fam, ev).items() if len(v) >= 3}
    key = rng.choice(sorted(series))
    i = series[key][len(series[key]) // 2]
    old = float(ev[i].fields[key[1]])
    factor = rng.choice([8, 12, 20, -4])
    ev[i].fields[key[1]] = f"{old * factor:.1f}"
    return ev, f"{key[0].split('|')[0]}의 {key[1]} 값이 한 번 {ev[i].fields[key[1]]}(평소 {old:.1f} 안팎)."


def m_drift(fam, rng, ev):
    series = {k: v for k, v in gauss_series(fam, ev).items() if len(v) >= 8}
    key = rng.choice(sorted(series))
    idx = series[key]
    start = len(idx) * 2 // 5
    for n, i in enumerate(idx[start:], start=1):
        value = float(ev[i].fields[key[1]]) * (1 + 1.8 * n / (len(idx) - start))
        ev[i].fields[key[1]] = f"{value:.1f}"
    return ev, f"{key[0].split('|')[0]}의 {key[1]} 값이 뒤로 갈수록 계속 올라 끝에는 평소의 약 2.8배."


def m_stuck(fam, rng, ev):
    series = {k: v for k, v in gauss_series(fam, ev).items() if len(v) >= 12}
    key = rng.choice(sorted(series))
    idx = series[key]
    start = len(idx) // 3
    value = ev[idx[start]].fields[key[1]]
    for i in idx[start:start + 9]:
        ev[i].fields[key[1]] = value
    return ev, f"{key[0].split('|')[0]}의 {key[1]} 값이 9번 연속 {value}로 똑같음(평소엔 매번 조금씩 다름)."


def m_burst(fam, rng, ev):
    i = middle(rng, len(ev))
    t = ev[i].at
    comp = rng.choice(fam.comps)
    burst = []
    for _ in range(rng.randint(7, 10)):
        t += timedelta(milliseconds=rng.randint(15, 60))
        burst.append(Ev(t, "WARN", comp, fam.warn, {}))
    rest = ev[i + 1:]
    shift(rest, 0, t - ev[i].at + timedelta(milliseconds=50))
    return ev[:i + 1] + burst + rest, f"WARN {fam.warn}가 연달아 {len(burst)}번."


def txn_ids(ev: list[Ev]) -> list[str]:
    seen: list[str] = []
    for e in ev:
        if e.txn and e.txn not in seen:
            seen.append(e.txn)
    return seen


def m_txn_incomplete(fam, rng, ev):
    ids = txn_ids(ev)
    txn = ids[len(ids) // 2]
    last = [e for e in ev if e.txn == txn][-1]
    return [e for e in ev if e is not last], f"작업 {txn}의 마지막 단계 {last.name}가 없음(오류 없음)."


def m_txn_dup(fam, rng, ev):
    ids = txn_ids(ev)
    chosen = set(rng.sample(ids, max(2, len(ids) * 2 // 5)))
    out = []
    for e in ev:
        out.append(e)
        block = next(b for b in fam.blocks if isinstance(b, TxnBlock))
        if e.txn in chosen and e.name == block.work[-1].name:
            out.append(Ev(e.at + timedelta(milliseconds=4), e.level, e.comp, e.name, dict(e.fields), "txn", txn=e.txn))
    return sorted(out, key=lambda e: e.at), f"작업 {len(chosen)}개에서 마지막 단계가 두 번씩 나옴."


def m_txn_order(fam, rng, ev):
    block = next(b for b in fam.blocks if isinstance(b, TxnBlock))
    ids = txn_ids(ev)
    for txn in (ids[len(ids) // 3], ids[2 * len(ids) // 3]):
        first = next(e for e in ev if e.txn == txn and e.name == block.work[0].name)
        last = next(e for e in ev if e.txn == txn and e.name == block.work[-1].name)
        first.at, last.at = last.at, first.at
    return sorted(ev, key=lambda e: e.at), f"작업 2개에서 {block.work[-1].name}가 {block.work[0].name}보다 먼저 나옴."


def m_dropout(fam, rng, ev):
    periodic = next((b for b in fam.blocks if isinstance(b, PeriodicBlock)), None)
    if periodic:
        comp = rng.choice(periodic.channels)[0]
        samples = [i for i, e in enumerate(ev) if e.channel == comp]
        cut = samples[len(samples) // 5]
        return [e for i, e in enumerate(ev) if not (e.channel == comp and i > cut)], f"채널 {comp}의 측정이 앞부분 뒤로 끊김."
    block = next(b for b in fam.blocks if isinstance(b, TxnBlock))
    worker = rng.choice(block.workers)
    gone = {e.txn for e in ev if e.comp == worker and e.txn}  # 작업이 아닌 단계는 지우지 않는다
    return [e for e in ev if not (e.txn and e.txn in gone)], f"작업자 {worker}가 한 번도 안 나옴(그 작업들도 없음)."


def m_const_change(fam, rng, ev):
    consts = [(s.name, f.key) for s in fam.prologue + fam.epilogue for f in s.fields if f.kind == "const"]
    for block in fam.blocks:
        if isinstance(block, LoopBlock):
            consts += [(s.name, f.key) for s in block.steps for f in s.fields if f.kind == "const"]
    name, key = rng.choice(consts)
    hits = [e for e in ev if e.name == name and key in e.fields]
    target = hits[len(hits) // 2]
    target.fields[key] = rng.choice([w for w in BAD])
    return ev, f"{name}의 {key}가 늘 {hits[0].fields.get(key) if hits[0] is not target else '같은 값'}이었는데 한 번 {target.fields[key]}(레벨은 그대로)."


def m_loop_step_missing(fam, rng, ev):
    block = next(b for b in fam.blocks if isinstance(b, LoopBlock))
    step = block.steps[-1]
    hits = [e for e in ev if e.name == step.name and e.block == "loop"]
    target = hits[len(hits) // 2]
    return [e for e in ev if e is not target], f"반복 중 한 번 {step.name}가 빠짐(그 회차만 단계 하나 없음)."


def applicable(fam: Family, sample: list[Ev]) -> list[tuple[str, Callable]]:
    kinds = [("오류 후 중단", m_error_stop), ("오류 한 번", m_error_continue), ("기록 끊김", m_truncate),
             ("재시작 반복", m_restart), ("공백", m_gap), ("경고 폭주", m_burst)]
    once = [n for n in fam.once_steps() if sum(e.name == n for e in sample) == 1]
    if len(once) >= 3:
        kinds += [("단계 빠짐", m_skip_once), ("순서 바뀜", m_swap)]
    series = gauss_series(fam, sample)
    if any(len(v) >= 3 for v in series.values()):
        kinds.append(("값 튐", m_spike))
    if any(len(v) >= 8 for v in series.values()):
        kinds.append(("값 오름", m_drift))
    if any(len(v) >= 12 for v in series.values()):
        kinds.append(("값 멈춤", m_stuck))
    if any(isinstance(b, TxnBlock) for b in fam.blocks):
        kinds += [("작업 미완료", m_txn_incomplete), ("작업 단계 겹침", m_txn_dup), ("작업 순서", m_txn_order),
                  ("작업자 빠짐", m_dropout)]
    elif any(isinstance(b, PeriodicBlock) for b in fam.blocks):
        kinds.append(("채널 끊김", m_dropout))
    consts = [f for s in fam.prologue + fam.epilogue for f in s.fields if f.kind == "const"]
    consts += [f for b in fam.blocks if isinstance(b, LoopBlock) for s in b.steps for f in s.fields if f.kind == "const"]
    if consts:
        kinds.append(("상태값 바뀜", m_const_change))
    if any(isinstance(b, LoopBlock) for b in fam.blocks):
        kinds.append(("반복 단계 빠짐", m_loop_step_missing))
    return kinds


# ============================================================ 로그 양식

TS_STYLES = ["iso_ms", "iso_t_tz", "iso_comma", "slash", "dmy_mon", "compact", "ctime", "us_ampm", "kr_ampm",
             "epoch_s", "epoch_ms", "iso_bracket", "time_only"]
LEVEL_STYLES = ["upper", "lower", "bracket", "letter", "angle", "short", "kr", "win", "none"]
LAYOUTS = ["{ts} {lvl} {comp}: {msg}", "{ts} {lvl} [{comp}] {msg}", "{ts} | {lvl} | {comp} | {msg}",
           "{lvl} {ts} {comp} - {msg}", "{ts} {comp} {lvl} {msg}", "{ts}\t{lvl}\t{comp}\t{msg}"]
LEVEL_TEXT = {
    "upper": dict(zip(LEVELS, ("DEBUG", "INFO", "WARN", "ERROR"))),
    "lower": dict(zip(LEVELS, ("debug", "info", "warn", "error"))),
    "bracket": dict(zip(LEVELS, ("[DEBUG]", "[INFO]", "[WARN]", "[ERROR]"))),
    "letter": dict(zip(LEVELS, ("[D]", "[I]", "[W]", "[E]"))),
    "angle": dict(zip(LEVELS, ("<debug>", "<info>", "<warning>", "<error>"))),
    "short": dict(zip(LEVELS, ("DBG", "INF", "WRN", "ERR"))),
    "kr": dict(zip(LEVELS, ("[디버그]", "[정보]", "[경고]", "[오류]"))),
    "win": dict(zip(LEVELS, ("Verbose", "Information", "Warning", "Error"))),
    "none": dict(zip(LEVELS, ("", "", "", ""))),
}


@dataclass
class LineFormat:
    kind: str  # text · json · logfmt · csv
    ts: str
    level: str
    layout: str = ""
    keys: dict[str, str] = field(default_factory=dict)
    delimiter: str = ","
    fields_inline: bool = True
    encoding: str = "utf-8"
    newline: str = "\n"

    def label(self) -> str:
        if self.kind == "text":
            return f"텍스트 · 시각 {self.ts} · 레벨 {self.level} · 배치 '{self.layout.replace(chr(9), '<탭>')}'"
        extra = "" if self.kind != "csv" else f" · 구분자 '{self.delimiter}'"
        return f"{self.kind} · 시각 {self.ts} · 키 {self.keys}{extra}"


def stamp(style: str, at: datetime) -> str:
    ms = f"{at.microsecond // 1000:03d}"
    if style == "iso_ms":
        return f"{at:%Y-%m-%d %H:%M:%S}.{ms}"
    if style == "iso_t_tz":
        return at.isoformat(timespec="milliseconds")
    if style == "iso_comma":
        return f"{at:%Y-%m-%d %H:%M:%S},{ms}"
    if style == "slash":
        return f"{at:%Y/%m/%d %H:%M:%S}.{ms}"
    if style == "dmy_mon":
        return f"{at.day:02d}-{MONTHS[at.month - 1]}-{at.year} {at:%H:%M:%S}.{ms}"
    if style == "compact":
        return f"{at:%Y%m%d %H%M%S}.{ms}"
    if style == "ctime":
        return f"{WEEKDAYS[at.weekday()]} {MONTHS[at.month - 1]} {at.day:>2} {at:%H:%M:%S} {at.year}"
    if style == "us_ampm":
        return f"{at.month}/{at.day}/{at.year} {at.hour % 12 or 12}:{at:%M:%S} {'AM' if at.hour < 12 else 'PM'}"
    if style == "kr_ampm":
        return f"{at:%Y.%m.%d} {'오전' if at.hour < 12 else '오후'} {at.hour % 12 or 12:02d}:{at:%M:%S}.{ms}"
    if style == "epoch_s":
        return f"{(at - EPOCH).total_seconds():.3f}"
    if style == "epoch_ms":
        return str((at - EPOCH) // timedelta(milliseconds=1))
    if style == "iso_bracket":
        return f"[{at:%Y-%m-%d %H:%M:%S}.{ms}]"
    return f"[{at:%H:%M:%S}.{ms}]"  # time_only (날짜는 머리 줄)


def message(event: Ev, kv_style: str, inline: bool = True) -> str:
    if event.comp == "TEST-NOTE":
        return event.name
    if not inline or not event.fields:
        return event.name
    if kv_style == "colon":
        return event.name + " " + ", ".join(f"{k}: {v}" for k, v in event.fields.items())
    return event.name + " " + " ".join(f"{k}={v}" for k, v in event.fields.items())


def render(fam: Family, events: list[Ev]) -> list[str]:
    fmt = fam.fmt
    if fmt.kind == "text":
        lines: list[str] = []
        day = None
        for e in events:
            if fmt.ts == "time_only" and e.at.date() != day:
                day = e.at.date()
                lines.append(f"---- {day:%Y-%m-%d} ----")
            lvl = LEVEL_TEXT[fmt.level][e.level]
            layout = fmt.layout if lvl else fmt.layout.replace("{lvl} ", "").replace(" | {lvl}", "").replace("\t{lvl}", "")
            lines.append(layout.format(ts=stamp(fmt.ts, e.at), lvl=lvl, comp=e.comp, msg=message(e, fam.kv_style)))
        return lines
    keys = fmt.keys
    if fmt.kind in ("json", "logfmt"):
        lines = []
        for e in events:
            item: dict = {keys["ts"]: stamp(fmt.ts, e.at), keys["level"]: LEVEL_TEXT["lower"][e.level] or "info",
                          keys["comp"]: e.comp, keys["msg"]: message(e, fam.kv_style, fmt.fields_inline)}
            if not fmt.fields_inline:
                item.update({k: (float(v) if _number(v) and "." in v else int(v) if _number(v) else v)
                             for k, v in e.fields.items()})
            if fmt.kind == "json":
                lines.append(json.dumps(item, ensure_ascii=False))
            else:
                lines.append(" ".join(f'{k}="{v}"' if isinstance(v, str) and (" " in v or "," in v or not v) else f"{k}={v}"
                                      for k, v in item.items()))
        return lines
    buffer = io.StringIO()  # csv
    writer = csv.writer(buffer, delimiter=fmt.delimiter, lineterminator="\n")
    writer.writerow([keys["ts"], keys["level"], keys["comp"], keys["msg"]])
    for e in events:
        writer.writerow([stamp(fmt.ts, e.at), LEVEL_TEXT["upper"][e.level], e.comp, message(e, fam.kv_style)])
    return buffer.getvalue().rstrip("\n").split("\n")


def _number(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False


def make_format(rng: random.Random) -> LineFormat:
    kind = rng.choices(["text", "json", "logfmt", "csv"], weights=[6, 2, 1, 1])[0]
    if kind == "text":
        ts = rng.choice(TS_STYLES)
        return LineFormat("text", ts, rng.choice(LEVEL_STYLES), rng.choice(LAYOUTS),
                          encoding=rng.choice(["utf-8", "utf-8", "cp949"]) if ts == "kr_ampm" else "utf-8",
                          newline=rng.choice(["\n", "\r\n"]))
    keys = {"ts": rng.choice(["time", "ts", "@timestamp", "t", "when", "datetime"]),
            "level": rng.choice(["level", "lvl", "sev", "severity", "loglevel"]),
            "comp": rng.choice(["comp", "module", "src", "unit", "logger", "source"]),
            "msg": rng.choice(["msg", "message", "text", "event", "what"])}
    ts = rng.choice(["iso_ms", "iso_t_tz", "epoch_ms", "slash"])
    if kind == "csv":
        return LineFormat("csv", ts, "upper", keys=keys, delimiter=rng.choice([",", ";", "|"]))
    return LineFormat(kind, ts, "lower", keys=keys, fields_inline=rng.random() < 0.5)


# ============================================================ 만들기

def start_time(rng: random.Random) -> datetime:
    day = datetime(2026, 11, 2, tzinfo=KST) + timedelta(days=rng.randint(0, 15))
    return day + timedelta(seconds=rng.randint(7 * 3600, 20 * 3600), microseconds=rng.randint(0, 999_999))


def main() -> None:
    parser = argparse.ArgumentParser(description="무작위 제품군 로그 세트")
    parser.add_argument("--seed", required=True)
    parser.add_argument("--families", type=int, default=6)
    parser.add_argument("--normals", type=int, default=40)
    parser.add_argument("--abnormals", type=int, default=10)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    seed = str(args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    plan_lines = [f"무작위 제품군 세트 · 씨앗 {seed} · 모두 가상(TEST- 이름, 실제 장비 아님)", ""]
    for index in range(1, args.families + 1):
        rng = random.Random(f"{seed}-family-{index}")
        fam = make_family(rng, index, make_format(rng))
        fam.slug = f"{seed}_F{index}"
        folder = args.out / fam.slug
        folder.mkdir(exist_ok=True)
        for old in folder.glob("*"):
            old.unlink()
        sample = fam.normal(random.Random(f"{seed}-{index}-probe"), start_time(rng))
        kinds = applicable(fam, sample)
        chosen = rng.sample(kinds, min(args.abnormals, len(kinds)))
        while len(chosen) < args.abnormals:
            chosen.append(rng.choice(kinds))
        labels: list = ["정상"] * args.normals + chosen
        rng.shuffle(labels)
        ext = ".log" if fam.fmt.kind != "csv" else ".csv.txt"
        answers = []
        for position, label in enumerate(labels, start=1):
            file_rng = random.Random(f"{seed}-{index}-{position}")
            events = fam.normal(file_rng, start_time(file_rng))
            if label == "정상":
                kind, guide = "정상", ""
            else:
                kind, mutate = label
                for attempt in range(5):  # 고장이 들어갈 자리가 없으면 다른 정상 실행으로 다시
                    try:
                        events, guide = mutate(fam, file_rng, events)
                        break
                    except (ValueError, IndexError, StopIteration):
                        events = fam.normal(file_rng, start_time(file_rng))
                else:
                    raise RuntimeError(f"{fam.slug}: {kind} 고장을 넣지 못함")
            name = f"{fam.slug}_{position:03d}{ext}"
            text = fam.fmt.newline.join(render(fam, events)) + fam.fmt.newline
            (folder / name).write_bytes(text.encode(fam.fmt.encoding))
            answers.append((name, "정상" if kind == "정상" else "비정상", kind if kind != "정상" else "", guide))
        key = ["파일\t구분\t고장 종류\t판단 가이드(예, 비정상만)"] + ["\t".join(row) for row in answers]
        (folder / "정답표.txt").write_bytes(("\r\n".join(key) + "\r\n").encode("utf-8-sig"))
        plan_lines += [f"■ {fam.slug}", f"  동작 순서: {fam.sequence()}", f"  로그 양식: {fam.fmt.label()}",
                       f"  비정상 종류: {', '.join(k for k, _m in chosen)}", ""]
        print(f"{fam.slug}: {fam.sequence()}\n    양식: {fam.fmt.label()}")
    (args.out / f"제품군설계_{seed}.txt").write_text("\n".join(plan_lines), encoding="utf-8-sig")


if __name__ == "__main__":
    main()
