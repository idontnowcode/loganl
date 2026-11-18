"""서로 다른 제품군 5개의 가상 로그 세트(제품군마다 정상 40·비정상 10)와 정답표를 만든다 (CR-08 DT-05).

- 실제 장비의 로그 형식·오류 코드의 뜻·동작 사양이 아니다(CLAUDE.md 절대 규칙 2). 이름은 모두 TEST-, 코드는 TEST-E###.
- 제품군마다 로그 양식·구성요소·이벤트 이름·동작 구조·값 항목·고장 방식이 모두 다르다(서로 닮지 않게).
  P1 사이클 반복형(ISO + 레벨) · P2 통신 세션형(JSON Lines) · P3 단계 공정형(탭 내보내기, 초 단위) ·
  P4 주기 측정형(BSD syslog, 연도 없음) · P5 동시 작업 큐형(logfmt, 작업이 섞여 진행)
- 비정상 10개는 제품군마다 종류가 다르고, 오류 코드가 없는 미묘한 고장(작업 하나가 끝나지 않음, 값이 멈춤 등)을 섞었다.
- 파일 이름으로 정상/비정상을 알 수 없다. 답은 제품군 폴더의 정답표.txt에 있다.

실행: uv run python samples/make_product_family_samples.py [--seed 20261118] [--out samples/제품군_세트]
"""

from __future__ import annotations

import argparse
import json
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import make_new_format_samples as base
from make_new_format_samples import NOTE, UTC, Event, Timeline, ms, r_syslog, r_tabbed, split_kv, start_time

SEED = "20261118"
OUT = Path(__file__).resolve().parent / "제품군_세트"
Scenario = Callable[[Timeline, int], tuple[str, str]]  # (메모, 판단 가이드)


# ============================================================ P1 사이클 반복형 (기존 생성기의 장면)

def p1_normal(tl: Timeline, lot_no: int) -> tuple[str, str]:
    mode = tl.rng.choice(["A", "B", "C", "C", "C+"])
    memo = base.normal_run(tl, mode, f"TEST-LOT-{lot_no:04d}", with_blocks=False, force_wait=tl.rng.random() < 0.3)
    return memo, ""


def p1_wrap(kind: Callable) -> Scenario:
    def scenario(tl: Timeline, lot_no: int) -> tuple[str, str]:
        _mode, memo, guide = kind(tl, f"TEST-LOT-{lot_no:04d}", False)
        return memo, guide
    return scenario


P1_ABNORMAL = [p1_wrap(k) for k in (base.ab_error_abort, base.ab_truncated, base.ab_timeout, base.ab_out_of_range,
                                     base.ab_order, base.ab_retry_storm, base.ab_restart_loop, base.ab_missing_ready,
                                     base.ab_crash_dump, base.ab_hidden_info_code)]


def r_iso(events: list[Event], rng: random.Random) -> list[str]:
    lines = []
    for e in events:
        lines.append(f"{e.at:%Y-%m-%d %H:%M:%S}.{ms(e.at)} {e.level} {e.comp}: {e.msg}")
        lines.extend(f"    {x}" for x in e.extra)
    return lines


# ============================================================ P2 통신 세션형

def p2_session(tl: Timeline, *, blocks: int | None = None, hook: Callable | None = None,
               skip_ack: int | None = None, fin: bool = True) -> int:
    """정상 세션. hook(tl, blk, phase)는 'before'/'after' 시점에 끼워 넣을 고장. 반환: 보낸 블록 수."""
    r = tl.rng
    tl.add(0, "INFO", "TEST-NOTE", NOTE)
    tl.add(1000, "INFO", "TEST-LINK", f"TEST-OPEN port={r.randint(5000, 5010)}")
    tl.add(r.randint(20, 80), "INFO", "TEST-PEER", f"TEST-HELLO ver=3 peer={r.randint(1, 8)}")
    tl.add(r.randint(20, 80), "INFO", "TEST-LINK", "TEST-AUTH result=OK")
    blocks = blocks if blocks is not None else r.randint(5, 40)
    total = 0
    for blk in range(1, blocks + 1):
        if hook and hook(tl, blk, "before") == "stop":
            return blk - 1
        length = r.randint(512, 1024)
        total += length
        tl.add(r.randint(5, 30), "INFO", "TEST-LINK", f"TEST-XFER blk={blk} len={length}")
        rtt = r.randint(2, 9)
        if r.random() < 0.05:
            rtt = r.randint(15, 25)
            tl.add(rtt, "WARN", "TEST-PEER", f"TEST-RTT-HIGH rtt_ms={rtt}")
        if blk != skip_ack:
            tl.add(rtt, "INFO", "TEST-PEER", f"TEST-ACK blk={blk} rtt_ms={rtt}")
        if r.random() < 0.08:
            tl.add(r.randint(5, 15), "DEBUG", "TEST-LINK", "TEST-KEEPALIVE")
        if hook and hook(tl, blk, "after") == "stop":
            return blk
    if fin:
        tl.add(r.randint(10, 40), "INFO", "TEST-LINK", f"TEST-FIN blocks={blocks} bytes={total}")
        tl.add(r.randint(5, 20), "INFO", "TEST-LINK", "TEST-CLOSE reason=normal")
    return blocks


