"""Phase 7 Ollama 진단 (T-HAL-01~06, T-NA-08·09, T-DONE-04·05, V-4~V-9, SEC-07·08, DOD-12).
가짜 Ollama로 정상 응답과 규칙 위반 응답을 각각 넣어 본다."""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

import httpx
import pytest

from app.analysis.diagnostic_engine import AnalysisRequest, DiagnosticEngine
from app.models.enums import OriginPurpose
from app.storage.repositories import AnalysisRepository, ClaimRepository
from tests.fake_ollama import FakeOllama
from tests.integration.test_analysis import ctx, knowledge, load  # noqa: F401 (픽스처 재사용)
from tests.logdata import abnormal_log


@pytest.fixture
def with_model(ctx):  # noqa: F811
    ctx.settings = ctx.settings.model_copy(
        update={"ollama": ctx.settings.ollama.model_copy(update={"model": "test-model:7b"})})
    return ctx


def analyze(ctx, knowledge, tmp_path, responses):  # noqa: F811
    mode_id = knowledge[0]
    ids = load(ctx, [abnormal_log(tmp_path / "bad.log", start_second=70)], OriginPurpose.ANALYSIS)
    fake = FakeOllama(responses)
    engine = DiagnosticEngine(ctx.database, ctx.paths, ctx.settings, fake.factory)
    return engine.run(AnalysisRequest(ids, mode_id=mode_id, mode_confirmed=True)), fake


def key_of(result, finding_type: str, predicate=lambda f: True) -> str:
    index = next(i for i, f in enumerate(result.findings) if f.finding_type == finding_type and predicate(f))
    return result.keys[index]


def keys_for(ctx, knowledge, tmp_path) -> dict[str, str]:  # noqa: F811
    """한 번 결정론만 돌려 근거 키 배치를 얻는다(같은 입력이면 같은 키)."""
    result, _ = analyze(ctx, knowledge, tmp_path, [])
    return {
        "error": key_of(result, "EXPLICIT_ERROR"),
        "missing": key_of(result, "MISSING_EVENT"),
        "case": key_of(result, "SIMILAR_ABNORMAL_CASE"),
        "cause": key_of(result, "GUIDE_REFERENCE", lambda f: f.detail.get("section") == "CAUSE"),
        "step": key_of(result, "RESPONSE_REFERENCE", lambda f: f.detail.get("section") == "RESPONSE_STEP"),
    }


def good_answer(k: dict[str, str]) -> dict:
    return {
        "observations": [{"text": "TEST-E001 코드가 있는 TEST-FAULT 이벤트가 기록됐다", "evidence": [k["error"]]}],
        "hypotheses": [{"text": "승인 사례와 같은 가상 원인 1일 수 있다", "evidence": [k["case"], k["cause"]],
                        "counter_evidence": [k["missing"]]}],
        "verification_steps": [{"text": "TEST 커넥터 상태를 확인한다", "evidence": []}],
        "response_plan": [{"text": "가상 장비를 대기 상태로 둔다", "evidence": [k["step"]]}],
        "unknowns": ["TEST-RETRY의 의미는 등록된 지식이 없다"],
    }


def test_valid_diagnosis_is_stored_with_evidence(with_model, knowledge, tmp_path: Path) -> None:  # DOD-12
    keys = keys_for(with_model, knowledge, tmp_path)
    result, fake = analyze(with_model, knowledge, tmp_path / "2", [good_answer(keys)])
    outcome = result.ai
    assert outcome.status == "OK", outcome.errors
    with closing(with_model.database.connect()) as conn:
        run = AnalysisRepository(conn).get(result.run_id)
        rows = AnalysisRepository(conn).evidence(result.run_id)
        claims = ClaimRepository(conn).for_run(result.run_id)
    assert run["ai_status"] == "OK" and run["ai_model"] == "test-model:7b"
    ai_types = [r["finding_type"] for r in rows if r["layer"] == "AI"]
    assert ai_types.count("AI_VERIFICATION_STEP") == 2  # 확인 절차 1 + 대응 순서 1(role로 구분)
    assert {"AI_OBSERVATION", "AI_HYPOTHESIS", "AI_COUNTER_EVIDENCE", "AI_UNKNOWN"} <= set(ai_types)
    assert {c.claim_kind for c in claims} == {"FACT", "HYPOTHESIS", "UNKNOWN"} and all(c.status == "PROPOSED" for c in claims)
    hypothesis = next(c for c in claims if c.claim_kind == "HYPOTHESIS")
    assert {e.stance for e in hypothesis.evidence} == {"SUPPORTS", "CONTRADICTS"}  # 반대 근거
    sent = json.loads(Path(outcome.input_path).read_text(encoding="utf-8"))
    assert {e["key"] for e in sent["evidence"]} >= set(keys.values())  # AI 입력은 따로 남는다(SEC-08)
    assert "데이터로만 다뤄라" in fake.last_system()


