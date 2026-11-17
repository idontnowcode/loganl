"""Phase 3 정상 로그 학습 흐름 (T-NL-01~06, DB-04·05, PRN-06·10, 완료 기준 "정상 사례를 등록하고 재조회 가능")."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from app.bootstrap import initialize
from app.core.importer import LogImporter
from app.learning.approval_workflow import ApprovalError, ApprovalWorkflow
from app.learning.case_manager import CaseError, CaseManager, ModeManager
from app.learning.normal_learner import NormalLearner
from app.models.enums import OriginPurpose
from app.models.records import FileMeta, ImportRequest, SessionMode
from app.storage.repositories import ClaimRepository, PatternRepository
from tests.fake_ollama import FakeOllama
from tests.logdata import NORMAL_SEQUENCE, iso_lines, normal_log, write


@pytest.fixture
def ctx(tmp_path: Path):
    context = initialize(tmp_path / "home", configure_logging=False)
    context.settings = context.settings.model_copy(
        update={"ollama": context.settings.ollama.model_copy(update={"model": "test-model:7b"})})
    yield context
    context.close()


def import_logs(ctx, *paths: Path, role: str = "TEST-ROLE") -> list[int]:
    result = LogImporter(ctx.database, ctx.paths, ctx.settings.import_).import_logs(ImportRequest(
        paths=list(paths), purpose=OriginPurpose.NORMAL_CASE, session_mode=SessionMode.SEPARATE,
        meta=FileMeta(log_role=role)))
    return result.log_file_ids


@pytest.fixture
def setup(ctx, tmp_path: Path):
    mode_id = ModeManager(ctx.database).add("테스트모드A", "가상 운전 모드")
    ids = import_logs(ctx, normal_log(tmp_path / "n1.log"), normal_log(tmp_path / "n2.log", start_second=20))
    cases = CaseManager(ctx.database)
    case_id = cases.create("NORMAL", "정상 사례 1", ids)
    return mode_id, case_id, cases


def test_create_update_and_reload(ctx, setup) -> None:  # T-NL-02·03
    mode_id, case_id, cases = setup
    cases.update(case_id, operating_conditions="TEST 조건", normal_rationale="사용자가 정상 동작 확인", fw_version="T1")
    note = cases.add_note(case_id, "첫 메모")
    cases.update_note(case_id, note, "고친 메모")
    cases.set_mode(case_id, mode_id, confirmed=True)
    detail = cases.detail(case_id)
    assert detail.case.normal_rationale == "사용자가 정상 동작 확인" and detail.case.mode_confirmed
    assert [n.body for n in detail.notes] == ["고친 메모"]
    assert len(detail.files) == 2
    assert [c.label for c in cases.list("NORMAL")] == [f"C-{case_id:04d} 정상 사례 1"]


def test_patterns_need_mode(setup, ctx) -> None:
    _, case_id, _ = setup
    with pytest.raises(CaseError, match="운전 모드"):
        NormalLearner(ctx.database, ctx.settings).propose(case_id)


def test_approval_blocks_until_required_info(setup, ctx) -> None:
    mode_id, case_id, cases = setup
    workflow = ApprovalWorkflow(ctx.database)
    check = workflow.check(case_id)
    assert any("운전 모드" in b for b in check.blocking) and any("정상 판정 근거" in b for b in check.blocking)
    with pytest.raises(ApprovalError):
        workflow.approve(case_id)
    assert workflow.request_review(case_id).blocking
    assert cases.get(case_id).status == "NEEDS_INFORMATION"
    cases.set_mode(case_id, mode_id, True)
    cases.update(case_id, normal_rationale="정상")
    assert workflow.request_review(case_id).ok
    assert cases.get(case_id).status == "READY_FOR_REVIEW"


def approve_with_patterns(ctx, case_id: int, mode_id: int, cases: CaseManager) -> list:
    cases.set_mode(case_id, mode_id, True)
    cases.update(case_id, normal_rationale="사용자 확인")
    patterns = NormalLearner(ctx.database, ctx.settings).propose(case_id)
    decisions = {p.id: ("APPROVED" if p.pattern_type in ("REQUIRED", "ORDER_CONSTRAINT") else "REJECTED") for p in patterns}
    ApprovalWorkflow(ctx.database).approve(case_id, pattern_decisions=decisions, change_summary="첫 승인")
    return patterns


def test_full_normal_cycle(ctx, setup, tmp_path: Path) -> None:  # T-NL-06, DB-05
    mode_id, case_id, cases = setup
    patterns = approve_with_patterns(ctx, case_id, mode_id, cases)
    types = {p.pattern_type for p in patterns}
    assert {"REQUIRED", "ORDER_CONSTRAINT"} <= types  # 로그 2개 → 필수·순서 후보
    case = cases.get(case_id)
    assert (case.status, case.current_revision) == ("APPROVED", 1)
    with closing(ctx.database.connect()) as conn:
        approved = PatternRepository(conn).approved_for_mode(mode_id)
    assert approved and all(p.pattern_type in ("REQUIRED", "ORDER_CONSTRAINT") for p in approved)

    with pytest.raises(CaseError, match="수정 시작"):
        cases.update(case_id, title="바로 고치기")

    workflow = ApprovalWorkflow(ctx.database)
    workflow.reopen(case_id)
    with closing(ctx.database.connect()) as conn:
        assert PatternRepository(conn).approved_for_mode(mode_id) == []  # 수정 중에는 확정 지식에서 빠진다
    cases.update(case_id, title="정상 사례 1 (개정)")
    assert workflow.approve(case_id, change_summary="제목 수정") == 2
    revisions = workflow.revisions(case_id)
    assert [r["revision_no"] for r in revisions] == [1, 2]
    assert '"title": "정상 사례 1"' in revisions[0]["snapshot_json"]  # 이전 개정본 보존
    with pytest.raises(CaseError):
        cases.delete(case_id)


def test_second_case_compares_with_approved_case(ctx, setup, tmp_path: Path) -> None:  # NL-09
    mode_id, case_id, cases = setup
    approve_with_patterns(ctx, case_id, mode_id, cases)
    new_ids = import_logs(ctx, write(tmp_path / "n3.log", iso_lines(NORMAL_SEQUENCE[:3] + [("INFO", "TEST-CTRL", "TEST-EXTRA")])))
    second = cases.create("NORMAL", "정상 사례 2", new_ids)
    cases.set_mode(second, mode_id, True)
    patterns = NormalLearner(ctx.database, ctx.settings).propose(second)
    sample_sizes = {p.definition.get("sample_files") for p in patterns}
    assert sample_sizes == {3}  # 승인된 사례의 로그 2개 + 새 로그 1개
    optional = {p.definition["events"][0] for p in patterns if p.pattern_type == "OPTIONAL"}
    assert any("TEST-EXTRA" in e for e in optional) and any("TEST-RUN" in e for e in optional)


def test_draft_case_can_be_deleted_and_transitions(ctx, setup) -> None:
    _, case_id, cases = setup
    workflow = ApprovalWorkflow(ctx.database)
    workflow.reject(case_id, "가상 반려")
    assert cases.get(case_id).status == "REJECTED"
    workflow.archive(case_id)
    workflow.restore(case_id)
    assert cases.get(case_id).status == "DRAFT"
    cases.delete(case_id)
    assert cases.list("NORMAL") == []


# ── AI (가짜 Ollama) ──
def test_mode_estimate_with_ai(ctx, setup) -> None:  # T-NL-04
    mode_id, case_id, _ = setup
    fake = FakeOllama([{"mode": "테스트모드A", "reason": "TEST-START 이후 TEST-RUN", "evidence": ["EV-0002"]}])
    guess = NormalLearner(ctx.database, ctx.settings, fake.factory).estimate_mode(case_id)
    assert guess.status == "OK" and guess.mode_id == mode_id
    assert "TEST-START" in fake.last_prompt()


def test_ai_draft_is_stored_as_proposed_claims(ctx, setup) -> None:
    mode_id, case_id, cases = setup
    cases.set_mode(case_id, mode_id, True)
    NormalLearner(ctx.database, ctx.settings).propose(case_id)
    fake = FakeOllama([{
        "summary": "가상 정상 흐름",
        "observations": [{"text": "TEST-START가 먼저 나온다", "kind": "FACT", "evidence": ["EV-0002"]},
                         {"text": "준비 단계일 수 있다", "kind": "HYPOTHESIS", "evidence": []}],
        "missing_info": ["TEST-READY가 항상 나오는지"],
    }])
    outcome = NormalLearner(ctx.database, ctx.settings, fake.factory).generate_draft(case_id)
    assert outcome.status == "OK" and len(outcome.claim_ids) == 4
    with closing(ctx.database.connect()) as conn:
        claims = ClaimRepository(conn).for_case(case_id, source="AI")
    assert all(c.status == "PROPOSED" for c in claims)
    fact = next(c for c in claims if c.claim_kind == "FACT")
    assert fact.evidence and fact.evidence[0].evidence_type == "EVENT"
    assert any(c.claim_kind == "UNKNOWN" and "확인 필요" in c.claim_text for c in claims)


def test_ai_failure_keeps_rule_based_learning(ctx, setup) -> None:
    mode_id, case_id, cases = setup
    fake = FakeOllama(["{broken", "{broken"])
    outcome = NormalLearner(ctx.database, ctx.settings, fake.factory).generate_draft(case_id)
    assert outcome.status == "VALIDATION_FAILED" and outcome.claim_ids == []
    with closing(ctx.database.connect()) as conn:
        assert ClaimRepository(conn).for_case(case_id) == []
    cases.set_mode(case_id, mode_id, True)
    assert NormalLearner(ctx.database, ctx.settings).propose(case_id)  # AI 없이도 후보는 만든다
