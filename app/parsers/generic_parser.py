"""파서 프로파일 하나 = 파서 하나 (configs/parser_profiles/*.json).

줄 형식(line_format, CR-08 PA-01):
- regex: line_regex의 이름 그룹 ts, level, component, code, message(필수), rest(메시지 뒤에 붙임)
- json: 한 줄에 JSON 객체 하나. fields 별칭으로 칸을 찾고, 나머지 키는 key=value로 메시지 뒤에 붙인다.
- logfmt: key=value(값은 따옴표 가능). json과 같다.
- delimited: 칸 이름 머리 줄로 열을 정하고, 줄마다 delimiter로 나눈다(CSV 따옴표 규칙).

공통:
- 맞지 않는 줄은 continuation=true면 앞 이벤트에 이어 붙인다(여러 줄 이벤트). 앞 이벤트가 없으면 원문 이벤트로 남긴다.
- 칸 이름 머리 줄·날짜 머리 줄(timestamp.date_line_regex)은 따로 이벤트로 남기고(attrs로 표시) 뒤 줄 해석에 쓴다.
- 오류 코드는 code 칸이나 code_regex로 **문자열만** 뽑는다. 뜻은 붙이지 않는다.
- 레벨은 level_map(별칭표) → syslog 우선순위(level_from) → 대문자 순으로 정한다.
"""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.cancel import NEVER_CANCEL, CancelToken
from app.core.event_normalizer import EventTyper
from app.models.enums import EventTimeKind, TimestampBasis
from app.parsers.auto_parser import parse_any
from app.models.records import ParsedEvent, ParserProfile, RawLine

CANCEL_CHECK_EVERY = 5000
SNIFF_LINES = 50
_UNIT_US = {"s": 1_000_000, "ms": 1_000, "us": 1}
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_SYSLOG_LEVELS = ("EMERG", "ALERT", "CRIT", "ERROR", "WARN", "NOTICE", "INFO", "DEBUG")  # 우선순위 % 8
_MONTHS = {name: number for number, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
_MONTH_WORD = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?=\s|$|,)", re.IGNORECASE)
_LOGFMT_TOKEN = re.compile(r'\s*([A-Za-z_@][\w.@\-]*)=("(?:[^"\\]|\\.)*"|[^\s"]*)')
_NO_YEAR_BASE = 2000  # 연도 없는 시각을 읽을 때만 쓰는 윤년(2월 29일도 읽힘). 값은 경과 시간으로만 남긴다
_FIELDS = ("ts", "level", "component", "message", "code")


@dataclass
class _State:
    """파일 하나를 읽는 동안의 문맥: 칸 이름 머리 줄, 날짜 머리 줄, 연도 없는 시각의 기준."""

    columns: dict[str, int] | None = None
    header: list[str] | None = None
    date: str | None = None
    year: int = _NO_YEAR_BASE
    first: datetime | None = None
    last: datetime | None = None


def _indented(text: str) -> bool:
    return text[:1] in (" ", "\t")