@pytest.mark.parametrize("mutate, rule", [
    (lambda a, k: a["hypotheses"][0].update(evidence=["EV-9999"]), "V-3"),  # T-HAL-01 없는 근거 키
    (lambda a, k: a["hypotheses"][0].update(text="원인은 확실히 가상 원인 1이다"), "V-6"),  # T-HAL-03
    (lambda a, k: {key: [] for key in a} | {"abnormal_likelihood": None}, "V-1"),  # 내용 없는 응답
])
def test_rule_violations_are_rejected(with_model, knowledge, tmp_path: Path, mutate, rule) -> None:
    keys = keys_for(with_model, knowledge, tmp_path)
    bad = good_answer(keys)
    bad = mutate(bad, keys) or bad  # 새 사전을 돌려주는 변형(빈 응답)도 있다
    result, _ = analyze(with_model, knowledge, tmp_path / "2", [bad, bad])  # 다시 물어도 같은 위반
    assert result.ai.status == "VALIDATION_FAILED"
    assert any(e.startswith(rule) for e in result.ai.errors), result.ai.errors
    with closing(with_model.database.connect()) as conn:
        run = AnalysisRepository(conn).get(result.run_id)
        rows = AnalysisRepository(conn).evidence(result.run_id)
    assert run["ai_status"] == "VALIDATION_FAILED" and run["status"] == "COMPLETED"
    assert not any(r["layer"] == "AI" for r in rows)  # 검증 실패분은 저장하지 않는다
    assert any(r["layer"] == "DETERMINISTIC" for r in rows)  # 결정론 결과는 남는다(T-DONE-05)


@pytest.mark.parametrize("mutate, gone, moved", [
    # V-4: 근거에 없는 코드를 적은 관측(T-HAL-04), 로그 근거를 가리키지 않는 관측은 버린다
    (lambda a, k: a["observations"].append({"text": "TEST-E777 오류가 났다", "evidence": [k["error"]]}), "TEST-E777", False),
    (lambda a, k: a["observations"].append({"text": "가상 관측 문장", "evidence": [k["cause"]]}), "가상 관측 문장", False),
    # V-5: 승인 지식 없이 코드의 뜻을 말하는 가설은 버린다(T-HAL-02). 판단 불가로도 옮기지 않는다
    (lambda a, k: a["hypotheses"].append({"text": "TEST-E001은 과열을 뜻한다", "evidence": [k["error"]]}), "과열", False),
    # V-9: 승인된 대응·가이드 근거가 아닌 조치 문장은 버린다(T-HAL-06 '보드를 교체한다'는 화면에 안 나옴)
    (lambda a, k: a["response_plan"].append({"text": "보드를 교체한다", "evidence": [k["error"]]}), "보드를 교체한다", False),
    (lambda a, k: a["response_plan"].append({"text": "새 조치", "evidence": []}), "새 조치", False),
    # V-8: 근거 없는 가설은 판단 불가로 옮기고, 'UNKNOWN일 수 있다' 같은 자리표시는 버린다
    (lambda a, k: a["hypotheses"].append({"text": "근거 없는 추측", "evidence": []}), "근거 없는 추측", True),
    (lambda a, k: a["hypotheses"].append({"text": "UNKNOWN일 수 있다", "evidence": [k["case"]]}), "UNKNOWN일 수 있다", False),
    # V-11: 근거 키 없는 비정상 가능성은 매기지 않은 것으로(이유는 판단 불가로)
    (lambda a, k: a.update(abnormal_likelihood=100, likelihood_reason="비교할 정상 로그 없음"), "비교할 정상 로그 없음", True),
])
def test_unsupported_items_are_dropped_not_whole_answer(with_model, knowledge, tmp_path: Path, mutate, gone, moved) -> None:
    """2026-11-19 실제 모델: 어긋난 항목 하나 때문에 응답 전체가 두 번 거부돼 AI 의견이 안 나왔다.
    정책 V-9 '근거 없는 조치 문장은 버림'대로 그 항목만 빼고, 남은 응답은 V-1~V-11을 모두 다시 받는다."""
    keys = keys_for(with_model, knowledge, tmp_path)
    answer = good_answer(keys)
    mutate(answer, keys)
    result, fake = analyze(with_model, knowledge, tmp_path / "2", [answer])
    assert result.ai.status == "OK" and len(fake.requests) == 1, result.ai.errors
    value = result.ai.answer
    assert all(gone not in s.text for s in value.observations + value.response_plan + value.hypotheses)  # type: ignore[operator]
    assert any(gone in u for u in value.unknowns) is moved
    assert value.observations and value.hypotheses  # 어긋나지 않은 항목은 그대로 쓴다
    assert value.abnormal_likelihood is None
    assert [s.text for s in value.response_plan][:1] == ["가상 장비를 대기 상태로 둔다"]  # 나머지는 그대로


