"""운전 모드 없이 분석할 때 비슷한 정상 로그와 비교 (CR-08 DT-01~04, 분석 엔진 경로 전체).

정상 로그 4개를 '정상'으로 저장한 뒤 새 로그를 분석한다. 데이터는 모두 가상 값(TEST- 이름)이다.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from app.analysis.diagnostic_engine import AnalysisRequest, DiagnosticEngine
from app.bootstrap import initialize
from app.core.importer import LogImporter
from app.learning.log_entry import LogEntryService
from app.models.enums import OriginPurpose
from app.models.records import ImportRequest, SessionMode
from tests.logdata import iso_lines, write


def run_sequence(values: tuple[int, ...], *, stop: bool = True) -> list[tuple[str, str, str]]:
    events = [("INFO", "TEST-CTRL", "TEST-START step=1"), ("INFO", "TEST-SUB", "TEST-READY slot=2")]
    for cycle, value in enumerate(values, start=1):
        events += [("INFO", "TEST-CTRL", f"TEST-RUN cycle={cycle} value={value}"),
                   ("INFO", "TEST-SUB", f"TEST-CHECK cycle={cycle} result=OK")]
    return events + ([("INFO", "TEST-CTRL", "TEST-STOP step=9")] if stop else [])


@pytest.fixture
def env(tmp_path: Path):
    ctx = initialize(tmp_path / "home", configure_logging=False)
    importer = LogImporter(ctx.database, ctx.paths, ctx.settings.import_)

    def add(name: str, events, start: int) -> int:
        path = write(tmp_path / name, iso_lines(events, start_second=start))
        return importer.import_logs(ImportRequest(paths=[path], purpose=OriginPurpose.NORMAL_CASE,
                                                  session_mode=SessionMode.SEPARATE)).log_file_ids[0]

    entries = LogEntryService(ctx.database)
    for index, values in enumerate([(41, 43, 42), (44, 40, 42), (42, 41, 43), (43, 44, 40)]):  # 정상 4개(누락 판단 기준)
        entries.save(add(f"normal_{index}.log", run_sequence(values), start=index * 30), "NORMAL", "")
    engine = DiagnosticEngine(ctx.database, ctx.paths, ctx.settings)
    yield ctx, add, engine
    ctx.close()


def analyze(engine: DiagnosticEngine, log_file_id: int):
    return engine.run(AnalysisRequest([log_file_id], use_ai=False))


def supporting(result) -> dict[str, list]:
    out: dict[str, list] = {}
    for f in result.findings:
        if f.layer == "DETERMINISTIC" and f.stance == "SUPPORTS":
            out.setdefault(f.finding_type, []).append(f)
    return out


def test_normal_log_is_not_flagged(env) -> None:
    _ctx, add, engine = env
    result = analyze(engine, add("new_normal.log", run_sequence((43, 42, 41, 44)), start=200))
    assert result.axes["anomaly_level"] == "NONE_FOUND" and supporting(result) == {}


def test_truncated_log_is_suspected(env) -> None:  # DT-01
    _ctx, add, engine = env
    result = analyze(engine, add("cut.log", run_sequence((42, 43), stop=False), start=200))
    found = supporting(result)
    assert result.axes["anomaly_level"] == "SUSPECTED"  # 승인 규칙이 아니므로 '규칙 위반'이 아니다
    assert [f.detail["event_type"] for f in found["MISSING_EVENT"]] == ["TEST-CTRL|TEST-STOP"]
    assert found["MISSING_EVENT"][0].detail["basis"] == "REFERENCE_NORMAL"


def test_value_out_of_range_is_stored_as_evidence(env) -> None:  # DT-04, 마이그레이션 0007
    ctx, add, engine = env
    result = analyze(engine, add("high.log", run_sequence((42, 418, 41)), start=200))
    assert result.axes["anomaly_level"] == "SUSPECTED"
    assert [f.detail["value"] for f in supporting(result)["VALUE_OUT_OF_RANGE"]] == [418]
    with closing(ctx.database.connect()) as conn:
        stored = conn.execute("SELECT stance FROM analysis_evidence WHERE run_id = ? AND finding_type = 'VALUE_OUT_OF_RANGE'",
                              (result.run_id,)).fetchall()
    assert [row[0] for row in stored] == ["SUPPORTS"]