def p2_normal(tl: Timeline, _n: int) -> tuple[str, str]:
    return f"세션에서 {p2_session(tl)}블록 전송, 정상 종료", ""


def p2_crc(tl, _n):
    def hook(t, blk, phase):
        if blk == 4 and phase == "after":
            for attempt in range(1, 4):
                t.add(30, "ERROR", "TEST-PEER", f"TEST-CRC blk=4 result=BAD code=TEST-E201")
                t.add(20, "WARN", "TEST-LINK", f"TEST-RESEND blk=4 try={attempt}")
            t.add(20, "ERROR", "TEST-LINK", "TEST-ABORT code=TEST-E201")
            t.add(10, "INFO", "TEST-LINK", "TEST-CLOSE reason=abort")
            return "stop"
    p2_session(tl, blocks=12, hook=hook)
    return "세션 시작, 4번째 블록에서 중단", "4번째 블록에 TEST-CRC result=BAD(TEST-E201)가 3번, 재전송 3번 뒤 TEST-ABORT. TEST-FIN 없음."


def p2_ack_missing(tl, _n):
    p2_session(tl, blocks=20, skip_ack=11)
    return "세션 20블록 전송, 결과는 정상 종료처럼 보임", "11번 블록 TEST-XFER 뒤 TEST-ACK blk=11이 없음(정상은 블록마다 XFER→ACK). 오류 코드·경고 없음."


def p2_handshake_order(tl, _n):
    r = tl.rng
    tl.add(0, "INFO", "TEST-NOTE", NOTE)
    tl.add(1000, "INFO", "TEST-LINK", f"TEST-OPEN port={r.randint(5000, 5010)}")
    tl.add(30, "INFO", "TEST-PEER", "TEST-HELLO ver=3 peer=2")
    for blk in (1, 2):
        tl.add(20, "INFO", "TEST-LINK", f"TEST-XFER blk={blk} len={r.randint(512, 1024)}")
        tl.add(5, "INFO", "TEST-PEER", f"TEST-ACK blk={blk} rtt_ms={r.randint(2, 9)}")
    tl.add(40, "INFO", "TEST-LINK", "TEST-AUTH result=OK")
    for blk in range(3, 9):
        tl.add(20, "INFO", "TEST-LINK", f"TEST-XFER blk={blk} len={r.randint(512, 1024)}")
        tl.add(5, "INFO", "TEST-PEER", f"TEST-ACK blk={blk} rtt_ms={r.randint(2, 9)}")
    tl.add(20, "INFO", "TEST-LINK", "TEST-FIN blocks=8 bytes=6000")
    tl.add(10, "INFO", "TEST-LINK", "TEST-CLOSE reason=normal")
    return "세션 8블록 전송", "TEST-AUTH 전에 TEST-XFER 블록 2개가 먼저 나감(정상은 OPEN→HELLO→AUTH→XFER). 오류 코드 없음."


def p2_stall(tl, _n):
    def hook(t, blk, phase):
        if blk == 9 and phase == "before":
            t.add(60_000, "DEBUG", "TEST-LINK", "TEST-KEEPALIVE")
    p2_session(tl, blocks=15, hook=hook)
    return "세션 15블록 전송, 정상 종료", "8번 블록 뒤 약 60초 동안 전송이 멈춤(정상은 블록 사이 수십 ms). 오류 코드 없음."


def p2_rtt_spike(tl, _n):
    def hook(t, blk, phase):
        if blk == 6 and phase == "after":
            t.add(450, "INFO", "TEST-PEER", "TEST-ACK blk=6 rtt_ms=450")
    p2_session(tl, blocks=14, skip_ack=6, hook=hook)
    return "세션 14블록 전송, 정상 종료", "6번 블록 ACK의 rtt_ms가 450(정상은 2~25). 레벨은 INFO."


def p2_reset(tl, _n):
    def hook(t, blk, phase):
        if blk == 7 and phase == "after":
            t.add(15, "WARN", "TEST-LINK", "TEST-RESET by=peer")
            return "stop"
    p2_session(tl, blocks=20, hook=hook)
    return "세션 시작, 7블록 뒤 끊김", "7번 블록 뒤 TEST-RESET(경고) 다음 기록이 없음. TEST-FIN·TEST-CLOSE 없음."


