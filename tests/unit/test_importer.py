"""로그 가져오기 (Phase 2: 복수 파일·인코딩·해시·원본 보존·세션·파싱·원본 행 연결, STO-03·06, MLG-02)."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.bootstrap import AppContext, initialize
from app.config import ImportSettings
from app.core.cancel import CancelToken
from app.core.importer import ImportRejected, LogImporter
from app.models.enums import OriginPurpose, TimestampBasis
from app.models.records import FileMeta, ImportRequest, SessionMode
from app.storage.repositories import EventRepository, LogFileRepository, SessionRepository
from tests.logdata import abnormal_log, iso_lines, normal_log, write


@pytest.fixture
def ctx(tmp_path: Path):
    context = initialize(tmp_path / "home", configure_logging=False)
    yield context
    context.close()


def importer(ctx: AppContext, **limits) -> LogImporter:
    settings = ImportSettings(**limits) if limits else ctx.settings.import_
    return LogImporter(ctx.database, ctx.paths, settings)


def request(paths, mode=SessionMode.UNKNOWN, purpose=OriginPurpose.NORMAL_CASE, **kw) -> ImportRequest:
    return ImportRequest(paths=list(paths), purpose=purpose, session_mode=mode, **kw)


def test_same_session_import(ctx: AppContext, tmp_path: Path) -> None:
    a = normal_log(tmp_path / "in" / "a.log")
    b = abnormal_log(tmp_path / "in" / "b.log", tz="Z")
    result = importer(ctx).import_logs(request([a, b], SessionMode.SAME, meta=FileMeta(log_role="TEST-ROLE", fw_version="T1")))
    assert result.rejected == [] and not result.cancelled
    assert len(result.files) == 2
    assert result.files[0].session_id == result.files[1].session_id
    with closing(ctx.database.connect()) as conn:
        session = SessionRepository(conn).get(result.files[0].session_id)
        records = LogFileRepository(conn).get_many(result.log_file_ids)
        counts = [EventRepository(conn).count(i) for i in result.log_file_ids]
    assert session.relation_confirmed is True
    assert counts == [6, 6]  # 머리 줄 + 5줄
    assert [r.timestamp_basis for r in records] == [TimestampBasis.ABSOLUTE_NO_TZ, TimestampBasis.ABSOLUTE]
    assert all(r.encoding == "utf-8" and r.line_count == 6 and r.parser_profile == "generic_iso_level" for r in records)
    assert all(r.log_role == "TEST-ROLE" and r.fw_version == "T1" for r in records)


def test_stored_copy_is_identical_and_read_only(ctx: AppContext, tmp_path: Path) -> None:
    source = normal_log(tmp_path / "in" / "keep.log")
    before = (source.read_bytes(), source.stat().st_mtime_ns)
    result = importer(ctx).import_logs(request([source]))
    with closing(ctx.database.connect()) as conn:
        record = LogFileRepository(conn).get(result.files[0].log_file_id)
    stored = ctx.paths.root / record.stored_path
    assert stored.read_bytes() == source.read_bytes()
    assert not os.access(stored, os.W_OK)
    assert stored.is_relative_to(ctx.paths.cases_normal)
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before  # 원본은 건드리지 않는다


@pytest.mark.parametrize("mode, confirmed", [(SessionMode.SEPARATE, True), (SessionMode.UNKNOWN, False)])
def test_separate_sessions(ctx: AppContext, tmp_path: Path, mode: SessionMode, confirmed: bool) -> None:
    a = normal_log(tmp_path / "a.log")
    b = normal_log(tmp_path / "b.log", start_second=30)
    result = importer(ctx).import_logs(request([a, b], mode))
    ids = {f.session_id for f in result.files}
    assert len(ids) == 2
    with closing(ctx.database.connect()) as conn:
        assert {SessionRepository(conn).get(i).relation_confirmed for i in ids} == {confirmed}


def test_existing_session(ctx: AppContext, tmp_path: Path) -> None:
    first = importer(ctx).import_logs(request([normal_log(tmp_path / "a.log")], SessionMode.SAME))
    session_id = first.files[0].session_id
    second = importer(ctx).import_logs(
        request([abnormal_log(tmp_path / "b.log")], SessionMode.EXISTING, existing_session_id=session_id))
    assert second.files[0].session_id == session_id
    with pytest.raises(ImportRejected, match="세션"):
        importer(ctx).import_logs(request([tmp_path / "a.log"], SessionMode.EXISTING, existing_session_id=999))


def test_duplicate_content_reuses_record(ctx: AppContext, tmp_path: Path) -> None:
    a = normal_log(tmp_path / "a.log")
    copy = tmp_path / "copy_of_a.log"
    copy.write_bytes(a.read_bytes())
    first = importer(ctx).import_logs(request([a]))
    second = importer(ctx).import_logs(request([copy, a], SessionMode.SAME))
    assert [f.duplicate for f in second.files] == [True, True]
    assert {f.log_file_id for f in second.files} == {first.files[0].log_file_id}
    assert "이미 있어" in second.files[0].warnings[0]
    with closing(ctx.database.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM log_files").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 6


def test_rejections_do_not_stop_other_files(ctx: AppContext, tmp_path: Path) -> None:
    good = normal_log(tmp_path / "good.log")
    wrong_ext = write(tmp_path / "x.csv", ["a"])
    empty = tmp_path / "empty.log"
    empty.write_bytes(b"")
    binary = tmp_path / "bin.log"
    binary.write_bytes(b"\x00\x01\x02binary")
    missing = tmp_path / "missing.log"
    result = importer(ctx).import_logs(request([good, wrong_ext, empty, binary, missing]))
    assert [f.original_name for f in result.files] == ["good.log"]
    reasons = {Path(r.path).name: r.reason for r in result.rejected}
    assert set(reasons) == {"x.csv", "empty.log", "bin.log", "missing.log"}
    assert "확장자" in reasons["x.csv"] and "빈 파일" in reasons["empty.log"] and "바이너리" in reasons["bin.log"]
    stored = list(ctx.paths.cases_normal.rglob("*.log"))
    assert len(stored) == 1  # 거부된 바이너리 파일은 사본을 남기지 않는다


def test_file_size_limit(ctx: AppContext, tmp_path: Path) -> None:
    big = write(tmp_path / "big.log", ["x" * 100] * 30)  # 약 3KB
    result = importer(ctx, max_file_mb=0.001, max_total_mb=1).import_logs(request([big]))
    assert result.files == [] and "MB를 넘습니다" in result.rejected[0].reason


def test_total_limit_rejects_whole_request(ctx: AppContext, tmp_path: Path) -> None:
    a = write(tmp_path / "a.log", ["x" * 100] * 8)
    b = write(tmp_path / "b.log", ["y" * 100] * 8)
    with pytest.raises(ImportRejected, match="총 용량"):
        importer(ctx, max_file_mb=0.001, max_total_mb=0.0015).import_logs(request([a, b]))
    with closing(ctx.database.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM log_files").fetchone()[0] == 0


def test_raw_fallback_marks_timestamp_unknown(ctx: AppContext, tmp_path: Path) -> None:
    free = write(tmp_path / "free.txt", ["TEST free one", "TEST free two"])
    result = importer(ctx).import_logs(request([free]))
    imported = result.files[0]
    assert imported.parser_profile == "raw" and imported.timestamp_basis == TimestampBasis.UNKNOWN
    assert any("맞는 파서" in w for w in imported.warnings)


def test_korean_cp949_log(ctx: AppContext, tmp_path: Path) -> None:
    path = write(tmp_path / "k.log", iso_lines([("INFO", "TEST-CTRL", "가상 이벤트 시작")]), encoding="cp949")
    result = importer(ctx).import_logs(request([path]))
    with closing(ctx.database.connect()) as conn:
        events = EventRepository(conn).list(result.files[0].log_file_id)
    assert result.files[0].encoding == "cp949"
    assert events[-1].message == "가상 이벤트 시작"


def test_cancel_between_files(ctx: AppContext, tmp_path: Path) -> None:
    a, b = normal_log(tmp_path / "a.log"), abnormal_log(tmp_path / "b.log")
    token = CancelToken()
    calls = []

    def progress(done: int, total: int, message: str) -> None:
        calls.append(done)
        if done == 1:
            token.cancel()

    result = importer(ctx).import_logs(request([a, b]), cancel=token, progress=progress)
    assert result.cancelled and [f.original_name for f in result.files] == ["a.log"]
    assert len(list(ctx.paths.cases_normal.rglob("*.log"))) == 1


def test_cancel_during_parse_leaves_nothing(ctx: AppContext, tmp_path: Path, monkeypatch) -> None:
    path = normal_log(tmp_path / "a.log")
    token = CancelToken()
    from app.parsers.generic_parser import RegexProfileParser

    original = RegexProfileParser.parse

    def cancelling_parse(self, lines, cancel):
        token.cancel()
        return original(self, lines, cancel)

    monkeypatch.setattr(RegexProfileParser, "parse", cancelling_parse)
    result = importer(ctx).import_logs(request([path]), cancel=token)
    assert result.cancelled and result.files == []
    assert list(ctx.paths.cases_normal.rglob("*.log")) == []
    with closing(ctx.database.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM log_files").fetchone()[0] == 0


def test_db_failure_removes_copy(ctx: AppContext, tmp_path: Path, monkeypatch) -> None:
    path = normal_log(tmp_path / "a.log")

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("가짜 DB 오류")

    monkeypatch.setattr(EventRepository, "add_many", broken)
    result = importer(ctx).import_logs(request([path]))
    assert result.files == [] and "가짜 DB 오류" in result.rejected[0].reason
    assert list(ctx.paths.cases_normal.rglob("*.log")) == []
    with closing(ctx.database.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM log_files").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM collection_sessions").fetchone()[0] == 0


def test_import_is_audited(ctx: AppContext, tmp_path: Path) -> None:
    result = importer(ctx).import_logs(request([normal_log(tmp_path / "a.log")]))
    with closing(ctx.database.connect()) as conn:
        row = conn.execute("SELECT action, actor FROM audit_history WHERE entity_type = 'log_file'").fetchone()
    assert tuple(row) == ("IMPORT", "USER")
    assert result.files[0].event_count == 6


def test_analysis_purpose_goes_to_analysis_folder(ctx: AppContext, tmp_path: Path) -> None:
    result = importer(ctx).import_logs(request([normal_log(tmp_path / "a.log")], purpose=OriginPurpose.ANALYSIS))
    with closing(ctx.database.connect()) as conn:
        record = LogFileRepository(conn).get(result.files[0].log_file_id)
    assert record.stored_path.startswith("analysis/inputs/")