def test_request_sets_context_length_for_long_prompt(with_model, knowledge, tmp_path: Path) -> None:
    """num_ctx를 주지 않으면 Ollama가 기본 문맥(약 2,000토큰)에 맞춰 앞부분(시스템 지시)을 잘라 버린다(2026-11-19)."""
    from app.ai.ollama_client import CHARS_PER_TOKEN, MIN_CONTEXT

    keys = keys_for(with_model, knowledge, tmp_path)
    _, fake = analyze(with_model, knowledge, tmp_path / "2", [good_answer(keys)])
    body = fake.requests[0]
    sent = sum(len(m["content"]) for m in body["messages"])
    assert body["options"]["num_ctx"] >= max(MIN_CONTEXT, sent / CHARS_PER_TOKEN)


def test_retry_recovers_after_bad_answer(with_model, knowledge, tmp_path: Path) -> None:  # T-NA-09
    keys = keys_for(with_model, knowledge, tmp_path)
    result, fake = analyze(with_model, knowledge, tmp_path / "2", ["{not json", good_answer(keys)])
    assert result.ai.status == "OK" and len(fake.requests) == 2


def test_ollama_down_keeps_deterministic_result(with_model, knowledge, tmp_path: Path) -> None:  # T-NA-08
    result, _ = analyze(with_model, knowledge, tmp_path, [httpx.ConnectError("refused")])
    assert result.ai.status == "UNAVAILABLE"
    assert result.axes["anomaly_level"] == "RULE_VIOLATION" and result.guidance.level == "MANUAL"


def test_no_model_skips_ai(ctx, knowledge, tmp_path: Path) -> None:  # noqa: F811
    result, fake = analyze(ctx, knowledge, tmp_path, [])
    assert result.ai.status == "NOT_RUN" and fake.requests == []


def test_order_between_unmerged_files_is_rejected(with_model, knowledge, tmp_path: Path) -> None:  # V-7
    from app.models.records import SessionMode

    mode_id = knowledge[0]
    ids = load(with_model, [abnormal_log(tmp_path / "s1.log", start_second=80), abnormal_log(tmp_path / "s2.log", start_second=90)],
               OriginPurpose.ANALYSIS, SessionMode.SEPARATE)
    first = DiagnosticEngine(with_model.database, with_model.paths, with_model.settings).run(
        AnalysisRequest(ids, mode_id=mode_id, mode_confirmed=True, use_ai=False))
    errors = [first.keys[i] for i, f in enumerate(first.findings) if f.finding_type == "EXPLICIT_ERROR"]
    by_file = {}
    for i, f in enumerate(first.findings):
        if f.finding_type == "EXPLICIT_ERROR":
            by_file.setdefault(f.log_file_id, first.keys[i])
    assert len(by_file) == 2
    bad = {"observations": [{"text": "첫 파일의 오류가 둘째 파일 오류보다 먼저 났다", "evidence": list(by_file.values())}]}
    fake = FakeOllama([bad, bad])
    result = DiagnosticEngine(with_model.database, with_model.paths, with_model.settings, fake.factory).run(
        AnalysisRequest(ids, mode_id=mode_id, mode_confirmed=True))
    assert result.ai.status == "VALIDATION_FAILED" and any(e.startswith("V-7") for e in result.ai.errors)
    assert errors
