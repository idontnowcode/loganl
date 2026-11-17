"""Phase 5 확인 질문 (T-QE-01~07, QE-01~09, DOD-04·05·07, 완료 기준 "불명확한 사례를 AI가 질문하고 답변을 반영")."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from app.bootstrap import initialize
from app.core.importer import LogImporter
from app.learning.abnormal_learner import AbnormalLearner
from app.learning.approval_workflow import ApprovalWorkflow
from app.learning.case_manager import CaseManager, ModeManager
from app.learning.normal_learner import NormalLearner
from app.learning.question_engine import QuestionEngine, QuestionError
from app.models.enums import OriginPurpose
from app.models.records import ImportRequest, SessionMode
from app.storage.repositories import GuideRepository, LogFileRepository, QuestionRepository, SessionRepository
from tests.fake_ollama import FakeOllama
from tests.logdata import abnormal_log, normal_log, write
from tests.unit.test_guide_structurer import GUIDE


@pytest.fixture
def ctx(tmp_path: Path):
    context = initialize(tmp_path / "home", configure_logging=False)
    context.settings = context.settings.model_copy(
        update={"ollama": context.settings.ollama.model_copy(update={"model": "test-model:7b"})})
    yield context
    context.close()


def import_paths(ctx, paths, mode=SessionMode.SEPARATE, purpose=OriginPurpose.NORMAL_CASE) -> list[int]:
    return LogImporter(ctx.database, ctx.paths, ctx.settings.import_).import_logs(
        ImportRequest(paths=list(paths), purpose=purpose, session_mode=mode)).log_file_ids


def triggers(questions) -> list[str]:
    return [q.trigger for q in questions]


@pytest.fixture
def normal_case(ctx, tmp_path: Path):
    ids = import_paths(ctx, [normal_log(tmp_path / "a.log"), normal_log(tmp_path / "b.log", start_second=9)])
    return CaseManager(ctx.database).create("NORMAL", "질문 시험", ids)


def test_missing_required_info_creates_questions(ctx, normal_case) -> None:  # T-QE-01
    engine = QuestionEngine(ctx.database)
    questions = engine.refresh(normal_case)
    assert triggers(questions)[:2] == ["MODE_UNKNOWN", "NORMAL_RATIONALE_MISSING"]  # 필수·중요도 순
    assert all(q.reason for q in questions)  # 질문 이유
    assert CaseManager(ctx.database).get(normal_case).status == "NEEDS_INFORMATION"
    check = ApprovalWorkflow(ctx.database).check(normal_case)
    assert any("필수 확인 질문" in b for b in check.blocking)  # 필수 정보 미충족 시 승인 보류
    assert len(engine.next_questions(normal_case)) <= 3


def test_answers_update_case_and_reanalyze(ctx, normal_case) -> None:  # T-QE-02·04
    mode_id = ModeManager(ctx.database).add("테스트모드A")
    engine = QuestionEngine(ctx.database)
    questions = {q.trigger: q for q in engine.refresh(normal_case)}
    assert (str(mode_id), "테스트모드A") in questions["MODE_UNKNOWN"].choices
    outcome = engine.answer(questions["MODE_UNKNOWN"].id, "CHOICE", value=str(mode_id))
    assert "운전 모드" in outcome.applied and outcome.reanalyzed and "패턴 후보" in outcome.reanalyzed[0]
    case = CaseManager(ctx.database).get(normal_case)
    assert case.mode_id == mode_id and case.mode_confirmed
    rationale = next(q for q in engine.open_questions(normal_case) if q.trigger == "NORMAL_RATIONALE_MISSING")
    engine.answer(rationale.id, "TEXT", text="가상: 사용자가 정상 동작을 직접 봤다")
    assert CaseManager(ctx.database).get(normal_case).status == "DRAFT"  # 필수 질문이 사라지면 정보 필요 해제
    assert ApprovalWorkflow(ctx.database).check(normal_case).ok


def test_unknown_answer_is_kept_and_not_repeated(ctx, normal_case) -> None:  # T-QE-03, QE-07·08·09
    engine = QuestionEngine(ctx.database)
    rationale = next(q for q in engine.refresh(normal_case) if q.trigger == "NORMAL_RATIONALE_MISSING")
    engine.answer(rationale.id, "UNKNOWN")
    engine.refresh(normal_case)
    with closing(ctx.database.connect()) as conn:
        rows = QuestionRepository(conn).for_case(normal_case)
    same = [r for r in rows if r["dedup_key"] == "normal_rationale"]
    assert len(same) == 1 and same[0]["status"] == "UNKNOWN"  # 같은 질문을 다시 만들지 않는다
    assert any("정상 판정 근거" in b for b in ApprovalWorkflow(ctx.database).check(normal_case).blocking)


def test_session_question_and_merge(ctx, tmp_path: Path) -> None:
    ids = import_paths(ctx, [abnormal_log(tmp_path / "a.log"), abnormal_log(tmp_path / "b.log", start_second=5)],
                       SessionMode.UNKNOWN, OriginPurpose.ABNORMAL_CASE)
    assert len(ids) == 2
    case_id = CaseManager(ctx.database).create("ABNORMAL", "세션 시험", ids)
    engine = QuestionEngine(ctx.database)
    session_q = next(q for q in engine.refresh(case_id) if q.trigger == "SESSION_RELATION_UNKNOWN")
    assert not session_q.required
    engine.answer(session_q.id, "CHOICE", value="SAME")
    with closing(ctx.database.connect()) as conn:
        files = LogFileRepository(conn).get_many(ids)
        assert len({f.session_id for f in files}) == 1
        assert SessionRepository(conn).get(files[0].session_id).relation_confirmed
    assert "SESSION_RELATION_UNKNOWN" not in triggers(engine.open_questions(case_id))


def test_timestamp_question_for_raw_log(ctx, tmp_path: Path) -> None:
    ids = import_paths(ctx, [write(tmp_path / "free.txt", ["TEST free a", "TEST free b"])])
    case_id = CaseManager(ctx.database).create("NORMAL", "시각 시험", ids)
    engine = QuestionEngine(ctx.database)
    q = next(q for q in engine.refresh(case_id) if q.trigger == "TIMESTAMP_BASIS_UNKNOWN")
    engine.answer(q.id, "CHOICE", value="NONE")
    with closing(ctx.database.connect()) as conn:
        assert LogFileRepository(conn).get(ids[0]).timestamp_basis == "NONE"


def test_abnormal_questions(ctx, tmp_path: Path) -> None:
    ids = import_paths(ctx, [abnormal_log(tmp_path / "a.log")], purpose=OriginPurpose.ABNORMAL_CASE)
    case_id = CaseManager(ctx.database).create("ABNORMAL", "비정상 질문", ids,
                                               abnormal_criteria="TEST-FAULT가 자주 나오면", symptom="TEST-E999 발생")
    engine = QuestionEngine(ctx.database)
    found = {q.trigger: q for q in engine.refresh(case_id)}
    assert found["GUIDE_INFO_MISSING"].required and found["CAUSE_STATUS_UNKNOWN"].required
    assert not found["MODE_UNKNOWN"].required  # 비정상 사례에서 모드는 필수 아님
    assert "CRITERIA_DETAIL_UNKNOWN" in found  # 판정 기준에 숫자가 없다
    assert "TEST-E999" in found["USER_LOG_CONFLICT"].text  # 설명의 코드가 로그에 없다
    engine.answer(found["CAUSE_STATUS_UNKNOWN"].id, "CHOICE", value="SUSPECTED")
    assert CaseManager(ctx.database).get(case_id).cause_status == "SUSPECTED"
    engine.answer(found["CRITERIA_DETAIL_UNKNOWN"].id, "TEXT", text="10초 안에 2회 이상")
    assert "10초" in CaseManager(ctx.database).get(case_id).abnormal_criteria


def test_missing_guide_section_answer_adds_user_item(ctx, tmp_path: Path) -> None:
    ids = import_paths(ctx, [abnormal_log(tmp_path / "a.log")], purpose=OriginPurpose.ABNORMAL_CASE)
    case_id = CaseManager(ctx.database).create("ABNORMAL", "가이드 부족", ids)
    guide = tmp_path / "g.md"
    guide.write_bytes("## 가능한 원인\n가상 원인\n".encode("utf-8"))
    AbnormalLearner(ctx.database, ctx.paths, ctx.settings).attach(case_id, guide, "DIAGNOSTIC_GUIDE")
    engine = QuestionEngine(ctx.database)
    symptom_q = next(q for q in engine.refresh(case_id) if q.target_ref == "section:SYMPTOM")
    engine.answer(symptom_q.id, "TEXT", text="가상 증상: TEST-FAULT 반복")
    with closing(ctx.database.connect()) as conn:
        user_items = [r for r in GuideRepository(conn).items(case_id) if r["source_kind"] == "USER"]
    assert [(r["section"], r["status"]) for r in user_items] == [("SYMPTOM", "PROPOSED")]


def test_normal_case_with_errors_asks_conflict(ctx, tmp_path: Path) -> None:  # 사용자 설명과 로그 충돌
    ids = import_paths(ctx, [abnormal_log(tmp_path / "a.log")])
    case_id = CaseManager(ctx.database).create("NORMAL", "정상이라는데 오류", ids)
    q = next(q for q in QuestionEngine(ctx.database).refresh(case_id) if q.trigger == "USER_LOG_CONFLICT")
    assert "TEST-E001" in q.text


def test_conflicting_knowledge_between_cases(ctx, tmp_path: Path) -> None:  # T-QE-05
    learner = AbnormalLearner(ctx.database, ctx.paths, ctx.settings)
    cases = CaseManager(ctx.database)

    def abnormal_case(name: str, cause: str) -> int:
        ids = import_paths(ctx, [abnormal_log(tmp_path / f"{name}.log", start_second=len(name))],
                           purpose=OriginPurpose.ABNORMAL_CASE)
        case_id = cases.create("ABNORMAL", name, ids, symptom="증상", abnormal_criteria="2회", cause_status="SUSPECTED")
        doc = tmp_path / f"{name}.md"
        doc.write_bytes(f"## 오류 증상\n가상\n## 판정 기준\n2회\n## 가능한 원인\n{cause}\n".encode("utf-8"))
        learner.attach(case_id, doc, "DIAGNOSTIC_GUIDE")
        return case_id

    first = abnormal_case("first", "가상 원인 X")
    with closing(ctx.database.connect()) as conn:
        decisions = {r["id"]: "APPROVED" for r in GuideRepository(conn).items(first)}
    ApprovalWorkflow(ctx.database).approve(first, guide_decisions=decisions)
    second = abnormal_case("second", "가상 원인 Y")
    q = next(q for q in QuestionEngine(ctx.database).refresh(second) if q.trigger == "CONFLICTING_KNOWLEDGE")
    assert f"C-{first:04d}" in q.text and "TEST-E001" in q.text


def test_ai_missing_info_becomes_ai_question(ctx, normal_case) -> None:  # DOD-04 "AI가 부족한 정보를 질문"
    fake = FakeOllama([{"summary": "가상", "observations": [],
                        "missing_info": [{"text": "TEST-READY가 항상 나오나요?", "trigger": "NORMAL_RATIONALE_MISSING"}]}])
    NormalLearner(ctx.database, ctx.settings, fake.factory).generate_draft(normal_case)
    engine = QuestionEngine(ctx.database)
    ai_q = next(q for q in engine.refresh(normal_case) if q.generated_by == "AI")
    assert not ai_q.required and ai_q.text == "TEST-READY가 항상 나오나요?"
    engine.answer(ai_q.id, "TEXT", text="가상: 네")
    notes = CaseManager(ctx.database).detail(normal_case).notes
    assert any("TEST-READY" in n.body and "가상: 네" in n.body for n in notes)
    engine.refresh(normal_case)
    assert not any(q.generated_by == "AI" for q in engine.open_questions(normal_case))  # 다시 묻지 않음


def test_answer_rules(ctx, normal_case) -> None:  # T-QE-06
    engine = QuestionEngine(ctx.database)
    q = engine.refresh(normal_case)[0]
    with pytest.raises(QuestionError):
        engine.answer(q.id, "TEXT", text="  ")
    with pytest.raises(QuestionError):
        engine.answer(q.id, "BOGUS")
    engine.answer(q.id, "SKIPPED")
    with pytest.raises(QuestionError, match="이미 처리"):
        engine.answer(q.id, "SKIPPED")
