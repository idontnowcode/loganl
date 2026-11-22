"""CT 유사 세트보다 복잡한 가상 제품군: 여러 챔버가 동시에 도는 기판 처리 장비 흐름의 로그 100개와 정답표 (2026-11-22).

- 실제 장비의 로그 형식·오류 코드의 뜻·동작 사양이 아니다(CLAUDE.md 절대 규칙 2). 특정 제조사 장비를 흉내 내지 않는다.
  '로봇이 로드포트·정렬기·처리 챔버 사이로 기판을 옮기고, 챔버마다 레시피 단계를 돌리며 센서 값을 남긴다'는 일반 개념만 빌렸다.
  이름은 모두 TEST-, 코드는 TEST-E###, 숫자는 가상 값이다.
- CT 유사 세트보다 복잡한 점: 챔버 3개가 **동시에** 돌아 여러 기판의 흐름이 한 로그에 뒤섞인다(스케줄러·로봇이 끼어듦),
  레시피 3종·단계별 설정값이 다른 주기 센서 값, 기판 묶음(10~25장) 1~2개, 한 로그 수천 줄.
- 정상에도 있는 것: 정비로 꺼 둔 챔버(CHAMBER_DISABLED 알림), 로봇 잡기 재시도 경고, 누설 점검, 조작자 일시 정지.
- 비정상 37개. 로봇 이송 오류 TEST-E710은 **같은 메시지**로 네 위치(로드포트에서 집기·챔버에 놓기·챔버에서 집기·로드포트에 놓기)에서 난다.
  그 밖에 한 챔버만 배기 실패(다른 챔버는 계속), 온도가 서서히 오름(코드 없음), RF 아크, 가스 값 멈춤, 기판 하나 빠짐·두 번 처리,
  레시피 단계 순서 뒤바뀜, 인터록 뒤 중단, 챔버 하나가 알림 없이 빠짐, 스케줄러 멈춤, 로그 끊김.
- 파일 이름으로 정상/비정상을 알 수 없다. 답은 정답표.txt(파일 · 구분 · 동작 메모 · 판단 가이드).

실행: uv run python samples/make_cluster_tool_samples.py [--seed 20261123] [--out samples/다중챔버_가상세트] [--total 100]
시험용(학습 뒤 처음 보는 로그): --seed 20261124 --total 50 --out samples/다중챔버_시험세트
"""

from __future__ import annotations

import argparse
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

SEED = "20261123"
OUT = Path(__file__).resolve().parent / "다중챔버_가상세트"
NOTE = "가상 테스트 로그, 실제 장비 형식과 의미 아님"
CHAMBERS = ("A", "B", "C")
RECIPES = {  # 단계: (이름, 시간 초 범위, 설정값)
    "TEST-RCP-1": [("PUMP", (18, 26), {"pressure": 0.05}), ("HEAT", (25, 35), {"temp": 350.0}),
                   ("PROCESS", (60, 110), {"rf": 300.0, "gas": 50.0}), ("PURGE", (12, 18), {"gas": 200.0}),
                   ("VENT", (12, 16), {"pressure": 760.0})],
    "TEST-RCP-2": [("PUMP", (18, 26), {"pressure": 0.05}), ("PROCESS", (40, 70), {"rf": 250.0, "gas": 40.0}),
                   ("PROCESS2", (40, 70), {"rf": 450.0, "gas": 80.0}), ("PURGE", (12, 18), {"gas": 200.0}),
                   ("VENT", (12, 16), {"pressure": 760.0})],
    "TEST-RCP-3": [("PUMP", (18, 26), {"pressure": 0.05}), ("HEAT", (25, 35), {"temp": 420.0}),
                   ("SOAK", (30, 50), {"temp": 420.0}), ("PROCESS", (50, 90), {"rf": 350.0, "gas": 60.0}),
                   ("COOL", (25, 40), {"temp": 120.0}), ("VENT", (12, 16), {"pressure": 760.0})],
}
TEL_EVERY = 3  # 초마다 센서 값
WHERE_KO = {"LP_PICK": "로드포트에서 기판 집기", "CH_PLACE": "챔버에 기판 놓기", "CH_PICK": "처리 끝난 챔버에서 기판 집기",
            "LP_PLACE": "로드포트에 기판 돌려놓기"}