def p2_dup_ack(tl, _n):
    def hook(t, blk, phase):
        if blk == 5 and phase == "after":
            for _ in range(8):
                t.add(3, "INFO", "TEST-PEER", "TEST-ACK blk=5 rtt_ms=3")
    p2_session(tl, blocks=12, hook=hook)
    return "세션 12블록 전송, 정상 종료", "5번 블록 TEST-ACK가 연달아 9번(정상은 블록마다 1번)."


def p2_auth_fail(tl, _n):
    r = tl.rng
    tl.add(0, "INFO", "TEST-NOTE", NOTE)
    tl.add(1000, "INFO", "TEST-LINK", f"TEST-OPEN port={r.randint(5000, 5010)}")
    tl.add(30, "INFO", "TEST-PEER", "TEST-HELLO ver=3 peer=5")
    tl.add(40, "ERROR", "TEST-LINK", "TEST-AUTH result=FAIL code=TEST-E205")
    tl.add(10, "INFO", "TEST-LINK", "TEST-CLOSE reason=auth")
    return "세션 시작, 인증 단계에서 끝남", "TEST-AUTH result=FAIL(TEST-E205) 뒤 바로 TEST-CLOSE. 블록 전송 없음."


def p2_len_zero(tl, _n):
    r = tl.rng
    tl.add(0, "INFO", "TEST-NOTE", NOTE)
    tl.add(1000, "INFO", "TEST-LINK", f"TEST-OPEN port={r.randint(5000, 5010)}")
    tl.add(30, "INFO", "TEST-PEER", "TEST-HELLO ver=3 peer=1")
    tl.add(30, "INFO", "TEST-LINK", "TEST-AUTH result=OK")
    for blk in range(1, 13):
        length = 0 if 4 <= blk <= 9 else r.randint(512, 1024)
        tl.add(20, "INFO", "TEST-LINK", f"TEST-XFER blk={blk} len={length}")
        tl.add(5, "INFO", "TEST-PEER", f"TEST-ACK blk={blk} rtt_ms={r.randint(2, 9)}")
    tl.add(20, "INFO", "TEST-LINK", "TEST-FIN blocks=12 bytes=4100")
    tl.add(10, "INFO", "TEST-LINK", "TEST-CLOSE reason=normal")
    return "세션 12블록 전송, 정상 종료", "4~9번 블록의 len이 0(정상은 512~1024). 레벨·코드로는 안 보임."


def p2_early_close(tl, _n):
    r = tl.rng
    tl.add(0, "INFO", "TEST-NOTE", NOTE)
    tl.add(1000, "INFO", "TEST-LINK", f"TEST-OPEN port={r.randint(5000, 5010)}")
    tl.add(30, "INFO", "TEST-PEER", "TEST-HELLO ver=3 peer=4")
    tl.add(30, "INFO", "TEST-LINK", "TEST-AUTH result=OK")
    tl.add(2000, "INFO", "TEST-LINK", "TEST-FIN blocks=0 bytes=0")
    tl.add(10, "INFO", "TEST-LINK", "TEST-CLOSE reason=normal")
    return "세션 시작 후 바로 끝남", "인증까지 정상이었지만 블록을 하나도 보내지 않고 TEST-FIN blocks=0으로 끝남."


P2_ABNORMAL = [p2_crc, p2_ack_missing, p2_handshake_order, p2_stall, p2_rtt_spike, p2_reset, p2_dup_ack,
               p2_auth_fail, p2_len_zero, p2_early_close]


def r_json_fields(events: list[Event], rng: random.Random) -> list[str]:
    lines = []
    for e in events:
        head, kv = (e.msg, []) if e.comp == "TEST-NOTE" else split_kv(e.msg)
        item: dict = {"@timestamp": e.at.isoformat(timespec="milliseconds"), "severity": e.level.lower(),
                      "logger": e.comp, "message": head}
        for pair in kv:
            key, value = pair.split("=", 1)
            item[key] = int(value) if value.lstrip("-").isdigit() else value
        lines.append(json.dumps(item, ensure_ascii=False))
    return lines


# ============================================================ P3 단계 공정형

STAGES = 6


def p3_stage(tl: Timeline, stage: int, *, samples: list[float] | None = None, holds: int = 0,
             gap_s: int | None = None, enter: bool = True, exit_: bool = True) -> None:
    r = tl.rng
    comp = f"TEST-ST{stage}"
    if enter:
        tl.add(r.randint(2000, 5000), "INFO", comp, "TEST-ENTER")
    base_level = 10 * stage
    values = samples if samples is not None else [base_level + r.uniform(-1.5, 1.5) for _ in range(r.randint(3, 5))]
    duration = 0
    for value in values:
        step = r.randint(4000, 9000)
        duration += step
        tl.add(step, "INFO", comp, f"TEST-MEASURE lvl={value:.1f}")
    for _ in range(holds):
        tl.add(r.randint(1000, 3000), "WARN", comp, f"TEST-HOLD sec={r.randint(5, 15)}")
    if holds:
        tl.add(r.randint(5000, 15000), "INFO", comp, "TEST-RESUME")
    if gap_s:
        tl.add(gap_s * 1000, "INFO", comp, f"TEST-MEASURE lvl={base_level + r.uniform(-1.5, 1.5):.1f}")
        duration += gap_s * 1000
    if exit_:
        tl.add(r.randint(1000, 3000), "INFO", comp, f"TEST-EXIT dur_s={duration // 1000}")


