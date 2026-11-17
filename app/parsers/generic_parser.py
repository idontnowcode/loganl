"""정규식 프로파일 파서. 프로파일 JSON 하나가 파서 하나다 (configs/parser_profiles/*.json).

- line_regex의 이름 그룹: ts, level, component, code, message(필수)
- 맞지 않는 줄은 continuation=true면 앞 이벤트에 이어 붙인다(여러 줄 이벤트). 앞 이벤트가 없으면 원문 이벤트로 남긴다.
- 오류 코드는 code 그룹이나 code_regex로 **문자열만** 뽑는다. 뜻은 붙이지 않는다.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from datetime import datetime, timezone

from app.core.cancel import NEVER_CANCEL, CancelToken
from app.core.event_normalizer import EventTyper
from app.models.enums import EventTimeKind, TimestampBasis
from app.models.records import ParsedEvent, ParserProfile, RawLine

CANCEL_CHECK_EVERY = 5000
SNIFF_LINES = 50
_UNIT_US = {"s": 1_000_000, "ms": 1_000, "us": 1}
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class RegexProfileParser:
    def __init__(self, profile: ParserProfile) -> None:
        self.profile = profile
        self.name = profile.name
        self._line = re.compile(profile.line_regex)
        self._code = re.compile(profile.code_regex) if profile.code_regex else None
        self._typer = EventTyper(profile.event_rules)

    @property
    def timestamp_basis(self) -> TimestampBasis:
        spec = self.profile.timestamp
        if spec is None:
            return TimestampBasis.NONE
        if spec.kind == "relative":
            return TimestampBasis.RELATIVE
        return TimestampBasis.ABSOLUTE  # 시간대 없는 값이 섞이면 가져오기에서 ABSOLUTE_NO_TZ로 낮춘다

    def sniff(self, head_lines: Sequence[str]) -> float:
        sample = [line for line in head_lines if line.strip()][:SNIFF_LINES]
        if not sample:
            return 0.0
        hits = sum(1 for line in sample if self._line.match(line))
        score = hits / len(sample)
        return score if score >= self.profile.sniff_threshold else 0.0

    def parse(self, lines: Iterable[RawLine], cancel: CancelToken = NEVER_CANCEL) -> Iterator[ParsedEvent]:
        current: dict | None = None
        pending_blank: list[str] = []
        seq = 0
        for index, line in enumerate(lines):
            if index % CANCEL_CHECK_EVERY == 0:
                cancel.raise_if_cancelled()
            match = self._line.match(line.text)
            if match:
                if current is not None:
                    yield self._finish(current, seq)
                    seq += 1
                current = self._start(match, line)
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
            current = {
                "line_no": line.line_no, "line_end": None, "byte_offset": line.byte_offset,
                "ts_raw": None, "level": None, "component": None, "code": None,
                "msg": [line.text], "raw": [line.text], "unparsed": True,
            }
            pending_blank = []
        if current is not None:
            yield self._finish(current, seq)

    def _start(self, match: re.Match[str], line: RawLine) -> dict:
        groups = match.groupdict()
        return {
            "line_no": line.line_no,
            "line_end": None,
            "byte_offset": line.byte_offset,
            "ts_raw": _clean(groups.get("ts")),
            "level": (_clean(groups.get("level")) or None),
            "component": _clean(groups.get("component")),
            "code": _clean(groups.get("code")),
            "msg": [groups.get("message") or ""],
            "raw": [line.text],
            "unparsed": False,
        }

    def _finish(self, item: dict, seq: int) -> ParsedEvent:
        message = "\n".join(item["msg"])
        ts_kind, ts_value = (EventTimeKind.NONE, None)
        if item["ts_raw"] and self.profile.timestamp is not None:
            ts_kind, ts_value = self._parse_timestamp(item["ts_raw"])
        code = item["code"]
        if code is None and self._code is not None:
            found = self._code.search(message)
            if found:
                code = found.groupdict().get("code") or found.group(0)
        attrs = {"unparsed": True} if item["unparsed"] else None
        if item["ts_raw"] and ts_kind == EventTimeKind.NONE:
            attrs = {**(attrs or {}), "timestamp_unparsed": True}
        return ParsedEvent(
            seq=seq,
            line_no=item["line_no"],
            line_end=item["line_end"],
            byte_offset=item["byte_offset"],
            ts_raw=item["ts_raw"],
            ts_value_us=ts_value,
            ts_kind=ts_kind,
            level=item["level"].upper() if item["level"] else None,
            component=item["component"],
            event_type=self._typer.event_type(message, item["component"]),
            error_code=code,
            message=message,
            raw_text="\n".join(item["raw"]),
            attrs=attrs,
        )

    def _parse_timestamp(self, raw: str) -> tuple[EventTimeKind, int | None]:
        spec = self.profile.timestamp
        assert spec is not None
        if spec.kind == "relative":
            try:
                return EventTimeKind.RELATIVE, round(float(raw.replace(",", ".")) * _UNIT_US[spec.unit])
            except ValueError:
                return EventTimeKind.NONE, None
        parsed: datetime | None = None
        if spec.kind == "iso":
            try:
                parsed = datetime.fromisoformat(raw.replace(",", ".").replace(" ", "T", 1))
            except ValueError:
                parsed = None
        else:
            for fmt in spec.formats:
                try:
                    parsed = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    continue
        if parsed is None:
            return EventTimeKind.NONE, None
        if parsed.tzinfo is None:
            # 시간대가 없으면 숫자는 UTC로 두되 종류로 구분한다(다른 파일과 바로 비교하지 않음, §4.3)
            return EventTimeKind.ABSOLUTE_NO_TZ, _to_us(parsed.replace(tzinfo=timezone.utc))
        return EventTimeKind.ABSOLUTE, _to_us(parsed.astimezone(timezone.utc))


def _to_us(moment: datetime) -> int:
    delta = moment - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None
