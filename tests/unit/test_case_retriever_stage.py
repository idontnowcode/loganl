"""CR-13 SQ-02: 같은 오류 코드라도 직전 단계(시퀀스 위치)가 같은 사례를 앞에 둔다. 로그·코드는 가상이다."""

from __future__ import annotations

from app.analysis.case_retriever import compare, stage_match
from app.core.feature_extractor import code_stages, extract, is_stage
from tests.unit.test_reference_checks import make_file as _make_file


def make_file(file_id: int, lines: list[tuple]):
    """시험용 파일 특징. 파서처럼 code=TEST-E… 를 오류 코드 칸에 넣는다."""
    _features, events = _make_file(file_id, lines)
    events = [e.model_copy(update={"error_code": "TEST-E310"}) if "code=TEST-E310" in e.message else e for e in events]
    from app.models.enums import OriginPurpose, TimestampBasis
    from app.models.records import LogFileRecord

    record = LogFileRecord(id=file_id, session_id=file_id, original_name=f"TEST-{file_id}.log", stored_path="x",
                           sha256="0" * 64, size_bytes=1, timestamp_basis=TimestampBasis.ABSOLUTE_NO_TZ,
                           origin_purpose=OriginPurpose.NORMAL_CASE, imported_at="2026-11-22T00:00:00.000Z")
    return extract(record, events), events

FAULT = ("ERROR", "TEST-BED", "TEST-FAULT code=TEST-E310 axis=H")


def flow(fault_after: str) -> list[tuple]:
    """적재 단계와 촬영 단계가 있는 가상 흐름. fault_after 단계 시작 바로 뒤에 같은 오류."""
    lines = []
    for stage in ("LOAD", "SCAN"):
        lines.append(("INFO", "TEST-SYS", f"TEST-{stage}-START step=1"))
        if stage == fault_after:
            lines.append(FAULT)
        lines += [("INFO", "TEST-BED", f"TEST-{stage}-MOVE pos=10"), ("INFO", "TEST-SYS", f"TEST-{stage}-DONE step=2")]
    return lines


def test_stage_words_are_general() -> None:
    for text in ("TEST-SCAN-START step=1", "TEST-ENTER zone=2", "begin cycle", "측정 시작", "구역 진입"):
        assert is_stage(text), text
    for text in ("TEST-RESTARTED", "TEST-STARTUP", "TEST-MOVE pos=1"):  # 낱말 경계: 앞뒤에 글자가 붙은 말은 아님
        assert not is_stage(text), text


def test_code_stage_is_nearest_start_before_error() -> None:
    features, _ = make_file(1, flow("SCAN"))
    assert code_stages(features) == {"TEST-E310": "TEST-SYS|TEST-SCAN-START step=<N>"}


def test_same_stage_case_ranks_above_other_stage() -> None:
    target, _ = make_file(1, flow("SCAN"))
    at_load, _ = make_file(2, flow("LOAD"))
    at_scan, _ = make_file(3, flow("SCAN"))
    assert stage_match([target], [at_scan], ["TEST-E310"]) == (["TEST-E310"], [])
    assert stage_match([target], [at_load], ["TEST-E310"]) == ([], ["TEST-E310"])
    same, other = compare([target], [at_scan])[0], compare([target], [at_load])[0]
    assert same > other  # 유형·코드가 같아도 다른 단계면 코드 몫이 절반
