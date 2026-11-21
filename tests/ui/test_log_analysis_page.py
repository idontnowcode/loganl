"""신규 로그 분석 화면 (UI-05·16, NA-09·13·16, T-DONE-06)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models.enums import OriginPurpose
from app.ui.log_analysis import LogAnalysisPage
from tests.integration.test_analysis import ctx, knowledge, load  # noqa: F401 (픽스처 재사용)
from tests.logdata import abnormal_log


def keys_without_mode(ctx, tmp_path: Path) -> dict[str, str]:  # noqa: F811
    """화면과 같은 입력(운전 모드 없음)으로 결정론만 한 번 돌려 근거 키를 얻는다(같은 입력이면 같은 키)."""
    from app.analysis.diagnostic_engine import AnalysisRequest, DiagnosticEngine
    from tests.integration.test_diagnosis import key_of

    ids = load(ctx, [abnormal_log(tmp_path / "bad.log", start_second=70)], OriginPurpose.ANALYSIS)
    result = DiagnosticEngine(ctx.database, ctx.paths, ctx.settings).run(AnalysisRequest(ids, use_ai=False))
    return {
        "error": key_of(result, "EXPLICIT_ERROR"),
        "missing": key_of(result, "SIMILAR_NORMAL_CASE"),  # 반대 근거 자리(모드 없이는 승인 패턴 누락이 없다)
        "case": key_of(result, "SIMILAR_ABNORMAL_CASE"),
        "cause": key_of(result, "GUIDE_REFERENCE", lambda f: f.detail.get("section") == "CAUSE"),
        "step": key_of(result, "RESPONSE_REFERENCE", lambda f: f.detail.get("section") == "RESPONSE_STEP"),
    }


def test_analysis_page_shows_response_first(qtbot, ctx, knowledge, tmp_path: Path) -> None:  # noqa: F811
    _mode_id, _, abnormal = knowledge
    page = LogAnalysisPage(ctx)
    qtbot.addWidget(page)
    page.import_panel.set_log_file_ids(load(ctx, [abnormal_log(tmp_path / "bad.log", start_second=60)],
                                            OriginPurpose.ANALYSIS))
    page.use_ai.setChecked(False)  # CR-09: 운전 모드 없이 분석
    with qtbot.waitSignal(page.analysis_done, timeout=15_000):
        page.start_analysis()
    assert page.guidance_box.title() == "권장 대응 — 승인된 대응 매뉴얼 근거"
    first = page.guidance_list.item(0).text()
    assert first.startswith("[주의]") and f"C-{abnormal:04d}" in first
    assert any(page.guidance_list.item(i).text().startswith("[1. 절차] 가상 장비를") for i in range(page.guidance_list.count()))
    assert "비정상 의심" in page.axes_label.text() and "유사 사례 있음" in page.axes_label.text()
    assert page.findings_table.rowCount() == len(page.result.findings)

    row = next(i for i, f in enumerate(page.result.findings) if f.finding_type == "EXPLICIT_ERROR")
    assert page.jump_to_finding(row)  # 원본 행으로 이동
    assert page.import_panel.viewer.selected_event().line_no == page.result.findings[row].line_start
    assert page.run_button.isEnabled() and not page.cancel_button.isEnabled()


def test_ai_section_shows_hypotheses_and_labels(qtbot, ctx, knowledge, tmp_path: Path) -> None:  # noqa: F811
    from tests.fake_ollama import FakeOllama
    from tests.integration.test_diagnosis import good_answer

    ctx.settings = ctx.settings.model_copy(
        update={"ollama": ctx.settings.ollama.model_copy(update={"model": "test-model:7b"})})
    keys = keys_without_mode(ctx, tmp_path / "probe")
    answer = good_answer(keys)
    answer["hypotheses"][0]["likelihood"] = 40  # CR-07 AO-01: 가능성(AI 추정치)
    answer.update(abnormal_likelihood=75, likelihood_reason="가상 이유", likelihood_evidence=[keys["error"]])
    fake = FakeOllama([answer])
    page = LogAnalysisPage(ctx, fake.factory)
    qtbot.addWidget(page)
    page.import_panel.set_log_file_ids(load(ctx, [abnormal_log(tmp_path / "bad.log", start_second=70)],
                                            OriginPurpose.ANALYSIS))
    with qtbot.waitSignal(page.analysis_done, timeout=15_000):
        page.start_analysis()
    text = page.ai_view.toPlainText()
    assert "■ 원인 가설" in text and "반대 근거" in text
    assert "(AI 제안 확인 절차 — 매뉴얼 근거 없음)" in text  # 근거 없는 확인 절차는 따로 표시
    assert "■ AI가 정리한 대응 순서 (모두 승인된 매뉴얼·가이드 근거)" in text
    assert "사용자 검토 필요" in page.ai_status.text() and "통계 확률이 아닙니다" in page.ai_status.text()
    assert page.ai_likelihood.text() == "비정상일 가능성 75%" and not page.ai_likelihood.isHidden()
    assert " 40%  승인 사례와 같은 가상 원인 1일 수 있다" in text and " 60%  판단 불가" in text
    rel_json, _ = page.save_report()
    import json

    data = json.loads((ctx.paths.root / rel_json).read_text(encoding="utf-8"))
    assert data["ai"]["abnormal_likelihood"]["likelihood"] == 75
    assert data["ai"]["hypotheses"][0]["likelihood"] == 40 and len(data["ai"]["hypotheses"]) == 1


def test_empty_request_is_explained(qtbot, ctx) -> None:  # noqa: F811
    page = LogAnalysisPage(ctx)
    qtbot.addWidget(page)
    page.start_analysis()
    assert "먼저 올리세요" in page.notes_label.text()


def test_each_log_gets_its_own_result(qtbot, ctx, knowledge, tmp_path: Path) -> None:  # noqa: F811  CR-10 NA-20
    """로그가 여러 개면 로그마다 따로 분석하고, 왼쪽 목록에서 고르면 그 결과를 보인다(드롭다운 없음)."""
    from tests.logdata import normal_log

    page = LogAnalysisPage(ctx)
    qtbot.addWidget(page)
    page.use_ai.setChecked(False)
    bad, good = load(ctx, [abnormal_log(tmp_path / "bad.log", start_second=60),
                           normal_log(tmp_path / "good.log", start_second=30)], OriginPurpose.ANALYSIS)
    page.import_panel.set_log_file_ids([bad, good])
    assert page.run_button.text() == "분석 실행 (2개)"
    done = []
    page.analysis_done.connect(done.append)
    page.start_analysis()
    qtbot.waitUntil(lambda: len(done) == 2 and page.run_button.isEnabled(), timeout=20_000)
    assert done[0].run_id != done[1].run_id  # 분석 이력도 로그마다
    table = page.import_panel.file_table
    assert table.item(0, 1).text().startswith("비정상 의심") and table.item(1, 1).text() != "—"
    assert page.result is done[0] and page.result_title.text().startswith("결과 — bad.log")
    assert not page.result_hint.isHidden()
    page.import_panel.select_file(good)  # 목록에서 다른 로그 고르기
    assert page.result is done[1] and page.result_title.text().startswith("결과 — good.log")
    assert page.result_log_ids() == [good]
    assert page.run_button.text() == "모두 다시 분석 (2개)"
    page.import_panel.select_file(bad)
    assert page.result is done[0]


def test_only_new_logs_are_analyzed_and_case_uses_selected_log(qtbot, ctx, knowledge, tmp_path: Path) -> None:  # noqa: F811
    from tests.logdata import normal_log

    page = LogAnalysisPage(ctx)
    qtbot.addWidget(page)
    page.use_ai.setChecked(False)
    first = load(ctx, [abnormal_log(tmp_path / "bad.log", start_second=60)], OriginPurpose.ANALYSIS)
    page.import_panel.set_log_file_ids(first)
    with qtbot.waitSignal(page.analysis_done, timeout=15_000):
        page.start_analysis()
    second = load(ctx, [normal_log(tmp_path / "good.log", start_second=30)], OriginPurpose.ANALYSIS)
    page.import_panel.set_log_file_ids(first + second)
    assert page.run_button.text() == "분석 실행"  # 결과 없는 로그 1개만
    page.import_panel.select_file(second[0])
    assert page.result is None and "아직 분석하지 않았습니다" in page.guidance_message.text()
    assert not page.report_button.isEnabled()
    kept = page.results[first[0]]
    with qtbot.waitSignal(page.analysis_done, timeout=15_000) as signal:
        page.start_analysis()
    assert page.results[first[0]] is kept and page.results[second[0]] is signal.args[0]
    asked = []
    page.register_requested.connect(lambda *args: asked.append(args))
    page.request_case("NORMAL")
    assert asked[0][1] == second  # 고른 로그의 결과로 등록
    page.import_panel.clear_list()  # 목록 비우기(LI-12)
    assert page.results == {} and page.result is None and page.run_button.text() == "분석 실행"


def test_same_session_files_are_analyzed_together(qtbot, ctx, knowledge, tmp_path: Path) -> None:  # noqa: F811
    from app.models.records import SessionMode

    page = LogAnalysisPage(ctx)
    qtbot.addWidget(page)
    page.use_ai.setChecked(False)
    ids = load(ctx, [abnormal_log(tmp_path / "s1.log", start_second=80), abnormal_log(tmp_path / "s2.log", start_second=90)],
               OriginPurpose.ANALYSIS, SessionMode.SAME)
    page.import_panel.set_log_file_ids(ids)
    assert page.analysis_units() == [ids] and page.run_button.text() == "분석 실행"
    with qtbot.waitSignal(page.analysis_done, timeout=15_000):
        page.start_analysis()
    assert page.results[ids[0]] is page.results[ids[1]] and page.result_log_ids() == ids
    assert "같은 수집 세션" in page.result_title.text()


def test_abnormal_log_shows_presumed_error_messages(qtbot, ctx, knowledge, tmp_path: Path) -> None:  # noqa: F811
    """CR-11 EM-01·03: 비정상으로 보인 로그에는 문제로 보이는 원문 줄을, 정상으로 보인 로그에는 띄우지 않는다."""
    import json

    from tests.logdata import normal_log

    page = LogAnalysisPage(ctx)
    qtbot.addWidget(page)
    page.use_ai.setChecked(False)
    bad, good = load(ctx, [abnormal_log(tmp_path / "bad.log", start_second=60),
                           normal_log(tmp_path / "good.log", start_second=30)], OriginPurpose.ANALYSIS)
    page.import_panel.set_log_file_ids([bad, good])
    done = []
    page.analysis_done.connect(done.append)
    page.start_analysis()
    qtbot.waitUntil(lambda: len(done) == 2 and page.run_button.isEnabled(), timeout=20_000)
    assert not page.error_box.isHidden()
    texts = page.error_box.texts()
    assert texts[0].startswith("[에러로 기록된 줄] ") and "TEST-FAULT code=TEST-E001" in texts[0]  # 원문 그대로
    assert "이유: 오류 코드 TEST-E001" in texts[0]
    assert "예상 에러 메시지" in page.import_panel.file_table.item(0, 1).toolTip()  # 목록 칸 풀이(EM-03)
    message = page.error_box.messages[0]
    assert page.error_box.jump(0)  # 두 번 누르면 원본 행
    assert page.import_panel.viewer.selected_event().line_no == message.line
    rel_json, rel_html = page.save_report()
    data = json.loads((ctx.paths.root / rel_json).read_text(encoding="utf-8"))
    assert data["errors"][0]["text"] == message.text and data["errors"][0]["group"] == "ERROR"
    assert "예상 에러 메시지" in (ctx.paths.root / rel_html).read_text(encoding="utf-8")
    page.import_panel.select_file(good)
    if page.result.axes["anomaly_level"] in ("NONE_FOUND", "NOT_EVALUATED"):
        assert page.error_box.isHidden()  # 비정상으로 보이지 않으면 띄우지 않는다
        rel_json, _ = page.save_report()
        assert json.loads((ctx.paths.root / rel_json).read_text(encoding="utf-8"))["errors"] is None


def test_three_steps_lock_move_and_jump(qtbot, ctx, knowledge, tmp_path: Path) -> None:  # noqa: F811  CR-12 ST-02
    from tests.logdata import normal_log

    page = LogAnalysisPage(ctx)
    qtbot.addWidget(page)
    page.use_ai.setChecked(False)
    assert page.steps.current() == 0 and not page.steps.bar.buttons[1].isEnabled()  # 분석 전에는 ②·③ 잠김
    assert "분석 실행" in page.steps.bar.buttons[2].toolTip()
    bad, good = load(ctx, [abnormal_log(tmp_path / "bad.log", start_second=60),
                           normal_log(tmp_path / "good.log", start_second=30)], OriginPurpose.ANALYSIS)
    page.import_panel.set_log_file_ids([bad, good])
    page.import_panel.set_checked(good, False)  # 체크한 로그만 분석
    assert page.run_button.text() == "분석 실행"
    with qtbot.waitSignal(page.analysis_done, timeout=15_000):
        page.start_analysis()
    assert page.steps.current() == 1 and page.steps.bar.buttons[2].isEnabled()  # 시작하면 ② 분석 결과로
    assert good not in page.results and page.import_panel.status_of(good)[0] == "—"
    assert page.result_list.rowCount() == 2 and page.result_list.item(0, 1).text().startswith("비정상 의심")
    assert page.verdict.text() == "비정상 의심" and page.verdict.property("tone") == "warn"
    assert "에러로 기록된 줄" in page.evidence_summary.text() and "AI 가능성 없음" in page.evidence_summary.text()
    assert len(page.error_preview.messages) <= 3 and not page.error_preview.isHidden()
    assert page.guidance_detail_list.count() == page.guidance_list.count()  # ③에도 권장 대응
    page.result_list.setCurrentCell(1, 0)  # 목록에서 다른 로그 → 아직 결과 없음
    assert page.result is None and page.import_panel.current_file_id() == good
    page.detail_list.setCurrentCell(0, 0)
    assert page.result is page.results[bad]
    row = next(i for i, f in enumerate(page.result.findings) if f.finding_type == "EXPLICIT_ERROR")
    page.steps.go(1)
    assert page.jump_to_finding(row)  # 근거에서 원본 행 → ③으로 가서 로그 원문을 편다
    assert page.steps.current() == 2 and page.import_panel.views_section.is_expanded()
