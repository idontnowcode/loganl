"""CR-11 예상 에러 메시지 (EM-01·02·04). 로그·코드는 가상이다."""

from __future__ import annotations

from pathlib import Path

from app.analysis.anomaly_detector import Finding
from app.analysis.error_messages import (
    DIFFERENT,
    ERROR,
    FAIL_WORDS,
    MISSING,
    NEW_WARNING,
    WARNING,
    file_error_candidates,
    message_line,
    presumed_errors,
    reference_texts,
    should_show,
)


def findings() -> list[Finding]:
    return [
        Finding("DETERMINISTIC", "EXTRA_EVENT", "승인된 정상 사례에서 본 적 없는 이벤트 (20행): TEST-AUX ok", event_id=20,
                log_file_id=1, line_start=20),
        Finding("DETERMINISTIC", "VALUE_OUT_OF_RANGE", "'val' 값 303.7(30행): TEST-SAMPLE val=303.7. 평소 116~123",
                stance="SUPPORTS", event_id=30, log_file_id=1, line_start=30),
        Finding("DETERMINISTIC", "EXPLICIT_ERROR", "오류 코드 TEST-E409 이벤트가 4회 기록됨 (처음 40행): TEST-FAULT",
                stance="SUPPORTS", event_id=40, log_file_id=1, line_start=40,
                detail={"count": 4, "error_code": "TEST-E409"}),
        Finding("DETERMINISTIC", "REPETITION", "같은 이벤트가 연달아 4번 나옴(40행부터): TEST-FAULT", stance="SUPPORTS",
                event_id=40, log_file_id=1, line_start=40),
        Finding("DETERMINISTIC", "EXTRA_EVENT", "승인된 정상 사례에서 본 적 없는 이벤트 (50행): TEST-LINK", event_id=50,
                log_file_id=1, line_start=50),
        Finding("DETERMINISTIC", "MISSING_EVENT", "비슷한 정상 로그 20개 모두에 있던 이벤트가 이 로그에 없음: TEST-STOP done",
                stance="SUPPORTS", log_file_id=1),
        Finding("RETRIEVAL", "SIMILAR_NORMAL_CASE", "비슷한 정상 사례 C-0001", stance="CONTRADICTS", case_id=1),
    ]


RAW = {
    20: "2026-11-10 11:00:20 [INFO ] [TEST-AUX] TEST-AUX ok",
    30: "2026-11-10 11:00:30 [INFO ] [TEST-CH2] TEST-SAMPLE val=303.7",
    40: "2026-11-10 11:00:40 [ERROR] [TEST-SUB] TEST-FAULT code=TEST-E409",
    50: "2026-11-10 11:00:50 [INFO ] [TEST-LINK] TEST-AUTH result=FAIL",
}


def test_groups_order_and_original_lines() -> None:
    keys = {i: f"EV-{i + 1:04d}" for i in range(7)}
    picked = presumed_errors(findings(), keys, RAW, references={"TEST-E409": "명칭: 가상 명칭 [출처 가상 코드표]"})
    assert [(m.group, m.line) for m in picked] == [(ERROR, 40), (DIFFERENT, 30), (NEW_WARNING, 50), (MISSING, None)]
    error = picked[0]
    assert error.text == RAW[40] and error.count == 4 and error.key == "EV-0003"  # 원문 그대로
    assert error.reference == "명칭: 가상 명칭 [출처 가상 코드표]"  # 승인된 참고 자료 글 그대로(EM-02)
    assert error.reasons == ["같은 이벤트가 연달아 4번 나옴(40행부터): TEST-FAULT"]  # 같은 줄은 한 번만
    assert picked[2].text == RAW[50]  # 본 적 없는 이벤트 중 실패 낱말이 있는 것만
    assert picked[3].text == "TEST-STOP done"  # 빠진 메시지는 근거 요약에서
    assert all(m.reference is None for m in picked[1:])  # 참고 자료가 없으면 뜻을 붙이지 않는다
    assert message_line(error).startswith("[에러로 기록된 줄] 40행 ×4  ") and "참고 자료: 명칭" in message_line(error)