def p3_run(tl: Timeline, *, overrides: dict[int, dict] | None = None, skip: set[int] = frozenset(),
           stop_after: int | None = None, done: bool = True) -> None:
    r = tl.rng
    tl.add(0, "INFO", "TEST-NOTE", NOTE)
    tl.add(1000, "INFO", "TEST-RECIPE", f"TEST-LOAD id={r.randint(10, 99)}")
    for stage in range(1, STAGES + 1):
        if stage in skip:
            continue
        options = dict((overrides or {}).get(stage, {}))
        if "holds" not in options and r.random() < 0.15:
            options["holds"] = 1
        p3_stage(tl, stage, **options)
        if stop_after == stage:
            return
    if done:
        tl.add(r.randint(2000, 4000), "INFO", "TEST-RECIPE", f"TEST-DONE units={r.randint(1, 4)}")


def p3_normal(tl: Timeline, _n: int) -> tuple[str, str]:
    p3_run(tl)
    return f"레시피 {STAGES}단계 모두 진행, 정상 완료", ""


def p3_skip(tl, _n):
    p3_run(tl, skip={4})
    return "레시피 진행, 완료 기록 있음", "4단계(TEST-ST4)의 ENTER·MEASURE·EXIT가 통째로 없음. 오류 코드 없음."


def p3_rework(tl, _n):
    p3_run(tl, overrides={3: {"holds": 0}})
    # 3단계를 두 번 더 반복(재작업)
    for _ in range(2):
        p3_stage(tl, 3, holds=0)
    tl.add(2000, "INFO", "TEST-RECIPE", "TEST-DONE units=2")
    return "레시피 진행 후 3단계 반복", "TEST-ST3 ENTER~EXIT가 3번 나옴(정상은 단계마다 1번)."


def p3_drift(tl, _n):
    p3_run(tl, overrides={2: {"samples": [20.1, 26.0, 33.4, 41.2]}})
    return "레시피 6단계 진행, 정상 완료", "2단계 lvl이 20.1→41.2로 계속 오름(정상 2단계는 18.5~21.5)."


def p3_overtime(tl, _n):
    p3_run(tl, overrides={5: {"gap_s": 400}})
    return "레시피 6단계 진행, 정상 완료", "5단계 안에서 다음 측정까지 약 400초가 비어 있음(정상 간격은 몇 초)."


def p3_overlap(tl, _n):
    r = tl.rng
    tl.add(0, "INFO", "TEST-NOTE", NOTE)
    tl.add(1000, "INFO", "TEST-RECIPE", f"TEST-LOAD id={r.randint(10, 99)}")
    p3_stage(tl, 1)
    p3_stage(tl, 2, exit_=False)
    tl.add(2000, "INFO", "TEST-ST3", "TEST-ENTER")
    tl.add(1500, "INFO", "TEST-ST2", "TEST-EXIT dur_s=21")
    p3_stage(tl, 3, enter=False)
    for stage in range(4, STAGES + 1):
        p3_stage(tl, stage)
    tl.add(3000, "INFO", "TEST-RECIPE", "TEST-DONE units=2")
    return "레시피 6단계 진행, 완료", "TEST-ST2 EXIT 전에 TEST-ST3 ENTER가 먼저 나옴(정상은 앞 단계가 끝난 뒤 다음 단계 시작)."


def p3_alarm(tl, _n):
    p3_run(tl, stop_after=3)
    tl.add(4000, "INFO", "TEST-ST4", "TEST-ENTER")
    tl.add(6000, "ERROR", "TEST-ST4", "TEST-ALARM code=TEST-E304")
    tl.add(1000, "ERROR", "TEST-ST4", "TEST-ABORT code=TEST-E304")
    tl.add(2000, "INFO", "TEST-RECIPE", "TEST-DONE units=0")
    return "레시피 진행 중 4단계에서 멈춤", "4단계에 TEST-ALARM(TEST-E304)과 TEST-ABORT, 5·6단계 없음, units=0."


def p3_hold_storm(tl, _n):
    p3_run(tl, overrides={3: {"holds": 7}})
    return "레시피 6단계 진행, 정상 완료", "3단계에서 TEST-HOLD 경고가 연달아 7번(정상은 가끔 1번)."


