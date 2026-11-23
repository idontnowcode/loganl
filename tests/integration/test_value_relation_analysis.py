"""CR-17: 오류 줄 없이 숫자만 어긋난 로그(계획 수 ≠ 처리 수)를 정상 로그에서 배운 숫자 관계로 잡는다. 로그는 가상이다."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.analysis.diagnostic_engine import AnalysisRequest, DiagnosticEngine
from app.analysis.error_messages import RELATION, presumed_errors
from app.analysis.value_relations import is_relation
from app.bootstrap import initialize
from app.core.importer import LogImporter
from app.learning.log_entry import LogEntryService
from app.models.enums import OriginPurpose
from app.models.records import ImportRequest, SessionMode
from tests.logdata import iso_lines, write


@pytest.fixture
def ctx(tmp_path: Path):
    context = initialize(tmp_path / "home", configure_logging=False)
    yield context
    context.close()


def lot(planned: int, done: int) -> list[tuple[str, str, str]]:
    body = [("INFO", "TEST-SCHED", f"TEST-LOT-START lot=TEST-LOT-1 count={planned}")]
    for i in range(done):
        body += [("INFO", "TEST-ROBOT", f"TEST-ITEM-LOAD item=W{i:03d}"), ("INFO", "TEST-CELL", f"TEST-ITEM-DONE item=W{i:03d}")]
    return body + [("INFO", "TEST-SCHED", f"TEST-LOT-END lot=TEST-LOT-1 processed={done} result=OK")]


def load(ctx, path: Path) -> int:
    return LogImporter(ctx.database, ctx.paths, ctx.settings.import_).import_logs(ImportRequest(
        paths=[path], purpose=OriginPurpose.ANALYSIS, session_mode=SessionMode.SEPARATE)).log_file_ids[0]


def learn(ctx, tmp_path: Path, n: int) -> None:
    entries = LogEntryService(ctx.database)
    for i in range(n):
        size = 8 + i % 5  # 계획 수 8~12, 모두 다 처리
        entries.save(load(ctx, write(tmp_path / f"n{i}.log", iso_lines(lot(size, size), start_second=i * 30))),
                     "NORMAL", "가상 정상")


def run(ctx, tmp_path: Path, name: str, planned: int, done: int):
    log_id = load(ctx, write(tmp_path / name, iso_lines(lot(planned, done), start_second=900)))
    return DiagnosticEngine(ctx.database, ctx.paths, ctx.settings).run(AnalysisRequest([log_id], use_ai=False))


def test_count_mismatch_without_error_lines_is_suspected(ctx, tmp_path: Path) -> None:  # VR-01~03
    learn(ctx, tmp_path, 12)
    ok = run(ctx, tmp_path, "ok.log", 10, 10)
    assert not any(is_relation(f) for f in ok.findings)
    short = run(ctx, tmp_path, "short.log", 10, 9)  # 한 개 덜 처리, 오류·경고 줄 없음
    relations = [f for f in short.findings if is_relation(f)]
    assert relations and all(f.stance == "SUPPORTS" for f in relations)
    assert short.verdict.state == "ABNORMAL" and short.axes["anomaly_level"] == "SUSPECTED"
    equal = next(f for f in relations if f.detail["kind"] == "value_equal")
    assert "'count'" in equal.summary and "10" in equal.summary and "9" in equal.summary
    messages = presumed_errors(short.findings, raw_lines={})
    assert any(m.group == RELATION for m in messages)  # 예상 에러 메시지 '정상과 다른 숫자 관계'


def test_few_normals_only_ask_the_engineer(ctx, tmp_path: Path) -> None:  # VR-03·05
    learn(ctx, tmp_path, 6)
    short = run(ctx, tmp_path, "short.log", 10, 9)
    relations = [f for f in short.findings if is_relation(f)]
    assert relations and all(f.stance == "NEUTRAL" and f.detail["candidate"] for f in relations)
    asked = [q for q in short.questions if q.kind == "RELATION"]
    assert asked and "다릅니다" in asked[0].text  # 약한 후보는 판정을 바꾸지 않고 엔지니어에게 묻는다