def test_reads_stored_rows_too() -> None:
    """보고서는 저장된 근거 행(dict·sqlite3.Row)에서 다시 고른다."""
    rows = [{"layer": f.layer, "finding_type": f.finding_type, "stance": f.stance, "summary": f.summary,
             "event_id": f.event_id, "log_file_id": f.log_file_id, "line_start": f.line_start} for f in findings()]
    picked = presumed_errors(rows, None, RAW, details=[f.detail for f in findings()])
    assert [m.group for m in picked] == [ERROR, DIFFERENT, NEW_WARNING, MISSING] and picked[0].code == "TEST-E409"


def test_fail_words_are_general_and_conservative() -> None:
    for text in ("result=NG", "TEST-AUTH FAIL", "connect timed out", "Timeout", "처리 실패", "경고: 온도"):
        assert FAIL_WORDS.search(text), text
    for text in ("ping ok", "TEST-SAMPLE val=1", "이상 없음", "string", "warned_flag"):
        assert not FAIL_WORDS.search(text), text


def test_show_only_for_abnormal_looking_logs() -> None:
    assert should_show("SUSPECTED", None) and should_show("RULE_VIOLATION", None)
    assert should_show("NONE_FOUND", 70) and not should_show("NONE_FOUND", 30) and not should_show("NONE_FOUND", None)


def test_reference_texts_take_first_source_verbatim() -> None:
    matches = [{"code": "TEST-E409", "entries": [{"title": "가상 명칭", "description": "가상 설명", "cause": "x",
                                                 "document_title": "가상 코드표"}]},
               {"code": "TEST-E001", "entries": []}]
    assert reference_texts(matches) == {"TEST-E409": "명칭: 가상 명칭 / 설명: 가상 설명 [출처 가상 코드표]"}


def test_file_candidates_without_normals(tmp_path: Path) -> None:  # EM-04
    from app.bootstrap import initialize
    from app.core.importer import LogImporter
    from app.models.enums import OriginPurpose
    from app.models.records import ImportRequest, SessionMode
    from tests.logdata import abnormal_log, normal_log

    ctx = initialize(tmp_path / "home", configure_logging=False)
    try:
        importer = LogImporter(ctx.database, ctx.paths, ctx.settings.import_)
        bad, good = importer.import_logs(ImportRequest(
            paths=[abnormal_log(tmp_path / "a.log"), normal_log(tmp_path / "n.log")], purpose=OriginPurpose.NORMAL_CASE,
            session_mode=SessionMode.SEPARATE)).log_file_ids
        picked = file_error_candidates(ctx.database, bad)
        assert [(m.group, m.line) for m in picked] == [(ERROR, 4), (WARNING, 5)]  # 1행은 가상 로그 머리 줄
        assert "TEST-FAULT code=TEST-E001" in picked[0].text and picked[0].count == 2 and picked[0].code == "TEST-E001"
        assert picked[1].reason == "WARN 레벨"
        assert file_error_candidates(ctx.database, good) == []
    finally:
        ctx.close()


def test_stage_and_context_lines(tmp_path: Path) -> None:  # CR-13 SQ-01
    from app.analysis.error_messages import ErrorMessage, attach_context
    from app.bootstrap import initialize
    from app.core.importer import LogImporter
    from app.models.enums import OriginPurpose
    from app.models.records import ImportRequest, SessionMode
    from tests.logdata import abnormal_log

    ctx = initialize(tmp_path / "home", configure_logging=False)
    try:
        bad = LogImporter(ctx.database, ctx.paths, ctx.settings.import_).import_logs(ImportRequest(
            paths=[abnormal_log(tmp_path / "a.log")], purpose=OriginPurpose.NORMAL_CASE,
            session_mode=SessionMode.SEPARATE)).log_file_ids[0]
        candidate = file_error_candidates(ctx.database, bad)[0]  # 4행 TEST-FAULT
        assert candidate.stage_line == 2 and "TEST-START step=1" in candidate.stage  # 가장 가까운 앞쪽 시작 표시 줄
        assert len(candidate.context) == 3 and "TEST-INIT value=10" in candidate.context[-1]
        message = ErrorMessage(group=ERROR, text="x", reason="y", log_file_id=bad, line=4)
        attach_context(ctx.database, [message])
        assert message.stage_line == 2 and message.context == candidate.context
        assert "직전 단계: 2행" in message_line(message)
    finally:
        ctx.close()