def p3_mismatch(tl, _n):
    r = tl.rng
    tl.add(0, "INFO", "TEST-NOTE", NOTE)
    tl.add(1000, "INFO", "TEST-RECIPE", f"TEST-LOAD id={r.randint(10, 99)}")
    tl.add(800, "WARN", "TEST-RECIPE", "TEST-VERIFY result=MISMATCH code=TEST-E310")
    for stage in range(1, STAGES + 1):
        p3_stage(tl, stage)
    tl.add(2000, "INFO", "TEST-RECIPE", "TEST-DONE units=3")
    return "레시피 6단계 진행, 완료", "시작 직후 TEST-VERIFY result=MISMATCH(TEST-E310) 경고가 있는데 그대로 진행."


def p3_truncated(tl, _n):
    p3_run(tl, stop_after=3)
    tl.add(3000, "INFO", "TEST-ST4", "TEST-ENTER")
    return "레시피 진행 중 기록이 끝남", "TEST-ST4 ENTER 뒤로 기록이 없음. 4~6단계 EXIT와 TEST-DONE 없음."


def p3_stuck(tl, _n):
    p3_run(tl, overrides={1: {"samples": [10.4] * 6}})
    return "레시피 6단계 진행, 정상 완료", "1단계 lvl이 6번 연속 10.4로 똑같음(정상은 측정마다 조금씩 다름)."


P3_ABNORMAL = [p3_skip, p3_rework, p3_drift, p3_overtime, p3_overlap, p3_alarm, p3_hold_storm, p3_mismatch,
               p3_truncated, p3_stuck]


# ============================================================ P4 주기 측정형

CHANNELS = {1: 50.0, 2: 120.0, 3: 7.5, 4: 300.0}


def p4_run(tl: Timeline, *, cycles: int | None = None, value: Callable | None = None,
           dropout: tuple[int, int] | None = None, gap_at: int | None = None, extra: Callable | None = None,
           reports: bool = True, start_errors: int = 0) -> int:
    """value(cycle, ch, normal) → 바꿀 값 또는 None. extra(tl, cycle)는 사이클 끝에 끼워 넣기."""
    r = tl.rng
    tl.add(0, "INFO", "TEST-NOTE", NOTE)
    for attempt in range(start_errors):
        tl.add(1000, "INFO", "TEST-AGG", "TEST-START rate=1")
        tl.add(300, "ERROR", "TEST-AGG", "TEST-INIT code=TEST-E409")
    tl.add(1000, "INFO", "TEST-AGG", "TEST-START rate=1")
    if start_errors:
        tl.add(300, "ERROR", "TEST-AGG", "TEST-INIT code=TEST-E409")
        tl.add(200, "ERROR", "TEST-AGG", "TEST-ABORT code=TEST-E409")
        return 0
    cycles = cycles if cycles is not None else r.randint(30, 120)
    for cycle in range(1, cycles + 1):
        gap = r.randint(950, 1050) if cycle != gap_at else 90_000
        first = True
        for ch, center in CHANNELS.items():
            if dropout and ch == dropout[0] and cycle > dropout[1]:
                continue
            normal = center * (1 + r.uniform(-0.03, 0.03))
            override = value(cycle, ch, normal) if value else None
            tl.add(gap if first else r.randint(5, 30), "INFO", f"TEST-CH{ch}",
                   f"TEST-SAMPLE val={override if override is not None else normal:.1f}")
            first = False
        if r.random() < 0.03:
            tl.add(r.randint(5, 20), "WARN", f"TEST-CH{r.randint(1, 4)}", "TEST-NOISE level=high")
        if extra:
            extra(tl, cycle)
        if reports and cycle % 5 == 0:
            tl.add(r.randint(5, 20), "INFO", "TEST-AGG", f"TEST-REPORT n=5 seq={cycle // 5}")
    if r.random() < 0.05:
        tl.add(100, "DEBUG", "TEST-AGG", "TEST-CALIB result=ok")
    tl.add(500, "INFO", "TEST-AGG", f"TEST-STOP samples={cycles * 4}")
    return cycles


def p4_normal(tl: Timeline, _n: int) -> tuple[str, str]:
    return f"채널 4개 {p4_run(tl)}주기 측정, 정상 종료", ""


def p4_dropout(tl, _n):
    p4_run(tl, cycles=60, dropout=(3, 10))
    return "60주기 측정, 정상 종료", "TEST-CH3 샘플이 10주기 뒤로 끊김(다른 채널은 계속). 오류 코드 없음."


def p4_spike(tl, _n):
    p4_run(tl, cycles=50, value=lambda c, ch, v: 9999.0 if (c, ch) == (23, 2) else None)
    return "50주기 측정, 정상 종료", "23주기 TEST-CH2 val=9999.0(정상 CH2는 116~124)."


