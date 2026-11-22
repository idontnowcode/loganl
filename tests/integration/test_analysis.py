"""Phase 6 사례 검색·분석 (T-NA-01~08·10~13, DB-04, NA-04·05·12·13·16·17, 완료 기준 "신규 로그를 승인된 사례와 비교")."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from app.analysis.diagnostic_engine import AnalysisRequest, DiagnosticEngine
from app.bootstrap import initialize
from app.core.importer import LogImporter
from app.learning.abnormal_learner import AbnormalLearner
from app.learning.approval_workflow import ApprovalWorkflow
from app.learning.case_manager import CaseManager, ModeManager
from app.learning.normal_learner import NormalLearner
from app.models.enums import OriginPurpose
from app.models.records import FileMeta, ImportRequest, SessionMode
from app.storage.repositories import AnalysisRepository, GuideRepository, SearchRepository
from tests.logdata import NORMAL_SEQUENCE, abnormal_log, iso_lines, normal_log, write
from tests.unit.test_guide_structurer import GUIDE, MANUAL

ROLE = FileMeta(log_role="TEST-ROLE")


@pytest.fixture
def ctx(tmp_path: Path):
    context = initialize(tmp_path / "home", configure_logging=False)
    yield context
    context.close()


def load(ctx, paths, purpose, mode=SessionMode.SEPARATE) -> list[int]:
    return LogImporter(ctx.database, ctx.paths, ctx.settings.import_).import_logs(
        ImportRequest(paths=list(paths), purpose=purpose, session_mode=mode, meta=ROLE)).log_file_ids


@pytest.fixture
def knowledge(ctx, tmp_path: Path):
    """승인된 정상 사례(로그 2개)와 승인된 비정상 사례(가이드+매뉴얼)."""
    mode_id = ModeManager(ctx.database).add("테스트모드A")
    cases = CaseManager(ctx.database)
    workflow = ApprovalWorkflow(ctx.database)
    normal_ids = load(ctx, [normal_log(tmp_path / "n1.log"), normal_log(tmp_path / "n2.log", start_second=7)],
                      OriginPurpose.NORMAL_CASE)
    normal = cases.create("NORMAL", "승인 정상", normal_ids, mode_id=mode_id, mode_confirmed=True,
                          normal_rationale="가상 확인")
    patterns = NormalLearner(ctx.database, ctx.settings).propose(normal)
    workflow.approve(normal, pattern_decisions={p.id: ("APPROVED" if p.pattern_type in ("REQUIRED", "ORDER_CONSTRAINT")
                                                       else "REJECTED") for p in patterns})
    abnormal_ids = load(ctx, [abnormal_log(tmp_path / "x1.log")], OriginPurpose.ABNORMAL_CASE)
    abnormal = cases.create("ABNORMAL", "승인 비정상", abnormal_ids, symptom="가상 증상", abnormal_criteria="2회 이상",
                            cause_status="SUSPECTED")
    learner = AbnormalLearner(ctx.database, ctx.paths, ctx.settings)
    for name, text, kind in (("g.md", GUIDE + "\n## 해결 방법\n가상 해결 문장\n", "DIAGNOSTIC_GUIDE"),
                             ("m.txt", MANUAL, "RESPONSE_MANUAL")):
        doc = tmp_path / name
        doc.write_bytes(text.encode("utf-8"))
        learner.attach(abnormal, doc, kind)
    with closing(ctx.database.connect()) as conn:
        decisions = {r["id"]: "APPROVED" for r in GuideRepository(conn).items(abnormal)}
    workflow.approve(abnormal, guide_decisions=decisions)
    return mode_id, normal, abnormal


def run(ctx, ids, mode_id=None, confirmed=True):
    return DiagnosticEngine(ctx.database, ctx.paths, ctx.settings).run(
        AnalysisRequest(ids, mode_id=mode_id, mode_confirmed=confirmed, use_ai=False))


def test_normal_log_matches_learned_pattern(ctx, knowledge, tmp_path: Path) -> None:  # T-NA-01
    mode_id, normal, _ = knowledge
    ids = load(ctx, [normal_log(tmp_path / "new.log", start_second=40)], OriginPurpose.ANALYSIS)
    result = run(ctx, ids, mode_id)
    assert result.axes["normal_match"] == "MATCH" and result.axes["anomaly_level"] == "NONE_FOUND"
    assert result.axes["evidence_sufficiency"] == "SUFFICIENT"
    assert result.similar_normal[0].case.id == normal


def test_abnormal_log_gets_violations_similar_case_and_manual_response(ctx, knowledge, tmp_path: Path) -> None:
    mode_id, _, abnormal = knowledge  # T-NA-03, CR-01 NA-16
    ids = load(ctx, [abnormal_log(tmp_path / "new_bad.log", start_second=50)], OriginPurpose.ANALYSIS)
    result = run(ctx, ids, mode_id)
    types = {f.finding_type for f in result.findings}
    assert {"EXPLICIT_ERROR", "MISSING_EVENT"} <= types
    assert result.axes["anomaly_level"] == "RULE_VIOLATION"  # 결과 축은 독립: 위반이면서 유사 사례도 있음
    assert result.axes["similar_abnormal"] == "FOUND"
    assert result.similar_abnormal[0].case.id == abnormal and "TEST-E001" in result.similar_abnormal[0].shared_codes
    guidance = result.guidance
    assert guidance.level == "MANUAL" and result.axes["response_guidance"] == "MANUAL"
    steps = [i for i in guidance.items if i.section == "RESPONSE_STEP"]
    assert len(steps) == 3 and guidance.items[0].section == "PRECAUTION"  # 주의사항을 먼저
    assert all(i.case_id == abnormal for i in guidance.items)  # 출처: 승인된 사례의 승인된 매뉴얼
    assert any(i.section == "CHECK_METHOD" for i in guidance.check_methods)
    with closing(ctx.database.connect()) as conn:
        stored = AnalysisRepository(conn).evidence(result.run_id)
        row = AnalysisRepository(conn).get(result.run_id)
    assert [r["evidence_key"] for r in stored][:3] == ["EV-0001", "EV-0002", "EV-0003"]
    assert {r["finding_type"] for r in stored} >= {"RESPONSE_REFERENCE", "SIMILAR_ABNORMAL_CASE"}
    assert row["status"] == "COMPLETED" and row["ai_status"] == "NOT_RUN"
    similar = next(r for r in stored if r["finding_type"] == "SIMILAR_ABNORMAL_CASE")
    assert "확률 아님" in similar["summary"]  # 유사도를 확률로 표시하지 않음


def test_guide_only_and_none_levels(ctx, tmp_path: Path) -> None:  # T-NA-12·13
    cases = CaseManager(ctx.database)
    ids = load(ctx, [abnormal_log(tmp_path / "x.log")], OriginPurpose.ABNORMAL_CASE)
    case_id = cases.create("ABNORMAL", "가이드만", ids, symptom="s", abnormal_criteria="2회", cause_status="UNKNOWN")
    doc = tmp_path / "g.md"
    doc.write_bytes((GUIDE + "\n## 해결 방법\n가상 해결\n").encode("utf-8"))
    AbnormalLearner(ctx.database, ctx.paths, ctx.settings).attach(case_id, doc, "DIAGNOSTIC_GUIDE")
    with closing(ctx.database.connect()) as conn:
        decisions = {r["id"]: "APPROVED" for r in GuideRepository(conn).items(case_id)}
    ApprovalWorkflow(ctx.database).approve(case_id, guide_decisions=decisions)
    new = load(ctx, [abnormal_log(tmp_path / "y.log", start_second=3)], OriginPurpose.ANALYSIS)
    result = run(ctx, new, None, False)
    assert result.guidance.level == "GUIDE_ONLY" and "가상 해결" in result.guidance.items[0].content
    unrelated = load(ctx, [write(tmp_path / "z.log", iso_lines([("INFO", "TEST-OTHER", "TEST-UNRELATED")]))],
                     OriginPurpose.ANALYSIS)
    none = run(ctx, unrelated, None, False)
    assert none.guidance.level == "NONE" and "대응을 붙이지 않았습니다" in none.guidance.message
    assert none.verdict.state == "UNDECIDED"  # 비교할 정상 로그가 없음 → 판단 보류(대응도 붙이지 않음)


def test_no_knowledge_is_not_evaluated(ctx, tmp_path: Path) -> None:  # T-NA-05
    ids = load(ctx, [normal_log(tmp_path / "a.log")], OriginPurpose.ANALYSIS)
    result = run(ctx, ids, None, False)
    assert result.axes == {"normal_match": "NOT_EVALUATED", "anomaly_level": "NOT_EVALUATED",
                           "similar_abnormal": "NOT_EVALUATED", "evidence_sufficiency": "INSUFFICIENT",
                           "response_guidance": "NONE"}


def test_without_mode_compares_with_most_similar_normal(ctx, knowledge, tmp_path: Path) -> None:  # CR-06 LI-06
    ids = load(ctx, [normal_log(tmp_path / "new.log", start_second=40)], OriginPurpose.ANALYSIS)
    result = run(ctx, ids, None, False)
    assert result.axes["normal_match"] == "MATCH" and result.axes["evidence_sufficiency"] == "SUFFICIENT"
    assert any("가장 비슷한 정상 로그 C-0001 승인 정상(점수" in n for n in result.notes)
    extra = write(tmp_path / "extra.log", iso_lines(NORMAL_SEQUENCE + [("INFO", "TEST-CTRL", "TEST-NEWTHING x=1")],
                                                    start_second=80))
    with_extra = run(ctx, load(ctx, [extra], OriginPurpose.ANALYSIS), None, False)
    assert any(f.finding_type == "EXTRA_EVENT" for f in with_extra.findings)  # 비슷한 정상 로그에 없던 이벤트
    assert with_extra.axes["anomaly_level"] != "RULE_VIOLATION"  # 모드 없이는 확정 패턴을 쓰지 않는다


def test_without_mode_and_no_similar_normal_is_partial(ctx, knowledge, tmp_path: Path) -> None:
    other = write(tmp_path / "other.log", iso_lines([("INFO", "TEST-OTHER", f"TEST-ALIEN{i} v=1") for i in range(4)]))
    result = run(ctx, load(ctx, [other], OriginPurpose.ANALYSIS), None, False)
    assert result.axes["evidence_sufficiency"] == "PARTIAL"
    assert any("비슷한 정상 로그가 없어" in n for n in result.notes)


def test_draft_cases_are_excluded(ctx, tmp_path: Path) -> None:  # DB-04, T-DONE-03
    ids = load(ctx, [abnormal_log(tmp_path / "x.log")], OriginPurpose.ABNORMAL_CASE)
    CaseManager(ctx.database).create("ABNORMAL", "초안", ids)
    new = load(ctx, [abnormal_log(tmp_path / "y.log", start_second=3)], OriginPurpose.ANALYSIS)
    result = run(ctx, new, None, False)
    assert result.similar_abnormal == [] and result.axes["similar_abnormal"] == "NOT_EVALUATED"


def test_separate_sessions_are_not_merged(ctx, knowledge, tmp_path: Path) -> None:
    mode_id = knowledge[0]
    ids = load(ctx, [normal_log(tmp_path / "s1.log", start_second=11), normal_log(tmp_path / "s2.log", start_second=13)],
               OriginPurpose.ANALYSIS, SessionMode.SEPARATE)
    result = run(ctx, ids, mode_id)
    assert any("따로 분석" in n for n in result.notes)


def test_reordered_independent_events_are_not_abnormal(ctx, knowledge, tmp_path: Path) -> None:  # T-NA-02
    mode_id = knowledge[0]
    seq = [NORMAL_SEQUENCE[0], NORMAL_SEQUENCE[1], NORMAL_SEQUENCE[2], NORMAL_SEQUENCE[3], NORMAL_SEQUENCE[4]]
    ids = load(ctx, [write(tmp_path / "r.log", iso_lines(seq, start_second=55))], OriginPurpose.ANALYSIS)
    assert run(ctx, ids, mode_id).axes["anomaly_level"] == "NONE_FOUND"


def test_search_index_and_rebuild(ctx, knowledge) -> None:  # STO-08
    with closing(ctx.database.connect()) as conn:
        hits = SearchRepository(conn).search("TEST-E001")
        assert hits and all(h.error_code == "TEST-E001" or "TEST-E001" in h.message for h in hits)
        SearchRepository(conn).rebuild()
        assert SearchRepository(conn).search("TEST-E001")


def test_cancel_marks_run(ctx, tmp_path: Path) -> None:
    from app.core.cancel import Cancelled, CancelToken

    ids = load(ctx, [normal_log(tmp_path / "a.log")], OriginPurpose.ANALYSIS)
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        DiagnosticEngine(ctx.database, ctx.paths, ctx.settings).run(AnalysisRequest(ids, use_ai=False), token)
    with closing(ctx.database.connect()) as conn:
        assert AnalysisRepository(conn).list()[0]["status"] == "CANCELLED"


def test_response_shows_only_for_abnormal_with_applicable_case(ctx, knowledge, tmp_path: Path) -> None:  # 리뷰 2026-11-22
    """정상 로그에는 승인된 비정상 대응을 붙이지 않고, 같은 오류 코드처럼 적용 근거가 확인된 사례일 때만 붙인다."""
    normal = load(ctx, [normal_log(tmp_path / "ok.log", start_second=40)], OriginPurpose.ANALYSIS)
    calm = run(ctx, normal, None, False)
    assert calm.axes["anomaly_level"] == "NONE_FOUND"
    assert calm.verdict.state == "UNDECIDED" and "로그가 짧아" in calm.verdict.reasons[0]  # 가상 정상 로그는 5줄(DT-20)
    assert calm.guidance.level == "NONE" and "대응을 붙이지 않았습니다" in calm.guidance.message
    assert not any(f.finding_type == "RESPONSE_REFERENCE" for f in calm.findings)  # AI에도 대응 근거가 가지 않음
    bad = load(ctx, [abnormal_log(tmp_path / "bad.log", start_second=50)], OriginPurpose.ANALYSIS)
    hit = run(ctx, bad, None, False)
    assert hit.verdict.state == "ABNORMAL" and hit.guidance.level == "MANUAL"
    assert "적용할 근거가 확인된" in hit.guidance.message
    assert "같은 오류 코드 TEST-E001" in hit.guidance.message or "근거 지문이 비슷함" in hit.guidance.message


def test_answered_question_becomes_a_criterion_the_code_uses(ctx, knowledge, tmp_path: Path) -> None:  # CR-15 JQ-01~04
    """처음 보는 알림 줄은 규칙이 뜻을 몰라 이상으로 보지 않는다 → 질문이 나오고, '문제의 표시'로 답하면
    다음 분석부터 코드 판정이 그 기준으로 비정상 의심. '상황에 따라 다름'이면 판단 보류."""
    from app.learning.criteria import ABNORMAL_SIGN, HOLD, CriteriaService
    from tests.logdata import NORMAL_SEQUENCE

    notice = NORMAL_SEQUENCE + [("INFO", "TEST-SYS", "TEST-NOTICE flag=TEST-FK unit=2")]
    first = run(ctx, load(ctx, [write(tmp_path / "a.log", iso_lines(notice))], OriginPurpose.ANALYSIS), None, False)
    assert first.axes["anomaly_level"] in ("NONE_FOUND", "NOT_EVALUATED") or first.verdict.state != "ABNORMAL"
    line = next(q for q in first.questions if q.kind == "LINE" and "TEST-FK" in (q.example or ""))
    service = CriteriaService(ctx.database)
    service.save_answer(line, ABNORMAL_SIGN, method_text="가상: 이 알림은 점검 필요", log_ids=[1], run_id=first.run_id)
    again = run(ctx, load(ctx, [write(tmp_path / "b.log", iso_lines(notice, start_second=30))], OriginPurpose.ANALYSIS),
                None, False)
    assert again.axes["anomaly_level"] == "SUSPECTED" and again.verdict.state == "ABNORMAL"
    assert any("엔지니어 기준" in r for r in again.verdict.reasons)
    assert not any(q.feature == line.feature for q in again.questions)  # 이미 기준이 있으면 다시 묻지 않음
    service.save_answer(line, HOLD, log_ids=[1])  # 같은 특징의 새 답이 이전 기준을 대신
    held = run(ctx, load(ctx, [write(tmp_path / "c.log", iso_lines(notice, start_second=60))], OriginPurpose.ANALYSIS),
               None, False)
    assert held.verdict.state == "UNDECIDED" and "사람이 확인해야 함" in held.verdict.reasons[0] and len(service.all()) == 1
