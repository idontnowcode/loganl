"""CR-16 RJ-01~06: 참고 자료의 판정 설명(에러·정상)을 판정에 쓰기. 코드·알림·판정 낱말은 모두 가상이다."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.analysis.diagnostic_engine import AnalysisRequest, DiagnosticEngine
from app.analysis.reference_matcher import match_references
from app.bootstrap import initialize
from app.core.importer import LogImporter
from app.core.reference_structurer import judgment_side
from app.learning.log_entry import LogEntryService
from app.learning.reference_library import ReferenceLibrary
from app.models.enums import OriginPurpose
from app.models.records import ImportRequest, SessionMode
from tests.logdata import NORMAL_SEQUENCE, iso_lines, write
from tests.refdata import write_xlsx

FK = ("INFO", "TEST-SYS", "TEST-NOTICE flag=TEST-FK unit=2")
FA = ("INFO", "TEST-SYS", "TEST-NOTICE flag=TEST-FA unit=1")
E900 = ("ERROR", "TEST-SUB", "TEST-FAULT code=TEST-E900 value=1")
TABLE = [
    ("코드/메시지", "판정", "설명"),
    ("NOTICE flag=TEST-FK", "점검 필요", "가상 알림 K"),
    ("NOTICE flag=TEST-FA", "정상", "가상 알림 A"),
    ("TEST-E001", "경고", "가상 코드(판정에 안 쓰는 낱말)"),
    ("TEST-E900", "무시", "가상: 무해한 코드"),
]


@pytest.fixture
def ctx(tmp_path: Path):
    context = initialize(tmp_path / "home", configure_logging=False)
    yield context
    context.close()


def load(ctx, path: Path) -> int:
    return LogImporter(ctx.database, ctx.paths, ctx.settings.import_).import_logs(ImportRequest(
        paths=[path], purpose=OriginPurpose.ANALYSIS, session_mode=SessionMode.SEPARATE)).log_file_ids[0]


def log(tmp_path: Path, name: str, extra=(), start: int = 0) -> Path:
    return write(tmp_path / name, iso_lines(NORMAL_SEQUENCE * 8 + list(extra), start_second=start))  # 짧은 로그 보류가 없게


def approve_table(ctx, tmp_path: Path, rows=TABLE) -> int:
    library = ReferenceLibrary(ctx.database, ctx.paths, ctx.settings.import_)
    doc = library.import_file(write_xlsx(tmp_path / "가상 판정표.xlsx", rows=rows)).document_id
    library.approve(doc)
    return doc


def run(ctx, log_id: int):
    return DiagnosticEngine(ctx.database, ctx.paths, ctx.settings).run(AnalysisRequest([log_id], use_ai=False))


def test_judgment_words() -> None:  # RJ-02
    assert [judgment_side(w) for w in ("에러", "점검 필요", "Critical", "비정상")] == ["ABNORMAL"] * 4
    assert [judgment_side(w) for w in ("정상", "문제 없음", "무시", "INFO", "OK")] == ["NORMAL"] * 5
    assert [judgment_side(w) for w in ("경고", "Warning", "정상 아님", "정상/에러", "token", "", None)] == [None] * 7


def test_judgment_column_and_text_entries_match(ctx, tmp_path: Path) -> None:  # RJ-01·03
    approve_table(ctx, tmp_path)
    log_id = load(ctx, log(tmp_path, "a.log", [FK, FA]))
    matches = {m.code: m for m in match_references(ctx.database, [log_id])}
    fk = next(m for m in matches.values() if m.code_norm == "NOTICEFLAG=TEST-FK")
    assert fk.via == "TEXT" and fk.entries[0].judgment == "점검 필요" and fk.entries[0].side == "ABNORMAL"
    assert len(fk.event_ids) == 1 and fk.first_line > len(NORMAL_SEQUENCE) * 8 and fk.code == "NOTICE flag=TEST-FK"
    fa = next(m for m in matches.values() if m.code_norm == "NOTICEFLAG=TEST-FA")
    assert fa.entries[0].side == "NORMAL"
    other = load(ctx, log(tmp_path, "b.log", [("INFO", "TEST-SYS", "TEST-NOTICE flag=TEST-FKX unit=2")]))
    assert not [m for m in match_references(ctx.database, [other]) if m.code_norm == "NOTICEFLAG=TEST-FK"]  # 낱말 경계


def test_abnormal_entry_decides_even_without_learned_logs(ctx, tmp_path: Path) -> None:  # RJ-04
    approve_table(ctx, tmp_path)
    plain = run(ctx, load(ctx, log(tmp_path, "plain.log")))
    assert plain.verdict.state == "UNDECIDED"  # 정상 로그를 학습하지 않음
    result = run(ctx, load(ctx, log(tmp_path, "fk.log", [FK])))
    assert result.verdict.state == "ABNORMAL" and result.axes["anomaly_level"] == "SUSPECTED"
    assert "가상 판정표" in result.verdict.reasons[0] and "'점검 필요'" in result.verdict.reasons[0]
    assert result.reference_judgment.signed
    assert any(f.detail.get("role") == "REFERENCE_JUDGMENT" for f in result.findings)


def test_normal_entry_clears_rule_suspicion_and_is_not_asked(ctx, tmp_path: Path) -> None:  # RJ-04·06
    entries = LogEntryService(ctx.database)
    for i in range(3):
        entries.save(load(ctx, log(tmp_path, f"n{i}.log", start=i * 10)), "NORMAL", "가상 정상")
    before = run(ctx, load(ctx, log(tmp_path, "e0.log", [E900], start=40)))
    assert before.verdict.state == "ABNORMAL"  # 참고 자료 없이: 명시적 오류 코드
    assert any("TEST-E900" in (q.example or q.text) for q in before.questions)
    approve_table(ctx, tmp_path)
    after = run(ctx, load(ctx, log(tmp_path, "e1.log", [E900], start=50)))
    assert after.verdict.state == "NORMAL" and "정상으로 적힌 줄" in after.verdict.reasons[0]
    assert not any("TEST-E900" in (q.example or q.text) for q in after.questions)  # 참고 자료가 답한 근거는 묻지 않음


def test_entries_contradicted_by_confirmed_logs_are_skipped(ctx, tmp_path: Path) -> None:  # RJ-05
    approve_table(ctx, tmp_path)
    entries = LogEntryService(ctx.database)
    entries.save(load(ctx, log(tmp_path, "c1.log", [FK], start=10)), "NORMAL", "가상: 알림이 있어도 정상이었음")
    first = run(ctx, load(ctx, log(tmp_path, "t1.log", [FK], start=20)))
    assert first.verdict.state == "ABNORMAL"  # 한 번 어긋난 것으로는 건너뛰지 않는다
    entries.save(load(ctx, log(tmp_path, "c2.log", [FK], start=30)), "NORMAL", "가상: 알림이 있어도 정상이었음")
    second = run(ctx, load(ctx, log(tmp_path, "t2.log", [FK], start=40)))
    assert not second.reference_judgment.used and "확인된 정상 로그 2개" in second.reference_judgment.skipped[0]
    assert second.verdict.state != "ABNORMAL"
