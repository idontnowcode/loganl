"""CT 장비와 비슷한 흐름의 가상 로그 100개와 정답표를 만든다 (사용자 요청 2026-11-22).

- 실제 장비의 로그 형식·오류 코드의 뜻·동작 사양이 아니다(CLAUDE.md 절대 규칙 2). 특정 제조사 장비를 흉내 내지 않는다.
  일반적인 CT 개념(갠트리 회전, 환자 테이블=베드 구동, X선 발생·노출, 콜리메이터, 디텍터 영상 획득, 재구성, 냉각)을
  역할 이름으로만 빌렸다. 이름은 모두 TEST-, 코드는 TEST-E###, 숫자는 가상 값이다.
- 한 로그 = 검사 한 번 또는 일일 점검 한 번. 촬영 방식(가상): 스카우트 · 축(스텝) · 나선(연속) · 동적(같은 위치 반복).
- 비정상 36개. 베드 구동 오류 TEST-E310은 로그상 **같은 메시지**지만 시퀀스의 다섯 위치(환자 올리기 · 스카우트 이동 ·
  축 촬영 단계 이동 · 나선 촬영 연속 이동 · 원위치 복귀)에서 따로 나고, 위치마다 앞뒤 흐름과 결과가 다르다.
  정상 로그에도 작은 베드 추종 오차 경고·냉각 팬 조정 경고·조작자 일시 정지가 가끔 있다(경고 = 비정상이 아님).
- 오류 코드 없이 값만 어긋나는 고장(베드 위치 오차가 큼), 흐름이 끊긴 로그(전원 끊김)도 섞었다.
- 파일 이름으로 정상/비정상을 알 수 없다. 답은 정답표.txt(파일 · 구분 · 동작 메모 · 판단 가이드)에 있다.

실행: uv run python samples/make_ct_like_samples.py [--seed 20261122] [--out samples/CT유사_가상세트]
"""

from __future__ import annotations

import argparse
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

SEED = "20261122"
OUT = Path(__file__).resolve().parent / "CT유사_가상세트"
NOTE = "가상 테스트 로그, 실제 장비 형식과 의미 아님"
C = {"sys": "TEST-SYS", "gan": "TEST-GANTRY", "bed": "TEST-BED", "hvg": "TEST-HVG", "tube": "TEST-TUBE",
     "col": "TEST-COLL", "det": "TEST-DET", "rec": "TEST-RECON", "cool": "TEST-COOL"}
MODES = {"HELICAL": "TEST-MODE-HELICAL", "AXIAL": "TEST-MODE-AXIAL", "DYNAMIC": "TEST-MODE-DYNAMIC"}
MODE_KO = {"HELICAL": "나선", "AXIAL": "축", "DYNAMIC": "동적"}
WHERE_KO = {"LOAD": "환자 올리기(베드 상하 구동)", "SCOUT": "스카우트 촬영 중 수평 이동", "AXIAL_STEP": "축 촬영 단계 이동 사이",
            "HELICAL": "나선 촬영 연속 이동 중(X선 노출 중)", "RETURN": "촬영 뒤 원위치 복귀"}


class Stop(Exception):
    """검사가 여기서 끝났다(고장으로 중단·로그 끊김)."""


@dataclass
class Fault:
    kind: str
    where: str = ""
    variant: str = ""
    fired: bool = False


