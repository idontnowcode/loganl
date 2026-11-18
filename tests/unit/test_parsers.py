"""파서 프로파일·레지스트리·정규화 (Phase 2 "이벤트 파싱", "원본 행 연결", DEV-12)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.cancel import Cancelled, CancelToken
from app.core.decoding import decode_log_bytes
from app.core.event_normalizer import EventTyper, message_template
from app.models.enums import EventTimeKind, TimestampBasis
from app.models.records import EventRule, ParserProfile
from app.parsers.base_parser import RawTextParser
from app.parsers.generic_parser import RegexProfileParser
from app.parsers.parser_registry import ParserRegistry, UnknownProfileError
from tests.logdata import NORMAL_SEQUENCE, iso_lines


def lines_of(text: str):
    return decode_log_bytes(text.encode("utf-8")).lines


def assert_no_loss(text: str, events) -> None:
    """빈 줄이 아닌 모든 원본 줄이 정확히 하나의 이벤트에, 같은 줄 번호로 들어 있다."""
    original = {line.line_no: line.text for line in lines_of(text)}
    covered: dict[int, str] = {}
    for event in events:
        parts = event.raw_text.split("\n")
        expected_end = event.line_end or event.line_no
        assert len(parts) == expected_end - event.line_no + 1
        for offset, part in enumerate(parts):
            number = event.line_no + offset
            assert number not in covered, f"{number}행이 두 이벤트에 들어 있다"
            covered[number] = part
    for number, content in original.items():
        if content.strip():
            assert covered.get(number) == content, f"{number}행 손실"


@pytest.fixture(scope="module")
def registry() -> ParserRegistry:
    return ParserRegistry.default()


def test_shipped_profiles_load(registry: ParserRegistry) -> None:
    assert {"generic_iso_level", "generic_iso_plain", "generic_relative_seconds"} <= set(registry.names())
    assert registry.load_errors == []


def test_iso_level_fields(registry: ParserRegistry) -> None:
    text = "\n".join(iso_lines(NORMAL_SEQUENCE[:2], tz="Z") + ["2026-11-16 10:00:01.000Z ERROR TEST-SUB: TEST-FAULT code=TEST-E001"])
    parser = registry.select([l.text for l in lines_of(text)])
    assert parser.name == "generic_iso_level"
    events = list(parser.parse(lines_of(text)))
    assert len(events) == 4
    fault = events[-1]
    assert fault.level == "ERROR" and fault.component == "TEST-SUB"
    assert fault.error_code == "TEST-E001"
    assert fault.ts_kind == EventTimeKind.ABSOLUTE and fault.ts_value_us is not None
    assert fault.line_no == 4 and fault.byte_offset is not None
    assert events[2].ts_value_us - events[1].ts_value_us == 100_000  # 0번은 머리 줄
    assert_no_loss(text, events)


def test_naive_timestamp_is_marked(registry: ParserRegistry) -> None:
    text = "\n".join(iso_lines(NORMAL_SEQUENCE))
    events = list(registry.get("generic_iso_level").parse(lines_of(text)))
    assert {e.ts_kind for e in events} == {EventTimeKind.ABSOLUTE_NO_TZ}


def test_multiline_events_keep_every_line(registry: ParserRegistry) -> None:
    text = "\n".join([
        "앞에 붙은 형식 밖 줄",
        "2026-11-16 10:00:00.000 ERROR TEST-CTRL: TEST-TRACE begin",
        "    at frame 1",
        "",
        "    at frame 2",
        "",
        "2026-11-16 10:00:01.000 INFO TEST-CTRL: TEST-NEXT",
    ])
    events = list(registry.get("generic_iso_level").parse(lines_of(text)))
    assert [(e.line_no, e.line_end) for e in events] == [(1, None), (2, 5), (7, None)]
    assert events[0].attrs == {"unparsed": True}
    assert events[1].raw_text.split("\n")[2] == ""  # 이벤트 안의 빈 줄도 보존
    assert_no_loss(text, events)


def test_relative_profile(registry: ParserRegistry) -> None:
    text = "[    0.500000] TEST-CTRL: TEST-START\n[    1.250000] TEST-RUN value=3\n"
    parser = registry.select([l.text for l in lines_of(text)])
    assert parser.name == "generic_relative_seconds"
    assert parser.timestamp_basis == TimestampBasis.RELATIVE
    events = list(parser.parse(lines_of(text)))
    assert [e.ts_value_us for e in events] == [500_000, 1_250_000]
    assert events[0].component == "TEST-CTRL" and events[1].component is None


def test_unknown_format_falls_back_to_raw(registry: ParserRegistry) -> None:
    text = "TEST free text one\n\nTEST free text two\n"
    parser = registry.select([l.text for l in lines_of(text)])
    assert parser.name == "raw"
    assert parser.timestamp_basis == TimestampBasis.UNKNOWN
    events = list(parser.parse(lines_of(text)))
    assert [e.line_no for e in events] == [1, 3]
    assert_no_loss(text, events)


def test_plain_iso_without_level_is_not_misread(registry: ParserRegistry) -> None:
    text = "2026-11-16 10:00:00 Started TEST-RUN\n2026-11-16 10:00:01 Stopped TEST-RUN\n"
    parser = registry.select([l.text for l in lines_of(text)])
    assert parser.name == "generic_iso_plain"
    events = list(parser.parse(lines_of(text)))
    assert events[0].level is None and events[0].message == "Started TEST-RUN"


def test_explicit_profile_and_unknown_name(registry: ParserRegistry) -> None:
    assert registry.select(["anything"], "generic_iso_plain").name == "generic_iso_plain"
    assert registry.select(["anything"], "raw").name == "raw"
    with pytest.raises(UnknownProfileError):
        registry.select(["anything"], "no_such_profile")


def test_event_type_groups_varying_values() -> None:
    assert message_template("TEST-RUN value=42 at 0x1F id='abc'") == message_template("TEST-RUN value=7 at 0xFF id='z'")
    typer = EventTyper([EventRule(name="사용자정의-시작", regex=r"TEST-START")])
    assert typer.event_type("TEST-START step=1", "TEST-CTRL") == "사용자정의-시작"
    assert typer.event_type("TEST-RUN value=1", "TEST-CTRL") == "TEST-CTRL|TEST-RUN value=<N>"


def test_profile_requires_message_group() -> None:
    with pytest.raises(ValidationError, match="message"):
        ParserProfile(name="bad", line_regex=r"^(?P<ts>\d+) (?P<text>.*)$")


def test_broken_profile_file_is_reported(tmp_path: Path) -> None:
    (tmp_path / "ok.json").write_text(json.dumps({"name": "ok", "line_regex": "^(?P<message>.*)$"}), encoding="utf-8")
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    registry = ParserRegistry()
    assert registry.load_profiles(tmp_path) == ["ok"]
    assert len(registry.load_errors) == 1 and "bad.json" in registry.load_errors[0]


def test_sniff_threshold() -> None:
    parser = RegexProfileParser(ParserProfile(name="p", line_regex=r"^OK (?P<message>.*)$", sniff_threshold=0.8))
    assert parser.sniff(["OK a", "OK b", "no", "OK c"]) == 0.0  # 75% < 80%
    assert parser.sniff(["OK a", "OK b", "OK c", "OK d", "no"]) == 0.8


def test_parse_can_be_cancelled() -> None:
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        list(RawTextParser().parse(lines_of("a\nb\n"), token))


# ── CR-08 PA-01~03: 새 줄 형식 (모두 가상 형식, TEST- 이름) ──

def parse_text(registry: ParserRegistry, text: str, *, encoding: str = "utf-8"):
    lines = decode_log_bytes(text.encode(encoding)).lines
    parser = registry.select([line.text for line in lines])
    return parser, list(parser.parse(lines))


def test_json_lines_profile(registry: ParserRegistry) -> None:
    text = (
        '{"ts": "2026-11-11T13:14:22.103+09:00", "level": "info", "module": "TEST-CTRL", "msg": "TEST-START mode=A", "seq": 1}\n'
        '{"ts": "2026-11-11T13:14:23.103+09:00", "level": "error", "module": "TEST-SUB", '
        '"msg": "TEST-FAULT code=TEST-E101", "detail": ["a b"], "seq": 2}\n'
    )
    parser, events = parse_text(registry, text)
    assert parser.name == "json_lines"
    assert [e.level for e in events] == ["INFO", "ERROR"] and events[1].component == "TEST-SUB"
    assert events[1].error_code == "TEST-E101"
    assert events[1].message == 'TEST-FAULT code=TEST-E101 detail=["a b"] seq=2'  # 남은 키는 key=value로
    assert events[1].ts_value_us - events[0].ts_value_us == 1_000_000 and events[0].ts_kind == EventTimeKind.ABSOLUTE
    assert events[0].event_type != events[1].event_type  # 예전엔 따옴표 때문에 모든 줄이 한 종류였다
    assert_no_loss(text, events)


def test_logfmt_profile(registry: ParserRegistry) -> None:
    text = ('time=2026-11-11T06:52:28.357Z level=info comp=TEST-CTRL msg="TEST-START" mode=A step=1\n'
            'time=2026-11-11T06:52:29.000Z level=warn comp=TEST-SUB msg="TEST-WAIT busy" retry=2\n')
    parser, events = parse_text(registry, text)
    assert parser.name == "logfmt"
    assert events[0].message == "TEST-START mode=A step=1" and events[1].message == "TEST-WAIT busy retry=2"
    assert events[1].level == "WARN" and events[1].component == "TEST-SUB" and events[1].ts_kind == EventTimeKind.ABSOLUTE


def test_csv_header_profile(registry: ParserRegistry) -> None:
    text = ("timestamp,level,module,message\n"
            '2026-11-11 12:44:11.976,INFO,TEST-CTRL,"TEST-START mode=B, step=1"\n'
            "2026-11-11 12:44:12.339,ERROR,TEST-SUB,TEST-FAULT code=TEST-E002\n")
    parser, events = parse_text(registry, text)
    assert parser.name == "csv_header"
    assert events[0].attrs == {"header": True} and events[0].level is None
    assert events[1].message == "TEST-START mode=B, step=1" and events[1].component == "TEST-CTRL"
    assert events[2].error_code == "TEST-E002" and events[2].ts_kind == EventTimeKind.ABSOLUTE_NO_TZ
    assert_no_loss(text, events)


def test_tsv_header_us_date_and_continuation(registry: ParserRegistry) -> None:
    text = ("Level\tDate and Time\tSource\tMessage\n"
            "Information\t11/14/2026 12:31:15 PM\tTEST-CTRL\tTEST-START\n"
            "Critical\t11/14/2026 4:05:17 PM\tTEST-SUB\tTEST-CRASH code=TEST-E003\n"
            "    frame 0: TEST-MOD-A\n")
    parser, events = parse_text(registry, text)
    assert parser.name == "tsv_header"
    start, crash = events[1], events[2]
    assert start.level == "INFO" and crash.level == "FATAL" and crash.line_end == 4
    assert (crash.ts_value_us - start.ts_value_us) // 1_000_000 == (16 * 3600 + 5 * 60 + 17) - (12 * 3600 + 31 * 60 + 15)
    assert_no_loss(text, events)


def test_syslog_bsd_profile_without_year(registry: ParserRegistry) -> None:
    text = ("<14>Nov  5 15:08:16 TEST-HOST01 TEST-CTRL[7110]: TEST-START\n"
            "<11>Nov  5 15:09:01 TEST-HOST01 TEST-SUB[2469]: TEST-CHECK result=NG code=TEST-E004\n"
            "<15>Dec 31 23:59:59 TEST-HOST01 TEST-SUB[2469]: TEST-LATE\n"
            "<14>Jan  1 00:00:01 TEST-HOST01 TEST-SUB[2469]: TEST-NEW-YEAR\n")
    parser, events = parse_text(registry, text)
    assert parser.name == "syslog_bsd" and parser.timestamp_basis == TimestampBasis.RELATIVE
    assert [e.level for e in events] == ["INFO", "ERROR", "DEBUG", "INFO"]
    assert events[0].component == "TEST-CTRL" and events[1].error_code == "TEST-E004"
    assert {e.ts_kind for e in events} == {EventTimeKind.RELATIVE}
    assert events[0].ts_value_us == 0 and events[1].ts_value_us == 45_000_000  # 지어낸 연도 없이 경과 시간
    assert events[3].ts_value_us - events[2].ts_value_us == 2_000_000  # 해가 바뀌어도 이어진다


def test_ymd_bracket_level_letters_and_korean(registry: ParserRegistry) -> None:
    slash = ("2026/11/10 15:32:06.080 [I] TEST-CTRL - TEST-START\n"
             "2026/11/10 15:32:07.152 [E] TEST-SUB - TEST-FAULT code=TEST-E005\n")
    parser, events = parse_text(registry, slash)
    assert parser.name == "ymd_bracket_level" and [e.level for e in events] == ["INFO", "ERROR"]
    korean = ("2026.11.05 오후 12:18:24.597 [정보] TEST-CTRL > 시작: TEST-START\n"
              "2026.11.05 오후 01:18:24.597 [오류] TEST-SUB > TEST-SEQ code=TEST-E006\n"
              "2026.11.06 오전 12:00:00.000 [경고] TEST-SUB > TEST-WAIT\n")
    parser, events = parse_text(registry, korean, encoding="cp949")
    assert parser.name == "ymd_bracket_level"
    assert [e.level for e in events] == ["INFO", "ERROR", "WARN"] and events[0].component == "TEST-CTRL"
    hour = 3_600_000_000
    assert events[1].ts_value_us - events[0].ts_value_us == hour  # 오후 12시 → 오후 1시
    assert events[2].ts_value_us - events[1].ts_value_us == 11 * hour - 18 * 60_000_000 - 24_597_000  # 오전 12시 = 자정


def test_epoch_pipe_profile_auto_unit(registry: ParserRegistry) -> None:
    text = "1794108168264|INF|TEST-CTRL|TEST-START\n1794108169264|ERR|TEST-CTRL|TEST-INIT failed code=TEST-E007\n"
    parser, events = parse_text(registry, text)
    assert parser.name == "epoch_pipe" and [e.level for e in events] == ["INFO", "ERROR"]
    assert events[0].ts_value_us == 1794108168264 * 1000 and events[1].error_code == "TEST-E007"


def test_time_only_with_date_header(registry: ParserRegistry) -> None:
    text = ("==== 날짜 2026-11-12 ====\n"
            "[23:59:59.500] I TEST-CTRL    TEST-RUN cycle=1\n"
            "==== 날짜 2026-11-13 ====\n"
            "[00:00:00.500] E TEST-SUB     TEST-FAULT code=TEST-E008\n")
    parser, events = parse_text(registry, text)
    assert parser.name == "time_only_date_header"
    assert [e.attrs for e in events] == [{"date_line": True}, None, {"date_line": True}, None]
    run, fault = events[1], events[3]
    assert run.component == "TEST-CTRL" and fault.level == "ERROR" and fault.error_code == "TEST-E008"
    assert fault.ts_value_us - run.ts_value_us == 1_000_000  # 자정을 넘겨도 날짜 머리 줄로 이어짐
    assert_no_loss(text, events)


def test_iso_angle_level_and_indented_blocks_still_sniff(registry: ParserRegistry) -> None:
    lines = ["2026-11-05T11:50:06.011026+09:00 <info> TEST-CTRL: TEST-RUN cycle=1"]
    for cycle in range(2, 12):  # 들여 쓴 블록이 줄 수의 절반을 넘어도 형식 판정이 떨어지지 않는다(PA-03)
        lines += [f"2026-11-05T11:50:{cycle:02d}.000000+09:00 <warning> TEST-CTRL: TEST-RUN cycle={cycle}",
                  "    | param.a = 1", "    | param.b = 2"]
    parser, events = parse_text(registry, "\n".join(lines) + "\n")
    assert parser.name == "generic_iso_level"
    assert events[0].level == "INFO" and events[1].level == "WARNING" and events[1].line_end == 4


def test_new_profile_options_validate() -> None:
    with pytest.raises(ValidationError, match="AM 또는 PM"):
        ParserProfile(name="p", line_regex=r"^(?P<message>.*)$",
                      timestamp={"kind": "strptime", "formats": ["%H:%M"], "ampm": {"오전": "MORNING"}})
    structured = ParserProfile(name="j", line_format="json")  # 구조형은 line_regex가 없어도 된다
    assert structured.line_regex == ""
