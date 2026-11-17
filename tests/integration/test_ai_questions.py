"""CR-07 LLM 로그 질문과 사용자 답 지식 (QA-01·03, AO-02, V-12). 로그·질문·답은 가상이다."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from app.analysis.diagnostic_engine import AnalysisRequest, DiagnosticEngine
from app.bootstrap import initialize
from app.core.importer import LogImporter
from app.learning.ai_questions import AiQuestionService, knowledge_texts
from app.learning.case_manager import CaseError, CaseManager
from app.learning.log_entry import LogEntryService
from app.models.enums import OriginPurpose
from app.models.records import ImportRequest, SessionMode
from app.storage.repositories import AiQuestionRepository
from tests.fake_ollama import FakeOllama
from tests.logdata import abnormal_log, normal_log


@pytest.fixture
def ctx(tmp_path: Path):
    context = initialize(tmp_path / "home", configure_logging=False)
    context.settings = context.settings.model_copy(
        update={"ollama": context.settings.ollama.model_copy(update={"model": "test-model:7b"})})
    yield context
    context.close()


def logs(ctx, paths, purpose=OriginPurpose.NORMAL_CASE) -> list[int]:
    return LogImporter(ctx.database, ctx.paths, ctx.settings.import_).import_logs(ImportRequest(
        paths=list(paths), purpose=purpose, session_mode=SessionMode.SEPARATE)).log_file_ids


@pytest.fixture
def saved(ctx, tmp_path: Path):
    ids = logs(ctx, [normal_log(tmp_path / "n.log"), abnormal_log(tmp_path / "a.log", start_second=30)])
    service = LogEntryService(ctx.database)
    normal = service.save(ids[0], "NORMAL", "가상 동작 한 번")
    abnormal = service.save(ids[1], "ABNORMAL", "가상 동작", "TEST-E001이 나오면 에러로 봄")
    return normal.case_id, abnormal.case_id


QUESTIONS = {"questions": [
    {"kind": "guess", "text": "TEST-RETRY가 한 번 나온 뒤 멈춘 것으로 보이는데 맞나요?", "reason": "가상 이유",
     "evidence": ["EV-0003"]},
    {"kind": "ASK", "text": "TEST-INIT은 어떤 동작 단계인가요?", "reason": "", "evidence": []},
]}


def test_generate_stores_questions_with_evidence(ctx, saved) -> None:  # QA-01
    _, abnormal = saved
    fake = FakeOllama([QUESTIONS])
    service = AiQuestionService(ctx.database, ctx.settings, fake.factory)
    outcome = service.generate(abnormal)
    assert outcome.status == "OK" and len(outcome.added) == 2
    views = service.questions([abnormal])
    assert {(v.kind, v.text) for v in views} == {("GUESS", QUESTIONS["questions"][0]["text"]),
                                                 ("ASK", QUESTIONS["questions"][1]["text"])}
    guess = next(v for v in views if v.kind == "GUESS")
    assert guess.evidence and guess.evidence[0]["key"] == "EV-0003" and guess.evidence[0]["line"]
    assert guess.file_name == "a.log" and guess.label == "ABNORMAL"
    system, prompt = fake.last_system(), fake.last_prompt()
    assert "'비정상'으로 표시" in system and "판정은 바꾸지 않는다" in system
    assert "판단 가이드: TEST-E001이 나오면 에러로 봄" in prompt
    assert "[비슷한 승인 로그]" in prompt or "근거 목록" in prompt
    assert "OK" in outcome.message("a.log") or "질문 2개" in outcome.message("a.log")


def test_guess_without_evidence_is_rejected_then_retried(ctx, saved) -> None:  # V-12
    _, abnormal = saved
    bad = {"questions": [{"kind": "GUESS", "text": "근거 없는 추측인데 맞나요?", "evidence": []}]}
    fake = FakeOllama([bad, {"questions": [{"kind": "ASK", "text": "다시 묻는 가상 질문인가요?", "evidence": []}]}])
    outcome = AiQuestionService(ctx.database, ctx.settings, fake.factory).generate(abnormal)
    assert outcome.status == "OK" and len(outcome.added) == 1
    assert "V-12" in fake.last_prompt()  # 두 번째 요청에 거부 이유가 들어간다


def test_code_meaning_guess_needs_knowledge(ctx, saved) -> None:  # V-12 (V-5와 같은 뜻)
    _, abnormal = saved
    guess = {"questions": [{"kind": "GUESS", "text": "TEST-E001은 응답 지연을 뜻하는 것 같은데 맞나요?",
                            "evidence": ["EV-0003"]}]}
    fake = FakeOllama([guess, guess])
    outcome = AiQuestionService(ctx.database, ctx.settings, fake.factory).generate(abnormal)
    assert outcome.status == "VALIDATION_FAILED"
    assert not AiQuestionService(ctx.database, ctx.settings, fake.factory).questions([abnormal])


def test_regenerate_obsoletes_open_and_never_duplicates(ctx, saved) -> None:
    _, abnormal = saved
    service = AiQuestionService(ctx.database, ctx.settings, FakeOllama([QUESTIONS, QUESTIONS]).factory)
    first = service.generate(abnormal)
    service.answer(first.added[1], "TEXT", "가상 답: 준비 단계")  # 두 번째 질문(ASK)에 답함
    second = service.generate(abnormal)
    with closing(ctx.database.connect()) as conn:
        rows = AiQuestionRepository(conn).for_cases([abnormal])
    assert len(rows) == 2  # 같은 질문은 새로 만들지 않는다
    statuses = {r["kind"]: r["status"] for r in rows}
    assert statuses == {"GUESS": "OPEN", "ASK": "ANSWERED"}  # 다시 나온 열린 질문은 다시 열린다
    assert second.added == [first.added[0]]


def test_answers_become_knowledge_except_unknown(ctx, saved) -> None:  # QA-03
    normal, abnormal = saved
    fake = FakeOllama([QUESTIONS, {"questions": [{"kind": "ASK", "text": "가상 정상 질문인가요?", "evidence": []}]}])
    service = AiQuestionService(ctx.database, ctx.settings, fake.factory)
    ids = service.generate(abnormal).added
    normal_q = service.generate(normal).added[0]
    service.answer(ids[0], "NO", "가상: 멈춘 게 아니라 대기")
    service.answer(ids[1], "UNKNOWN")
    service.skip(normal_q)
    texts = knowledge_texts(ctx.database, [abnormal, normal])
    assert texts == ["[사용자 확인] AI 추측 'TEST-RETRY가 한 번 나온 뒤 멈춘 것으로 보이는데 맞나요?' → 사용자가 틀렸다고 함"
                     " — 사용자 설명: 가상: 멈춘 게 아니라 대기"]
    assert not service.questions([abnormal, normal])  # 열린 질문 없음
    with pytest.raises(CaseError, match="답을 적어"):
        service.answer(ids[0], "TEXT", " ")


def test_no_model_skips(ctx, saved) -> None:
    settings = ctx.settings.model_copy(update={"ollama": ctx.settings.ollama.model_copy(update={"model": None})})
    outcome = AiQuestionService(ctx.database, settings, FakeOllama().factory).generate(saved[0])
    assert outcome.status == "NO_MODEL" and "모델" in outcome.message("n.log")


def test_analysis_ai_gets_memos_and_user_answers(ctx, saved, tmp_path: Path) -> None:  # AO-02
    _, abnormal = saved
    service = AiQuestionService(ctx.database, ctx.settings, FakeOllama([QUESTIONS]).factory)
    ids = service.generate(abnormal).added
    service.answer(ids[0], "YES", "가상 한마디")
    new = logs(ctx, [abnormal_log(tmp_path / "new.log", start_second=90)], OriginPurpose.ANALYSIS)
    fake = FakeOllama([{"observations": [], "hypotheses": [], "verification_steps": [], "response_plan": [],
                        "unknowns": ["가상: 판단 불가"]}])
    result = DiagnosticEngine(ctx.database, ctx.paths, ctx.settings, fake.factory).run(AnalysisRequest(new, use_ai=True))
    prompt = fake.last_prompt()
    assert "판단 가이드: TEST-E001이 나오면 에러로 봄" in prompt  # 비슷한 비정상 로그의 사용자 가이드
    assert "[사용자 확인] AI 추측" in prompt and "사용자가 맞다고 확인" in prompt
    assert "ANSWER" in result.ai.kinds.values()
    assert CaseManager(ctx.database).get(abnormal).status == "APPROVED"