@dataclass
class Exam:
    rnd: random.Random
    t: datetime
    fault: Fault | None = None
    lines: list[str] = field(default_factory=list)
    bed_h: float = 0.0
    bed_v: float = 650.0
    heat: float = 12.0
    spinning: bool = False
    series: int = 0
    done: list[str] = field(default_factory=list)  # 메모용: 마친 촬영

    # ── 기록 ──
    def add(self, level: str, comp: str, msg: str, gap: tuple[int, int] = (6, 45)) -> None:
        self.t += timedelta(milliseconds=self.rnd.randint(*gap))
        self.lines.append(f"{self.t:%Y-%m-%d %H:%M:%S}.{self.t.microsecond // 1000:03d} {level:<5} [{C[comp]}] {msg}")

    def wait(self, ms: int) -> None:
        self.t += timedelta(milliseconds=ms)

    def hit(self, kind: str, where: str = "") -> bool:
        f = self.fault
        if f and not f.fired and f.kind == kind and (not where or f.where == where):
            f.fired = True
            return True
        return False

    def cool_status(self) -> None:
        self.add("INFO", "cool", f"STATUS temp_c={self.rnd.uniform(24, 31):.1f} flow_lpm={self.rnd.uniform(9.5, 11.5):.1f}")
        if self.rnd.random() < 0.08:  # 정상에도 가끔 있는 경고
            self.add("WARN", "cool", f"FAN_ADJUST speed_pct={self.rnd.choice([60, 70, 80])} reason=TEST-AMBIENT")

    # ── 베드 ──
    def bed_fault(self, axis: str, pos: float, target: float) -> None:
        """TEST-E310: 위치가 달라도 로그에는 같은 메시지가 남는다."""
        err = self.rnd.uniform(0.7, 1.0)
        for _ in range(self.rnd.randint(1, 3)):
            self.add("WARN", "bed", f"FOLLOW_ERR_WARN axis={axis} err_mm={err:.1f}", gap=(40, 160))
            err *= self.rnd.uniform(1.4, 1.9)
        self.add("ERROR", "bed", f"FAULT code=TEST-E310 TEST-BED motion fault axis={axis} pos_mm={pos:.1f} target_mm={target:.1f}",
                 gap=(20, 90))
        self.add("INFO", "bed", f"STOP axis={axis} reason=TEST-E310")
        self.add("INFO", "bed", "BRAKE state=ENGAGED")

    def bed_move(self, axis: str, target: float, speed: float, where: str = "") -> None:
        start = self.bed_h if axis == "H" else self.bed_v
        self.add("INFO", "bed", f"MOVE_START axis={axis} from_mm={start:.1f} target_mm={target:.1f} speed_mm_s={speed:.1f}")
        distance = abs(target - start)
        steps = max(1, min(4, int(distance // 150)))
        for k in range(1, steps + 1):
            self.wait(int(distance / steps / speed * 1000))
            pos = start + (target - start) * k / (steps + 1)
            if k == steps // 2 + 1 and where and self.hit("bed_e310", where):
                self.bed_fault(axis, pos, target)
                if axis == "H":
                    self.bed_h = pos
                else:
                    self.bed_v = pos
                raise Stop(where)
            if k < steps or self.rnd.random() < 0.5:
                self.add("INFO", "bed", f"POS axis={axis} pos_mm={pos:.1f}")
            if self.rnd.random() < 0.05 and not (self.fault and self.fault.kind == "bed_e310"):
                # 정상에도 가끔 있는 작은 추종 오차 경고(오류로 이어지지 않음)
                self.add("WARN", "bed", f"FOLLOW_ERR_WARN axis={axis} err_mm={self.rnd.uniform(0.5, 0.7):.1f}")
        self.wait(self.rnd.randint(150, 400))
        err = self.rnd.uniform(0.0, 0.3)
        self.add("INFO", "bed", f"MOVE_DONE axis={axis} pos_mm={target:.1f} err_mm={err:.2f}")
        if axis == "H":
            self.bed_h = target
        else:
            self.bed_v = target

    # ── 갠트리 ──
    def spin_up(self, rot_s: float) -> None:
        if self.spinning:
            self.add("INFO", "gan", f"SPEED_CHANGE target_rot_s={rot_s:.2f}")
            self.add("INFO", "gan", f"SPEED_REACHED rot_s={rot_s:.2f} time_ms={self.rnd.randint(800, 1500)}", gap=(800, 1500))
            return
        self.add("INFO", "gan", f"SPIN_UP target_rot_s={rot_s:.2f}")
        if self.hit("gan_e210"):
            act = rot_s * self.rnd.uniform(1.6, 2.2)
            for _ in range(3):
                self.add("INFO", "gan", f"SPEED rot_s_act={act:.2f}", gap=(1500, 2500))
                act *= self.rnd.uniform(0.94, 0.99)
            self.add("WARN", "gan", f"SPEED_LOW target_rot_s={rot_s:.2f} act_rot_s={act:.2f}", gap=(1500, 2500))
            self.add("ERROR", "gan", f"FAULT code=TEST-E210 TEST-GANTRY speed not reached target_rot_s={rot_s:.2f} "
                                     f"act_rot_s={act:.2f}", gap=(3000, 5000))
            self.add("INFO", "gan", "SPIN_DOWN reason=TEST-E210")
            raise Stop("gantry")
        act = rot_s * self.rnd.uniform(2.5, 3.0)
        for _ in range(2):
            self.add("INFO", "gan", f"SPEED rot_s_act={act:.2f}", gap=(1200, 2200))
            act = max(rot_s, act * 0.6)
        self.add("INFO", "gan", f"SPEED_REACHED rot_s={rot_s:.2f} time_ms={self.rnd.randint(4200, 6500)}", gap=(800, 1600))
        self.spinning = True

    def spin_down(self) -> None:
        if self.spinning:
            self.add("INFO", "gan", "SPIN_DOWN reason=TEST-SESSION-END")
            self.add("INFO", "gan", "STOPPED", gap=(4000, 7000))
            self.spinning = False

    # ── X선 ──
    def prep(self, kv: int, ma: int) -> None:
        self.add("INFO", "hvg", f"PREP kv={kv} ma={ma}")
        self.add("INFO", "hvg", f"READY kv={kv} ma={ma} filament=OK", gap=(300, 900))

    def heat_after(self, amount: float) -> None:
        self.heat = min(88.0, self.heat + amount)
        self.add("INFO", "tube", f"HEAT heat_pct={self.heat:.0f}")
        if self.hit("tube_heat"):
            self.heat = self.rnd.uniform(90, 95)
            wait_s = self.rnd.randint(150, 260)
            self.add("WARN", "tube", f"HEAT_HIGH heat_pct={self.heat:.0f} cooling_wait_s={wait_s}")
            self.add("INFO", "sys", f"WAIT reason=TEST-TUBE-COOLING wait_s={wait_s}")
            for _ in range(3):
                self.wait(wait_s * 1000 // 3)
                self.heat -= self.rnd.uniform(4, 7)
                self.add("INFO", "tube", f"HEAT heat_pct={self.heat:.0f}")
            self.add("INFO", "tube", f"HEAT_OK heat_pct={self.heat:.0f}")

    # ── 촬영 ──
    def scout(self, angle: int, length: float) -> None:
        self.series += 1
        kv, ma = self.rnd.choice([100, 120]), self.rnd.choice([30, 40, 50])
        self.add("INFO", "sys", f"SERIES_START series={self.series} mode=TEST-MODE-SCOUT angle_deg={angle} length_mm={length:.0f}")
        self.add("INFO", "gan", f"HOLD angle_deg={angle}")
        self.add("INFO", "col", f"SET width_mm={self.rnd.choice([0.6, 1.2])}")
        self.prep(kv, ma)
        frames = int(length / 2)
        speed = self.rnd.choice([80.0, 100.0, 120.0])
        start = self.bed_h
        self.add("INFO", "hvg", f"EXPOSURE_ON series={self.series}")
        self.add("INFO", "bed", f"MOVE_START axis=H from_mm={start:.1f} target_mm={start + length:.1f} speed_mm_s={speed:.1f}")
        self.add("INFO", "det", f"ACQ_START series={self.series} frames_expected={frames}")
        for pct in (25, 50, 75):
            self.wait(int(length / speed * 250))
            pos = start + length * pct / 100
            if pct == 50 and self.hit("bed_e310", "SCOUT"):
                self.bed_fault("H", pos, start + length)
                self.add("WARN", "hvg", f"EXPOSURE_OFF series={self.series} forced=yes reason=TEST-E310")
                self.add("WARN", "det", f"ACQ_ABORT series={self.series} frames={frames * pct // 100}/{frames}")
                self.add("ERROR", "sys", f"SERIES_ABORT series={self.series} reason=TEST-E310")
                self.bed_h = pos
                if self.fault.variant == "retry":
                    self.add("INFO", "sys", f"SERIES_RETRY series={self.series} by=TEST-OPERATOR", gap=(8000, 20000))
                    self.add("INFO", "bed", "FAULT_RESET code=TEST-E310")
                    self.add("INFO", "bed", "BRAKE state=RELEASED")
                    self.bed_move("H", start, 80.0)
                    self.series -= 1
                    self.scout(angle, length)
                    return
                raise Stop("SCOUT")
            self.add("INFO", "det", f"ACQ_PROGRESS series={self.series} pct={pct} frames={frames * pct // 100}")
            self.add("INFO", "bed", f"POS axis=H pos_mm={pos:.1f}")
        self.wait(int(length / speed * 250))
        self.add("INFO", "bed", f"MOVE_DONE axis=H pos_mm={start + length:.1f} err_mm={self.rnd.uniform(0, 0.3):.2f}")
        self.bed_h = start + length
        dur = int(length / speed * 1000)
        self.add("INFO", "hvg", f"EXPOSURE_OFF series={self.series} dur_ms={dur} mas={ma * dur / 1000:.1f}")
        lost = self.frame_loss(frames, "scout")
        self.add("INFO", "det", f"ACQ_DONE series={self.series} frames={frames - lost} lost={lost}")
        self.recon("scout", 1)
        self.done.append(f"스카우트({angle}도)")
        self.bed_move("H", start, 150.0)

    def frame_loss(self, total: int, kind: str) -> int:
        if self.hit("det_e510", kind):
            lost = self.rnd.randint(12, 60)
            self.add("WARN", "det", f"FRAME_LOSS code=TEST-E510 TEST-DET frames lost count={lost}")
            return lost
        return 0

    def recon(self, kind: str, images: int, lost: int = 0) -> None:
        job = self.rnd.randint(1000, 9999)
        self.add("INFO", "rec", f"JOB_START job={job} series={self.series} type={kind} images={images}")
        if lost:
            self.add("WARN", "rec", f"VIEW_INTERPOLATED job={job} count={lost}")
        if kind != "scout" and self.hit("rec_e610"):  # 스카우트 재구성에서 고장을 써 버리지 않게 순서 주의
            for _ in range(self.rnd.randint(4, 6)):
                self.add("INFO", "rec", f"PROGRESS job={job} pct=40", gap=(20000, 30000))
            self.add("ERROR", "rec", f"FAULT code=TEST-E610 TEST-RECON job timeout job={job}", gap=(20000, 30000))
            self.add("INFO", "sys", f"RECON_FAILED series={self.series} job={job}")
            return
        for pct in (50, 100) if images > 50 else (100,):
            self.add("INFO", "rec", f"PROGRESS job={job} pct={pct}", gap=(400, 2500))
        self.add("INFO", "rec", f"JOB_DONE job={job} images={images} time_ms={self.rnd.randint(900, 9000)}")

    def helical(self, length: float) -> None:
        self.series += 1
        rot_s = self.rnd.choice([0.5, 0.75, 1.0])
        pitch = self.rnd.choice([0.6, 0.9, 1.2])
        width = self.rnd.choice([20.0, 40.0])
        speed = pitch * width / rot_s
        kv, ma = self.rnd.choice([80, 100, 120, 140]), self.rnd.choice([150, 200, 250, 300, 350])
        self.add("INFO", "sys", f"SERIES_START series={self.series} mode=TEST-MODE-HELICAL length_mm={length:.0f} "
                                f"pitch={pitch} rot_s={rot_s}")
        self.spin_up(rot_s)
        self.add("INFO", "col", f"SET width_mm={width:.0f}")
        self.prep(kv, ma)
        start = self.bed_h
        rotations = max(4, int(length / speed / rot_s))
        views = self.rnd.choice([984, 1152, 1200])
        self.add("INFO", "hvg", f"EXPOSURE_ON series={self.series}")
        self.add("INFO", "bed", f"MOVE_START axis=H from_mm={start:.1f} target_mm={start + length:.1f} "
                                f"speed_mm_s={speed:.1f} sync=TEST-GANTRY")
        self.add("INFO", "det", f"ACQ_START series={self.series} views_expected={rotations * views}")
        drift = self.hit("bed_dev", "HELICAL")  # 오류 메시지 없이 위치 오차만 커짐
        arc_at = rotations // 2 if self.fault and self.fault.kind == "hvg_e420" and self.fault.where == "HELICAL" else -1
        for rot in range(1, rotations + 1):
            self.wait(int(rot_s * 1000))
            pos = start + speed * rot_s * rot
            if rot == rotations // 2 and self.hit("bed_e310", "HELICAL"):
                self.bed_fault("H", pos, start + length)
                self.add("WARN", "hvg", f"EXPOSURE_OFF series={self.series} forced=yes reason=TEST-E310")
                self.add("WARN", "det", f"ACQ_ABORT series={self.series} views={rot * views}/{rotations * views}")
                self.add("ERROR", "sys", f"SERIES_ABORT series={self.series} reason=TEST-E310")
                self.bed_h = pos
                raise Stop("HELICAL")
            if rot == rotations // 2 and self.hit("trunc"):
                raise Stop("trunc")  # 전원이 끊겨 로그가 여기서 끝남
            if rot == arc_at and self.hit("hvg_e420", "HELICAL"):
                self.arc(kv)
                if self.fault.variant != "retry":
                    self.add("WARN", "det", f"ACQ_ABORT series={self.series} views={rot * views}/{rotations * views}")
                    self.add("WARN", "bed", "STOP axis=H reason=TEST-SERIES-ABORT")
                    self.add("ERROR", "sys", f"SERIES_ABORT series={self.series} reason=TEST-E420")
                    raise Stop("arc")
            self.add("INFO", "det", f"ROT_DONE series={self.series} rot={rot} views={views}", gap=(2, 12))
            if rot % max(1, rotations // 5) == 0:
                dev = self.rnd.uniform(0.0, 0.25) if not drift else 0.4 + rot / rotations * self.rnd.uniform(2.0, 3.0)
                self.add("INFO", "bed", f"POS axis=H pos_mm={pos:.1f} dev_mm={dev:.2f}", gap=(2, 12))
                self.add("INFO", "hvg", f"STATUS kv_act={kv + self.rnd.uniform(-0.6, 0.6):.1f} ma_act={ma + self.rnd.uniform(-2, 2):.0f}",
                         gap=(2, 12))
        dur = int(rotations * rot_s * 1000)
        self.add("INFO", "hvg", f"EXPOSURE_OFF series={self.series} dur_ms={dur} mas={ma * dur / 1000:.0f}")
        self.add("INFO", "bed", f"MOVE_DONE axis=H pos_mm={start + length:.1f} err_mm={self.rnd.uniform(0, 0.3):.2f}")
        self.bed_h = start + length
        lost = self.frame_loss(rotations * views, "helical")
        self.add("INFO", "det", f"ACQ_DONE series={self.series} views={rotations * views - lost} lost={lost}")
        self.heat_after(rotations * ma / 400)
        self.recon("helical", int(length / self.rnd.choice([1.0, 2.0, 5.0])), lost)
        self.done.append("나선")

    def arc(self, kv: int) -> None:
        self.add("INFO", "hvg", f"STATUS kv_act={kv - self.rnd.uniform(18, 35):.1f} ma_act=0", gap=(1, 5))
        self.add("ERROR", "hvg", f"FAULT code=TEST-E420 TEST-HVG arc detected kv_set={kv} kv_min={kv * 0.6:.0f}", gap=(1, 5))
        self.add("WARN", "hvg", f"EXPOSURE_OFF series={self.series} forced=yes reason=TEST-E420", gap=(1, 5))
        if self.fault.variant == "retry":
            self.add("INFO", "hvg", "ARC_RECOVERY retry=1", gap=(1500, 3000))
            self.add("INFO", "hvg", f"EXPOSURE_ON series={self.series} resume=yes")

    def axial(self, steps: int) -> None:
        self.series += 1
        rot_s = self.rnd.choice([0.75, 1.0])
        width = self.rnd.choice([10.0, 20.0, 40.0])
        kv, ma = self.rnd.choice([100, 120, 140]), self.rnd.choice([200, 250, 300])
        views = self.rnd.choice([984, 1152])
        self.add("INFO", "sys", f"SERIES_START series={self.series} mode=TEST-MODE-AXIAL steps={steps} step_mm={width:.0f} rot_s={rot_s}")
        self.spin_up(rot_s)
        self.add("INFO", "col", f"SET width_mm={width:.0f}")
        self.prep(kv, ma)
        drift = self.hit("bed_dev", "AXIAL")
        arc_step = steps // 2 + 1 if self.fault and self.fault.kind == "hvg_e420" and self.fault.where == "AXIAL" else -1
        for step in range(1, steps + 1):
            if step > 1:
                target = self.bed_h + width
                self.add("INFO", "bed", f"STEP_MOVE axis=H from_mm={self.bed_h:.1f} target_mm={target:.1f}")
                self.wait(self.rnd.randint(500, 900))
                if step == steps // 2 + 1 and self.hit("bed_e310", "AXIAL_STEP"):
                    self.bed_fault("H", self.bed_h + width * 0.4, target)
                    self.add("ERROR", "sys", f"SERIES_ABORT series={self.series} reason=TEST-E310 step={step}/{steps}")
                    self.recon("axial", (step - 1) * 4)
                    raise Stop("AXIAL_STEP")
                err = self.rnd.uniform(0.0, 0.3) if not drift or step < 3 else self.rnd.uniform(1.3, 2.4)
                self.add("INFO", "bed", f"STEP_DONE axis=H pos_mm={target:.1f} err_mm={err:.2f}")
                self.bed_h = target
            self.add("INFO", "hvg", f"EXPOSURE_ON series={self.series} step={step}")
            self.wait(int(rot_s * 1000))
            if step == arc_step and self.hit("hvg_e420", "AXIAL"):
                self.arc(kv)
                if self.fault.variant != "retry":
                    self.add("ERROR", "sys", f"SERIES_ABORT series={self.series} reason=TEST-E420 step={step}/{steps}")
                    raise Stop("arc")
            lost = self.frame_loss(views, "axial")
            self.add("INFO", "det", f"ACQ_DONE series={self.series} step={step} views={views - lost} lost={lost}")
            self.add("INFO", "hvg", f"EXPOSURE_OFF series={self.series} step={step} dur_ms={int(rot_s * 1000)} "
                                    f"mas={ma * rot_s:.0f}")
            if lost:
                self.add("WARN", "rec", f"VIEW_INTERPOLATED series={self.series} step={step} count={lost}")
        self.heat_after(steps * ma / 600)
        self.recon("axial", steps * 4)
        self.done.append("축")

    def dynamic(self, repeats: int) -> None:
        self.series += 1
        rot_s = self.rnd.choice([0.5, 0.75])
        interval = self.rnd.choice([2, 3, 5])
        kv, ma = self.rnd.choice([80, 100]), self.rnd.choice([100, 150, 200])
        self.add("INFO", "sys", f"SERIES_START series={self.series} mode=TEST-MODE-DYNAMIC repeats={repeats} interval_s={interval}")
        self.spin_up(rot_s)
        self.add("INFO", "col", f"SET width_mm={self.rnd.choice([20, 40])}")
        self.prep(kv, ma)
        self.add("INFO", "bed", f"HOLD axis=H pos_mm={self.bed_h:.1f}")
        views = self.rnd.choice([984, 1152])
        for k in range(1, repeats + 1):
            self.add("INFO", "hvg", f"EXPOSURE_ON series={self.series} repeat={k}")
            self.wait(int(rot_s * 1000))
            self.add("INFO", "det", f"ACQ_DONE series={self.series} repeat={k} views={views} lost=0")
            self.add("INFO", "hvg", f"EXPOSURE_OFF series={self.series} repeat={k} dur_ms={int(rot_s * 1000)}")
            if k < repeats:
                self.wait(interval * 1000)
        self.heat_after(repeats * ma / 800)
        self.recon("dynamic", repeats * 8)
        self.done.append("동적")

    # ── 검사 한 번 ──
    def run_exam(self, number: int, plan: list[tuple[str, int]], warmup: bool, pause: bool) -> str:
        """정상 종료면 'COMPLETED', 아니면 중단 위치."""
        self.add("INFO", "sys", f"NOTE {NOTE}", gap=(0, 1))
        self.add("INFO", "sys", f"SESSION_START exam=TEST-EX-{number:04d} type=TEST-EXAM protocol=TEST-PROTO-{self.rnd.randint(1, 9):02d}")
        try:
            self.selfcheck()
            if warmup:
                self.warmup()
            self.bed_move("V", self.rnd.uniform(860, 940), 25.0, where="LOAD")
            if self.hit("bed_e320", "LOAD"):
                self.comm_timeout()
            self.add("INFO", "sys", "PATIENT_READY by=TEST-OPERATOR", gap=(15000, 60000))
            self.bed_move("H", self.rnd.uniform(300, 480), 150.0)
            self.scout(self.rnd.choice([0, 90]), self.rnd.uniform(350, 650))
            for index, (mode, size) in enumerate(plan):
                if index and pause:
                    self.add("INFO", "sys", "OPERATOR_PAUSE by=TEST-OPERATOR")
                    self.wait(self.rnd.randint(20000, 90000))
                    self.add("INFO", "sys", "OPERATOR_RESUME by=TEST-OPERATOR")
                elif index:
                    self.wait(self.rnd.randint(3000, 12000))
                if mode == "HELICAL" and self.fault and self.fault.kind == "bed_e320" and self.fault.where == "HELICAL":
                    self.comm_timeout(fired_check=True)
                getattr(self, mode.lower())(size)
                self.cool_status()
            self.bed_move("H", 0.0, 150.0, where="RETURN")
            self.bed_move("V", 650.0, 25.0)
            self.spin_down()
            result = "COMPLETED_WITH_ERROR" if any(x in "\n".join(self.lines[-80:]) for x in ("RECON_FAILED",)) else "COMPLETED"
            self.add("INFO", "sys", f"SESSION_END exam=TEST-EX-{number:04d} result={result} series={self.series}")
            return "COMPLETED"
        except Stop as stop:
            where = str(stop)
            if where == "trunc":
                return where  # 로그가 끊겨 마무리 줄이 없다
            if where == "RETURN":
                self.add("ERROR", "sys", "MANUAL_RELEASE_REQUIRED axis=H by=TEST-OPERATOR")
                self.spin_down()
                self.add("INFO", "sys", f"SESSION_END exam=TEST-EX-{number:04d} result=COMPLETED_WITH_ERROR series={self.series}")
                return where
            if self.spinning:
                self.add("INFO", "gan", "SPIN_DOWN reason=TEST-ABORT")
                self.add("INFO", "gan", "STOPPED", gap=(4000, 7000))
                self.spinning = False
            stage = {"LOAD": "TEST-STAGE-LOAD"}.get(where, "TEST-STAGE-SCAN")
            self.add("ERROR", "sys", f"SESSION_ABORT exam=TEST-EX-{number:04d} stage={stage} series={self.series}")
            return where

    def selfcheck(self) -> None:
        self.add("INFO", "sys", "SELFCHECK_START")
        self.add("INFO", "gan", "STATUS state=READY angle_deg=0", gap=(200, 600))
        self.add("INFO", "bed", f"STATUS state=READY pos_h_mm={self.bed_h:.1f} pos_v_mm={self.bed_v:.1f}", gap=(200, 600))
        self.add("INFO", "hvg", f"STATUS state=READY heat_pct={self.heat:.0f}", gap=(200, 600))
        self.add("INFO", "det", f"STATUS state=READY temp_c={self.rnd.uniform(27, 32):.1f}", gap=(200, 600))
        self.add("INFO", "rec", f"STATUS state=READY queue={self.rnd.randint(0, 2)}", gap=(200, 600))
        self.cool_status()
        self.add("INFO", "sys", "SELFCHECK_DONE result=OK")

    def warmup(self) -> None:
        self.add("INFO", "tube", "WARMUP_START steps=4")
        self.spin_up(1.0)
        for kv in (80, 100, 120, 140):
            self.prep(kv, 100)
            self.add("INFO", "hvg", "EXPOSURE_ON series=0")
            self.wait(2000)
            self.add("INFO", "hvg", "EXPOSURE_OFF series=0 dur_ms=2000")
        self.heat = self.rnd.uniform(20, 28)
        self.add("INFO", "tube", f"WARMUP_DONE heat_pct={self.heat:.0f}")

    def comm_timeout(self, fired_check: bool = False) -> None:
        if fired_check and not self.hit("bed_e320", "HELICAL"):
            return
        for attempt in range(1, 4):
            self.add("WARN", "sys", f"COMM_RETRY peer=TEST-BED try={attempt}", gap=(1000, 1000))
        self.add("ERROR", "sys", "COMM_TIMEOUT code=TEST-E320 peer=TEST-BED", gap=(1000, 1000))
        raise Stop("comm")

    # ── 일일 점검 ──
    def run_qa(self, number: int) -> str:
        self.add("INFO", "sys", f"NOTE {NOTE}", gap=(0, 1))
        self.add("INFO", "sys", f"SESSION_START exam=TEST-QA-{number:04d} type=TEST-DAILY-QA")
        try:
            self.selfcheck()
            self.spin_up(1.0)
            self.add("INFO", "det", "OFFSET_CAL_START frames=200")
            self.add("INFO", "det", f"OFFSET_CAL_DONE frames=200 noise={self.rnd.uniform(0.8, 1.2):.2f}", gap=(6000, 9000))
            temp = self.rnd.uniform(27, 32)
            hot = self.hit("det_e520")
            for kv in (80, 100, 120, 140):
                self.prep(kv, 100)
                self.add("INFO", "hvg", f"EXPOSURE_ON series=0 cal=TEST-AIR kv={kv}")
                self.wait(1000)
                self.add("INFO", "hvg", "EXPOSURE_OFF series=0 dur_ms=1000")
                if hot:
                    temp += self.rnd.uniform(2.5, 4.0)
                dev = self.rnd.uniform(0.1, 0.9) if not hot or kv < 120 else self.rnd.uniform(3.5, 6.0)
                self.add("INFO", "det", f"GAIN_CAL kv={kv} dev_pct={dev:.1f} temp_c={temp:.1f}")
            if hot:
                self.add("ERROR", "det", f"FAULT code=TEST-E520 TEST-DET temperature out of range temp_c={temp:.1f}")
                self.add("INFO", "det", "GAIN_CAL_RESULT result=FAIL")
                self.add("INFO", "sys", "QA_RESULT result=FAIL")
            else:
                self.add("INFO", "det", "GAIN_CAL_RESULT result=PASS")
                self.add("INFO", "sys", "QA_RESULT result=PASS")
            self.cool_status()
            self.spin_down()
            self.add("INFO", "sys", f"SESSION_END exam=TEST-QA-{number:04d} result=COMPLETED")
            return "COMPLETED" if not hot else "QA_FAIL"
        except Stop as stop:
            self.add("ERROR", "sys", f"SESSION_ABORT exam=TEST-QA-{number:04d} stage=TEST-STAGE-QA")
            return str(stop)


# ── 세트 구성 ──
ABNORMAL = (
    [("bed_e310", w, v) for w in ("LOAD", "SCOUT", "AXIAL_STEP", "HELICAL", "RETURN") for v in ("", "", "retry" if w == "SCOUT" else "")]
    + [("bed_dev", "AXIAL", ""), ("bed_dev", "HELICAL", ""), ("bed_dev", "HELICAL", "")]
    + [("bed_e320", "LOAD", ""), ("bed_e320", "HELICAL", "")]
    + [("gan_e210", "EXAM", ""), ("gan_e210", "EXAM", ""), ("gan_e210", "QA", "")]
    + [("hvg_e420", "HELICAL", "retry"), ("hvg_e420", "HELICAL", ""), ("hvg_e420", "AXIAL", "")]
    + [("tube_heat", "", ""), ("tube_heat", "", "")]
    + [("det_e510", "helical", ""), ("det_e510", "axial", ""), ("det_e510", "scout", "")]
    + [("det_e520", "QA", ""), ("det_e520", "QA", "")]
    + [("rec_e610", "", ""), ("rec_e610", "", "")]
    + [("trunc", "", "")]
)
NORMAL_EXAMS, NORMAL_QA = 56, 8


def plan_for(rnd: random.Random, need: str | None) -> list[tuple[str, int]]:
    """촬영 계획. need가 있으면 그 방식을 꼭 넣는다(고장 위치를 만들려고)."""
    plan = []
    for _ in range(rnd.choice([1, 1, 2, 2, 3])):
        mode = rnd.choices(["HELICAL", "AXIAL", "DYNAMIC"], weights=[5, 3, 2])[0]
        plan.append((mode, {"HELICAL": rnd.randint(200, 600), "AXIAL": rnd.randint(4, 10),
                            "DYNAMIC": rnd.randint(4, 10)}[mode]))
    if need and need not in [m for m, _ in plan]:
        plan.insert(rnd.randrange(len(plan) + 1), (need, {"HELICAL": rnd.randint(250, 600), "AXIAL": rnd.randint(5, 10),
                                                          "DYNAMIC": 6}[need]))
    return plan


def memo_and_guide(kind: str | None, where: str, variant: str, exam: Exam, plan: list[tuple[str, int]], outcome: str) -> tuple[str, str]:
    planned = " + ".join(f"{MODE_KO[m]}" for m, _ in plan)
    if kind is None:
        if outcome == "QA":
            return "일일 점검: 오프셋·공기 보정(80~140) 통과", ""
        return f"검사: 스카우트 + {planned} 촬영, 정상 종료", ""
    done = ", ".join(exam.done) or "촬영 전"
    if kind == "bed_e310":
        result = {"LOAD": "촬영 시작 전에 검사 중단", "SCOUT": "스카우트 중 노출 강제 종료",
                  "AXIAL_STEP": "남은 단계 취소·검사 중단", "HELICAL": "노출·영상 획득 강제 종료, 검사 중단",
                  "RETURN": "촬영은 모두 끝났으나 베드를 수동으로 풀어야 함"}[where]
        if variant == "retry":
            result = "스카우트 다시 촬영해 이어서 정상 종료"
        return (f"검사(계획: 스카우트 + {planned}), 마친 촬영: {done}",
                f"베드 구동 오류 TEST-E310이 {WHERE_KO[where]}에서 남. 앞에 추종 오차 경고(FOLLOW_ERR_WARN)가 커지다가 "
                f"FAULT → STOP·BRAKE. 결과: {result}. 같은 메시지가 다른 위치에서도 나므로 어느 단계였는지로 구분.")
    guides = {
        "bed_dev": f"오류 메시지 없음. {'축 촬영 STEP_DONE의 err_mm' if where == 'AXIAL' else '나선 촬영 중 베드 POS의 dev_mm'}이 "
                   "평소(0.3 이하)보다 크게 나옴(1 이상). 검사는 끝났지만 베드 위치가 어긋남.",
        "bed_e320": f"{'환자 올리기 직후' if where == 'LOAD' else '나선 촬영 시작 전'} TEST-SYS가 TEST-BED와 통신 재시도 3번(COMM_RETRY) 뒤 "
                    "COMM_TIMEOUT TEST-E320, 검사 중단. 베드 구동 오류(TEST-E310)와 다른 코드.",
        "gan_e210": ("일일 점검 시작 때" if where == "QA" else "촬영 전 갠트리를 돌릴 때") +
                    " 회전이 목표 속도에 못 미침(SPEED rot_s_act가 목표보다 큼, SPEED_LOW) → TEST-E210, " +
                    ("점검 중단." if where == "QA" else "검사 중단."),
        "hvg_e420": f"{MODE_KO.get(where, '')} 촬영 노출 중 kV가 떨어지고 TEST-E420(arc) → 노출 강제 종료. "
                    + ("ARC_RECOVERY로 다시 노출해 촬영은 끝남." if variant == "retry" else "그 촬영 중단·검사 중단."),
        "tube_heat": "오류 코드 없음. 촬영 뒤 튜브 열(HEAT_HIGH 90% 이상)로 냉각 대기(WAIT) 2~4분, 그 뒤 이어서 촬영. 검사 시간이 평소보다 김.",
        "det_e510": f"{ {'helical': '나선', 'axial': '축', 'scout': '스카우트'}.get(where, '') } 촬영 영상 획득에서 프레임 손실(FRAME_LOSS TEST-E510, lost가 0 아님). "
                    "검사는 끝났지만 재구성에서 보간(VIEW_INTERPOLATED).",
        "det_e520": "일일 점검 중 디텍터 온도가 계속 올라 TEST-E520, 높은 kV에서 게인 편차(dev_pct)가 커지고 QA_RESULT FAIL.",
        "rec_e610": "촬영은 끝났지만 재구성 작업이 40%에서 멈춰 PROGRESS가 반복되다 TEST-E610(job timeout), RECON_FAILED.",
        "trunc": "나선 촬영 노출 중에 로그가 끊김(EXPOSURE_OFF·SESSION_END 없음). 전원 끊김 등.",
    }
    head = "일일 점검" if outcome == "QA" or (kind == "gan_e210" and where == "QA") or kind == "det_e520" else \
        f"검사(계획: 스카우트 + {planned}), 마친 촬영: {done}"
    return head, guides[kind]


def build(seed: str, out: Path) -> list[tuple[str, str, str, str]]:
    rnd = random.Random(seed)
    jobs: list[tuple[str | None, str, str]] = [(None, "EXAM", "")] * NORMAL_EXAMS + [(None, "QA", "")] * NORMAL_QA + list(ABNORMAL)
    rnd.shuffle(jobs)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("CT_*.log"):
        old.unlink()
    rows = []
    for index, (kind, where, variant) in enumerate(jobs, start=1):
        day = datetime(2026, 11, 1) + timedelta(days=rnd.randint(0, 19), hours=rnd.randint(7, 18), minutes=rnd.randint(0, 59))
        exam = Exam(rnd, day, Fault(kind, where, variant) if kind else None)
        exam.bed_v = 650.0
        is_qa = (kind is None and where == "QA") or kind == "det_e520" or (kind == "gan_e210" and where == "QA")
        if is_qa:
            outcome = exam.run_qa(index)
            plan: list[tuple[str, int]] = []
        else:
            need = {"AXIAL_STEP": "AXIAL", "HELICAL": "HELICAL", "AXIAL": "AXIAL", "helical": "HELICAL",
                    "axial": "AXIAL"}.get(where) if kind else None
            if kind in ("tube_heat", "rec_e610", "trunc", "gan_e210"):
                need = "HELICAL"
            plan = plan_for(rnd, need)
            outcome = exam.run_exam(index, plan, warmup=rnd.random() < 0.3, pause=rnd.random() < 0.15)
        if kind and not exam.fault.fired:
            raise RuntimeError(f"고장이 들어가지 않음: {kind} {where} (CT_{index:03d})")
        name = f"CT_{index:03d}.log"
        (out / name).write_text("\n".join(exam.lines) + "\n", encoding="utf-8")
        memo, guide = memo_and_guide(kind, where, variant, exam, plan, "QA" if is_qa else outcome)
        rows.append((name, "비정상" if kind else "정상", memo, guide))
    head = ["파일\t구분\t동작 메모(예)\t판단 가이드(예, 비정상만)"]
    (out / "정답표.txt").write_bytes(("\r\n".join(head + ["\t".join(r) for r in rows]) + "\r\n").encode("utf-8-sig"))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", default=SEED)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    rows = build(args.seed, args.out)
    abnormal = sum(r[1] == "비정상" for r in rows)
    print(f"{args.out}: 로그 {len(rows)}개 (정상 {len(rows) - abnormal}, 비정상 {abnormal})")


if __name__ == "__main__":
    main()
