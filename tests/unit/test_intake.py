"""CR-05 자유 글 정리 (NL-02·03·06, AI_POLICY V-10). 글과 모드 이름은 가상이다."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ai.intake import FieldPick, IntakeAnswer, Story, filter_answer, split_story
from app.bootstrap import initialize
from app.learning.case_manager import CaseError, CaseManager, ModeManager
from app.learning.intake import IntakeService, match_cause_words, match_mode_name, title_from_story
from app.models.records import ModeRecord
from tests.fake_ollama import FakeOllama

STORY = "어제 오후 TEST-MODE-A로 돌렸습니다. 끝까지 TEST-E001 없이 끝났음.\nFW는 2.3이고 장비는 기본 구성"


@pytest.fixture
def ctx(tmp_path: Path):
    context = initialize(tmp_path / "home", configure_logging=False)
    context.settings = context.settings.model_copy(
        update={"ollama": context.settings.ollama.model_copy(update={"model": "test-model:7b"})})
    yield context
    context.close()


def mode(name: str, mode_id: int = 1) -> ModeRecord:
    return ModeRecord(id=mode_id, name=name, source="USER", status="ACTIVE")


# ── 결정론 ──
def test_split_story_keeps_decimal_and_spans() -> None:
    segments = split_story(STORY)
    assert [s.text for s in segments] == [
        "어제 오후 TEST-MODE-A로 돌렸습니다.", "끝까지 TEST-E001 없이 끝났음.", "FW는 2.3이고 장비는 기본 구성"]
    for s in segments:
        assert STORY[s.span_start:s.span_end] == s.text  # 원문 위치 그대로


def test_split_story_handles_crlf_and_blank_lines() -> None:
    assert [s.text for s in split_story("첫 줄\r\n\r\n  둘째 줄  \r\n")] == ["첫 줄", "둘째 줄"]
    assert split_story("   \n ") == []


def test_copied_text_must_be_in_story() -> None:
    text = "FW는 2.3이고\n장비는  기본 구성"
    story = Story(text, split_story(text))
    assert story.find("2.3") == ("2.3", 4)
    assert story.find("“2.3”")[0] == "2.3"  # 감싼 따옴표는 봐준다
    assert story.find("기본 구성")[0] == "기본 구성"
    assert story.find("장비는 기본 구성")[0] == "장비는 기본 구성"  # 공백 차이
    assert story.find("U-0002")[0] == "장비는  기본 구성"  # 조각 키로 답해도 그 조각 글
    assert story.find("U-0009") is None and story.find("3.0") is None and story.find("  ") is None


def test_title_from_story() -> None:
    assert title_from_story("\n  첫 줄 제목  \n둘째") == "첫 줄 제목"
    assert title_from_story("가" * 50) == "가" * 40 + "…"
    assert title_from_story("  ") is None


def test_match_mode_name_needs_whole_name_and_single_mode() -> None:
    a, b = mode("TEST-MODE-A", 1), mode("TEST-MODE-B", 2)
    assert match_mode_name("오늘 test-mode-a로 돌림", [a, b]).id == 1  # 대소문자 무시, 조사 붙어도 됨
    assert match_mode_name("TEST-MODE-AB로 돌림", [a, b]) is None  # 이름 일부는 아님
    assert match_mode_name("TEST-MODE-A 다음 TEST-MODE-B", [a, b]) is None  # 둘이면 정하지 않음
    long = mode("TEST-MODE-A 저속", 3)
    assert match_mode_name("TEST-MODE-A 저속으로", [a, long]).id == 3  # 긴 이름이 짧은 이름을 품으면 긴 쪽


# ── V-10 거르기 ──
def _story() -> Story:
    return Story(STORY, split_story(STORY))


def test_filter_keeps_only_user_words() -> None:
    answer = IntakeAnswer(
        picks=[FieldPick(field="normal_rationale", text="끝까지 TEST-E001 없이 끝났음."),
               FieldPick(field="fw_version", text="2.3"),
               FieldPick(field="device_config", text="기본 구성"),
               FieldPick(field="operating_conditions", text="어제 오후"),
               FieldPick(field="symptom", text="어제 오후"),  # 정상 화면에 없는 칸
               FieldPick(field="fw_version", text="FW 3.0")],  # 글에 없는 값
        mode="TEST-MODE-A", mode_text="TEST-MODE-A로")
    picks = filter_answer(answer, ("operating_conditions", "fw_version", "device_config", "normal_rationale"),
                          _story(), ["TEST-MODE-A"], "NORMAL")
    assert picks.values == {"normal_rationale": "끝까지 TEST-E001 없이 끝났음.", "fw_version": "2.3",
                            "device_config": "기본 구성", "operating_conditions": "어제 오후"}
    assert picks.mode_name == "TEST-MODE-A"
    assert len(picks.dropped) == 2 and "3.0" not in str(picks.values)


def test_filter_drops_conditions_that_are_only_the_mode_name() -> None:
    answer = IntakeAnswer(picks=[FieldPick(field="operating_conditions", text="TEST-MODE-A")])
    picks = filter_answer(answer, ("operating_conditions",), _story(), ["TEST-MODE-A"], "NORMAL")
    assert picks.values == {} and picks.dropped  # 모드는 따로 다룬다


def test_match_cause_words() -> None:
    assert match_cause_words("TEST-E001이 뜸. 원인은 아직 모르겠어요.") == "UNKNOWN"
    assert match_cause_words("원인은 케이블 접촉으로 추정.") == "SUSPECTED"
    assert match_cause_words("원인 확인됨: 커넥터 풀림") == "CONFIRMED"
    assert match_cause_words("케이블로 추정") is None  # '원인'이라는 말이 없으면 정하지 않는다
    assert match_cause_words("원인은 모르지만 케이블 같음") is None  # 뜻이 섞이면 정하지 않는다
    assert match_cause_words("원인은 모름.\n다른 원인으로 추정.") is None  # '원인' 문장이 둘이면 정하지 않는다


def test_filter_rejects_paraphrase() -> None:
    answer = IntakeAnswer(picks=[FieldPick(field="normal_rationale", text="오류 없이 정상 종료")])  # 글을 고쳐 씀
    picks = filter_answer(answer, ("normal_rationale",), _story(), [], "NORMAL")
    assert picks.values == {} and picks.dropped


def test_filter_joins_in_text_order_without_duplicates() -> None:
    answer = IntakeAnswer(picks=[FieldPick(field="normal_rationale", text="끝까지 TEST-E001 없이 끝났음."),
                                 FieldPick(field="normal_rationale", text="어제 오후 TEST-MODE-A로 돌렸습니다."),
                                 FieldPick(field="normal_rationale", text="TEST-E001 없이"),  # 이미 들어간 글의 일부
                                 FieldPick(field="normal_rationale", text="끝까지 TEST-E001 없이 끝났음.")])
    picks = filter_answer(answer, ("normal_rationale",), _story(), [], "NORMAL")
    assert picks.values["normal_rationale"] == "어제 오후 TEST-MODE-A로 돌렸습니다. 끝까지 TEST-E001 없이 끝났음."


def test_filter_rejects_unregistered_mode_and_unbacked_cause() -> None:
    answer = IntakeAnswer(mode="새로운 모드", mode_text="어제 오후", cause_status="CONFIRMED", cause_text="원인 확정")
    picks = filter_answer(answer, ("symptom",), _story(), ["TEST-MODE-A"], "ABNORMAL")
    assert picks.mode_name is None and picks.cause_status is None and len(picks.dropped) == 2
    ok = filter_answer(IntakeAnswer(mode="test-mode-a", mode_text="TEST-MODE-A", cause_status="unknown",
                                    cause_text="TEST-E001 없이"), ("symptom",), _story(), ["TEST-MODE-A"], "ABNORMAL")
    assert ok.mode_name == "TEST-MODE-A" and ok.cause_status == "UNKNOWN"
    normal = filter_answer(IntakeAnswer(cause_status="CONFIRMED", cause_text="끝까지"), ("normal_rationale",),
                           _story(), [], "NORMAL")
    assert normal.cause_status is None  # 정상 화면에는 원인 확정 여부가 없다


# ── 사례 반영 ──
def test_organize_case_fills_found_fields_and_keeps_mode_unconfirmed(ctx) -> None:
    mode_id = ModeManager(ctx.database).add("TEST-MODE-A")
    cases = CaseManager(ctx.database)
    case_id = cases.create("NORMAL", "", [], fw_version="직접 쓴 값")
    fake = FakeOllama([{"picks": [{"field": "normal_rationale", "text": "끝까지 TEST-E001 없이 끝났음."}],
                        "mode": "UNKNOWN", "mode_text": "", "cause_status": "NOT_SAID", "cause_text": ""}])
    result = IntakeService(ctx.database, ctx.settings, fake.factory).organize_case(case_id, STORY)
    case = cases.get(case_id)
    assert result.status == "OK"
    assert case.user_description == STORY  # 글은 그대로 저장
    assert case.normal_rationale == "끝까지 TEST-E001 없이 끝났음."
    assert case.fw_version == "직접 쓴 값"  # 못 찾은 칸은 그대로 (NL-06)
    assert case.mode_id == mode_id and not case.mode_confirmed  # 이름 일치로 찾았어도 확인은 사용자
    first_line = STORY.splitlines()[0]
    assert case.title == first_line[:40].rstrip() + "…"  # 제목은 글 첫 줄 40자
    assert "U-0001" in fake.last_prompt() and "TEST-MODE-A" in fake.last_prompt()
    assert "한 글자도 바꾸지 말고" in fake.last_system()
    assert "정상 판정 근거" in result.message() and "운전 모드" in result.message()


def test_organize_case_without_ai_saves_text_and_rules(ctx) -> None:
    ModeManager(ctx.database).add("TEST-MODE-A")
    cases = CaseManager(ctx.database)
    case_id = cases.create("ABNORMAL", "제목 없는 사례", [])
    settings = ctx.settings.model_copy(update={"ollama": ctx.settings.ollama.model_copy(update={"model": None})})
    result = IntakeService(ctx.database, settings, FakeOllama().factory).organize_case(case_id, STORY)
    case = cases.get(case_id)
    assert result.status == "RULES_ONLY" and result.ai_status == "NO_MODEL"
    assert case.user_description == STORY and case.title.startswith("어제 오후") and case.mode_id is not None
    assert case.symptom is None
    assert "칸으로 직접 고치기" in result.message()


def test_organize_case_keeps_confirmed_mode_and_existing_title(ctx) -> None:
    mode_id = ModeManager(ctx.database).add("TEST-MODE-A")
    cases = CaseManager(ctx.database)
    case_id = cases.create("ABNORMAL", "내가 붙인 제목", [], mode_id=mode_id, mode_confirmed=True)
    fake = FakeOllama([{"picks": [{"field": "symptom", "text": "U-0002"}], "mode": "TEST-MODE-A",
                        "mode_text": "TEST-MODE-A", "cause_status": "SUSPECTED", "cause_text": "끝까지"}])
    result = IntakeService(ctx.database, ctx.settings, fake.factory).organize_case(case_id, STORY)
    case = cases.get(case_id)
    assert case.title == "내가 붙인 제목" and case.mode_confirmed  # 같은 모드면 확인 상태 유지
    assert case.symptom == "끝까지 TEST-E001 없이 끝났음." and case.cause_status == "SUSPECTED"
    assert set(result.applied) == {"symptom", "cause_status"}


def test_organize_case_refuses_approved_case(ctx) -> None:
    cases = CaseManager(ctx.database)
    case_id = cases.create("NORMAL", "t", [])
    from contextlib import closing

    with closing(ctx.database.connect()) as conn:
        conn.execute("UPDATE cases SET status='APPROVED', approved_at='2026-11-17T00:00:00.000Z', current_revision=1 "
                     "WHERE id=?", (case_id,))
    with pytest.raises(CaseError):
        IntakeService(ctx.database, ctx.settings, FakeOllama().factory).organize_case(case_id, STORY)


def test_organize_text_for_analysis_only_conditions(ctx) -> None:
    ModeManager(ctx.database).add("TEST-MODE-A")
    fake = FakeOllama([{"picks": [{"field": "operating_conditions", "text": "어제 오후"},
                                  {"field": "symptom", "text": "끝까지"}]}])
    result = IntakeService(ctx.database, ctx.settings, fake.factory).organize_text(STORY, "ANALYSIS")
    assert result.fields == {"operating_conditions": "어제 오후"} and result.mode_name == "TEST-MODE-A"
    assert IntakeService(ctx.database, ctx.settings, fake.factory).organize_text("  ", "ANALYSIS").status == "NO_TEXT"
