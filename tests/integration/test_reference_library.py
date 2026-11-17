"""CR-04 참고 자료 올리기·고치기·승인·보관·분석 반영 (REF-01·03·04·05). 모든 코드·설명은 가상이다."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.analysis.diagnostic_engine import AnalysisRequest, DiagnosticEngine
from app.analysis.reference_matcher import match_references
from app.bootstrap import initialize
from app.core.importer import LogImporter
from app.learning.reference_library import ReferenceError, ReferenceLibrary
from app.models.enums import OriginPurpose
from app.models.records import ImportRequest, SessionMode
from app.storage.repositories import AnalysisRepository, ReferenceRepository
from tests.fake_ollama import FakeOllama
from tests.logdata import abnormal_log, iso_lines, write
from tests.refdata import TEXT_DOC, write_bytes, write_xlsx


@pytest.fixture
def ctx(tmp_path: Path):
    context = initialize(tmp_path / "home", configure_logging=False)
    context.settings = context.settings.model_copy(
        update={"ollama": context.settings.ollama.model_copy(update={"model": "test-model:7b"})})
    yield context
    context.close()


@pytest.fixture
def library(ctx) -> ReferenceLibrary:
    return ReferenceLibrary(ctx.database, ctx.paths, ctx.settings.import_)


def logs(ctx, paths) -> list[int]:
    return LogImporter(ctx.database, ctx.paths, ctx.settings.import_).import_logs(ImportRequest(
        paths=list(paths), purpose=OriginPurpose.ANALYSIS, session_mode=SessionMode.SEPARATE)).log_file_ids


def test_import_copies_original_and_extracts(ctx, library, tmp_path: Path) -> None:  # REF-01·02
    source = write_xlsx(tmp_path / "가상 코드표.xlsx")
    outcome = library.import_file(source)
    assert not outcome.duplicate and outcome.entries == 3
    doc = library.document(outcome.document_id)
    assert (doc["title"], doc["format"], doc["status"], doc["included_count"]) == ("가상 코드표", "XLSX", "DRAFT", 3)
    stored = ctx.paths.root / doc["stored_path"]
    assert stored.read_bytes() == source.read_bytes() and doc["stored_path"].startswith("attachments/references/")
    again = library.import_file(source)
    assert again.duplicate and again.document_id == outcome.document_id  # 같은 파일은 기존 문서를 연다
    assert "표가 깨질" not in " ".join(outcome.warnings)


def test_rejects_unsupported_and_too_big(ctx, library, tmp_path: Path) -> None:
    with pytest.raises(ReferenceError, match="지원하지 않는"):
        library.import_file(write_bytes(tmp_path / "a.xls", b"x"))
    small = ReferenceLibrary(ctx.database, ctx.paths, ctx.settings.import_.model_copy(update={"max_file_mb": 0.001}))
    with pytest.raises(ReferenceError, match="너무 큽니다"):
        small.import_file(write_bytes(tmp_path / "big.txt", ("TEST-E001: 가상\n" * 200).encode("utf-8")))


def test_draft_editing_and_column_roles(ctx, library, tmp_path: Path) -> None:  # REF-02·03
    doc_id = library.import_file(write_xlsx(tmp_path / "codes.xlsx")).document_id
    entries = library.entries(doc_id)
    library.update_entry(entries[0]["id"], description="고친 가상 설명", code=" test-e001 ")
    library.update_entry(entries[2]["id"], status="EXCLUDED")
    user_id = library.add_entry(doc_id, "TEST-E099", description="직접 더한 가상 설명")
    assert ReferenceRepository  # noqa: B018
    rows = {r["code"]: r for r in library.entries(doc_id)}
    assert rows["test-e001"]["code_norm"] == "TEST-E001" and rows["test-e001"]["description"] == "고친 가상 설명"
    assert rows["TEST-W010"]["status"] == "EXCLUDED" and rows["TEST-E099"]["source_kind"] == "USER"
    count = library.set_column_map(doc_id, "코드표", {"header_row": 0, "code": 0, "description": 2, "action": 4})
    assert count == 3
    rows = {r["code"]: r for r in library.entries(doc_id)}
    assert rows["TEST-E001"]["title"] is None and rows["TEST-E001"]["action"] == "가상 조치 A"  # 열 역할대로 다시 뽑음
    assert user_id in {r["id"] for r in library.entries(doc_id)}  # 직접 더한 항목은 남는다
    with pytest.raises(ReferenceError, match="코드 열"):
        library.set_column_map(doc_id, "코드표", {"header_row": 0, "description": 2})


def test_approve_locks_and_new_version_archives_old(ctx, library, tmp_path: Path) -> None:  # REF-03
    first = library.import_file(write_bytes(tmp_path / "v1.txt", TEXT_DOC.encode("utf-8")), title="가상 사전").document_id
    library.approve(first)
    assert library.document(first)["status"] == "APPROVED"
    with pytest.raises(ReferenceError, match="고칠 수 없습니다"):
        library.add_entry(first, "TEST-E100")
    with closing(ctx.database.connect()) as conn, pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE reference_entries SET description = 'x' WHERE document_id = ?", (first,))  # DB 트리거
    second = library.import_file(write_bytes(tmp_path / "v2.txt", (TEXT_DOC + "TEST-E004: 가상 설명 D\n").encode("utf-8")),
                                 title="가상 사전").document_id
    assert library.approve(second) == [first]
    assert library.document(first)["status"] == "ARCHIVED" and library.document(second)["replaces_id"] == first
    assert [d["id"] for d in library.documents()] == [second]
    assert len(library.documents(include_archived=True)) == 2


def test_cannot_approve_empty_and_delete_draft(ctx, library, tmp_path: Path) -> None:
    doc_id = library.import_file(write_bytes(tmp_path / "prose.txt", "가상 문장만 있음\n".encode("utf-8"))).document_id
    assert library.document(doc_id)["included_count"] == 0
    with pytest.raises(ReferenceError, match="읽은 문단도 없습니다"):
        library.approve(doc_id)
    stored = ctx.paths.root / library.document(doc_id)["stored_path"]
    library.delete_draft(doc_id)
    assert not stored.exists() and library.documents() == []


def test_analysis_shows_codes_from_approved_references_only(ctx, library, tmp_path: Path) -> None:  # REF-04
    log = write(tmp_path / "new.log", iso_lines([
        ("INFO", "TEST-CTRL", "TEST-START step=1"),
        ("ERROR", "TEST-SUB", "TEST-FAULT code=TEST-E001 value=7"),
        ("ERROR", "TEST-SUB", "TEST-FAULT code=TEST-E001 value=8"),
        ("WARN", "TEST-CTRL", "see TEST-W010 in message"),
        ("INFO", "TEST-CTRL", "count 1001 done"),
    ]))
    ids = logs(ctx, [log])
    doc_id = library.import_file(write_xlsx(tmp_path / "codes.xlsx", rows=[
        ("코드", "명칭", "설명", "원인", "조치"),
        ("TEST-E001", "가상 오류 A", "가상 설명 A", "가상 원인 A", "가상 조치 A"),
        ("TEST-W010", "가상 경고", "가상 설명 W", "", ""),
        ("1001", "숫자 코드", "메시지 안 숫자는 맞추지 않음", "", ""),
    ])).document_id
    assert match_references(ctx.database, ids) == []  # 승인 전에는 쓰지 않는다
    library.approve(doc_id)
    matches = {m.code_norm: m for m in match_references(ctx.database, ids)}
    assert set(matches) == {"TEST-E001", "TEST-W010"}  # 숫자만인 코드는 메시지 안에서 맞추지 않는다
    e001 = matches["TEST-E001"]
    assert (e001.count, e001.via, e001.first_line) == (2, "ERROR_CODE", 3)
    assert e001.entries[0].action == "가상 조치 A" and e001.entries[0].source() == "codes · 코드표 2행"
    assert matches["TEST-W010"].via == "MESSAGE"

    result = DiagnosticEngine(ctx.database, ctx.paths, ctx.settings).run(AnalysisRequest(ids, use_ai=False))
    assert {m.code_norm for m in result.references} == {"TEST-E001", "TEST-W010"}
    assert result.guidance.level == "NONE"  # 권장 대응(CR-01)과 섞지 않는다
    with closing(ctx.database.connect()) as conn:
        stored = [r for r in AnalysisRepository(conn).evidence(result.run_id) if r["finding_type"] == "TEXT_MATCH"]
    assert len(stored) == 2 and all('"REFERENCE_MATCH"' in r["detail_json"] for r in stored)


def test_ai_gets_reference_as_knowledge(ctx, library, tmp_path: Path) -> None:  # REF-05
    ids = logs(ctx, [abnormal_log(tmp_path / "a.log")])
    doc_id = library.import_file(write_bytes(tmp_path / "codes.txt", "TEST-E001: 가상 설명 A\n".encode("utf-8"))).document_id
    library.approve(doc_id)
    fake = FakeOllama([{"observations": [], "hypotheses": [], "related_guides": [], "response_plan": [],
                        "verification_steps": [], "unknowns": ["가상: 판단 불가"]}])
    result = DiagnosticEngine(ctx.database, ctx.paths, ctx.settings, fake.factory).run(AnalysisRequest(ids, use_ai=True))
    prompt = fake.last_prompt()
    assert "REFERENCE" in prompt and "[참고 자료] 코드 TEST-E001" in prompt and "설명 가상 설명 A" in prompt
    assert "REFERENCE" in result.ai.kinds.values()


GENERAL_DOC = """# 가상 이슈 설명서 (시험용, 실제 장비 의미 아님)

