"""계획서 §15 개발 완료 정의 — 사용자 시나리오 1~15를 한 흐름으로 (가짜 Ollama, 인터넷 차단 상태).
화면 대신 응용 계층 함수를 같은 순서로 부른다. 화면 경로는 tests/ui에서 따로 확인한다."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from app.analysis.diagnostic_engine import AnalysisRequest, DiagnosticEngine
from app.bootstrap import initialize
from app.core.importer import LogImporter
from app.learning.abnormal_learner import AbnormalLearner
from app.learning.approval_workflow import ApprovalWorkflow
from app.learning.case_manager import CaseManager, ModeManager
from app.learning.normal_learner import NormalLearner
from app.learning.question_engine import QuestionEngine
from app.models.enums import OriginPurpose
from app.models.records import FileMeta, ImportRequest, SessionMode
from app.reports.report_data import save_reports
from app.storage.repositories import GuideRepository, PatternRepository
from tests.fake_ollama import FakeOllama
from tests.logdata import abnormal_log, normal_log
from tests.unit.test_guide_structurer import GUIDE, MANUAL


def test_development_completion_scenarios(tmp_path: Path) -> None:
    home = tmp_path / "home"
    ctx = initialize(home, configure_logging=False)
    ctx.settings = ctx.settings.model_copy(
        update={"ollama": ctx.settings.ollama.model_copy(update={"model": "test-model:7b"})})
    importer = LogImporter(ctx.database, ctx.paths, ctx.settings.import_)
    cases, workflow, questions = CaseManager(ctx.database), ApprovalWorkflow(ctx.database), QuestionEngine(ctx.database)
    mode_id = ModeManager(ctx.database).add("테스트모드A", "가상 운전 모드")
    meta = FileMeta(log_role="TEST-ROLE")

    # 1. 정상 로그 여러 개 등록, 메모
    normal_ids = importer.import_logs(ImportRequest(
        paths=[normal_log(tmp_path / "n1.log"), normal_log(tmp_path / "n2.log", start_second=8)],
        purpose=OriginPurpose.NORMAL_CASE, session_mode=SessionMode.SEPARATE, meta=meta)).log_file_ids
    normal = cases.create("NORMAL", "시나리오 정상", normal_ids)
    cases.add_note(normal, "가상 메모")
    # 2. AI 모드 추정 → 사용자 확인
    fake = FakeOllama([{"mode": "테스트모드A", "reason": "가상", "evidence": ["EV-0002"]}])
    guess = NormalLearner(ctx.database, ctx.settings, fake.factory).estimate_mode(normal)
    assert guess.mode_id == mode_id
    cases.set_mode(normal, guess.mode_id, confirmed=True)
    # 3. 정상 패턴 후보 + AI 초안
    patterns = NormalLearner(ctx.database, ctx.settings).propose(normal)
    fake.push({"summary": "가상 정상 흐름", "observations": [{"text": "TEST-START가 먼저", "kind": "FACT", "evidence": ["EV-0002"]}],
               "missing_info": [{"text": "TEST-READY가 항상 나오나요?", "trigger": "NORMAL_RATIONALE_MISSING"}]})
    assert NormalLearner(ctx.database, ctx.settings, fake.factory).generate_draft(normal).status == "OK"
    # 4. 부족한 정보 질문 (규칙 + AI)
    open_q = questions.refresh(normal)
    assert any(q.trigger == "NORMAL_RATIONALE_MISSING" and q.required for q in open_q)
    assert any(q.generated_by == "AI" for q in open_q)
    # 5. 답변·승인
    rationale = next(q for q in open_q if q.trigger == "NORMAL_RATIONALE_MISSING" and q.generated_by == "RULE")
    questions.answer(rationale.id, "TEXT", text="가상: 사용자가 정상 동작을 직접 확인")
    patterns = PatternRepository  # 이름 재사용 방지용
    with closing(ctx.database.connect()) as conn:
        own = PatternRepository(conn).for_case(normal)
    workflow.approve(normal, pattern_decisions={p.id: "APPROVED" if p.pattern_type in ("REQUIRED", "ORDER_CONSTRAINT")
                                                else "REJECTED" for p in own})

    # 6. 비정상 로그 여러 개 + 진단 가이드 (+ CR-01 대응 매뉴얼)
    abnormal_ids = importer.import_logs(ImportRequest(
        paths=[abnormal_log(tmp_path / "a1.log"), abnormal_log(tmp_path / "a2.log", start_second=4)],
        purpose=OriginPurpose.ABNORMAL_CASE, session_mode=SessionMode.SAME, meta=meta)).log_file_ids
    abnormal = cases.create("ABNORMAL", "시나리오 비정상", abnormal_ids)
    learner = AbnormalLearner(ctx.database, ctx.paths, ctx.settings)
    for name, text, kind in (("g.md", GUIDE, "DIAGNOSTIC_GUIDE"), ("m.txt", MANUAL, "RESPONSE_MANUAL")):
        doc = tmp_path / name
        doc.write_bytes(text.encode("utf-8"))
        learner.attach(abnormal, doc, kind)
    # 7. 가이드 구조화 + 확인 질문
    ab_q = {q.trigger: q for q in questions.refresh(abnormal)}
    assert ab_q["CAUSE_STATUS_UNKNOWN"].required
    questions.answer(ab_q["CAUSE_STATUS_UNKNOWN"].id, "CHOICE", value="SUSPECTED")
    cases.update(abnormal, **learner.suggest_fields(abnormal))
    # 8. 비정상 사례 승인
    with closing(ctx.database.connect()) as conn:
        items = {r["id"]: "APPROVED" for r in GuideRepository(conn).items(abnormal)}
    workflow.approve(abnormal, guide_decisions=items)

    # 9~12. 신규 로그 입력 → 사례 검색 → 이벤트·시퀀스 비교 → Ollama 근거 기반 가설
    new_ids = importer.import_logs(ImportRequest(paths=[abnormal_log(tmp_path / "new.log", start_second=30)],
                                                 purpose=OriginPurpose.ANALYSIS, meta=meta)).log_file_ids
    engine = DiagnosticEngine(ctx.database, ctx.paths, ctx.settings, FakeOllama().factory)
    probe = engine.run(AnalysisRequest(new_ids, mode_id=mode_id, mode_confirmed=True, use_ai=False))
    key = {f.finding_type: probe.keys[i] for i, f in reversed(list(enumerate(probe.findings)))}
    answer = {"observations": [{"text": "TEST-FAULT 이벤트가 기록됐다", "evidence": [key["EXPLICIT_ERROR"]]}],
              "hypotheses": [{"text": "승인 사례와 같은 문제일 수 있다", "evidence": [key["SIMILAR_ABNORMAL_CASE"]],
                              "counter_evidence": []}],
              "response_plan": [{"text": "매뉴얼 1단계를 따른다", "evidence": [key["RESPONSE_REFERENCE"]]}],
              "unknowns": ["TEST-RETRY의 뜻은 등록된 지식이 없다"]}
    engine = DiagnosticEngine(ctx.database, ctx.paths, ctx.settings, FakeOllama([answer]).factory)
    result = engine.run(AnalysisRequest(new_ids, mode_id=mode_id, mode_confirmed=True))
    assert result.similar_abnormal[0].case.id == abnormal  # 10. 사례 검색
    assert {"MISSING_EVENT", "EXPLICIT_ERROR"} <= {f.finding_type for f in result.findings}  # 11. 비교
    assert result.ai.status == "OK"  # 12. 근거 기반 가설
    assert result.guidance.level == "MANUAL"  # CR-01: CS 엔지니어가 볼 권장 대응
    # 13. 보고서 저장
    rel_json, rel_html = save_reports(ctx.database, ctx.paths, result.run_id)
    assert (ctx.paths.root / rel_html).exists()
    ctx.close()

    # 14. 종료·재실행 후 승인 데이터 유지
    again = initialize(home, configure_logging=False)
    try:
        approved = {c.id for c in CaseManager(again.database).list() if c.status == "APPROVED"}
        assert approved == {normal, abnormal}
        with closing(again.database.connect()) as conn:
            assert PatternRepository(conn).approved_for_mode(mode_id)
    finally:
        again.close()
    # 15. 인터넷 없이 전체 과정: 이 테스트 전체가 외부 소켓 차단(conftest) 아래에서 돌았다.
