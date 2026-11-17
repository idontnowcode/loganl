"""가상 테스트 로그 생성기. 실제 장비 형식·의미가 아니다 (docs/TEST_PLAN.md §1.2).

이벤트 이름은 TEST-로 시작하고, 오류 코드는 TEST-E### 형식만 쓴다. 뜻은 없다.
"""

from __future__ import annotations

from pathlib import Path

HEADER = "2026-11-16 {stamp}{tz} INFO TEST-NOTE: 가상 테스트 로그, 실제 장비 형식과 의미 아님"


def _stamp(total_ms: int) -> str:
    """10:00:00.000 기준으로 total_ms 뒤의 시각 문자열(음수면 9시 59분대)."""
    base = 10 * 3600 * 1000 + total_ms
    hours, rest = divmod(base, 3600 * 1000)
    minutes, rest = divmod(rest, 60 * 1000)
    seconds, ms = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{ms:03d}"


def iso_lines(events: list[tuple[str, str, str]], *, start_second: int = 0, tz: str = "", step_ms: int = 100) -> list[str]:
    """(레벨, 구성요소, 메시지) 목록 → generic_iso_level 형식 줄. 머리 줄은 첫 이벤트 1초 전."""
    lines = [HEADER.format(stamp=_stamp(start_second * 1000 - 1000), tz=tz)]
    for index, (level, component, message) in enumerate(events):
        total_ms = start_second * 1000 + index * step_ms
        seconds, ms = divmod(total_ms, 1000)
        minutes, seconds = divmod(seconds, 60)
        lines.append(f"2026-11-16 10:{minutes:02d}:{seconds:02d}.{ms:03d}{tz} {level} {component}: {message}")
    return lines


NORMAL_SEQUENCE = [
    ("INFO", "TEST-CTRL", "TEST-START step=1"),
    ("INFO", "TEST-CTRL", "TEST-INIT value=10"),
    ("INFO", "TEST-SUB", "TEST-READY slot=2"),
    ("INFO", "TEST-CTRL", "TEST-RUN value=42"),
    ("INFO", "TEST-CTRL", "TEST-STOP step=9"),
]

ABNORMAL_SEQUENCE = [
    ("INFO", "TEST-CTRL", "TEST-START step=1"),
    ("INFO", "TEST-CTRL", "TEST-INIT value=10"),
    ("ERROR", "TEST-SUB", "TEST-FAULT code=TEST-E001 value=7"),
    ("WARN", "TEST-CTRL", "TEST-RETRY count=1"),
    ("ERROR", "TEST-SUB", "TEST-FAULT code=TEST-E001 value=8"),
]


def write(path: Path, lines: list[str], *, encoding: str = "utf-8", newline: str = "\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((newline.join(lines) + newline).encode(encoding))
    return path


def normal_log(path: Path, **kwargs) -> Path:
    return write(path, iso_lines(NORMAL_SEQUENCE, **kwargs))


def abnormal_log(path: Path, **kwargs) -> Path:
    return write(path, iso_lines(ABNORMAL_SEQUENCE, **kwargs))
