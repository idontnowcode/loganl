"""T-PERF-01: 10MB 가상 로그 10개 가져오기 (계획서 §2.1 "초기 성능 검증은 10개 파일 기준"). LLA_PERF=1일 때만 돈다."""

from __future__ import annotations

import os
import time
from contextlib import closing
from pathlib import Path

import pytest

from app.bootstrap import initialize
from app.core.importer import LogImporter
from app.models.enums import OriginPurpose
from app.models.records import ImportRequest, SessionMode

pytestmark = pytest.mark.skipif(os.environ.get("LLA_PERF") != "1", reason="LLA_PERF=1일 때만 실행")

TARGET_BYTES = 10 * 1024 * 1024 - 4096


def make_big_log(path: Path, seed: int) -> int:
    lines = []
    size = 0
    i = 0
    while size < TARGET_BYTES:
        seconds, ms = divmod(i * 7, 1000)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        line = (f"2026-11-16 {hours % 24:02d}:{minutes:02d}:{seconds:02d}.{ms:03d} INFO TEST-PERF{seed}: "
                f"TEST-EVENT-{i % 37} value={i} slot={i % 5}")
        lines.append(line)
        size += len(line) + 1
        i += 1
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return len(lines)


def test_ten_files_of_10mb(tmp_path: Path) -> None:
    ctx = initialize(tmp_path / "home", configure_logging=False)
    try:
        paths = []
        expected = 0
        for n in range(10):
            path = tmp_path / "in" / f"perf_{n}.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            expected += make_big_log(path, n)
            paths.append(path)
        importer = LogImporter(ctx.database, ctx.paths, ctx.settings.import_)
        started = time.perf_counter()
        result = importer.import_logs(ImportRequest(paths=paths, purpose=OriginPurpose.ANALYSIS,
                                                    session_mode=SessionMode.SEPARATE))
        elapsed = time.perf_counter() - started
        with closing(ctx.database.connect()) as conn:
            stored = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        db_mb = ctx.paths.db_file.stat().st_size / 1024 / 1024
        print(f"\nT-PERF-01: 파일 10개, 이벤트 {stored:,}개, {elapsed:.1f}초, DB {db_mb:.0f}MB")
        assert result.rejected == [] and stored == expected
        assert elapsed < 300
    finally:
        ctx.close()
