"""새 형식 시험용 가상 로그 50개(정상 40·비정상 10)와 정답표를 만든다.

- 실제 장비의 로그 형식·오류 코드의 뜻·동작 사양이 아니다(CLAUDE.md 절대 규칙 2).
  이벤트 이름은 TEST-, 오류 코드는 TEST-E### 형식이고 뜻이 없다.
- 개발 중 테스트에서 쓰지 않은 줄 형식 10가지 × (정상 4 + 비정상 1).
- 파일 이름으로 정상/비정상을 알 수 없게 비정상 위치를 섞는다. 답은 정답표.txt에 있다.
- 같은 씨앗(SEED)이면 항상 같은 파일이 나온다.

실행: uv run python samples/make_new_format_samples.py
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

SEED = "20261118"
OUT = Path(__file__).resolve().parent / "새형식_시험_50"
KST = timezone(timedelta(hours=9))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
NOTE = "가상 테스트 로그, 실제 장비 형식과 의미 아님"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@dataclass
class Event:
    at: datetime
    level: str  # DEBUG INFO WARN ERROR FATAL
    comp: str
    msg: str
    extra: list[str] = field(default_factory=list)  # 여러 줄 이벤트의 뒷줄


class Timeline:
    def __init__(self, start: datetime, rng: random.Random) -> None:
        self.start = self.now = start
        self.rng = rng
        self.events: list[Event] = []

    def add(self, gap_ms: int, level: str, comp: str, msg: str, extra: list[str] | None = None) -> None:
        self.now += timedelta(milliseconds=gap_ms, microseconds=self.rng.randint(0, 999))
        self.events.append(Event(self.now, level, comp, msg, list(extra or [])))

    def step(self, lo: int = 80, hi: int = 400) -> int:
        return self.rng.randint(lo, hi)

    def duration_ms(self) -> int:
        return int((self.now - self.start).total_seconds() * 1000)


# ---------------------------------------------------------------- 동작 장면(형식과 무관)

def prologue(tl: Timeline, mode: str, lot: str, *, ready: bool = True) -> int:
    r = tl.rng
    tl.add(0, "INFO", "TEST-NOTE", NOTE)
    tl.add(1000, "INFO", "TEST-CTRL", f"TEST-START mode={mode} lot={lot} step=1")
    tl.add(tl.step(), "INFO", "TEST-CTRL", f"TEST-INIT value={r.randint(9, 11)}")
    tl.add(tl.step(), "DEBUG", "TEST-SUB", "TEST-SELFCHECK error_count=0 warn_count=0")
    slot = r.randint(1, 4)
    if ready:
        tl.add(tl.step(), "INFO", "TEST-SUB", f"TEST-READY slot={slot}")
    return slot


def load(tl: Timeline) -> str:
    item = f"TEST-ITEM-{tl.rng.randint(100, 999)}"
    tl.add(tl.step(), "INFO", "TEST-HANDLER", f"TEST-LOAD item={item}")
    tl.add(tl.step(), "INFO", "TEST-HANDLER", f"TEST-ALIGN offset={tl.rng.uniform(-0.05, 0.05):+.3f}")
    return item


def unload(tl: Timeline, item: str) -> None:
    tl.add(tl.step(), "INFO", "TEST-HANDLER", f"TEST-UNLOAD item={item}")


def blocks(cycle: int, value: int, slot: int) -> list[str]:
    return [f"param.cycle = {cycle}", f"param.value = {value}", f"param.slot = {slot}"]


def run_line(tl: Timeline, cycle: int, slot: int, *, with_blocks: bool, value: int | None = None) -> None:
    value = tl.rng.randint(40, 44) if value is None else value
    extra = blocks(cycle, value, slot) if with_blocks and tl.rng.random() < 0.5 else None
    tl.add(tl.step(150, 300), "INFO", "TEST-CTRL", f"TEST-RUN cycle={cycle} value={value}", extra)


def cycle_ok(tl: Timeline, cycle: int, slot: int, *, with_blocks: bool = False, wait: bool = False) -> int:
    """정상 사이클 하나. 가끔(또는 wait=True면) TEST-WAIT를 한 번 하고 이어 간다. 기다린 횟수를 돌려준다."""
    r = tl.rng
    run_line(tl, cycle, slot, with_blocks=with_blocks)
    if r.random() < 0.3:
        tl.add(tl.step(50, 150), "DEBUG", "TEST-SUB", f"TEST-HEARTBEAT seq={r.randint(1000, 9999)}")
    waited = 0
    if wait or r.random() < 0.05:
        tl.add(tl.step(100, 200), "WARN", "TEST-SUB", "TEST-WAIT busy retry=1")
        tl.add(r.randint(300, 800), "INFO", "TEST-SUB", "TEST-WAIT done retry=1")
        waited = 1
    tl.add(tl.step(300, 900), "INFO", "TEST-SUB", f"TEST-CHECK cycle={cycle} result=OK error_count=0")
    return waited


def summary(tl: Timeline, result: str, cycles: int, retry: int) -> None:
    tl.add(tl.step(), "INFO", "TEST-CTRL",
           f"TEST-SUMMARY result={result} cycles={cycles} retry_total={retry} duration_ms={tl.duration_ms()}")


def epilogue(tl: Timeline, cycles: int, retry: int, result: str = "OK") -> None:
    tl.add(tl.step(), "INFO", "TEST-CTRL", f"TEST-STOP step=9 cycles={cycles}")
    summary(tl, result, cycles, retry)


def normal_run(tl: Timeline, mode: str, lot: str, *, with_blocks: bool, force_wait: bool) -> str:
    r = tl.rng
    slot = prologue(tl, mode[0], lot)
    item = load(tl) if mode == "B" else None
    cycles = {"A": 1, "B": r.randint(2, 3), "C": r.randint(5, 12), "C+": r.randint(25, 40)}[mode]
    wait_at = r.randint(1, cycles) if force_wait else None
    waits = sum(cycle_ok(tl, c, slot, with_blocks=with_blocks, wait=(c == wait_at)) for c in range(1, cycles + 1))
    if item:
        unload(tl, item)
    epilogue(tl, cycles, waits)
    memo = f"모드 {mode[0]}로 {cycles}사이클 동작, 정상 종료"
    if waits:
        memo += f". 중간에 TEST-WAIT {waits}번 있었지만 이어서 끝남"
    if tl.start.date() != tl.now.date():
        memo += ". 자정을 넘겨 날짜가 바뀜"
    return memo


# 비정상 장면: (모드, 메모 예, 판단 가이드 예)를 돌려준다. 가이드는 로그에 보이는 것만 적는다.

def ab_error_abort(tl: Timeline, lot: str, with_blocks: bool) -> tuple[str, str, str]:
    slot = prologue(tl, "B", lot)
    load(tl)
    waits = cycle_ok(tl, 1, slot)
    run_line(tl, 2, slot, with_blocks=False)
    tl.add(tl.step(200, 500), "ERROR", "TEST-SUB", "TEST-FAULT cycle=2 code=TEST-E101 value=7")
    tl.add(tl.step(300, 600), "WARN", "TEST-CTRL", "TEST-RETRY count=1")
    tl.add(tl.step(200, 500), "ERROR", "TEST-SUB", "TEST-FAULT cycle=2 code=TEST-E101 value=8")
    tl.add(tl.step(300, 600), "WARN", "TEST-CTRL", "TEST-RETRY count=2")
    tl.add(tl.step(200, 500), "ERROR", "TEST-SUB", "TEST-FAULT cycle=2 code=TEST-E101 value=8")
    tl.add(tl.step(), "ERROR", "TEST-CTRL", "TEST-ABORT reason=retry_limit code=TEST-E101")
    summary(tl, "NG", 1, 2 + waits)
    return ("B", "모드 B로 시작, 2번째 사이클에서 멈춤",
            "2번째 사이클에서 정상 로그에 없는 TEST-E101(오류 레벨 TEST-FAULT)이 3번 나오고, 재시도 2번 뒤 TEST-ABORT로 끝남. "
            "TEST-STOP이 없고 요약이 result=NG.")


def ab_truncated(tl: Timeline, lot: str, with_blocks: bool) -> tuple[str, str, str]:
    slot = prologue(tl, "C", lot)
    for cycle in range(1, 4):
        cycle_ok(tl, cycle, slot)
    run_line(tl, 4, slot, with_blocks=False)
    return ("C", "모드 C 반복 동작 중 기록이 끝남",
            "TEST-RUN cycle=4 뒤로 기록이 끊김. 그 사이클의 TEST-CHECK와 TEST-STOP·TEST-SUMMARY가 없음"
            "(정상 로그는 항상 요약 줄로 끝남).")


def ab_timeout(tl: Timeline, lot: str, with_blocks: bool) -> tuple[str, str, str]:
    slot = prologue(tl, "A", lot)
    run_line(tl, 1, slot, with_blocks=False)
    tl.add(45000 + tl.rng.randint(0, 900), "WARN", "TEST-SUB", "TEST-TIMEOUT cycle=1 wait_ms=45000")
    tl.add(tl.step(), "ERROR", "TEST-SUB", "TEST-CHECK cycle=1 result=NG code=TEST-E203")
    epilogue(tl, 1, 0, "NG")
    return ("A", "모드 A 1사이클 동작",
            "TEST-RUN 다음 줄까지 약 45초가 비어 있음(정상 로그는 1초 안팎). "
            "이어서 TEST-TIMEOUT 경고와 TEST-E203, 요약 result=NG.")


def ab_out_of_range(tl: Timeline, lot: str, with_blocks: bool) -> tuple[str, str, str]:
    slot = prologue(tl, "B", lot)
    item = load(tl)
    waits = cycle_ok(tl, 1, slot)
    run_line(tl, 2, slot, with_blocks=False, value=418)
    tl.add(tl.step(300, 900), "INFO", "TEST-SUB", "TEST-CHECK cycle=2 result=NG error_count=1")
    waits += cycle_ok(tl, 3, slot)
    unload(tl, item)
    epilogue(tl, 3, waits, "NG")
    return ("B", "모드 B로 3사이클 동작",
            "2번째 사이클 TEST-RUN value가 418(정상 로그는 40~44). 같은 사이클 TEST-CHECK가 result=NG인데 "
            "줄 레벨은 INFO라 레벨만 보면 놓침. 요약 result=NG.")


def ab_order(tl: Timeline, lot: str, with_blocks: bool) -> tuple[str, str, str]:
    r = tl.rng
    tl.add(0, "INFO", "TEST-NOTE", NOTE)
    tl.add(1000, "INFO", "TEST-CTRL", f"TEST-START mode=A lot={lot} step=1")
    tl.add(tl.step(), "INFO", "TEST-CTRL", f"TEST-RUN cycle=1 value={r.randint(40, 44)}")
    tl.add(tl.step(), "ERROR", "TEST-SUB", "TEST-SEQ check=FAIL code=TEST-E305")
    tl.add(tl.step(), "INFO", "TEST-CTRL", f"TEST-INIT value={r.randint(9, 11)}")
    tl.add(tl.step(), "DEBUG", "TEST-SUB", "TEST-SELFCHECK error_count=0 warn_count=0")
    slot = r.randint(1, 4)
    tl.add(tl.step(), "INFO", "TEST-SUB", f"TEST-READY slot={slot}")
    epilogue(tl, 1, cycle_ok(tl, 1, slot), "NG")
    return ("A", "모드 A 1사이클 동작",
            "TEST-INIT·TEST-READY보다 TEST-RUN이 먼저 나옴(정상 로그는 시작, 초기화, 준비, 동작 순서). "
            "바로 뒤 오류 레벨 TEST-E305, 요약 result=NG.")


def ab_retry_storm(tl: Timeline, lot: str, with_blocks: bool) -> tuple[str, str, str]:
    r = tl.rng
    slot = prologue(tl, "A", lot)
    run_line(tl, 1, slot, with_blocks=False)
    retries = 9
    for count in range(1, retries + 1):
        tl.add(r.randint(600, 900), "WARN", "TEST-SUB", f"TEST-WAIT busy retry={count}")
    tl.add(r.randint(300, 800), "INFO", "TEST-SUB", f"TEST-WAIT done retry={retries}")
    tl.add(tl.step(300, 900), "INFO", "TEST-SUB", "TEST-CHECK cycle=1 result=OK error_count=0")
    epilogue(tl, 1, retries, "OK")
    return ("A", "모드 A 1사이클 동작, 결과 OK",
            "오류 코드도 없고 결과도 OK지만 한 사이클 안에서 TEST-WAIT 재시도가 retry=9까지 올라감"
            "(정상 로그는 retry=1에서 끝남). 그만큼 동작 시간이 길어짐. 결과 줄만 보면 놓치는 경우.")


def ab_restart_loop(tl: Timeline, lot: str, with_blocks: bool) -> tuple[str, str, str]:
    r = tl.rng
    tl.add(0, "INFO", "TEST-NOTE", NOTE)
    for attempt in range(1, 5):
        tl.add(1000 if attempt == 1 else r.randint(2000, 3000), "INFO", "TEST-CTRL",
               f"TEST-START mode=C lot={lot} step=1")
        tl.add(tl.step(), "INFO", "TEST-CTRL", f"TEST-INIT value={r.randint(9, 11)}")
        tl.add(tl.step(200, 600), "ERROR", "TEST-CTRL", "TEST-INIT failed code=TEST-E412")
        if attempt < 4:
            tl.add(tl.step(), "WARN", "TEST-CTRL", f"TEST-RESTART count={attempt}")
    tl.add(tl.step(), "ERROR", "TEST-CTRL", "TEST-ABORT reason=restart_limit code=TEST-E412")
    summary(tl, "NG", 0, 3)
    return ("C", "모드 C로 시작했지만 동작 못 함",
            "TEST-START가 4번 반복되고 매번 TEST-INIT 뒤 오류 레벨 TEST-E412. TEST-RUN 없이 TEST-ABORT로 끝남.")


def ab_missing_ready(tl: Timeline, lot: str, with_blocks: bool) -> tuple[str, str, str]:
    slot = prologue(tl, "A", lot, ready=False)
    for k in range(1, 5):
        tl.add(5000, "WARN", "TEST-CTRL", f"TEST-WAIT ready slot={slot} elapsed_ms={5000 * k}")
    tl.add(tl.step(), "ERROR", "TEST-CTRL", "TEST-READY missing code=TEST-E507")
    epilogue(tl, 0, 0, "NG")
    return ("A", "모드 A로 시작, 준비 단계에서 멈춤",
            "TEST-SUB의 TEST-READY가 끝까지 안 나옴. TEST-CTRL이 대기 경고(TEST-WAIT ready)를 5초 간격으로 4번 남긴 뒤 "
            "TEST-E507, 사이클 0번으로 요약 result=NG.")


def ab_crash_dump(tl: Timeline, lot: str, with_blocks: bool) -> tuple[str, str, str]:
    slot = prologue(tl, "C", lot)
    for cycle in range(1, 4):
        cycle_ok(tl, cycle, slot, with_blocks=with_blocks)
    run_line(tl, 4, slot, with_blocks=False)
    dump = ["frame 0: TEST-MOD-A+0x1f40", "frame 1: TEST-MOD-B+0x0a12", "frame 2: TEST-MOD-C+0x0003",
            "reg r0=0x00000000 r1=0x0000002a"]
    tl.add(tl.step(200, 500), "FATAL", "TEST-SUB", "TEST-CRASH cycle=4 code=TEST-E618", dump)
    tl.add(1500, "INFO", "TEST-CTRL", "TEST-SHUTDOWN reason=crash")
    return ("C", "모드 C 반복 동작 중 4번째 사이클에서 멈춤",
            "4번째 사이클에서 치명(FATAL) 레벨 TEST-CRASH(TEST-E618)와 여러 줄 덤프가 나오고 TEST-SHUTDOWN. "
            "TEST-STOP·요약 줄이 없음.")


def ab_hidden_info_code(tl: Timeline, lot: str, with_blocks: bool) -> tuple[str, str, str]:
    slot = prologue(tl, "B", lot)
    item = load(tl)
    waits = cycle_ok(tl, 1, slot, with_blocks=with_blocks)
    run_line(tl, 2, slot, with_blocks=with_blocks)
    tl.add(tl.step(200, 500), "INFO", "TEST-SUB", "TEST-STATUS cycle=2 status=FAIL code=TEST-E720")
    tl.add(tl.step(300, 900), "INFO", "TEST-SUB", "TEST-CHECK cycle=2 result=NG error_count=1")
    waits += cycle_ok(tl, 3, slot, with_blocks=with_blocks)
    unload(tl, item)
    epilogue(tl, 3, waits, "NG")
    return ("B", "모드 B로 3사이클 동작",
            "모든 줄 레벨이 INFO라 레벨로는 안 보임. 2번째 사이클에 TEST-STATUS status=FAIL code=TEST-E720과 "
            "TEST-CHECK result=NG가 있고 요약 result=NG.")


# ---------------------------------------------------------------- 줄 형식(렌더러)

def ms(at: datetime) -> str:
    return f"{at.microsecond // 1000:03d}"


def split_kv(msg: str) -> tuple[str, list[str]]:
    """'TEST-RUN cycle=1 value=42' → ('TEST-RUN', ['cycle=1', 'value=42']). 낱말은 앞쪽에 남긴다."""
    tokens = msg.split()
    words = [tokens[0]] + [t for t in tokens[1:] if "=" not in t]
    return " ".join(words), [t for t in tokens[1:] if "=" in t]


def r_jsonl(events: list[Event], rng: random.Random) -> list[str]:
    lines = []
    for seq, e in enumerate(events, 1):
        item = {"ts": e.at.isoformat(timespec="milliseconds"), "level": e.level.lower(), "module": e.comp,
                "msg": e.msg, "seq": seq}
        if e.extra:
            item["detail"] = e.extra
        lines.append(json.dumps(item, ensure_ascii=False))
    return lines


def r_csv(events: list[Event], rng: random.Random) -> list[str]:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["timestamp", "level", "module", "message"])
    for e in events:
        if e.comp == "TEST-NOTE":
            text = e.msg
        else:
            head, kv = split_kv(e.msg)
            text = head + (" " + ", ".join(kv) if kv else "")
        writer.writerow([f"{e.at:%Y-%m-%d %H:%M:%S}.{ms(e.at)}", e.level, e.comp, " / ".join([text, *e.extra])])
    return buffer.getvalue().rstrip("\n").split("\n")


SYSLOG_SEVERITY = {"DEBUG": 7, "INFO": 6, "WARN": 4, "ERROR": 3, "FATAL": 2}


def r_syslog(events: list[Event], rng: random.Random) -> list[str]:
    pids: dict[str, int] = {}
    lines = []
    for e in events:
        pid = pids.setdefault(e.comp, rng.randint(1000, 9999))
        pri = 8 + SYSLOG_SEVERITY[e.level]  # facility 1(user)
        lines.append(f"<{pri}>{MONTHS[e.at.month - 1]} {e.at.day:>2} {e.at:%H:%M:%S} TEST-HOST01 "
                     f"{e.comp}[{pid}]: {' | '.join([e.msg, *e.extra])}")
    return lines


LETTER = {"DEBUG": "D", "INFO": "I", "WARN": "W", "ERROR": "E", "FATAL": "F"}


def r_slash(events: list[Event], rng: random.Random) -> list[str]:
    return [f"{e.at:%Y/%m/%d %H:%M:%S}.{ms(e.at)} [{LETTER[e.level]}] {e.comp} - {' | '.join([e.msg, *e.extra])}"
            for e in events]


KO_LEVEL = {"DEBUG": "디버그", "INFO": "정보", "WARN": "경고", "ERROR": "오류", "FATAL": "치명"}
KO_WORD = {"TEST-START": "시작", "TEST-INIT": "초기화", "TEST-SELFCHECK": "자체 점검", "TEST-READY": "준비 완료",
           "TEST-LOAD": "투입", "TEST-ALIGN": "정렬", "TEST-RUN": "동작", "TEST-HEARTBEAT": "상태 알림",
           "TEST-WAIT": "대기", "TEST-CHECK": "점검", "TEST-UNLOAD": "배출", "TEST-STOP": "정지",
           "TEST-SUMMARY": "요약", "TEST-SEQ": "순서"}


def r_korean(events: list[Event], rng: random.Random) -> list[str]:
    lines = []
    for e in events:
        ampm = "오전" if e.at.hour < 12 else "오후"
        word = KO_WORD.get(e.msg.split(" ", 1)[0])
        text = f"{word}: {e.msg}" if word else e.msg
        lines.append(f"{e.at:%Y.%m.%d} {ampm} {e.at.hour % 12 or 12:02d}:{e.at:%M:%S}.{ms(e.at)} "
                     f"[{KO_LEVEL[e.level]}] {e.comp} > {' | '.join([text, *e.extra])}")
    return lines


def r_logfmt(events: list[Event], rng: random.Random) -> list[str]:
    lines = []
    for e in events:
        head, kv = (e.msg, []) if e.comp == "TEST-NOTE" else split_kv(e.msg)
        at = e.at.astimezone(UTC)
        line = f'time={at:%Y-%m-%dT%H:%M:%S}.{ms(at)}Z level={e.level.lower()} comp={e.comp} msg="{head}"'
        lines.append(" ".join([line, *kv]))
    return lines


SHORT = {"DEBUG": "DBG", "INFO": "INF", "WARN": "WRN", "ERROR": "ERR", "FATAL": "FTL"}


def r_epoch(events: list[Event], rng: random.Random) -> list[str]:
    return [f"{(e.at - EPOCH) // timedelta(milliseconds=1)}|{SHORT[e.level]}|{e.comp}|{' | '.join([e.msg, *e.extra])}"
            for e in events]


def r_sections(events: list[Event], rng: random.Random) -> list[str]:
    lines = [f"==== TEST-NOTE {NOTE} ===="]
    day = None
    for e in events:
        if e.comp == "TEST-NOTE":
            continue
        if e.at.date() != day:
            day = e.at.date()
            lines.append(f"==== 날짜 {day:%Y-%m-%d} ====")
        lines.append(f"[{e.at:%H:%M:%S}.{ms(e.at)}] {LETTER[e.level]} {e.comp:<12} {' | '.join([e.msg, *e.extra])}")
    return lines


WINDOWS_LEVEL = {"DEBUG": "Verbose", "INFO": "Information", "WARN": "Warning", "ERROR": "Error", "FATAL": "Critical"}


def r_tabbed(events: list[Event], rng: random.Random) -> list[str]:
    lines = ["Level\tDate and Time\tSource\tMessage"]
    for e in events:
        ampm = "AM" if e.at.hour < 12 else "PM"
        stamp = f"{e.at.month}/{e.at.day}/{e.at.year} {e.at.hour % 12 or 12}:{e.at:%M:%S} {ampm}"
        lines.append(f"{WINDOWS_LEVEL[e.level]}\t{stamp}\t{e.comp}\t{e.msg}")
        lines.extend(f"    {x}" for x in e.extra)
    return lines


ANGLE = {"DEBUG": "debug", "INFO": "info", "WARN": "warning", "ERROR": "error", "FATAL": "fatal"}


def r_isoangle(events: list[Event], rng: random.Random) -> list[str]:
    lines = []
    for e in events:
        lines.append(f"{e.at.isoformat(timespec='microseconds')} <{ANGLE[e.level]}> {e.comp}: {e.msg}")
        lines.extend(f"    | {x}" for x in e.extra)
    return lines


@dataclass
class Format:
    code: str
    slug: str
    title: str
    ext: str
    renderer: Callable[[list[Event], random.Random], list[str]]
    abnormal: Callable[[Timeline, str, bool], tuple[str, str, str]]
    encoding: str = "utf-8"
    newline: str = "\n"
    with_blocks: bool = False
    midnight: bool = False  # 정상 하나를 자정 직전에 시작해 날짜를 넘긴다


FORMATS = [
    Format("F01", "jsonl", "JSON Lines(한 줄에 JSON 하나, ISO+09:00, 소문자 레벨)", ".log", r_jsonl, ab_error_abort),
    Format("F02", "csv", "CSV(머리 줄, 쉼표 구분, 따옴표로 묶은 메시지)", ".txt", r_csv, ab_truncated),
    Format("F03", "syslog", "BSD syslog(<우선순위>월 일 시각 호스트 구성요소[PID]:, 연도 없음, 초 단위)", ".log",
           r_syslog, ab_timeout),
    Format("F04", "slash", "슬래시 날짜 + 한 글자 레벨([I] [W] [E]) + 구성요소 - 메시지", ".log", r_slash, ab_out_of_range),
    Format("F05", "korean", "한국어 오전/오후 시각 + [정보] [경고] [오류] 레벨(CP949, CRLF)", ".txt", r_korean, ab_order,
           encoding="cp949", newline="\r\n"),
    Format("F06", "logfmt", "logfmt(time= level= comp= msg=\"...\" key=value, UTC Z 시각)", ".log", r_logfmt,
           ab_retry_storm),
    Format("F07", "epoch", "유닉스 시간(밀리초) + 세로줄(|) 구분 + 세 글자 레벨(INF WRN ERR)", ".log", r_epoch,
           ab_restart_loop),
    Format("F08", "sections", "날짜는 구역 머리 줄에만, 줄마다 [시:분:초.밀리초], 자정 넘김 포함", ".txt", r_sections,
           ab_missing_ready, midnight=True),
    Format("F09", "tabbed", "탭 구분 내보내기(미국식 날짜 AM/PM, Information/Warning/Error/Critical, 여러 줄 메시지, "
           "UTF-8 BOM, CRLF)", ".txt", r_tabbed, ab_crash_dump, encoding="utf-8-sig", newline="\r\n",
           with_blocks=True),
    Format("F10", "isoangle", "ISO 마이크로초 +09:00 + <info> 꺾쇠 레벨 + 들여 쓴 여러 줄 블록", ".log", r_isoangle,
           ab_hidden_info_code, with_blocks=True),
]


def start_time(rng: random.Random, *, midnight: bool = False) -> datetime:
    day = datetime(2026, 11, 2, tzinfo=KST) + timedelta(days=rng.randint(0, 15))
    if midnight:
        # 모드 C는 짧아도 6초 넘게 걸리므로 23:59:56 이후에 시작하면 반드시 날짜를 넘긴다
        return day.replace(hour=23, minute=59, second=rng.randint(56, 58), microsecond=rng.randint(0, 999_999))
    return day + timedelta(seconds=rng.randint(8 * 3600, 19 * 3600), microseconds=rng.randint(0, 999_999))


def write_lines(path: Path, lines: list[str], fmt: Format) -> None:
    path.write_bytes((fmt.newline.join(lines) + fmt.newline).encode(fmt.encoding))


def main() -> None:
    parser = argparse.ArgumentParser(description="새 형식 시험용 가상 로그 50개(정상 40·비정상 10)와 정답표")
    parser.add_argument("--seed", default=SEED, help=f"씨앗(기본 {SEED} = samples/새형식_시험_50)")
    parser.add_argument("--out", type=Path, default=OUT, help="만들 폴더")
    parser.add_argument("--shuffle-kinds", action="store_true",
                        help="비정상 종류를 형식에 섞어 배치(같은 생성기에 맞춘 결과인지 확인할 때, CR-08 DT-05)")
    args = parser.parse_args()
    seed, out = str(args.seed), args.out
    kinds = [fmt.abnormal for fmt in FORMATS]
    if args.shuffle_kinds:
        random.Random(f"{seed}-kinds").shuffle(kinds)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("F[0-9][0-9]_*"):
        old.unlink()
    lot_no = 0
    answers: list[tuple[str, str, str, str]] = []
    for fmt, abnormal_kind in zip(FORMATS, kinds):
        rng = random.Random(f"{seed}-{fmt.code}")
        abnormal_at = rng.randrange(5)
        modes = ["A", "B", "C", rng.choice(["A", "B", "C+"])]
        rng.shuffle(modes)
        wait_index = rng.randrange(4)  # 정상 하나는 반드시 TEST-WAIT를 한 번 거친다(정상에도 경고가 있음)
        normal_index = 0
        for position in range(5):
            lot_no += 1
            lot = f"TEST-LOT-{lot_no:04d}"
            file_rng = random.Random(f"{seed}-{fmt.code}-{position}")
            name = f"{fmt.code}_{fmt.slug}_{position + 1:02d}{fmt.ext}"
            if position == abnormal_at:
                tl = Timeline(start_time(file_rng), file_rng)
                _mode, memo, guide = abnormal_kind(tl, lot, fmt.with_blocks)
                answers.append((name, "비정상", memo, guide))
            else:
                mode = modes[normal_index]
                midnight = fmt.midnight and mode == "C"
                tl = Timeline(start_time(file_rng, midnight=midnight), file_rng)
                memo = normal_run(tl, mode, lot, with_blocks=fmt.with_blocks, force_wait=(normal_index == wait_index))
                answers.append((name, "정상", memo, ""))
                normal_index += 1
            write_lines(out / name, fmt.renderer(tl.events, file_rng), fmt)
    key = ["파일\t구분\t동작 메모(예)\t판단 가이드(예, 비정상만)"] + ["\t".join(row) for row in answers]
    (out / "정답표.txt").write_bytes(("\r\n".join(key) + "\r\n").encode("utf-8-sig"))
    print(f"{len(answers)}개 생성: 정상 {sum(a[1] == '정상' for a in answers)} · 비정상 {sum(a[1] == '비정상' for a in answers)}")


if __name__ == "__main__":
    main()
