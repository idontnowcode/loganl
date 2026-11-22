"""Phase 8 보고서·백업·복원 (NA-10·19, STO-09, G-7, UI-13)."""

from __future__ import annotations

import json
import os
import zipfile
from contextlib import closing
from pathlib import Path

import pytest

from app import __version__
from app.analysis.diagnostic_engine import AnalysisRequest, DiagnosticEngine
from app.learning.case_manager import CaseManager
from app.models.enums import OriginPurpose
from app.reports.html_exporter import render_html
from app.reports.report_data import load_report, save_reports
from app.storage.backup import BackupError, create_backup, inspect_backup, restore_backup
from app.storage.repositories import AnalysisRepository
from tests.integration.test_analysis import ctx, knowledge, load  # noqa: F401
from tests.logdata import abnormal_log, iso_lines, write


def analyzed(ctx, knowledge, tmp_path, name="bad.log", lines=None):  # noqa: F811
    path = write(tmp_path / name, lines) if lines else abnormal_log(tmp_path / name, start_second=70)
    ids = load(ctx, [path], OriginPurpose.ANALYSIS)
    return DiagnosticEngine(ctx.database, ctx.paths, ctx.settings).run(
        AnalysisRequest(ids, mode_id=knowledge[0], mode_confirmed=True, use_ai=False))


def test_report_is_rebuilt_from_db(ctx, knowledge, tmp_path: Path) -> None:  # noqa: F811
    result = analyzed(ctx, knowledge, tmp_path)
    data = load_report(ctx.database, result.run_id)
    assert data["run"]["label"] == f"R-{result.run_id:04d}"
    assert data["axes"]["response_guidance"]["text"] == "승인된 대응 매뉴얼"
    assert data["guidance"]["items"][0]["label"] == "주의" and data["guidance"]["items"][1]["label"] == "1. 절차"
    assert all(item["case"] for item in data["guidance"]["items"])  # 출처 사례
    assert {f["type"] for f in data["findings"]} >= {"EXPLICIT_ERROR", "MISSING_EVENT", "RESPONSE_REFERENCE"}
    assert "확률이 아니다" in data["notice"] and data["files"][0]["sha256"]


def test_saved_reports_and_html_safety(ctx, knowledge, tmp_path: Path) -> None:  # noqa: F811
    lines = iso_lines([("ERROR", "TEST-SUB", "TEST-FAULT code=TEST-E001 <script>alert('x')</script> & <b>")])
    result = analyzed(ctx, knowledge, tmp_path, "html.log", lines)
    rel_json, rel_html = save_reports(ctx.database, ctx.paths, result.run_id)
    html = (ctx.paths.root / rel_html).read_text(encoding="utf-8")
    assert "<script" not in html.lower() and "&lt;script&gt;" in html  # 원문 태그는 실행되지 않게
    assert "http://" not in html and "https://" not in html and "src=" not in html  # 외부 자원 없음(오프라인)
    data = json.loads((ctx.paths.root / rel_json).read_text(encoding="utf-8"))
    assert data["run"]["id"] == result.run_id
    with closing(ctx.database.connect()) as conn:
        run = AnalysisRepository(conn).get(result.run_id)
    assert run["report_json_path"] == rel_json and run["report_html_path"] == rel_html
    assert render_html(data).startswith("<!doctype html>")


def test_backup_and_restore_round_trip(ctx, knowledge, tmp_path: Path) -> None:  # noqa: F811
    info = create_backup(ctx.paths, ctx.database, __version__)
    assert info.path.exists() and info.schema_version == 9
    manifest = inspect_backup(info.path)
    assert "database/knowledge.db" in manifest["files"] and any(n.startswith("cases/") for n in manifest["files"])

    cases = CaseManager(ctx.database)
    later = cases.create("NORMAL", "백업 뒤에 만든 사례")
    safety = restore_backup(info.path, ctx.paths, ctx.database, __version__)
    assert safety.exists() and safety.name.startswith("pre-restore-")
    assert all(c.id != later for c in cases.list())  # 백업 시점으로 돌아갔다
    aside = [p for p in ctx.paths.backups.iterdir() if p.is_dir() and p.name.endswith("-files")]
    assert aside and (aside[0] / "cases").exists()  # 지우지 않고 옮겼다
    originals = [p for p in ctx.paths.cases_normal.rglob("*") if p.is_file()]
    assert originals and all(not os.access(p, os.W_OK) for p in originals)  # 원본 사본은 다시 읽기 전용
    assert inspect_backup(safety)["schema_version"] == 9  # 복원 직전 상태도 복원 가능한 백업


def test_tampered_or_foreign_backup_is_refused(ctx, knowledge, tmp_path: Path) -> None:  # noqa: F811
    info = create_backup(ctx.paths, ctx.database, __version__)
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(info.path) as src, zipfile.ZipFile(tampered, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            dst.writestr(item, b"changed" if item.filename.startswith("cases/") else data)
    with pytest.raises(BackupError, match="손상"):
        inspect_backup(tampered)
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"not a zip")
    with pytest.raises(BackupError, match="zip"):
        inspect_backup(junk)
    no_manifest = tmp_path / "plain.zip"
    with zipfile.ZipFile(no_manifest, "w") as z:
        z.writestr("a.txt", "x")
    with pytest.raises(BackupError, match="manifest"):
        inspect_backup(no_manifest)
