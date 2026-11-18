"""자동 형식 인식 파서 (CR-08 PA-04). 프로파일에 없는 줄 형식도 시각·레벨·구성요소·메시지를 읽는다.
모두 가상 형식(TEST- 이름)이다."""

from __future__ import annotations

import pytest

from app.core.decoding import decode_log_bytes
from app.models.enums import EventTimeKind
from app.parsers.auto_parser import AutoParser, detect_layout, level_of
from app.parsers.parser_registry import ParserRegistry


@pytest.fixture(scope="module")
def registry() -> ParserRegistry:
    return ParserRegistry.default()


def parse(registry: ParserRegistry, text: str, encoding: str = "utf-8"):
    lines = decode_log_bytes(text.encode(encoding)).lines
    parser = registry.select([l.text for l in lines])
    return parser.name, list(parser.parse(lines))


CASES = {
    "ctime + 구성요소 레벨 순서": (
        "Mon Nov 16 07:50:54 2026 TEST-YOD INFO TEST-NODA kure=10\n"
        "Mon Nov 16 07:50:55 2026 TEST-YOD ERROR TEST-FALI code=TEST-E101\n",
        ("TEST-YOD", "ERROR", EventTimeKind.ABSOLUTE_NO_TZ)),
    "대괄호 ISO + [한국어 레벨]": (
        "[2026-11-09 15:39:36.146] [정보] TEST-TADU: TEST-NIZEMI norix=32\n"
        "[2026-11-09 15:39:37.146] [오류] TEST-TADU: TEST-FALI err: TEST-E102\n",
        ("TEST-TADU", "ERROR", EventTimeKind.ABSOLUTE_NO_TZ)),
    "미국식 AM/PM 텍스트": (
        "11/16/2026 12:39:38 PM info [TEST-NO-1] TEST-YOJO mopa_v: 41\n"
        "11/16/2026 12:39:40 PM error [TEST-NO-1] TEST-FALI code: TEST-E103\n",
        ("TEST-NO-1", "ERROR", EventTimeKind.ABSOLUTE_NO_TZ)),
    "레벨이 날짜 앞 + 붙여 쓴 날짜": (
        "INF 20261105 174932.589 TEST-DA-1 - TEST-NIHANI vox: 46\n"
        "ERR 20261105 174933.589 TEST-DA-1 - TEST-FALI code: TEST-E104\n",
        ("TEST-DA-1", "ERROR", EventTimeKind.ABSOLUTE_NO_TZ)),
    "일-월이름-연": (
        "18-Nov-2026 10:00:00.123 WARN TEST-UNIT: TEST-RUNA lv=3\n"
        "18-Nov-2026 10:00:01.123 ERROR TEST-UNIT: TEST-FALI error=TEST-E105\n",
        ("TEST-UNIT", "ERROR", EventTimeKind.ABSOLUTE_NO_TZ)),
    "유닉스 초 + 공백": (
        "1763424000.123 WARN TEST-UNIT: TEST-RUNA lv=3\n1763424001.123 ERROR TEST-UNIT: TEST-FALI code=TEST-E106\n",
        ("TEST-UNIT", "ERROR", EventTimeKind.ABSOLUTE)),
}


@pytest.mark.parametrize("name", CASES)
def test_unknown_text_formats(registry: ParserRegistry, name: str) -> None:
    text, (component, level, kind) = CASES[name]
    parser, events = parse(registry, text)
    assert parser == "auto", name
    last = events[-1]
    assert (last.component, last.level, last.ts_kind) == (component, level, kind), name
    assert last.error_code and last.error_code.startswith("TEST-E1")
    assert events[1].ts_value_us > events[0].ts_value_us


def test_semicolon_csv_with_unknown_header(registry: ParserRegistry) -> None:
    text = ("time;loglevel;comp;msg\n"
            "2026-11-02 19:37:12.155;INFO;TEST-MOLO;TEST-DANI kaki_ms: C\n"
            "2026-11-02 19:37:13.155;ERROR;TEST-MOLO;TEST-FALI code: TEST-E107\n")
    parser, events = parse(registry, text)
    assert parser == "auto" and events[0].attrs == {"header": True}
    assert (events[2].component, events[2].level, events[2].error_code) == ("TEST-MOLO", "ERROR", "TEST-E107")