class RegexProfileParser:
    def __init__(self, profile: ParserProfile) -> None:
        self.profile = profile
        self.name = profile.name
        self._line = re.compile(profile.line_regex) if profile.line_regex else None
        self._code = re.compile(profile.code_regex) if profile.code_regex else None
        self._typer = EventTyper(profile.event_rules)
        spec = profile.timestamp
        self._date_line = re.compile(spec.date_line_regex) if spec and spec.date_line_regex else None
        self._ampm = [(re.compile(rf"(?<!\S){re.escape(word)}(?!\S)"), half)
                      for word, half in (spec.ampm.items() if spec else [])]
        self._levels = {key.strip().lower(): value.strip().upper() for key, value in profile.level_map.items()}
        self._alias = {name: [alias.lower() for alias in getattr(profile.fields, name)] for name in _FIELDS}

    @property
    def timestamp_basis(self) -> TimestampBasis:
        spec = self.profile.timestamp
        if spec is None:
            return TimestampBasis.NONE
        if spec.kind == "relative" or spec.no_year:
            return TimestampBasis.RELATIVE
        return TimestampBasis.ABSOLUTE  # 시간대 없는 값이 섞이면 가져오기에서 ABSOLUTE_NO_TZ로 낮춘다

    # ------------------------------------------------------------------ 판정

    def sniff(self, head_lines: Sequence[str]) -> float:
        lines = [line for line in head_lines if line.strip()]
        # 들여 쓴 줄은 여러 줄 이벤트의 뒷줄일 수 있으므로 점수에서 뺀다(CR-08 PA-03)
        candidates = ([line for line in lines if not _indented(line)] if self.profile.continuation else lines) or lines
        state = _State()
        hits = counted = 0
        for text in candidates[:SNIFF_LINES]:
            context = self._context(text, state)
            if context is not None:
                self._apply_context(context, state)
                continue  # 머리 줄은 맞음·틀림에 세지 않는다
            counted += 1
            if self._fields(text, state) is not None:
                hits += 1
        if not counted:
            return 0.0
        score = hits / counted
        return score if score >= self.profile.sniff_threshold else 0.0

    # ------------------------------------------------------------------ 읽기

    def parse(self, lines: Iterable[RawLine], cancel: CancelToken = NEVER_CANCEL) -> Iterator[ParsedEvent]:
        state = _State()
        current: dict | None = None
        pending_blank: list[str] = []
        seq = 0
        for index, line in enumerate(lines):
            if index % CANCEL_CHECK_EVERY == 0:
                cancel.raise_if_cancelled()
            context = self._context(line.text, state) if line.text.strip() else None
            if context is not None:
                if current is not None:
                    yield self._finish(current, seq)
                    seq += 1
                self._apply_context(context, state)
                yield self._finish(self._raw_item(line, {context[0]: True}), seq)
                seq += 1
                current, pending_blank = None, []
                continue
            fields = self._fields(line.text, state)
            if fields is not None:
                if current is not None:
                    yield self._finish(current, seq)
                    seq += 1
                current = self._start(fields, line, state)
                pending_blank = []
                continue
            if not line.text.strip():
                if current is not None:
                    pending_blank.append(line.text)
                continue
            if current is not None and self.profile.continuation:
                current["raw"].extend(pending_blank)
                current["msg"].extend(pending_blank)
                pending_blank = []
                current["raw"].append(line.text)
                current["msg"].append(line.text)
                current["line_end"] = line.line_no
                continue
            # 맞지 않는 줄이고 이어 붙일 곳이 없으면 원문 이벤트로 남긴다
            if current is not None:
                yield self._finish(current, seq)
                seq += 1
            current = self._raw_item(line, {"unparsed": True})
            pending_blank = []
        if current is not None:
            yield self._finish(current, seq)

    # ------------------------------------------------------------------ 줄 → 칸

    def _context(self, text: str, state: _State) -> tuple[str, Any] | None:
        """칸 이름 머리 줄이면 ('header', 열), 날짜 머리 줄이면 ('date_line', 날짜)."""
        if self.profile.line_format == "delimited" and state.columns is None:
            cells = self._cells(text)
            names = [cell.strip().lower() for cell in cells]
            columns = {}
            for name in _FIELDS:
                for alias in self._alias[name]:
                    if alias in names:
                        columns[name] = names.index(alias)
                        break
            if len(cells) >= 2 and "message" in columns and "ts" in columns:
                return "header", (columns, names)
            return None
        if self._date_line is not None:
            match = self._date_line.match(text)
            if match:
                return "date_line", match.group("date")
        return None

    @staticmethod
    def _apply_context(context: tuple[str, Any], state: _State) -> None:
        kind, value = context
        if kind == "header":
            state.columns, state.header = value
        else:
            state.date = value

    def _fields(self, text: str, state: _State) -> dict[str, str | None] | None:
        line_format = self.profile.line_format
        if line_format == "regex":
            match = self._line.match(text) if self._line is not None else None
            if not match:
                return None
            groups = match.groupdict()
            message = groups.get("message") or ""
            rest = (groups.get("rest") or "").strip()
            if rest:
                message = f"{message} {rest}" if message else rest
            return {name: groups.get(name) for name in ("ts", "level", "component", "code")} | {"message": message}
        if self._line is not None and not self._line.match(text):
            return None
        if line_format == "json":
            if not text.lstrip().startswith("{"):
                return None
            try:
                item = json.loads(text)
            except ValueError:
                return None
            if not isinstance(item, dict):
                return None
            return self._from_pairs([(str(key), value, f"{key}={_compact(value)}") for key, value in item.items()])
        if line_format == "logfmt":
            pairs = _logfmt_pairs(text)
            return self._from_pairs(pairs) if pairs else None
        # delimited
        if state.columns is None:
            return None
        cells = self._cells(text)
        header = state.header or []
        if len(cells) < max(state.columns.values()) + 1 or len(cells) < 2:
            return None
        message_index = state.columns["message"]
        if len(cells) > len(header) and message_index == len(header) - 1:
            cells = cells[:message_index] + [self.profile.delimiter.join(cells[message_index:])]
        out: dict[str, str | None] = {name: cells[i] for name, i in state.columns.items()}
        used = set(state.columns.values())
        extra = [f"{header[i]}={cells[i]}" for i in range(min(len(header), len(cells)))
                 if i not in used and cells[i].strip()]
        if extra:
            out["message"] = " ".join([out["message"] or "", *extra]).strip()
        return out

    def _from_pairs(self, pairs: list[tuple[str, Any, str]]) -> dict[str, str | None] | None:
        lowered = [(key.lower(), value, text) for key, value, text in pairs]
        out: dict[str, str | None] = {}
        used: set[int] = set()
        for name in _FIELDS:
            for alias in self._alias[name]:
                index = next((i for i, (key, _v, _t) in enumerate(lowered) if key == alias and i not in used), None)
                if index is not None:
                    value = lowered[index][1]
                    out[name] = value if isinstance(value, str) else _compact(value)
                    used.add(index)
                    break
        if "message" not in out:
            return None
        extra = [text for i, (_k, _v, text) in enumerate(lowered) if i not in used]
        if extra:
            out["message"] = " ".join([out["message"] or "", *extra]).strip()
        return out

    def _cells(self, text: str) -> list[str]:
        try:
            return next(csv.reader([text], delimiter=self.profile.delimiter), [])
        except csv.Error:
            return []

    # ------------------------------------------------------------------ 이벤트 만들기

    def _start(self, fields: dict[str, str | None], line: RawLine, state: _State) -> dict:
        ts_raw = _clean(fields.get("ts"))
        ts_kind, ts_value = EventTimeKind.NONE, None
        if ts_raw and self.profile.timestamp is not None:
            ts_kind, ts_value = self._parse_timestamp(ts_raw, state)
        return {
            "line_no": line.line_no,
            "line_end": None,
            "byte_offset": line.byte_offset,
            "ts_raw": ts_raw,
            "ts_kind": ts_kind,
            "ts_value": ts_value,
            "level": self._level(fields.get("level")),
            "component": _clean(fields.get("component")),
            "code": _clean(fields.get("code")),
            "msg": [fields.get("message") or ""],
            "raw": [line.text],
            "attrs": None,
        }

    @staticmethod
    def _raw_item(line: RawLine, attrs: dict[str, Any]) -> dict:
        return {
            "line_no": line.line_no, "line_end": None, "byte_offset": line.byte_offset,
            "ts_raw": None, "ts_kind": EventTimeKind.NONE, "ts_value": None, "level": None, "component": None,
            "code": None, "msg": [line.text], "raw": [line.text], "attrs": attrs,
        }

    def _finish(self, item: dict, seq: int) -> ParsedEvent:
        message = "\n".join(item["msg"])
        code = item["code"]
        if code is None and self._code is not None:
            found = self._code.search(message)
            if found:
                code = found.groupdict().get("code") or found.group(0)
        attrs = dict(item["attrs"]) if item["attrs"] else None
        if item["ts_raw"] and item["ts_kind"] == EventTimeKind.NONE:
            attrs = {**(attrs or {}), "timestamp_unparsed": True}
        return ParsedEvent(
            seq=seq,
            line_no=item["line_no"],
            line_end=item["line_end"],
            byte_offset=item["byte_offset"],
            ts_raw=item["ts_raw"],
            ts_value_us=item["ts_value"],
            ts_kind=item["ts_kind"],
            level=item["level"],
            component=item["component"],
            event_type=self._typer.event_type(message, item["component"]),
            error_code=code,
            message=message,
            raw_text="\n".join(item["raw"]),
            attrs=attrs,
        )

    def _level(self, raw: str | None) -> str | None:
        value = _clean(raw)
        if not value:
            return None
        value = value.strip("[]<>").strip()
        if self.profile.level_from == "syslog_pri" and value.isdigit():
            return _SYSLOG_LEVELS[int(value) % 8]
        return self._levels.get(value.lower()) or value.upper()

    # ------------------------------------------------------------------ 시각

    def _parse_timestamp(self, raw: str, state: _State) -> tuple[EventTimeKind, int | None]:
        spec = self.profile.timestamp
        assert spec is not None
        if spec.kind == "relative":
            try:
                return EventTimeKind.RELATIVE, round(float(raw.replace(",", ".")) * _UNIT_US.get(spec.unit, 1_000_000))
            except ValueError:
                return EventTimeKind.NONE, None
        if spec.kind == "epoch":
            unit = spec.unit
            if unit == "auto":  # 정수 부분 자릿수: 10자리 안팎 초, 13자리 밀리초, 16자리 마이크로초
                digits = len(raw.split(".", 1)[0].lstrip("-"))
                unit = "us" if digits >= 16 else "ms" if digits >= 13 else "s"
            try:
                return EventTimeKind.ABSOLUTE, round(float(raw) * _UNIT_US[unit])
            except ValueError:
                return EventTimeKind.NONE, None
        parsed: datetime | None = None
        if spec.kind == "iso":
            # 숫자만 있는 값(유닉스 시간)은 ISO로 읽지 않는다: fromisoformat이 일부를 '1794-01-16…'처럼 잘못 읽는다
            if raw.replace(".", "", 1).isdigit():
                parsed = None
            else:
                try:
                    parsed = datetime.fromisoformat(raw.replace(",", ".").replace(" ", "T", 1))
                except ValueError:
                    parsed = None
        else:
            text = raw
            if self._date_line is not None:
                if state.date is None:
                    return EventTimeKind.NONE, None
                text = f"{state.date} {raw}"
            parsed = self._strptime(text, state.year if spec.no_year else None)
        if parsed is None:
            # 프로파일이 정한 모양으로 못 읽으면 아는 시각 모양 중 무엇으로든 읽어 본다(예: JSON의 유닉스 밀리초, CR-08)
            return parse_any(raw) if not spec.no_year and self._date_line is None else (EventTimeKind.NONE, None)
        if spec.no_year:
            return EventTimeKind.RELATIVE, self._elapsed(parsed, state)
        if parsed.tzinfo is None:
            # 시간대가 없으면 숫자는 UTC로 두되 종류로 구분한다(다른 파일과 바로 비교하지 않음, §4.3)
            return EventTimeKind.ABSOLUTE_NO_TZ, _to_us(parsed.replace(tzinfo=timezone.utc))
        return EventTimeKind.ABSOLUTE, _to_us(parsed.astimezone(timezone.utc))

    def _strptime(self, text: str, year: int | None) -> datetime | None:
        """strptime 목록으로 읽는다. 오전/오후·월 이름은 로캘과 무관하게 먼저 바꿔 둔다."""
        half = None
        for pattern, value in self._ampm:
            if pattern.search(text):
                text, half = pattern.sub(" ", text, count=1), value
                break
        text = " ".join(text.split())
        assert self.profile.timestamp is not None
        for fmt in self.profile.timestamp.formats:
            candidate, form = text, fmt
            if "%b" in form:
                form = form.replace("%b", "%m")
                candidate = _MONTH_WORD.sub(lambda m: str(_MONTHS[m.group(1).lower()]), candidate)
            if year is not None:
                candidate, form = f"{candidate} {year}", f"{form} %Y"
            try:
                parsed = datetime.strptime(candidate, form)
            except ValueError:
                continue
            if half is not None:
                parsed = parsed.replace(hour=parsed.hour % 12 + (12 if half == "PM" else 0))
            return parsed
        return None

    @staticmethod
    def _elapsed(parsed: datetime, state: _State) -> int:
        """연도 없는 시각 → 파일 첫 시각부터의 경과(µs). 하루 넘게 거꾸로 가면 해가 바뀐 것으로 본다."""
        if state.last is not None and parsed < state.last - timedelta(days=1):
            state.year += 1
            try:
                parsed = parsed.replace(year=parsed.year + 1)
            except ValueError:  # 2월 29일
                parsed = parsed + timedelta(days=365)
        if state.first is None:
            state.first = parsed
        state.last = parsed
        return _to_us(parsed.replace(tzinfo=timezone.utc)) - _to_us(state.first.replace(tzinfo=timezone.utc))


def _logfmt_pairs(text: str) -> list[tuple[str, Any, str]] | None:
    """줄 전체가 key=value 묶음일 때만 (키, 값, 원문 조각) 목록. 사이에 다른 글자가 있으면 None."""
    pairs: list[tuple[str, Any, str]] = []
    position = 0
    for match in _LOGFMT_TOKEN.finditer(text):
        if match.start() != position:
            return None
        position = match.end()
        key, value = match.group(1), match.group(2)
        if value.startswith('"'):
            value = re.sub(r"\\(.)", r"\1", value[1:-1])
        pairs.append((key, value, match.group(0).strip()))
    if not pairs or text[position:].strip():
        return None
    return pairs


def _compact(value: Any) -> str:
    if isinstance(value, str):
        return value if value and not any(ch.isspace() for ch in value) else json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _to_us(moment: datetime) -> int:
    delta = moment - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None