def p4_stuck(tl, _n):
    p4_run(tl, cycles=60, value=lambda c, ch, v: 50.0 if ch == 1 and 20 <= c < 45 else None)
    return "60주기 측정, 정상 종료", "TEST-CH1 값이 20~44주기 동안 50.0으로 그대로(정상은 매번 조금씩 다름)."


def p4_gap(tl, _n):
    p4_run(tl, cycles=50, gap_at=30)
    return "50주기 측정, 정상 종료", "30주기 전에 약 90초 동안 샘플이 없음(정상은 1초마다)."


def p4_read_error(tl, _n):
    def extra(t, cycle):
        if cycle in (12, 13, 15, 18, 22):
            t.add(10, "ERROR", "TEST-CH4", "TEST-READ result=FAIL code=TEST-E401")
    p4_run(tl, cycles=40, extra=extra)
    return "40주기 측정, 정상 종료", "TEST-CH4에서 TEST-READ result=FAIL(TEST-E401)이 5번."


def p4_no_report(tl, _n):
    p4_run(tl, cycles=60, reports=False)
    return "60주기 측정, 정상 종료", "TEST-AGG의 TEST-REPORT가 한 번도 없음(정상은 5주기마다)."


def p4_noise(tl, _n):
    def extra(t, cycle):
        if cycle == 25:
            for _ in range(8):
                t.add(15, "WARN", "TEST-CH2", "TEST-NOISE level=high")
    p4_run(tl, cycles=50, extra=extra)
    return "50주기 측정, 정상 종료", "25주기에 TEST-CH2 TEST-NOISE 경고가 연달아 8번(정상은 가끔 1번)."


def p4_drift(tl, _n):
    p4_run(tl, cycles=60, value=lambda c, ch, v: v * (1 + 1.5 * c / 60) if ch == 2 else None)
    return "60주기 측정, 정상 종료", "TEST-CH2 값이 점점 올라 끝에는 300 가까이(정상 CH2는 116~124)."


def p4_restart(tl, _n):
    p4_run(tl, start_errors=3)
    return "측정 시작 못 함", "TEST-START가 4번 반복되고 매번 TEST-INIT(TEST-E409), 샘플 없이 TEST-ABORT."


def p4_zero(tl, _n):
    p4_run(tl, cycles=60, value=lambda c, ch, v: 0.0 if c > 42 else None)
    return "60주기 측정, 정상 종료", "43주기부터 모든 채널 val=0.0(정상 범위 밖)."


P4_ABNORMAL = [p4_dropout, p4_spike, p4_stuck, p4_gap, p4_read_error, p4_no_report, p4_noise, p4_drift,
               p4_restart, p4_zero]


# ============================================================ P5 동시 작업 큐형

WORKERS = 3