@dataclass
class Fault:
    kind: str
    where: str = ""
    variant: str = ""
    fired: bool = False


@dataclass
class Tool:
    rnd: random.Random
    t0: datetime
    fault: Fault | None
    events: list[tuple[float, int, str]] = field(default_factory=list)  # (초, 순번, 줄)
    seq: int = 0
    disabled: str | None = None
    silent: str | None = None  # 알림 없이 빠진 챔버(비정상)
    abort_at: float | None = None  # 챔버 안 인터록: 이 시각에 로트 중단
    stopped_at: float | None = None  # 이 시각 뒤 로그 없음(끊김)
    moves: int = 0  # 로봇 이송 횟수
    fault_after: int = 0  # 이 이송 횟수가 지나야 고장이 남(기판 몇 장은 정상 처리)
    memo: list[str] = field(default_factory=list)

    def add(self, at: float, level: str, comp: str, thread: str, msg: str) -> None:
        self.seq += 1
        self.events.append((at, self.seq, f"{level:<5}|{comp:<12}|{thread}|{msg}"))

    def hit(self, kind: str, where: str = "") -> bool:
        f = self.fault
        if self.moves < self.fault_after:
            return False
        if f and not f.fired and f.kind == kind and (not where or f.where == where):
            f.fired = True
            return True
        return False

    def lines(self) -> list[str]:
        out = []
        for at, _seq, body in sorted(self.events):
            if self.stopped_at is not None and at > self.stopped_at:
                continue
            stamp = self.t0 + timedelta(seconds=at)
            level, comp, thread, msg = body.split("|", 3)
            out.append(f"{stamp:%Y-%m-%d %H:%M:%S}.{stamp.microsecond // 1000:03d} | {level} | {comp} | {thread} | {msg}")
        return out


class Abort(Exception):
    """로트 중단. logged=True면 LOT_ABORT 줄을 이미 남김."""

    def __init__(self, at: float, logged: bool = False) -> None:
        super().__init__(at)
        self.at = at
        self.logged = logged


def chamber_name(c: str) -> str:
    return f"TEST-CH{c}"


