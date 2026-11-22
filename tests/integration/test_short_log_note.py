"""CR-13 DT-20: 이벤트가 아주 적은 로그는 결과 메모에 '비교 근거가 적음'을 적는다."""

from __future__ import annotations

from pathlib import Path

from app.analysis.diagnostic_engine import SHORT_LOG_EVENTS, AnalysisRequest, DiagnosticEngine
from app.models.enums import OriginPurpose
from tests.integration.test_analysis import ctx, knowledge, load  # noqa: F401 (픽스처 재사용)
from tests.logdata import abnormal_log


def test_short_log_gets_note(ctx, knowledge, tmp_path: Path) -> None:  # noqa: F811
    ids = load(ctx, [abnormal_log(tmp_path / "short.log", start_second=60)], OriginPurpose.ANALYSIS)
    result = DiagnosticEngine(ctx.database, ctx.paths, ctx.settings).run(AnalysisRequest(ids, use_ai=False))
    note = next(n for n in result.notes if "로그가 짧아" in n)
    assert f"이벤트 {SHORT_LOG_EVENTS}개 미만" in note and "short.log" in note
