"""자동 형식 인식 파서 (CR-08 PA-04, 결정론). 어떤 프로파일도 맞지 않을 때 원문 파서 대신 쓴다.

파일 앞부분 줄들을 보고 배치를 한 번 정한 뒤 모든 줄에 적용한다.
- json: 한 줄에 JSON 객체. 키 이름을 몰라도 값 모양으로 칸을 고른다(시각으로 읽히는 값·레벨 낱말·긴 글·적은 종류의 이름).
- logfmt: 줄 전체가 key=value. 위와 같다.
- delimited: 탭·|·;·, 로 칸 수가 일정한 줄. 칸마다 값 모양으로 역할을 정하고, 칸 이름 머리 줄은 따로 둔다.
- text: 줄 앞부분(대괄호·우선순위·레벨 낱말 뒤 포함)에서 시각 모양을 찾고, 남은 글에서 레벨 낱말·구성요소·메시지를 나눈다.
  시각만 있는 줄은 '날짜만 있는 줄'(머리 줄)의 날짜를 붙이고, 그런 줄이 없으면 파일 안 경과 시간으로 둔다.
  연도 없는 시각(syslog)도 경과 시간으로 둔다(지어낸 연도를 붙이지 않음).
뜻은 붙이지 않는다. 오류 코드는 code=·err=·error= 뒤 글자만 문자열로 뽑는다.
점수는 프로파일보다 낮게(최대 0.59) 내서, 맞는 프로파일이 있으면 늘 그쪽을 쓴다.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.cancel import NEVER_CANCEL, CancelToken
from app.core.event_normalizer import EventTyper
from app.models.enums import EventTimeKind, TimestampBasis
from app.models.records import ParsedEvent, RawLine

CANCEL_CHECK_EVERY = 5000
SNIFF_LINES = 60
MAX_SCORE = 0.59
MIN_SHARE = 0.6
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MON = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
_MONTHS = {m.lower(): i for i, m in enumerate(_MON.split("|"), start=1)}
_FRAC = r"(?:[.,]\d{1,6})?"
_CODE = re.compile(r"(?i)\b(?:code|err(?:or)?)\s*[=:]\s*(?P<code>[A-Za-z0-9_\-]+)")
_LOGFMT = re.compile(r'\s*([A-Za-z_@][\w.@\-]*)=("(?:[^"\\]|\\.)*"|[^\s"]*)')
_IDENT = re.compile(r"^[A-Za-z][\w.\-/]*(?:\[\d+\])?$")
_DATE_ONLY = re.compile(r"^\W*(?:날짜|일자|date|Date|DATE)?\W*(\d{4})[-./](\d{1,2})[-./](\d{1,2})\W*$")

LEVELS = {
    "trace": "TRACE", "debug": "DEBUG", "dbg": "DEBUG", "verbose": "DEBUG", "info": "INFO", "inf": "INFO",
    "information": "INFO", "notice": "NOTICE", "warn": "WARN", "warning": "WARN", "wrn": "WARN", "error": "ERROR",
    "err": "ERROR", "fatal": "FATAL", "ftl": "FATAL", "critical": "FATAL", "crit": "CRIT", "severe": "FATAL",
    "alert": "ALERT", "emerg": "EMERG", "디버그": "DEBUG", "정보": "INFO", "알림": "NOTICE", "경고": "WARN",
    "주의": "WARN", "오류": "ERROR", "에러": "ERROR", "치명": "FATAL", "심각": "FATAL",
}
LETTERS = {"t": "TRACE", "d": "DEBUG", "v": "DEBUG", "i": "INFO", "n": "NOTICE", "w": "WARN", "e": "ERROR", "f": "FATAL"}
_SYSLOG = ("EMERG", "ALERT", "CRIT", "ERROR", "WARN", "NOTICE", "INFO", "DEBUG")
_KEY_HINTS = {
    "ts": ("ts", "time", "timestamp", "@timestamp", "datetime", "date", "t", "when", "date and time"),
    "level": ("level", "lvl", "severity", "sev", "loglevel", "priority"),
    "component": ("component", "comp", "module", "logger", "source", "src", "service", "unit", "from", "name"),
    "message": ("msg", "message", "text", "event", "what", "log", "body"),
}


# ---------------------------------------------------------------- 시각 모양

@dataclass(frozen=True)
class TsPattern:
    name: str
    regex: re.Pattern[str]
    kind: str  # absolute · no_year · time_only · epoch


def _p(name: str, pattern: str, kind: str = "absolute") -> TsPattern:
    return TsPattern(name, re.compile(pattern), kind)


TS_PATTERNS = (
    _p("iso", r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}" + _FRAC + r"(?:Z|[+-]\d{2}:?\d{2})?"),
    _p("ymd_ampm", r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}[ T]+(?:오전|오후|AM|PM)\s+\d{1,2}:\d{2}:\d{2}" + _FRAC),
    _p("ymd", r"\d{4}[/.]\d{1,2}[/.]\d{1,2}[ T]+\d{1,2}:\d{2}:\d{2}" + _FRAC),
    _p("ctime", r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) (?:" + _MON + r") [ \d]\d \d{2}:\d{2}:\d{2} \d{4}"),
    _p("dmy_mon", r"\d{1,2}[-/](?:" + _MON + r")[a-z]*[-/]\d{4}[ :T]\d{2}:\d{2}:\d{2}" + _FRAC + r"(?: ?[+-]\d{4})?"),
    _p("mdy", r"\d{1,2}/\d{1,2}/\d{4},? \d{1,2}:\d{2}(?::\d{2})?" + _FRAC + r"(?: ?[AaPp][Mm])?"),
    _p("compact", r"(?<!\d)\d{8}[ T-]?\d{6}(?![\d:])" + _FRAC),
    _p("syslog", r"(?:" + _MON + r") [ \d]\d \d{2}:\d{2}:\d{2}", "no_year"),
    _p("epoch", r"(?<![\d.])\d{10}(?:\d{3}|\d{6})?(?:\.\d{1,6})?(?![\d:])", "epoch"),
    _p("time_only", r"\d{1,2}:\d{2}:\d{2}" + _FRAC, "time_only"),
)


def parse_absolute(name: str, raw: str) -> datetime | None:
    """시각 모양 이름과 글자 → datetime(시간대 있으면 aware). 로캘과 무관."""
    text = raw.strip()
    try:
        if name == "iso":
            return datetime.fromisoformat(text.replace(",", ".").replace(" ", "T", 1))
        if name in ("ymd_ampm", "ymd"):
            half = "PM" if ("오후" in text or "PM" in text) else ("AM" if ("오전" in text or "AM" in text) else None)
            core = re.sub(r"\s*(오전|오후|AM|PM)\s*", " ", text)
            date, _, clock = core.replace("T", " ").partition(" ")
            y, m, d = re.split(r"[-/.]", date)
            return _hms(int(y), int(m), int(d), clock.strip(), half)
        if name == "ctime":
            parts = text.split()
            return _hms(int(parts[4]), _MONTHS[parts[1].lower()], int(parts[2]), parts[3], None)
        if name == "dmy_mon":
            m = re.match(r"(\d{1,2})[-/]([A-Za-z]{3})[a-z]*[-/](\d{4})[ :T](\S+)(?: ?([+-]\d{4}))?", text)
            if not m:
                return None
            value = _hms(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)), m.group(4), None)
            if value and m.group(5):
                sign = 1 if m.group(5)[0] == "+" else -1
                value = value.replace(tzinfo=timezone(sign * timedelta(hours=int(m.group(5)[1:3]),
                                                                      minutes=int(m.group(5)[3:]))))
            return value
        if name == "mdy":
            half = "PM" if text.upper().endswith("PM") else ("AM" if text.upper().endswith("AM") else None)
            core = re.sub(r"(?i)\s*[ap]m$", "", text).replace(",", " ")
            date, _, clock = core.partition(" ")
            mo, d, y = date.split("/")
            return _hms(int(y), int(mo), int(d), clock.strip(), half)
        if name == "compact":
            digits = re.sub(r"[ T-]", "", text.split(".")[0].split(",")[0])
            frac = re.search(r"[.,](\d+)$", text)
            clock = f"{digits[8:10]}:{digits[10:12]}:{digits[12:14]}" + (f".{frac.group(1)}" if frac else "")
            return _hms(int(digits[:4]), int(digits[4:6]), int(digits[6:8]), clock, None)
    except (ValueError, KeyError, IndexError):
        return None
    return None


def _hms(year: int, month: int, day: int, clock: str, half: str | None) -> datetime | None:
    m = re.match(r"(\d{1,2}):(\d{2})(?::(\d{2}))?(?:[.,](\d{1,6}))?$", clock)
    if not m:
        return None
    hour = int(m.group(1))
    if half == "PM" and hour < 12:
        hour += 12
    elif half == "AM" and hour == 12:
        hour = 0
    micro = int((m.group(4) or "0").ljust(6, "0")[:6])
    return datetime(year, month, day, hour, int(m.group(2)), int(m.group(3) or 0), micro)


def parse_any(raw: str) -> tuple[EventTimeKind, int | None]:
    """날짜가 든 시각 글자 하나를 아는 모양 중 무엇으로든 읽는다(유닉스 시간 포함). 프로파일이 시각을 못 읽을 때 쓴다."""
    text = raw.strip().strip("[]")
    for pattern in TS_PATTERNS:
        if pattern.kind in ("time_only", "no_year") or not pattern.regex.fullmatch(text):
            continue
        if pattern.kind == "epoch":
            digits = len(text.split(".")[0])
            return EventTimeKind.ABSOLUTE, round(float(text) * (1 if digits >= 16 else 1_000 if digits >= 13 else 1_000_000))
        moment = parse_absolute(pattern.name, text)
        if moment is not None:
            return (EventTimeKind.ABSOLUTE if moment.tzinfo else EventTimeKind.ABSOLUTE_NO_TZ), _to_us(moment)
    return EventTimeKind.NONE, None


def _to_us(moment: datetime) -> int:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    delta = moment.astimezone(timezone.utc) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def level_of(token: str) -> str | None:
    """레벨 낱말이면 표준 이름. 한 글자(I·W·E…)는 괄호로 감쌌을 때만."""
    raw = token.strip()
    bracketed = raw[:1] in "[<(" and raw[-1:] in "]>)"
    core = raw.strip("[]<>():").strip().lower()
    if core in LEVELS:
        return LEVELS[core]
    if bracketed and core in LETTERS:
        return LETTERS[core]
    return None


# ---------------------------------------------------------------- 배치

@dataclass
class Layout:
    kind: str  # json · logfmt · delimited · text
    score: float = 0.0
    ts: TsPattern | None = None
    roles: dict[str, Any] = field(default_factory=dict)  # 구조형: 칸 → 역할 키/열 번호
    delimiter: str = ""
    columns: int = 0
    header: bool = False
    component: bool = False  # text: 구성요소를 따로 뗄지
    has_level: bool = False  # text: 줄마다 레벨 낱말이 있는지

    @property
    def structured(self) -> bool:
        """시각 말고도 읽어 낸 구조(칸·레벨·구성요소)가 있는지. 포괄형 프로파일과 겨룰 때 쓴다."""
        return self.kind != "text" or self.has_level or self.component


def _find_ts(text: str, limit: int = 48) -> tuple[TsPattern, re.Match[str]] | None:
    head = text[: limit + 40]
    for pattern in TS_PATTERNS:
        for match in pattern.regex.finditer(head):
            if match.start() > limit:
                break
            if pattern.kind == "epoch" and match.start() != 0:
                continue  # 유닉스 시간은 줄 맨 앞에서만(메시지 속 숫자와 헷갈리지 않게)
            before = head[: match.start()]
            if before and not re.fullmatch(r"[\s\[<(]*(?:<\d{1,3}>)?[\s\[]*(?:\S+\s+)?[\s\[]*", before):
                continue  # 시각 앞에는 괄호·우선순위·낱말 하나(레벨 등)만 허용
            return pattern, match
    return None


def _ts_value(text: str) -> bool:
    candidate = text.strip()
    for pattern in TS_PATTERNS:
        if pattern.kind == "time_only":
            continue
        match = pattern.regex.fullmatch(candidate)
        if match and (pattern.kind != "absolute" or parse_absolute(pattern.name, candidate) is not None):
            return True
    return False


def _cells(text: str, delimiter: str) -> list[str]:
    if delimiter in (",", ";"):
        try:
            return [c.strip() for c in next(csv.reader([text], delimiter=delimiter))]
        except (csv.Error, StopIteration):
            return []
    return [c.strip() for c in text.split(delimiter)]


def _roles_by_values(columns: dict[Any, list[str]], names: dict[Any, str] | None = None) -> dict[str, Any] | None:
    """칸(열 번호·키)마다 값 목록 → 역할. names가 있으면 이름 힌트를 먼저 본다."""
    roles: dict[str, Any] = {}
    names = names or {}
    # 대부분 비어 있는 칸(예: 레벨 자리만 남은 '| |')은 어떤 역할도 맡기지 않는다
    columns = {c: v for c, v in columns.items() if sum(1 for x in v if str(x).strip()) >= 0.8 * max(1, len(v))}
    for role in ("ts", "level", "component", "message"):
        for col, name in names.items():
            if name.lower() in _KEY_HINTS[role] and col not in roles.values():
                roles[role] = col
                break
    candidates = [c for c in columns if c not in roles.values()]
    if "ts" not in roles:
        scored = [(sum(_ts_value(str(v)) for v in columns[c]) / max(1, len(columns[c])), c) for c in candidates]
        best = max(scored, default=(0, None))
        if best[0] >= MIN_SHARE:
            roles["ts"] = best[1]
            candidates.remove(best[1])
    if "level" not in roles:
        scored = [(sum(level_of(str(v)) is not None for v in columns[c]) / max(1, len(columns[c])), c) for c in candidates]
        best = max(scored, default=(0, None))
        if best[0] >= 0.8:
            roles["level"] = best[1]
            candidates.remove(best[1])
    texts = [c for c in candidates if all(isinstance(v, str) for v in columns[c])]
    if "message" not in roles and texts:
        roles["message"] = max(texts, key=lambda c: sum(len(v) for v in columns[c]) / max(1, len(columns[c])))
        texts.remove(roles["message"])
    if "component" not in roles:
        idents = [c for c in texts if all(_IDENT.match(v) for v in columns[c] if v)
                  and len(set(columns[c])) <= max(3, len(columns[c]) // 3)]
        if idents:
            roles["component"] = min(idents, key=lambda c: len(set(columns[c])))
    return roles if "message" in roles else None


def detect_layout(lines: Sequence[str]) -> Layout:
    usable = [l for l in lines if l.strip() and not _DATE_ONLY.match(l)]  # 날짜만 있는 머리 줄은 판정에서 뺀다
    sample = [l for l in usable if not l[:1].isspace()][:SNIFF_LINES] or usable[:SNIFF_LINES]
    if not sample:
        return Layout("text")
    n = len(sample)
    # JSON
    objects = []
    for line in sample:
        try:
            item = json.loads(line) if line.lstrip().startswith("{") else None
        except ValueError:
            item = None
        if isinstance(item, dict):
            objects.append(item)
    if len(objects) >= MIN_SHARE * n:
        columns: dict[Any, list] = {}
        for item in objects:
            for key, value in item.items():
                columns.setdefault(key, []).append(value if isinstance(value, str) else json.dumps(value))
        roles = _roles_by_values(columns, {k: k for k in columns})
        if roles:
            return Layout("json", len(objects) / n, roles=roles)
    # logfmt
    pairs = [_logfmt(line) for line in sample]
    good = [p for p in pairs if p and len(p) >= 3]
    if len(good) >= MIN_SHARE * n:
        columns = {}
        for p in good:
            for key, value, _ in p:
                columns.setdefault(key, []).append(value)
        roles = _roles_by_values(columns, {k: k for k in columns})
        if roles:
            return Layout("logfmt", len(good) / n, roles=roles)
    # delimited
    best: Layout | None = None
    for delimiter in ("\t", "|", ";", ","):
        rows = [_cells(line, delimiter) for line in sample]
        counts = Counter(len(r) for r in rows if len(r) >= 3)
        if not counts:
            continue
        width, hits = counts.most_common(1)[0]
        if hits < 0.8 * n:
            continue
        body = [r for r in rows if len(r) == width]
        header = bool(body) and not any(_ts_value(c) for c in body[0]) and any(_ts_value(c) for r in body[1:4] for c in r)
        data = body[1:] if header else body
        columns = {i: [r[i] for r in data] for i in range(width)}
        names = {i: body[0][i] for i in range(width)} if header else None
        roles = _roles_by_values(columns, names)
        if roles and "ts" in roles:
            layout = Layout("delimited", hits / n, roles=roles, delimiter=delimiter, columns=width, header=header)
            if best is None or layout.score > best.score:
                best = layout
    if best is not None:
        return best
    # text: 시각 모양
    found = Counter()
    for line in sample:
        hit = _find_ts(line)
        if hit:
            found[hit[0].name] += 1
    if found:
        name, hits = found.most_common(1)[0]
        pattern = next(p for p in TS_PATTERNS if p.name == name)
        if hits >= MIN_SHARE * n:
            layout = Layout("text", hits / n, ts=pattern)
            marked = leveled = 0
            for line in sample:
                rest, _ts, level = _strip_ts_level(line, pattern)
                leveled += level is not None
                if _component_split(rest, level is None):
                    marked += 1
            layout.has_level = leveled >= MIN_SHARE * n
            layout.component = marked >= MIN_SHARE * n
            return layout
    # 시각은 없지만 레벨 낱말이 줄마다 있으면 레벨만이라도 읽는다
    leveled = sum(1 for line in sample if any(level_of(t) for t in line.split()[:3]))
    if leveled >= MIN_SHARE * n:
        return Layout("text", leveled / n)
    return Layout("text", 0.0)


_COMPONENT = re.compile(r"^(?:\[(?P<b>[A-Za-z][\w.\-/]*)\]|(?P<p>[A-Za-z][\w.\-/]*?)(?:\[\d+\])?(?P<colon>:)?)(?P<ws>\s+)"
                        r"(?:(?P<sep>[-|>])\s+)?(?P<rest>.*)$")


def _component_split(rest: str, allow_level_after: bool) -> tuple[str, str, str | None] | None:
    """'구성요소 + 메시지'로 나눌 수 있으면 (구성요소, 메시지, 뒤따른 레벨). 표시가 있을 때만:
    [이름] · 이름: · 이름 - · 이름 | · 이름 > · 이름 다음에 레벨 낱말('구성요소 레벨 메시지' 배치)."""
    m = _COMPONENT.match(rest or "")
    if not m:
        return None
    name = m.group("b") or m.group("p")
    after = m.group("rest")
    if m.group("b") or m.group("colon") or m.group("sep") or "\t" in m.group("ws"):  # 탭도 칸 구분 표시
        return name, after, None
    if allow_level_after and after:
        head, _, tail = after.partition(" ")
        found = level_of(head)
        if found:
            return name, tail, found
    return None


def _logfmt(text: str) -> list[tuple[str, str, str]] | None:
    out, position = [], 0
    for match in _LOGFMT.finditer(text):
        if match.start() != position:
            return None
        position = match.end()
        value = match.group(2)
        if value.startswith('"'):
            value = re.sub(r"\\(.)", r"\1", value[1:-1])
        out.append((match.group(1), value, match.group(0).strip()))
    return out if out and not text[position:].strip() else None


def _strip_ts_level(line: str, pattern: TsPattern | None) -> tuple[str, str | None, str | None]:
    """(남은 글, 시각 글자, 레벨). 시각 앞뒤 괄호·우선순위와 레벨 낱말(앞 2개 토큰 안)을 뗀다."""
    ts_raw = None
    level = None
    rest = line
    priority = re.match(r"^\s*<(\d{1,3})>", rest)
    if priority:
        level = _SYSLOG[int(priority.group(1)) % 8]
        rest = rest[priority.end():]
    if pattern is not None:
        hit = None
        for match in pattern.regex.finditer(rest[:90]):
            if pattern.kind == "epoch" and match.start() != 0:
                continue
            hit = match
            break
        if hit:
            ts_raw = hit.group(0)
            before, after = rest[: hit.start()], rest[hit.end():]
            for token in before.split():
                level = level or level_of(token)
            rest = after.lstrip(" ]>)\t")
    rest = rest.lstrip(" |-\t:")
    parts = re.split(r"([ \t]+)", rest, maxsplit=2)  # [낱말0, 구분0, 낱말1, 구분1, 나머지] — 탭 구분도 살려 둔다
    tokens, seps = parts[0::2], parts[1::2]
    for i, token in enumerate(tokens[:2]):
        found = level_of(token.rstrip(":|-"))
        if found and level is None:
            level = found
            tail = tokens[2] if len(tokens) > 2 else ""
            if i == 1 and _IDENT.match(tokens[0].strip("[]:")):
                # '구성요소 레벨 메시지' 배치: 앞 낱말을 구성요소 표시(콜론)로 남겨 둔다
                rest = (tokens[0].strip("[]:") + ": " + tail).strip()
            elif i == 0 and len(tokens) > 1:
                rest = (tokens[1] + (seps[1] if len(seps) > 1 else " ") + tail).lstrip(" |-\t:").rstrip()
            else:
                rest = tokens[0] if i == 1 else ""
            break
    return rest, ts_raw, level


# ---------------------------------------------------------------- 파서

class AutoParser:
    name = "auto"

    def __init__(self) -> None:
        self._typer = EventTyper()

    @property
    def timestamp_basis(self) -> TimestampBasis:
        return TimestampBasis.ABSOLUTE  # 가져오기가 이벤트 시각 종류를 보고 낮춘다(시간대 없음·경과 시간)

    def sniff(self, head_lines: Sequence[str]) -> float:
        """구조(칸·레벨·구성요소)까지 읽히면 최대 0.59, 시각만 읽히면 최대 0.3.
        포괄형 프로파일(점수 절반, 최대 0.5)보다 구조를 더 읽을 때만 이기고, 다른 프로파일(0.6 이상)에는 늘 진다."""
        layout = detect_layout(head_lines)
        if layout.score < MIN_SHARE:
            return 0.0
        return round((MAX_SCORE if layout.structured else 0.3) * layout.score, 4)

    def parse(self, lines: Iterable[RawLine], cancel: CancelToken = NEVER_CANCEL) -> Iterator[ParsedEvent]:
        lines = list(lines)
        layout = detect_layout([l.text for l in lines[:200]])
        state = {"date": None, "first": None, "last": None, "day": 0, "header_done": not layout.header}
        current: dict | None = None
        seq = 0
        for index, line in enumerate(lines):
            if index % CANCEL_CHECK_EVERY == 0:
                cancel.raise_if_cancelled()
            text = line.text
            if not text.strip():
                if current is not None:
                    current["raw"].append(text)
                    current["msg"].append(text)
                    current["line_end"] = line.line_no
                continue
            item = self._line(text, line, layout, state)
            if item is None and current is not None and text[:1].isspace():
                current["raw"].append(text)
                current["msg"].append(text)
                current["line_end"] = line.line_no
                continue
            if current is not None:
                yield self._finish(current, seq)
                seq += 1
            current = item or self._raw(line, {"unparsed": True})
        if current is not None:
            yield self._finish(current, seq)

    # 한 줄 → 이벤트 재료(맞지 않으면 None)
    def _line(self, text: str, line: RawLine, layout: Layout, state: dict) -> dict | None:
        if layout.kind == "delimited" and not state["header_done"]:
            state["header_done"] = True
            return self._raw(line, {"header": True})
        date = _DATE_ONLY.match(text)
        if date and layout.kind == "text":
            state["date"] = (int(date.group(1)), int(date.group(2)), int(date.group(3)))
            return self._raw(line, {"date_line": True})
        if layout.kind in ("json", "logfmt", "delimited"):
            values = self._structured(text, layout)
            if values is None:
                return None
            return self._item(line, values.get("ts"), values.get("level"), values.get("component"),
                              values.get("message") or "", state)
        rest, ts_raw, level = _strip_ts_level(text, layout.ts)
        if layout.ts is not None and ts_raw is None:
            return None
        component = None
        if layout.component and rest:
            split = _component_split(rest, level is None)
            if split:
                component, rest, after_level = split
                level = level or after_level
        return self._item(line, ts_raw, level, component, rest.strip(), state)

    def _structured(self, text: str, layout: Layout) -> dict[str, str] | None:
        roles = layout.roles
        if layout.kind == "json":
            try:
                item = json.loads(text)
            except ValueError:
                return None
            if not isinstance(item, dict):
                return None
            pairs = [(k, v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)) for k, v in item.items()]
        elif layout.kind == "logfmt":
            parsed = _logfmt(text)
            if not parsed:
                return None
            pairs = [(k, v) for k, v, _ in parsed]
        else:
            cells = _cells(text, layout.delimiter)
            if len(cells) < layout.columns:
                return None
            if len(cells) > layout.columns and roles.get("message") == layout.columns - 1:
                cells = cells[: layout.columns - 1] + [layout.delimiter.join(cells[layout.columns - 1:])]
            pairs = list(enumerate(cells[: layout.columns]))
        out: dict[str, str] = {}
        used = set()
        for role, col in roles.items():
            for key, value in pairs:
                if key == col:
                    out[role] = value
                    used.add(key)
        if layout.kind != "delimited":
            extra = [f"{k}={v}" if " " not in v else f'{k}="{v}"' for k, v in pairs if k not in used]
            if extra:
                out["message"] = " ".join([out.get("message", ""), *extra]).strip()
        lvl = (out.get("level") or "").strip()
        out["level"] = (level_of(lvl) or lvl.upper()) if lvl else None  # 레벨 칸이 없는 배치도 있다
        return out

    def _item(self, line: RawLine, ts_raw: str | None, level: str | None, component: str | None, message: str,
              state: dict) -> dict:
        kind, value = self._timestamp(ts_raw, state) if ts_raw else (EventTimeKind.NONE, None)
        return {"line_no": line.line_no, "line_end": None, "byte_offset": line.byte_offset,
                "ts_raw": ts_raw.strip() if ts_raw else None, "ts_kind": kind, "ts_value": value,
                "level": level, "component": component, "msg": [message], "raw": [line.text], "attrs": None}

    def _timestamp(self, raw: str, state: dict) -> tuple[EventTimeKind, int | None]:
        text = raw.strip().strip("[]")
        pattern = next((p for p in TS_PATTERNS if p.regex.fullmatch(text)), None)
        if pattern is None:
            return EventTimeKind.NONE, None
        if pattern.kind == "epoch":
            digits = len(text.split(".")[0])
            scale = 1 if digits >= 16 else 1_000 if digits >= 13 else 1_000_000
            return EventTimeKind.ABSOLUTE, round(float(text) * scale)
        if pattern.kind == "absolute":
            moment = parse_absolute(pattern.name, text)
            if moment is None:
                return EventTimeKind.NONE, None
            kind = EventTimeKind.ABSOLUTE if moment.tzinfo else EventTimeKind.ABSOLUTE_NO_TZ
            return kind, _to_us(moment)
        if pattern.kind == "time_only" and state["date"]:
            y, m, d = state["date"]
            moment = _hms(y, m, d, text, None)
            return (EventTimeKind.ABSOLUTE_NO_TZ, _to_us(moment)) if moment else (EventTimeKind.NONE, None)
        # 연도 없는 시각·날짜 없는 시각 → 파일 첫 시각부터 경과(거꾸로 가면 날짜·해가 바뀐 것으로 본다)
        if pattern.kind == "no_year":
            parts = text.split()
            moment = _hms(2000, _MONTHS[parts[0].lower()], int(parts[1]), parts[2], None)
        else:
            moment = _hms(2000, 1, 1, text, None)
        if moment is None:
            return EventTimeKind.NONE, None
        us = _to_us(moment) + state["day"]
        if state["last"] is not None and us < state["last"] - 3_600_000_000:
            state["day"] += (366 if pattern.kind == "no_year" else 1) * 86_400_000_000
            us += (366 if pattern.kind == "no_year" else 1) * 86_400_000_000
        if state["first"] is None:
            state["first"] = us
        state["last"] = us
        return EventTimeKind.RELATIVE, us - state["first"]

    @staticmethod
    def _raw(line: RawLine, attrs: dict) -> dict:
        return {"line_no": line.line_no, "line_end": None, "byte_offset": line.byte_offset, "ts_raw": None,
                "ts_kind": EventTimeKind.NONE, "ts_value": None, "level": None, "component": None,
                "msg": [line.text], "raw": [line.text], "attrs": attrs}

    def _finish(self, item: dict, seq: int) -> ParsedEvent:
        message = "\n".join(item["msg"])
        found = _CODE.search(message.split("\n", 1)[0])
        return ParsedEvent(
            seq=seq, line_no=item["line_no"], line_end=item["line_end"], byte_offset=item["byte_offset"],
            ts_raw=item["ts_raw"], ts_value_us=item["ts_value"], ts_kind=item["ts_kind"], level=item["level"],
            component=item["component"], event_type=self._typer.event_type(message, item["component"]),
            error_code=found.group("code") if found else None, message=message, raw_text="\n".join(item["raw"]),
            attrs=item["attrs"],
        )
