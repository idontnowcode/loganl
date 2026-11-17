"""Phase 4 비정상 로그 학습 (T-AL-01~10, AL-02·06·08~19, 완료 기준 "비정상 사례와 진단 가이드를 함께 등록·조회 가능")."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from app.bootstrap import initialize
from app.core.decoding import BinaryFileError
from app.core.importer import ImportRejected, LogImporter
from app.learning.abnormal_learner import AbnormalLearner
from app.learning.approval_workflow import ApprovalWorkflow
from app.learning.case_manager import CaseError, CaseManager
from app.models.enums import OriginPurpose
from app.models.records import ImportRequest, SessionMode
from app.storage.repositories import ClaimRepository, GuideRepository, SessionRepository
from tests.fake_ollama import FakeOllama
from tests.logdata import abnormal_log
from tests.unit.test_guide_structurer import GUIDE, MANUAL


@pytest.fixture
def ctx(tmp_path: Path):
    context = initialize(tmp_path / "home", configure_logging=False)
    context.settings = context.settings.model_copy(
        update={"ollama": context.settings.ollama.model_copy(update={"model": "test-model:7b"})})
    yield context
    context.close()


def logs(ctx, tmp_path: Path, mode: SessionMode, n: int = 2) -> list[int]:
    paths = [abnormal_log(tmp_path / f"ab{i}.log", start_second=i * 30) for i in range(n)]
    return LogImporter(ctx.database, ctx.paths, ctx.settings.import_).import_logs(
        ImportRequest(paths=paths, purpose=OriginPurpose.ABNORMAL_CASE, session_mode=mode)).log_file_ids


def write_doc(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / "docs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


@pytest.fixture
def case(ctx, tmp_path: Path):
    ids = logs(ctx, tmp_path, SessionMode.SAME)
    case_id = CaseManager(ctx.database).create("ABNORMAL", "가상 비정상 사례", ids)
    return case_id, AbnormalLearner(ctx.database, ctx.paths, ctx.settings)


def test_guide_is_optional_for_approval(ctx, case) -> None:  # AL-02 → CR-06 LI-05
    case_id, _ = case
    workflow = ApprovalWorkflow(ctx.database)
    blocked = workflow.check(case_id)
    assert any("오류 증상" in b for b in blocked.blocking) and any("판정 기준" in b for b in blocked.blocking)
    CaseManager(ctx.database).update(case_id, symptom="증상", abnormal_criteria="기준")
    check = workflow.check(case_id)
    assert check.ok  # 가이드·원인 확정 여부 없이도 승인할 수 있다
    assert any("진단 가이드·대응 매뉴얼이 없습니다" in w for w in check.warnings)
    assert any("원인 확정 여부" in w for w in check.warnings)


def test_attach_structures_and_links(ctx, case, tmp_path: Path) -> None:  # T-AL-05, AL-06
    case_id, learner = case
    outcome = learner.attach(case_id, write_doc(tmp_path, "guide.md", GUIDE), "DIAGNOSTIC_GUIDE")
    assert outcome.items >= 6 and outcome.headings_found and not outcome.duplicate
    assert outcome.linked_codes == ["TEST-E001"]  # 로그 오류 코드가 가이드 원문에 나온다
    with closing(ctx.database.connect()) as conn:
        attachment = GuideRepository(conn).attachments(case_id)[0]
        claims = ClaimRepository(conn).for_case(case_id, source="GUIDE")
    stored = ctx.paths.root / attachment["stored_path"]
    assert stored.read_bytes() == GUIDE.encode("utf-8") and stored.is_relative_to(ctx.paths.guides)
    assert len(claims) == 1 and {e.evidence_type for e in claims[0].evidence} == {"GUIDE_SPAN", "EVENT"}
    again = learner.attach(case_id, write_doc(tmp_path, "guide_copy.md", GUIDE), "DIAGNOSTIC_GUIDE")
    assert again.duplicate and again.attachment_id == outcome.attachment_id


def test_attachment_rejections(ctx, case, tmp_path: Path) -> None:
    case_id, learner = case
    with pytest.raises(ImportRejected, match="확장자"):
        learner.attach(case_id, write_doc(tmp_path, "guide.pdf", "x"), "DIAGNOSTIC_GUIDE")
    binary = tmp_path / "docs" / "bin.txt"
    binary.write_bytes(b"\x00\x01binary")
    with pytest.raises(BinaryFileError):
        learner.attach(case_id, binary, "RESPONSE_MANUAL")
    normal_case = CaseManager(ctx.database).create("NORMAL", "정상")
    with pytest.raises(CaseError, match="비정상 사례에만"):
        learner.attach(normal_case, write_doc(tmp_path, "g.md", GUIDE), "DIAGNOSTIC_GUIDE")


def test_full_abnormal_cycle_with_optional_manual(ctx, case, tmp_path: Path) -> None:  # T-AL-02·06·07·09, CR-01
    case_id, learner = case
    learner.attach(case_id, write_doc(tmp_path, "guide.md", GUIDE), "DIAGNOSTIC_GUIDE")
    learner.attach(case_id, write_doc(tmp_path, "manual.txt", MANUAL), "RESPONSE_MANUAL")
    suggestion = learner.suggest_fields(case_id)
    assert "TEST-FAULT를 반복" in suggestion["symptom"] and "2회 이상" in suggestion["abnormal_criteria"]
    cases = CaseManager(ctx.database)
    cases.update(case_id, cause_status="UNKNOWN", **suggestion)  # 원인 미확정도 등록 가능
    with closing(ctx.database.connect()) as conn:
        items = GuideRepository(conn).items(case_id)
    decisions = {row["id"]: "APPROVED" for row in items}
    assert not any(row["section"] == "RESOLUTION" for row in items)  # 해결 방법 없이도 승인 가능
    revision = ApprovalWorkflow(ctx.database).approve(case_id, guide_decisions=decisions)
    assert revision == 1
    with closing(ctx.database.connect()) as conn:
        approved = GuideRepository(conn).approved_items_for_cases([case_id])
    kinds = {(r["attachment_kind"], r["section"]) for r in approved}
    assert ("RESPONSE_MANUAL", "RESPONSE_STEP") in kinds and ("DIAGNOSTIC_GUIDE", "SYMPTOM") in kinds
    detail = cases.detail(case_id)
    assert detail.case.cause_status == "UNKNOWN" and len(detail.files) == 2


def test_manual_is_optional(ctx, case, tmp_path: Path) -> None:  # T-AL-10
    case_id, learner = case
    learner.attach(case_id, write_doc(tmp_path, "guide.md", GUIDE), "DIAGNOSTIC_GUIDE")
    CaseManager(ctx.database).update(case_id, symptom="증상", abnormal_criteria="기준", cause_status="SUSPECTED")
    assert ApprovalWorkflow(ctx.database).approve(case_id) == 1


def test_guide_revision_keeps_old_attachment(ctx, case, tmp_path: Path) -> None:  # T-AL-08
    case_id, learner = case
    learner.attach(case_id, write_doc(tmp_path, "guide.md", GUIDE), "DIAGNOSTIC_GUIDE")
    CaseManager(ctx.database).update(case_id, symptom="증상", abnormal_criteria="기준", cause_status="UNKNOWN")
    workflow = ApprovalWorkflow(ctx.database)
    workflow.approve(case_id)
    with pytest.raises(CaseError, match="수정 시작"):
        learner.attach(case_id, write_doc(tmp_path, "guide_v2.md", GUIDE + "\n## 해결 방법\n가상 조치\n"),
                       "DIAGNOSTIC_GUIDE")
    workflow.reopen(case_id)
    learner.attach(case_id, write_doc(tmp_path, "guide_v2.md", GUIDE + "\n## 해결 방법\n가상 조치\n"), "DIAGNOSTIC_GUIDE")
    assert workflow.approve(case_id) == 2
    with closing(ctx.database.connect()) as conn:
        names = [r["original_name"] for r in GuideRepository(conn).attachments(case_id)]
    assert names == ["guide.md", "guide_v2.md"]
    assert len(workflow.revisions(case_id)) == 2


@pytest.mark.parametrize("mode, same", [(SessionMode.SAME, True), (SessionMode.SEPARATE, False)])
def test_sessions_distinguish_same_time_and_reproduction(ctx, tmp_path: Path, mode, same) -> None:  # T-AL-03·04
    ids = logs(ctx, tmp_path, mode)
    detail_files = CaseManager(ctx.database).detail(CaseManager(ctx.database).create("ABNORMAL", "s", ids)).files
    sessions = {f.session_id for f in detail_files}
    assert (len(sessions) == 1) is same
    with closing(ctx.database.connect()) as conn:
        assert all(SessionRepository(conn).get(s).relation_confirmed for s in sessions)


def test_ai_relabels_only_existing_text(ctx, case, tmp_path: Path) -> None:
    case_id, _ = case
    no_heading = "가상 증상 문장이다.\n\n가상 원인일 수 있다.\n\n가상 확인 절차."
    learner = AbnormalLearner(ctx.database, ctx.paths, ctx.settings, FakeOllama([
        {"labels": [{"segment": "S-0001", "section": "SYMPTOM"}, {"segment": "S-0002", "section": "CAUSE"},
                    {"segment": "S-0003", "section": "CHECK_METHOD"}]},
    ]).factory)
    outcome = learner.attach(case_id, write_doc(tmp_path, "free.txt", no_heading), "DIAGNOSTIC_GUIDE")
    assert not outcome.headings_found
    result = learner.restructure_with_ai(case_id, outcome.attachment_id)
    assert result.status == "OK"
    with closing(ctx.database.connect()) as conn:
        items = GuideRepository(conn).items(case_id)
    assert [(r["section"], r["source_kind"]) for r in items] == [
        ("CAUSE", "AI_PROPOSED"), ("CHECK_METHOD", "AI_PROPOSED"), ("SYMPTOM", "AI_PROPOSED")]
    for row in items:
        assert no_heading[row["span_start"]:row["span_end"]].strip() == row["content"]


def test_ai_unknown_segment_is_rejected(ctx, case, tmp_path: Path) -> None:
    case_id, _ = case
    fake = FakeOllama([{"labels": [{"segment": "S-0099", "section": "CAUSE"}]}] * 2)
    learner = AbnormalLearner(ctx.database, ctx.paths, ctx.settings, fake.factory)
    outcome = learner.attach(case_id, write_doc(tmp_path, "free.txt", "가상 문장.\n\n둘째 문단."), "DIAGNOSTIC_GUIDE")
    result = learner.restructure_with_ai(case_id, outcome.attachment_id)
    assert result.status == "VALIDATION_FAILED" and "S-0099" in result.errors[0]
    with closing(ctx.database.connect()) as conn:
        assert [r["source_kind"] for r in GuideRepository(conn).items(case_id)] == ["GUIDE_TEXT"]  # 원래 항목 유지