def run_chamber(tool: Tool, c: str, sub: str, start: float, recipe: str, lot_index: int) -> float:
    """기판 하나를 챔버에서 처리. 끝나는 시각을 돌려준다(센서 값은 단계마다 TEL_EVERY초)."""
    r = tool.rnd
    comp, th = chamber_name(c), f"T-CH{c}"
    at = start
    tool.add(at, "INFO", comp, th, f"DOOR state=CLOSED sub={sub}")
    at += r.uniform(0.5, 1.5)
    tool.add(at, "INFO", comp, th, f"RECIPE_START recipe={recipe} sub={sub}")
    steps = list(RECIPES[recipe])
    if tool.fault and tool.fault.kind == "step_swap" and tool.hit("step_swap"):
        names = [s[0] for s in steps]
        i = names.index("PROCESS")
        j = next(k for k in range(len(names)) if names[k] in ("PURGE", "COOL"))
        steps[i], steps[j] = steps[j], steps[i]
        tool.memo.append(f"챔버 {c}의 {sub}에서 {steps[i][0]}가 {steps[j][0]}보다 먼저")
    drift = tool.fault is not None and tool.fault.kind == "temp_drift" and tool.fault.where == c
    stuck = tool.fault is not None and tool.fault.kind == "gas_stuck" and tool.fault.where == c
    if drift:
        tool.fault.fired = True
    if stuck:
        tool.fault.fired = True
    for name, (lo, hi), setpoints in steps:
        dur = r.uniform(lo, hi)
        tool.add(at, "INFO", comp, th, f"STEP_START step={name} sub={sub} " + " ".join(f"{k}_set={v:g}" for k, v in setpoints.items()))
        n = max(1, int(dur // TEL_EVERY))
        for k in range(n):
            at += TEL_EVERY
            vals = []
            if name == "PUMP":
                pressure = max(0.04, 760 * (0.25 ** (k + 1)))
                vals.append(f"pressure={pressure:.3f}")
            elif "pressure" in setpoints:
                vals.append(f"pressure={setpoints['pressure'] * r.uniform(0.98, 1.02):.1f}")
            else:
                vals.append(f"pressure={r.uniform(0.045, 0.055):.3f}")
            temp_set = setpoints.get("temp")
            if temp_set:
                rise = (lot_index * 1.6 + k * 0.05) if drift else 0
                vals.append(f"temp={temp_set + r.uniform(-1.5, 1.5) + rise:.1f}")
            else:
                vals.append(f"temp={r.uniform(24, 28):.1f}")
            if "rf" in setpoints:
                rf = setpoints["rf"]
                ref = r.uniform(2, 6)
                if name.startswith("PROCESS") and k == n // 2 and tool.hit("rf_e730"):
                    tool.add(at, "INFO", comp, th, f"TEL rf_fwd={rf:.0f} rf_ref={rf * 0.4:.0f} sub={sub}")
                    tool.add(at + 0.1, "ERROR", comp, th, f"FAULT code=TEST-E730 {comp} rf arc detected sub={sub} step={name}")
                    if tool.fault.variant == "retry":
                        tool.add(at + 2, "INFO", comp, th, f"STEP_RETRY step={name} sub={sub} retry=1")
                        tool.memo.append(f"챔버 {c} {sub} 처리 중 RF 아크 TEST-E730 → 단계 다시 해서 끝남")
                    else:
                        tool.add(at + 1, "ERROR", comp, th, f"RECIPE_ABORT sub={sub} reason=TEST-E730")
                        tool.memo.append(f"챔버 {c} {sub} 처리 중 RF 아크 TEST-E730 → 그 기판 처리 중단")
                        tool.add(at + 2, "INFO", comp, th, f"DOOR state=OPEN sub={sub}")
                        return at + 3
                vals.append(f"rf_fwd={rf + r.uniform(-3, 3):.0f} rf_ref={ref:.1f}")
            if "gas" in setpoints:
                gas = setpoints["gas"]
                vals.append(f"gas={gas:.1f}" if stuck and name.startswith("PROCESS") else f"gas={gas + r.uniform(-1.2, 1.2):.1f}")
            if name == "PROCESS" and k == 1 and tool.fault and tool.fault.kind == "interlock" and tool.hit("interlock"):
                tool.add(at, "WARN", comp, th, f"INTERLOCK door=OPEN sub={sub}")
                tool.add(at + 0.2, "ERROR", comp, th, f"RECIPE_ABORT sub={sub} reason=TEST-INTERLOCK")
                tool.memo.append(f"챔버 {c} {sub} 처리 중 인터록 경고 뒤 레시피 중단, 로트 중단")
                tool.abort_at = at + 1
                return at + 1
            tool.add(at, "INFO", comp, th, "TEL " + " ".join(vals) + f" sub={sub}")
        tool.add(at, "INFO", comp, th, f"STEP_END step={name} sub={sub}")
        at += r.uniform(0.3, 1.0)
    tool.add(at, "INFO", comp, th, f"RECIPE_END recipe={recipe} sub={sub} result=OK")
    at += r.uniform(0.5, 1.5)
    tool.add(at, "INFO", comp, th, f"DOOR state=OPEN sub={sub}")
    return at


def robot_move(tool: Tool, at: float, src: str, dst: str, sub: str, where_pick: str, where_place: str) -> float:
    r = tool.rnd
    th = "T-ROBOT"
    tool.moves += 1
    tool.add(at, "INFO", "TEST-ROBOT", th, f"MOVE_START src={src} dst={dst} sub={sub}")
    at += r.uniform(2, 4)
    if where_pick and tool.hit("robot_e710", where_pick):
        at = robot_fault(tool, at, src, sub, where_pick)  # 복구 변형만 돌아옴
    if r.random() < 0.04:
        tool.add(at, "WARN", "TEST-ROBOT", th, f"GRIP_RETRY station={src} sub={sub} retry=1")  # 정상에도 가끔
        at += r.uniform(1, 2)
    tool.add(at, "INFO", "TEST-ROBOT", th, f"PICK_DONE station={src} sub={sub} vacuum=OK")
    at += r.uniform(3, 6)
    if where_place and tool.hit("robot_e710", where_place):
        at = robot_fault(tool, at, dst, sub, where_place)
    tool.add(at, "INFO", "TEST-ROBOT", th, f"PLACE_DONE station={dst} sub={sub}")
    return at + r.uniform(0.5, 1.5)


def robot_fault(tool: Tool, at: float, station: str, sub: str, where: str) -> float:
    """TEST-E710. 복구 변형이면 조작자가 푼 뒤 이어서 할 시각을 돌려주고, 아니면 로트 중단."""
    th = "T-ROBOT"
    for k, err in enumerate((0.6, 1.1)[: tool.rnd.randint(1, 2)]):
        tool.add(at + k * 0.3, "WARN", "TEST-ROBOT", th, f"POSITION_WARN station={station} err_mm={err + tool.rnd.uniform(0, 0.3):.1f}")
    tool.add(at + 0.8, "ERROR", "TEST-ROBOT", th, f"FAULT code=TEST-E710 TEST-ROBOT transfer fault station={station} sub={sub}")
    tool.add(at + 1.0, "INFO", "TEST-ROBOT", th, "STOP reason=TEST-E710")
    tool.add(at + 1.2, "INFO", "TEST-SCHED", "T-SCHED", "LOT_PAUSE reason=TEST-E710")
    tool.memo.append(f"로봇 이송 오류 TEST-E710 — {WHERE_KO[where]}({station}, {sub})")
    if tool.fault.variant == "recover":
        tool.add(at + 40, "INFO", "TEST-ROBOT", th, "FAULT_RESET code=TEST-E710 by=TEST-OPERATOR")
        tool.add(at + 41, "INFO", "TEST-SCHED", "T-SCHED", "LOT_RESUME")
        tool.memo.append("조작자가 풀고 같은 이송부터 이어서 진행")
        return at + 42
    raise Abort(at + 2)


def run_lot(tool: Tool, lot: int, start: float, n: int, recipe: str) -> float:
    """기판 묶음 하나. 끝 시각. 이산 사건 흉내: 로봇은 하나, 챔버는 동시에."""
    r = tool.rnd
    at = start
    sched = ("TEST-SCHED", "T-SCHED")
    tool.add(at, "INFO", *sched, f"LOT_START lot=TEST-LOT-{lot:03d} count={n} recipe={recipe} port=LP1")
    chambers = [c for c in CHAMBERS if c not in (tool.disabled, tool.silent)]
    subs = [f"W{lot:02d}{i:02d}" for i in range(1, n + 1)]
    if tool.fault and tool.fault.kind == "skip_sub" and tool.hit("skip_sub"):
        skipped = subs.pop(r.randrange(2, len(subs) - 2))
        tool.memo.append(f"기판 {skipped}가 처리 기록 없이 빠짐(로트 수량 {n}인데 {n - 1}장만 처리)")
    if tool.fault and tool.fault.kind == "double" and tool.hit("double"):
        k = r.randrange(2, len(subs) - 2)
        subs.insert(k + 2, subs[k])
        tool.memo.append(f"기판 {subs[k]}가 두 번 처리됨")
    waiting = list(subs)
    state = {c: "idle" for c in chambers}
    end = {c: 0.0 for c in chambers}
    holding = {c: "" for c in chambers}
    done_count = 0
    deadlock_at = len(subs) // 2 if tool.fault and tool.fault.kind == "deadlock" else -1
    while waiting or any(s != "idle" for s in state.values()):
        if tool.abort_at is not None and at >= tool.abort_at:
            raise Abort(at)
        if done_count == deadlock_at and tool.hit("deadlock"):
            tool.add(at, "INFO", *sched, "DISPATCH_WAIT reason=TEST-RESOURCE")
            tool.add(at + 300, "ERROR", *sched, f"LOT_ABORT lot=TEST-LOT-{lot:03d} code=TEST-E750 reason=TEST-DISPATCH-TIMEOUT")
            tool.memo.append("스케줄러가 기판을 더 보내지 않고 5분 기다리다 TEST-E750으로 로트 중단(로봇·챔버는 멀쩡)")
            raise Abort(at + 301, logged=True)
        for c in chambers:
            if state[c] == "busy" and end[c] <= at:
                state[c] = "done"
        done = [c for c in chambers if state[c] == "done"]
        if done:
            c = min(done, key=end.__getitem__)
            sub = holding[c]
            moved = robot_move(tool, at, chamber_name(c), "TEST-LP1", sub, "CH_PICK", "LP_PLACE")
            state[c] = "idle"
            done_count += 1
            tool.add(moved, "INFO", *sched, f"SUB_DONE sub={sub} chamber={c}")
            at = moved + r.uniform(0.5, 2)
            continue
        idle = [c for c in chambers if state[c] == "idle"]
        if waiting and idle:
            c = idle[0]
            sub = waiting.pop(0)
            tool.add(at, "INFO", *sched, f"DISPATCH sub={sub} chamber={c}")
            at = robot_move(tool, at + 0.2, "TEST-LP1", "TEST-ALIGNER", sub, "LP_PICK", "")
            tool.add(at, "INFO", "TEST-ALIGNER", "T-ALIGN", f"ALIGN_START sub={sub}")
            at += r.uniform(4, 7)
            tool.add(at, "INFO", "TEST-ALIGNER", "T-ALIGN", f"ALIGN_DONE sub={sub} offset_deg={r.uniform(-0.3, 0.3):.2f}")
            at = robot_move(tool, at + 0.3, "TEST-ALIGNER", chamber_name(c), sub, "", "CH_PLACE")
            if len(chambers) > 1 and tool.hit("pump_e720"):
                comp, th = chamber_name(c), f"T-CH{c}"
                tool.add(at, "INFO", comp, th, f"RECIPE_START recipe={recipe} sub={sub}")
                tool.add(at + 0.5, "INFO", comp, th, f"STEP_START step=PUMP sub={sub} pressure_set=0.05")
                p = 760.0
                for k in range(8):
                    p = max(4.0, p * 0.3)
                    tool.add(at + 3 * (k + 1), "INFO", comp, th, f"TEL pressure={p * r.uniform(1.0, 1.3):.3f} temp={r.uniform(24, 28):.1f} sub={sub}")
                tool.add(at + 27, "ERROR", comp, th, f"FAULT code=TEST-E720 {comp} pump down timeout pressure_set=0.05 sub={sub}")
                tool.add(at + 28, "INFO", *sched, f"CHAMBER_OFFLINE chamber={c} reason=TEST-E720")
                tool.add(at + 29, "INFO", *sched, f"REROUTE sub={sub} from={c}")
                tool.memo.append(f"챔버 {c}만 배기가 안 됨(압력이 내려가지 않음) TEST-E720 → 그 챔버를 빼고 나머지로 계속")
                chambers.remove(c)
                del state[c]
                waiting.insert(0, sub)
                at += 30
                continue
            end[c] = run_chamber(tool, c, sub, at, recipe, done_count)
            state[c] = "busy"
            holding[c] = sub
            at += r.uniform(1, 3)
            continue
        if any(s == "busy" for s in state.values()):
            at = max(at + 1, min(end[c] for c in chambers if state[c] == "busy"))
            continue
        break
    tool.add(at, "INFO", *sched, f"LOT_END lot=TEST-LOT-{lot:03d} processed={done_count} result=OK")
    return at


def heartbeat(tool: Tool, until: float) -> None:
    """주기 시스템 값: 메인 펌프·냉각수(30초마다)."""
    t = 5.0
    while t < until:
        tool.add(t, "INFO", "TEST-VAC", "T-SYS", f"STATUS foreline={tool.rnd.uniform(0.08, 0.12):.3f} pump_rpm={tool.rnd.randint(5900, 6100)}")
        tool.add(t + 0.5, "INFO", "TEST-COOLANT", "T-SYS", f"STATUS flow_lpm={tool.rnd.uniform(9.5, 10.5):.1f} temp_c={tool.rnd.uniform(18, 21):.1f}")
        t += 30


def make_log(rnd: random.Random, number: int, kind: str | None, where: str, variant: str,
             ambiguous: bool = False) -> tuple[list[str], str, str]:
    t0 = datetime(2026, 11, 1) + timedelta(days=rnd.randint(0, 19), hours=rnd.randint(6, 20), minutes=rnd.randint(0, 59))
    tool = Tool(rnd, t0, Fault(kind, where, variant) if kind else None)
    if kind in ("robot_e710", "rf_e730", "interlock", "step_swap", "pump_e720"):
        tool.fault_after = rnd.randint(9, 24)  # 기판 3~8장(한 장에 이송 3번)은 정상 처리한 뒤
    tool.add(0.0, "INFO", "TEST-SYS", "T-SYS", f"NOTE {NOTE}")
    tool.add(0.1, "INFO", "TEST-SYS", "T-SYS", f"SESSION_START tool=TEST-TOOL-01 session=TEST-SS-{number:04d}")
    at = 1.0
    if kind == "chamber_silent":
        tool.silent = where  # 알림 없이 빠짐: 점검 줄도, 처리 줄도 없음
        tool.fault.fired = True
        tool.memo.append(f"챔버 {where}가 알림(CHAMBER_DISABLED) 없이 한 줄도 안 나옴, 나머지 챔버로만 처리")
    for comp in ("TEST-ROBOT", "TEST-ALIGNER", "TEST-VAC", "TEST-COOLANT", *(chamber_name(c) for c in CHAMBERS)):
        if comp == chamber_name(tool.silent or "-"):
            continue
        at += rnd.uniform(0.2, 0.6)
        tool.add(at, "INFO", comp, "T-SYS", "SELFCHECK result=OK")
    if kind is None and rnd.random() < 0.15:
        tool.disabled = rnd.choice(CHAMBERS)
        tool.add(at + 0.5, "INFO", "TEST-SCHED", "T-SCHED", f"CHAMBER_DISABLED chamber={tool.disabled} reason=TEST-MAINT")
        tool.memo.append(f"챔버 {tool.disabled} 정비로 꺼 둠")
    if rnd.random() < 0.2:
        tool.add(at + 1, "INFO", "TEST-VAC", "T-SYS", "LEAK_CHECK_START")
        tool.add(at + 31, "INFO", "TEST-VAC", "T-SYS", f"LEAK_CHECK_DONE rate={rnd.uniform(0.1, 0.4):.2f} result=PASS")
        at += 32
    lots = rnd.choice([1, 1, 2])
    if kind in ("robot_e710", "pump_e720", "deadlock", "interlock", "trunc"):
        lots = 1
    recipes = []
    result = "COMPLETED"
    try:
        for lot in range(1, lots + 1):
            # 온도 상승은 온도 설정이 있는 레시피에서만 보인다
            recipe = rnd.choice(["TEST-RCP-1", "TEST-RCP-3"] if kind == "temp_drift" else list(RECIPES))
            recipes.append(recipe)
            n = rnd.randint(10, 25)
            if lot > 1 and rnd.random() < 0.3:
                tool.add(at + 1, "INFO", "TEST-SCHED", "T-SCHED", "OPERATOR_PAUSE by=TEST-OPERATOR")
                at += rnd.randint(60, 240)
                tool.add(at, "INFO", "TEST-SCHED", "T-SCHED", "OPERATOR_RESUME by=TEST-OPERATOR")
            at = run_lot(tool, lot, at + 2, n, recipe)
            if kind == "trunc" and tool.hit("trunc"):
                tool.stopped_at = at * 0.6
                tool.memo.append("로트 처리 중 로그가 끊김(LOT_END·SESSION_END 없음)")
                result = "TRUNC"
                break
    except Abort as stop:
        # 돌던 챔버는 지금 기판을 마저 끝내고, 그 뒤에 로트 중단·세션 끝
        at = max(stop.at, max(e[0] for e in tool.events))
        if not stop.logged:
            tool.add(at + 1, "ERROR", "TEST-SCHED", "T-SCHED", "LOT_ABORT reason=TEST-FAULT")
        at += 1
        result = "ABORTED"
    heartbeat(tool, at)
    if kind == "notice_fk":  # 애매한 세트: 줄 모양은 같고 코드만 다른 INFO 알림(뜻은 엔지니어 판정으로만 앎)
        tool.add(rnd.uniform(60, max(61, at - 10)), "INFO", "TEST-SYS", "T-SYS", "NOTICE flag=TEST-FK unit=2")
        tool.fault.fired = True
        tool.memo.append("INFO 알림 'NOTICE flag=TEST-FK'가 나옴(오류·경고 아님). 가상 판정: 엔지니어가 점검 필요로 본 알림")
    elif ambiguous and kind is None and rnd.random() < 0.12:
        tool.add(rnd.uniform(60, max(61, at - 10)), "INFO", "TEST-SYS", "T-SYS", "NOTICE flag=TEST-FA unit=1")
        tool.memo.append("INFO 알림 'NOTICE flag=TEST-FA'가 나옴(가상 판정: 문제없는 알림, 정비 아님)")
    if result != "TRUNC":
        tool.add(at + 3, "INFO", "TEST-SYS", "T-SYS", f"SESSION_END session=TEST-SS-{number:04d} result={result}")
    if kind and not tool.fault.fired:
        raise RuntimeError(f"고장이 안 들어감: {kind} {where}")
    memo = f"기판 처리 {lots}로트({', '.join(recipes) or '-'}), 챔버 {''.join(c for c in CHAMBERS if c not in (tool.disabled, tool.silent))}"
    memo += (", " + "; ".join(m for m in tool.memo if '정비' in m)) if any('정비' in m for m in tool.memo) else ""
    memo += "".join(f", {m}" for m in tool.memo if "TEST-FA" in m)
    guide = "; ".join(m for m in tool.memo if "정비" not in m and "TEST-FA" not in m) if kind else ""
    if kind in ("temp_drift", "gas_stuck") and not guide:
        guide = {"temp_drift": f"오류 코드 없음. 챔버 {where}의 가열 단계 온도(temp)가 기판마다 조금씩 올라 뒤쪽 기판은 설정보다 크게 높음.",
                 "gas_stuck": f"오류 코드 없음. 챔버 {where}의 처리 단계 가스 값(gas)이 소수점까지 똑같이 멈춰 있음."}[kind]
    return tool.lines(), memo, guide


ABNORMAL = (
    [("robot_e710", w, v) for w in ("LP_PICK", "CH_PLACE", "CH_PICK", "LP_PLACE") for v in ("", "", "recover")]
    + [("pump_e720", "", "")] * 3 + [("temp_drift", c, "") for c in ("A", "B", "C")]
    + [("rf_e730", "", "retry"), ("rf_e730", "", ""), ("rf_e730", "", "")]
    + [("gas_stuck", c, "") for c in ("A", "B", "C")]
    + [("skip_sub", "", "")] * 3 + [("double", "", "")] * 2 + [("step_swap", "", "")] * 2
    + [("interlock", "", "")] * 2 + [("chamber_silent", c, "") for c in ("A", "C")]
    + [("deadlock", "", ""), ("trunc", "", "")]
)
NORMALS = 63


def pick_abnormal(count: int, pool: Sequence[tuple[str, str, str]] = ABNORMAL) -> list[tuple[str, str, str]]:
    """비정상 count개: 고장 종류마다 돌아가며 하나씩(종류가 고루 들어가게). count가 전체 목록 이상이면 전체."""
    if count >= len(pool):
        return list(pool)
    by_kind: dict[str, list[tuple[str, str, str]]] = {}
    for job in pool:
        by_kind.setdefault(job[0], []).append(job)
    out: list[tuple[str, str, str]] = []
    while len(out) < count:
        for jobs in by_kind.values():
            if jobs and len(out) < count:
                out.append(jobs.pop(0))
    return out


AMBIGUOUS = list(ABNORMAL) + [("notice_fk", "", "")] * 7  # 애매한 세트: 오류 아닌 INFO 알림 하나만 다른 비정상


def build(seed: str, out: Path, total: int = 100, ambiguous: bool = False, notices: int | None = None) -> list[tuple[str, str, str, str]]:
    """total개: 정상·비정상 비율은 100개 세트와 같게(63:37). 시험용 세트는 다른 씨앗·total=50으로 만든다.
    ambiguous: 정상의 약 12%에 문제없는 알림(TEST-FA), 비정상에 점검 필요 알림(TEST-FK)만 다른 로그를 섞는다."""
    rnd = random.Random(seed)
    normals = round(total * NORMALS / (NORMALS + len(ABNORMAL)))
    pool = AMBIGUOUS if ambiguous else ABNORMAL
    if ambiguous:  # 알림 비정상이 시험용(50개)에도 고루 들어가게 앞쪽에 둔다
        count = notices if notices is not None else max(2, (total - normals) // 5)
        pool = [("notice_fk", "", "")] * count + [j for j in ABNORMAL]
    picked = pick_abnormal(total - normals, pool)
    if ambiguous and notices is not None:  # 알림 비정상 수를 정확히 맞춘다(나머지 비정상은 고장 종류를 돌아가며)
        others = pick_abnormal(total - normals - notices, list(ABNORMAL))
        picked = [("notice_fk", "", "")] * notices + others
    jobs: list[tuple[str | None, str, str]] = [(None, "", "")] * normals + picked
    rnd.shuffle(jobs)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("MC_*.log"):
        old.unlink()
    rows = []
    for index, (kind, where, variant) in enumerate(jobs, start=1):
        lines, memo, guide = make_log(rnd, index, kind, where, variant, ambiguous)
        name = f"MC_{index:03d}.log"
        (out / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
        rows.append((name, "비정상" if kind else "정상", memo, guide))
    head = ["파일\t구분\t동작 메모(예)\t판단 가이드(예, 비정상만)"]
    (out / "정답표.txt").write_bytes(("\r\n".join(head + ["\t".join(r) for r in rows]) + "\r\n").encode("utf-8-sig"))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", default=SEED)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--total", type=int, default=100, help="로그 수(시험용 세트는 50)")
    parser.add_argument("--ambiguous", action="store_true", help="오류 아닌 INFO 알림으로만 갈리는 애매한 로그를 섞는다(CR-14 시험)")
    parser.add_argument("--notices", type=int, default=None, help="애매한 세트의 점검 필요 알림(FK) 비정상 수(CR-15 시험)")
    args = parser.parse_args()
    rows = build(args.seed, args.out, args.total, args.ambiguous, args.notices)
    abnormal = sum(r[1] == "비정상" for r in rows)
    print(f"{args.out}: 로그 {len(rows)}개 (정상 {len(rows) - abnormal}, 비정상 {abnormal})")


if __name__ == "__main__":
    main()