def test_tab_separated_time_only_with_letter_levels(registry: ParserRegistry) -> None:  # 4회차에서 놓친 배치
    text = ("---- 2026-11-12 ----\n"
            "[10:00:00.100]\t[I]\tTEST-UNIT\tTEST-RUNA job=1\n"
            "[10:00:00.300]\t[E]\tTEST-UNIT\tTEST-FALI code: TEST-E110\n")
    parser, events = parse(registry, text)
    assert parser == "auto"
    assert [(e.level, e.component) for e in events[1:]] == [("INFO", "TEST-UNIT"), ("ERROR", "TEST-UNIT")]
    assert events[2].error_code == "TEST-E110" and events[2].message == "TEST-FALI code: TEST-E110"


def test_pipe_layout_without_level_column(registry: ParserRegistry) -> None:  # 레벨 칸 없는 배치에서 멈추던 버그
    text = ("11/16/2026 12:39:38 PM | TEST-NO-1 | TEST-YOJO mopa_v: 41\n"
            "11/16/2026 12:39:40 PM | TEST-NO-1 | TEST-FALI code: TEST-E109\n"
            "11/16/2026 12:39:41 PM | TEST-NO-2 | TEST-YOJO mopa_v: 42\n")
    parser, events = parse(registry, text)
    assert parser == "auto" and [e.level for e in events] == [None, None, None]
    assert events[1].component == "TEST-NO-1" and events[1].error_code == "TEST-E109"


def test_json_epoch_millis_are_not_read_as_iso(registry: ParserRegistry) -> None:
    # fromisoformat이 '1794011601347' 같은 숫자를 1794년 날짜로 읽던 문제(정상 로그에 4560초 공백이 생김)
    text = "".join(f'{{"t": "{1794011601164 + i * 100}", "level": "info", "comp": "TEST-A", "msg": "TEST-B"}}\n'
                   for i in range(4))
    _, events = parse(registry, text)
    assert {e.ts_kind for e in events} == {EventTimeKind.ABSOLUTE}
    assert [b.ts_value_us - a.ts_value_us for a, b in zip(events, events[1:])] == [100_000] * 3


def test_json_with_unknown_keys(registry: ParserRegistry) -> None:
    text = ('{"t": "2026-11-02T19:37:12.155+09:00", "sev": "info", "src": "TEST-MOLO", "what": "TEST-DANI", "kaki": 3}\n'
            '{"t": "2026-11-02T19:37:13.155+09:00", "sev": "error", "src": "TEST-MOLO", "what": "TEST-FALI", "code": "TEST-E108"}\n')
    parser, events = parse(registry, text)
    assert parser in ("auto", "json_lines")  # 키 별칭이 넓어져 JSON 프로파일이 읽을 수도 있다
    assert events[0].message == "TEST-DANI kaki=3" and events[1].level == "ERROR" and events[1].error_code == "TEST-E108"
    assert events[0].ts_kind == EventTimeKind.ABSOLUTE


def test_time_only_with_date_line_and_syslog_without_year(registry: ParserRegistry) -> None:
    text = "---- 2026-11-12 ----\n[23:59:59.500] [I] TEST-UNIT: TEST-RUNA\n---- 2026-11-13 ----\n[00:00:00.500] [E] TEST-UNIT: TEST-FALI\n"
    parser, events = parse(registry, text)
    assert parser == "auto" and events[3].level == "ERROR"
    assert events[3].ts_value_us - events[1].ts_value_us == 1_000_000
    syslog = "Nov  5 15:08:16 TEST-UNIT: TEST-RUNA\nNov  5 15:09:01 TEST-UNIT: TEST-FALI\n"
    _, events = AutoParser().sniff(syslog.splitlines()), list(AutoParser().parse(decode_log_bytes(syslog.encode()).lines))
    assert [e.ts_kind for e in events] == [EventTimeKind.RELATIVE] * 2 and events[1].ts_value_us == 45_000_000


def test_known_profiles_and_plain_text_keep_their_parser(registry: ParserRegistry) -> None:
    assert parse(registry, "2026-11-16 10:00:00.000 INFO TEST-CTRL: TEST-START\n")[0] == "generic_iso_level"
    assert parse(registry, "2026-11-16 10:00:00 Started TEST-RUN\n2026-11-16 10:00:01 Stopped TEST-RUN\n")[0] == "generic_iso_plain"
    assert parse(registry, "TEST free text one\n\nTEST free text two\n")[0] == "raw"


def test_level_words() -> None:
    assert [level_of(t) for t in ("[I]", "I", "Information", "<warning>", "[경고]", "WRN", "TEST")] == [
        "INFO", None, "INFO", "WARN", "WARN", "WARN", None]
    assert detect_layout(["no time here", "still none"]).score == 0.0
