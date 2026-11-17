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