## 재시도 반복 이슈
TEST-RETRY가 짧은 간격으로 반복되면 가상 부품 연결이 느슨한 경우가 있었다.
가상 해결 기록: 가상 커넥터를 다시 끼운 뒤 정상으로 돌아왔다.

## 화면 표시 이슈
가상 화면 밝기가 낮아지는 현상은 이 로그와 관계없는 가상 설명이다.
"""


def test_general_document_passages_are_used_in_analysis(ctx, library, tmp_path: Path) -> None:  # CR-07 REF-07
    outcome = library.import_file(write_bytes(tmp_path / "issues.md", GENERAL_DOC.encode("utf-8")), title="가상 이슈 설명서")
    assert outcome.entries == 0 and outcome.passages >= 2 and "일반 문서" in " ".join(outcome.warnings)
    passages = library.passages(outcome.document_id)
    assert any("TEST-RETRY가 짧은 간격으로" in p["body"] for p in passages) and passages[0]["locator"]
    ids = logs(ctx, [abnormal_log(tmp_path / "a.log")])
    from app.analysis.reference_matcher import match_passages

    assert match_passages(ctx.database, ids) == []  # 승인 전에는 쓰지 않는다
    library.approve(outcome.document_id)  # 코드 항목이 없어도 문단이 있으면 승인된다
    hits = match_passages(ctx.database, ids)
    assert hits and "TEST-RETRY" in hits[0].body and "TEST-RETRY" in hits[0].matched
    assert all("화면 밝기" not in h.body for h in hits)  # 겹치는 낱말이 없는 문단은 빼고
    result = DiagnosticEngine(ctx.database, ctx.paths, ctx.settings).run(AnalysisRequest(ids, use_ai=False))
    assert result.passages and result.passages[0].document_title == "가상 이슈 설명서"
    with closing(ctx.database.connect()) as conn:
        roles = [r["detail_json"] for r in AnalysisRepository(conn).evidence(result.run_id) if r["finding_type"] == "TEXT_MATCH"]
    assert any('"REFERENCE_PASSAGE"' in d for d in roles)
    with closing(ctx.database.connect()) as conn, pytest.raises(sqlite3.DatabaseError):  # 승인 뒤 문단 잠금
        conn.execute("DELETE FROM reference_passages WHERE document_id = ?", (outcome.document_id,))


def test_code_lines_are_not_repeated_as_passages(ctx, library, tmp_path: Path) -> None:
    outcome = library.import_file(write_bytes(tmp_path / "codes.txt", TEXT_DOC.encode("utf-8")))
    bodies = " ".join(p["body"] for p in library.passages(outcome.document_id))
    assert "TEST-E001: 가상 설명 A" not in bodies and "가상 설명 C 첫 줄" not in bodies  # 코드 항목으로 쓴 줄은 문단에서 뺀다
    assert "가상 코드 사전" in bodies  # 코드가 아닌 글은 문단으로 남는다