def p5_run(tl: Timeline, *, jobs: int | None = None, tweak: Callable | None = None, workers: int = WORKERS,
           stall_after: int | None = None, close: bool = True) -> int:
    """작업들이 섞여 진행되는 로그. tweak(job_index, events)로 작업별 이벤트를 바꿀 수 있다.
    이벤트는 절대 시각으로 만든 뒤 시간순으로 정렬한다."""
    r = tl.rng
    jobs = jobs if jobs is not None else r.randint(10, 40)
    start = tl.now
    planned: list[tuple[datetime, str, str, str]] = [(start, "INFO", "TEST-NOTE", NOTE),
                                                     (start + timedelta(seconds=1), "INFO", "TEST-QUEUE", "TEST-Q-OPEN cap=64")]
    clock = start + timedelta(seconds=1)
    busy_until = [clock] * workers
    waiting: list[datetime] = []
    job_numbers = r.sample(range(4000, 14000), jobs)  # 한 로그 안에서 작업 번호는 겹치지 않는다
    for index in range(jobs):
        clock += timedelta(milliseconds=r.randint(50, 300) + (120_000 if stall_after == index else 0))
        job = job_numbers[index]
        waiting = [t for t in waiting if t > clock]
        depth = len(waiting)
        worker = min(range(workers), key=lambda w: busy_until[w])
        begin = max(clock + timedelta(milliseconds=r.randint(10, 100)), busy_until[worker])
        duration = r.randint(80, 300)
        waiting.append(begin)
        events = [[clock, "INFO", "TEST-QUEUE", f"TEST-JOB-ACCEPT job={job} depth={depth}"],
                  [begin, "INFO", f"TEST-WORKER-{worker + 1}", f"TEST-JOB-START job={job}"]]
        end = begin + timedelta(milliseconds=duration)
        if r.random() < 0.08:
            events.append([begin + timedelta(milliseconds=duration // 2), "WARN", f"TEST-WORKER-{worker + 1}",
                           f"TEST-JOB-RETRY job={job}"])
            end += timedelta(milliseconds=r.randint(50, 150))
        events.append([end, "INFO", f"TEST-WORKER-{worker + 1}", f"TEST-JOB-DONE job={job} ms={int((end - begin).total_seconds() * 1000)}"])
        if tweak:
            events = tweak(index, events, worker + 1) or events
        busy_until[worker] = max(busy_until[worker], max(e[0] for e in events))
        planned.extend(tuple(e) for e in events)
        if r.random() < 0.05:
            planned.append((end + timedelta(milliseconds=5), "DEBUG", "TEST-QUEUE", "TEST-Q-IDLE"))
    last = max(p[0] for p in planned)
    if close:
        planned.append((last + timedelta(milliseconds=200), "INFO", "TEST-QUEUE", f"TEST-Q-CLOSE jobs={jobs}"))
    planned.sort(key=lambda p: p[0])
    for at, level, comp, msg in planned:
        tl.events.append(Event(at, level, comp, msg))
    tl.now = planned[-1][0]
    return jobs


def p5_normal(tl: Timeline, _n: int) -> tuple[str, str]:
    return f"작업 {p5_run(tl)}개 처리(작업자 {WORKERS}명이 섞여 진행), 정상 종료", ""


def p5_never_done(tl, _n):
    p5_run(tl, jobs=25, tweak=lambda i, ev, w: [e for e in ev if "DONE" not in e[3]] if i == 12 else None)
    return "작업 25개 처리, 종료 기록 있음", "13번째 작업이 ACCEPT·START만 있고 TEST-JOB-DONE이 없음. 오류 코드 없음."


def p5_backlog(tl, _n):
    def tweak(i, ev, w):
        if i >= 15:
            ev[0][3] = ev[0][3].rsplit("depth=", 1)[0] + f"depth={10 * (i - 14)}"
        return ev
    p5_run(tl, jobs=22, tweak=tweak)
    return "작업 22개 처리, 정상 종료", "16번째 작업부터 depth가 10, 20, … 70까지 늘어남(정상 depth는 0~6)."


def p5_crash(tl, _n):
    def tweak(i, ev, w):
        if i == 9:
            start_at = ev[1][0]
            return [ev[0], ev[1], [start_at + timedelta(milliseconds=40), "FATAL", f"TEST-WORKER-{w}",
                                   "TEST-WORKER-CRASH code=TEST-E501"]]
        return ev
    p5_run(tl, jobs=20, tweak=tweak)
    return "작업 20개 처리 중 작업자 하나 멈춤", "TEST-WORKER-CRASH(TEST-E501, 치명). 그 작업은 DONE이 없음."


def p5_slow(tl, _n):
    def tweak(i, ev, w):
        if i % 3 == 0:
            done = ev[-1]
            done[0] = ev[1][0] + timedelta(milliseconds=4000)
            done[3] = done[3].rsplit("ms=", 1)[0] + "ms=4000"
        return ev
    p5_run(tl, jobs=21, tweak=tweak)
    return "작업 21개 처리, 정상 종료", "작업 3개 중 1개꼴로 ms=4000(정상은 80~450)."


def p5_stall(tl, _n):
    p5_run(tl, jobs=24, stall_after=12)
    return "작업 24개 처리, 정상 종료", "12번째 작업 뒤 약 120초 동안 아무 기록이 없음."


def p5_retry_storm(tl, _n):
    def tweak(i, ev, w):
        if i == 8:
            begin = ev[1][0]
            retries = [[begin + timedelta(milliseconds=30 * (k + 1)), "WARN", f"TEST-WORKER-{w}",
                        f"TEST-JOB-RETRY job={ev[0][3].split('job=')[1].split()[0]}"] for k in range(10)]
            done = ev[-1]
            done[0] = begin + timedelta(milliseconds=400)
            return [ev[0], ev[1], *retries, done]
        return ev
    p5_run(tl, jobs=20, tweak=tweak)
    return "작업 20개 처리, 정상 종료", "9번째 작업에서 TEST-JOB-RETRY 경고가 연달아 10번(정상은 가끔 1번)."


def p5_worker_missing(tl, _n):
    p5_run(tl, jobs=30, workers=2)
    return "작업 30개 처리, 정상 종료", "TEST-WORKER-3이 한 번도 안 나옴(정상은 작업자 3명이 나눠 처리)."


def p5_dup_done(tl, _n):
    def tweak(i, ev, w):
        if i % 2 == 0:
            done = list(ev[-1])
            done[0] = done[0] + timedelta(milliseconds=7)
            return [*ev, done]
        return ev
    p5_run(tl, jobs=20, tweak=tweak)
    return "작업 20개 처리, 정상 종료", "작업 절반에서 TEST-JOB-DONE이 두 번씩 나옴(정상은 작업마다 1번)."


def p5_reject(tl, _n):
    def tweak(i, ev, w):
        if i in (4, 7, 11, 15):
            job = ev[0][3].split("job=")[1].split()[0]
            return [ev[0], [ev[0][0] + timedelta(milliseconds=5), "WARN", "TEST-QUEUE",
                            f"TEST-JOB-REJECT job={job} code=TEST-E507"]]
        return ev
    p5_run(tl, jobs=20, tweak=tweak)
    return "작업 20개 중 일부 거절", "TEST-JOB-REJECT(TEST-E507) 경고 4번, 그 작업은 시작·완료 없음."


def p5_done_first(tl, _n):
    def tweak(i, ev, w):
        if i in (6, 13):
            start, done = ev[1], ev[-1]
            start[0], done[0] = done[0] + timedelta(milliseconds=5), start[0] + timedelta(milliseconds=5)
        return ev
    p5_run(tl, jobs=20, tweak=tweak)
    return "작업 20개 처리, 정상 종료", "두 작업에서 TEST-JOB-DONE이 TEST-JOB-START보다 먼저 나옴."


P5_ABNORMAL = [p5_never_done, p5_backlog, p5_crash, p5_slow, p5_stall, p5_retry_storm, p5_worker_missing,
               p5_dup_done, p5_reject, p5_done_first]


def r_logfmt_service(events: list[Event], rng: random.Random) -> list[str]:
    lines = []
    for e in events:
        head, kv = (e.msg, []) if e.comp == "TEST-NOTE" else split_kv(e.msg)
        at = e.at.astimezone(UTC)
        line = f'ts={at:%Y-%m-%dT%H:%M:%S}.{ms(at)}Z level={e.level.lower()} service={e.comp} msg="{head}"'
        lines.append(" ".join([line, *kv]))
    return lines


# ============================================================ 공통

@dataclass
class Family:
    code: str
    slug: str
    title: str
    ext: str
    renderer: Callable[[list[Event], random.Random], list[str]]
    normal: Scenario
    abnormal: list[Scenario]
    encoding: str = "utf-8"
    newline: str = "\n"


FAMILIES = [
    Family("P1", "사이클반복형", "사이클 반복형 · ISO 시각 + 레벨", ".log", r_iso, p1_normal, P1_ABNORMAL),
    Family("P2", "통신세션형", "통신 세션형 · JSON Lines(@timestamp/severity/logger/message + 값 키)", ".log",
           r_json_fields, p2_normal, P2_ABNORMAL),
    Family("P3", "단계공정형", "단계 공정형 · 탭 내보내기(미국식 날짜 AM/PM, 초 단위)", ".txt", r_tabbed, p3_normal,
           P3_ABNORMAL, encoding="utf-8-sig", newline="\r\n"),
    Family("P4", "주기측정형", "주기 측정형 · BSD syslog(연도 없음, 초 단위)", ".log", r_syslog, p4_normal, P4_ABNORMAL),
    Family("P5", "작업큐형", "동시 작업 큐형 · logfmt(작업이 섞여 진행)", ".log", r_logfmt_service, p5_normal,
           P5_ABNORMAL),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="서로 다른 제품군 5개의 가상 로그 세트")
    parser.add_argument("--seed", default=SEED)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--normals", type=int, default=40)
    args = parser.parse_args()
    seed = str(args.seed)
    for family in FAMILIES:
        folder = args.out / f"{family.code}_{family.slug}"
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob(f"{family.code}_*"):
            old.unlink()
        rng = random.Random(f"{seed}-{family.code}")
        total = args.normals + len(family.abnormal)
        labels = ["정상"] * args.normals + list(range(len(family.abnormal)))
        rng.shuffle(labels)
        answers = []
        for position, label in enumerate(labels, start=1):
            file_rng = random.Random(f"{seed}-{family.code}-{position}")
            tl = Timeline(start_time(file_rng), file_rng)
            if label == "정상":
                memo, guide = family.normal(tl, position)
            else:
                memo, guide = family.abnormal[label](tl, position)
            name = f"{family.code}_{position:03d}{family.ext}"
            text = family.newline.join(family.renderer(tl.events, file_rng)) + family.newline
            (folder / name).write_bytes(text.encode(family.encoding))
            answers.append((name, "정상" if label == "정상" else "비정상", memo, guide))
        key = ["파일\t구분\t동작 메모(예)\t판단 가이드(예, 비정상만)"] + ["\t".join(row) for row in answers]
        (folder / "정답표.txt").write_bytes(("\r\n".join(key) + "\r\n").encode("utf-8-sig"))
        print(f"{family.code} {family.title}: {total}개(정상 {args.normals}·비정상 {len(family.abnormal)}) → {folder}")


if __name__ == "__main__":
    main()
