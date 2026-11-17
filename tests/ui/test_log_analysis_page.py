"""신규 로그 분석 화면 (UI-05·16, NA-09·13·16, T-DONE-06)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models.enums import OriginPurpose
from app.ui.log_analysis import LogAnalysisPage
from tests.integration.test_analysis import ctx, knowledge, load  # noqa: F401 (픽스처 재사용)
from tests.logdata import abnormal_log


def test_analysis_page_shows_response_first(qtbot, ctx, knowledge, tmp_path: Path) -> None:  # noqa: F811
    mode_id, _, abnormal = knowledge
    page = LogAnalysisPage(ctx)
    qtbot.addWidget(page)
    page.refresh_modes()
    page.import_panel.set_log_file_ids(load(ctx, [abnormal_log(tmp_path / "bad.log", start_second=60)],
                                            OriginPurpose.ANALYSIS))
    page.mode_combo.setCurrentIndex(page.mode_combo.findData(mode_id))
    page.mode_confirm.setChecked(True)
    page.use_ai.setChecked(False)
    with qtbot.waitSignal(page.analysis_done, timeout=15_000):
        page.start_analysis()
    assert page.guidance_box.title() == "권장 대응 — 승인된 대응 매뉴얼 근거"
    first = page.guidance_list.item(0).text()
    assert first.startswith("[주의]") and f"C-{abnormal:04d}" in first
    assert any(page.guidance_list.item(i).text().startswith("[1. 절차] 가상 장비를") for i in range(page.guidance_list.count()))
    assert "승인된 규칙 위반" in page.axes_label.text() and "유사 사례 있음" in page.axes_label.text()
    assert page.findings_table.rowCount() == len(page.result.findings)

    row = next(i for i, f in enumerate(page.result.findings) if f.finding_type == "EXPLICIT_ERROR")
    assert page.jump_to_finding(row)  # 원본 행으로 이동
    assert page.import_panel.viewer.selected_event().line_no == page.result.findings[row].line_start
    assert page.run_button.isEnabled() and not page.cancel_button.isEnabled()


def test_ai_section_shows_hypotheses_and_labels(qtbot, ctx, knowledge, tmp_path: Path) -> None:  # noqa: F811
    from tests.fake_ollama import FakeOllama
    from tests.integration.test_diagnosis import good_answer, keys_for

    ctx.settings = ctx.settings.model_copy(
        update={"ollama": ctx.settings.ollama.model_copy(update={"model": "test-model:7b"})})
    keys = keys_for(ctx, knowledge, tmp_path / "probe")
    fake = FakeOllama([good_answer(keys)])
    page = LogAnalysisPage(ctx, fake.factory)
    qtbot.addWidget(page)
    page.import_panel.set_log_file_ids(load(ctx, [abnormal_log(tmp_path / "bad.log", start_second=70)],
                                            OriginPurpose.ANALYSIS))
    page.mode_combo.setCurrentIndex(page.mode_combo.findData(knowledge[0]))
    page.mode_confirm.setChecked(True)
    with qtbot.waitSignal(page.analysis_done, timeout=15_000):
        page.start_analysis()
    text = page.ai_view.toPlainText()
    assert "■ 원인 가설" in text and "반대 근거" in text
    assert "(AI 제안 확인 절차 — 매뉴얼 근거 없음)" in text  # 근거 없는 확인 절차는 따로 표시
    assert "■ AI가 정리한 대응 순서 (모두 승인된 매뉴얼·가이드 근거)" in text
    assert "사용자 검토 필요" in page.ai_status.text()


def test_empty_request_is_explained(qtbot, ctx) -> None:  # noqa: F811
    page = LogAnalysisPage(ctx)
    qtbot.addWidget(page)
    page.start_analysis()
    assert "먼저 올리세요" in page.notes_label.text()
