"""계층 2 — 로그에 나온 코드와 승인된 참고 자료 맞추기 (변경 요청 CR-04 REF-04). 결정론.

- 이벤트의 오류 코드 칸이 승인된 항목의 코드와 같으면 맞춘다.
- 메시지 안 글자는 단어 단위로, 글자와 숫자가 섞인 3자 이상 코드만 맞춘다(숫자만인 코드는 오류 코드 칸에서만).
- 코드처럼 생기지 않은 4자 이상 항목(예: `NOTICE flag=TEST-FK`)은 메시지에 그 글이 그대로 있으면 맞춘다(대소문자·공백 차이 무시,
  앞뒤 낱말 경계, CR-16 RJ-03).
- 승인된 문서·포함 항목만 본다(approved_reference_entries 뷰). 뜻은 문서 글자 그대로 보여 준다.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field

from app.core.reference_structurer import judgment_side, looks_like_code, normalize_code
from app.storage.database import Database
from app.storage.repositories import EventRepository, LogFileRepository, ReferencePassageRepository, ReferenceRepository

TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]*")
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]{2,}|[가-힣]{2,}")
IDENTIFIER = re.compile(r"(?<![A-Za-z0-9_\-])(?=[A-Za-z0-9_\-]*[A-Z])(?=[A-Za-z0-9_\-]*[0-9_\-])"
                        r"[A-Za-z0-9][A-Za-z0-9_\-]{2,}")  # 이벤트 이름처럼 생긴 낱말: 대문자 + 숫자·-·_
PASSAGE_LIMIT = 3
CODE_WEIGHT = 3
IDENTIFIER_WEIGHT = 2
MIN_WORDS = 3  # 코드·이벤트 이름 없이 흔한 낱말만 겹치면 3개 이상일 때만
TEXT_KEY_MIN = 4  # 글 항목(코드처럼 생기지 않은 것)은 공백 뺀 4자 이상일 때만 맞춘다
EVENT_IDS_KEPT = 500  # 맞춘 이벤트 id는 이만큼만 기억한다(판정 적용용)


@dataclass
class ReferenceHit:
    entry_id: int
    document_id: int
    document_title: str
    document_name: str
    code: str
    title: str | None
    description: str | None
    cause: str | None
    action: str | None
    locator: str | None
    judgment: str | None = None  # 문서의 판정 낱말 그대로(CR-16 RJ-01)

    @property
    def side(self) -> str | None:
        """판정 쪽: ABNORMAL·NORMAL·None(판정 칸이 없거나 판정에 안 쓰는 낱말)."""
        return judgment_side(self.judgment)

    def source(self) -> str:
        return f"{self.document_title} · {self.locator}" if self.locator else self.document_title

    def as_dict(self) -> dict:
        return {"entry_id": self.entry_id, "document_id": self.document_id, "document_title": self.document_title,
                "document_name": self.document_name, "code": self.code, "title": self.title,
                "description": self.description, "cause": self.cause, "action": self.action, "locator": self.locator,
                "judgment": self.judgment}


@dataclass
class ReferenceMatch:
    code: str  # 로그에 나온 그대로(처음 본 것)
    code_norm: str
    count: int
    first_log_file_id: int
    first_file_name: str
    first_line: int
    first_event_id: int
    via: str  # ERROR_CODE / MESSAGE / TEXT
    entries: list[ReferenceHit] = field(default_factory=list)
    event_ids: list[int] = field(default_factory=list)  # 맞춘 이벤트(앞에서 EVENT_IDS_KEPT개, 판정 적용용)
    log_file_ids: list[int] = field(default_factory=list)

    def summary(self) -> str:
        first = self.entries[0] if self.entries else None
        meaning = (first.title or first.description or "") if first else ""
        return (f"참고 자료 코드 {self.code} ×{self.count} (처음 {self.first_file_name} {self.first_line}행)"
                + (f": {meaning[:120]}" if meaning else "") + (f" [출처 {first.source()}]" if first else ""))


@dataclass
class PassageHit:
    passage_id: int
    document_id: int
    document_title: str
    locator: str | None
    body: str
    score: int
    matched: list[str]

    def source(self) -> str:
        return f"{self.document_title} · {self.locator}" if self.locator else self.document_title

    def summary(self) -> str:
        return (f"참고 자료 관련 문단 [{self.source()}] (겹친 낱말: {', '.join(self.matched[:6])}): "
                f"{self.body[:160]}")

    def as_dict(self) -> dict:
        return {"passage_id": self.passage_id, "document_id": self.document_id, "document_title": self.document_title,
                "locator": self.locator, "body": self.body, "score": self.score, "matched": self.matched}


def _words(text: str) -> set[str]:
    return {w.upper() for w in WORD.findall(text or "")}


def match_passages(database: Database, log_file_ids: Sequence[int], extra_text: str | None = None,
                   limit: int = PASSAGE_LIMIT) -> list[PassageHit]:
    """CR-07 REF-07: 승인된 일반 문서 문단 중 로그와 겹치는 것. 점수: 오류 코드 3, 이벤트 이름처럼 생긴 낱말
    (대문자 + 숫자·-·_, 예: TEST-RETRY) 2, 그 밖의 낱말 1. 코드·이벤트 이름이 겹치는 문단을 먼저 세우고,
    흔한 낱말만 겹치는 문단은 3개 이상 겹칠 때만 넣는다. 문단 대부분에 나오는 낱말은 뺀다. 의미 검색은 쓰지 않는다."""
    with closing(database.connect()) as conn:
        rows = ReferencePassageRepository(conn).approved()
        if not rows:
            return []
        events = EventRepository(conn).for_files(list(log_file_ids))
    codes: set[str] = set()
    identifiers: set[str] = set()
    words: set[str] = _words(extra_text or "")
    for file_events in events.values():
        for event in file_events:
            if event.error_code:
                codes.add(normalize_code(event.error_code))
            text = event.message or event.raw_text or ""
            words |= _words(text)
            identifiers |= {t.upper() for t in IDENTIFIER.findall(text)}
    identifiers -= codes
    words -= codes | identifiers
    bodies = [(row, _words(row["body"])) for row in rows]
    if len(bodies) >= 4:  # 흔한 낱말 빼기
        common = {w for w in words | codes | identifiers if sum(1 for _r, b in bodies if w in b) > len(bodies) // 2}
        words -= common
        codes -= common
        identifiers -= common
    hits: list[PassageHit] = []
    ranks: dict[int, int] = {}
    for row, body_words in bodies:
        matched_codes = sorted(codes & body_words)
        matched_ids = sorted(identifiers & body_words)
        matched_words = sorted(words & body_words)
        strong = len(matched_codes) + len(matched_ids)
        if not strong and len(matched_words) < MIN_WORDS:
            continue
        score = CODE_WEIGHT * len(matched_codes) + IDENTIFIER_WEIGHT * len(matched_ids) + len(matched_words)
        hit = PassageHit(int(row["id"]), int(row["document_id"]), row["document_title"], row["locator"], row["body"],
                         score, matched_codes + matched_ids + matched_words)
        ranks[hit.passage_id] = strong
        hits.append(hit)
    hits.sort(key=lambda h: (-ranks[h.passage_id], -h.score, h.document_id, h.passage_id))
    return hits[:limit]


def _message_code(code_norm: str) -> bool:
    return len(code_norm) >= 3 and any(c.isalpha() for c in code_norm) and any(c.isdigit() for c in code_norm)


def _flat(text: str) -> str:
    return " ".join(text.split()).casefold()


def _text_pattern(keys: Sequence[str]) -> re.Pattern | None:
    """글 항목들을 하나의 정규식으로(앞뒤가 글자·숫자면 낱말 경계). 긴 것부터 맞춘다."""
    parts = []
    for index, key in enumerate(keys):
        body = re.escape(key).replace(r"\ ", r"\s+")
        head = r"(?<![0-9a-z_])" if key[0].isascii() and key[0].isalnum() else ""
        tail = r"(?![0-9a-z_])" if key[-1].isascii() and key[-1].isalnum() else ""
        parts.append((len(key), f"(?P<k{index}>{head}{body}{tail})"))
    if not parts:
        return None
    return re.compile("|".join(p for _n, p in sorted(parts, key=lambda x: -x[0])))


def match_references(database: Database, log_file_ids: Sequence[int]) -> list[ReferenceMatch]:
    with closing(database.connect()) as conn:
        approved = ReferenceRepository(conn).approved_entries()
        if not approved:
            return []
        events = EventRepository(conn).for_files(list(log_file_ids))
        names = {r.id: r.original_name for r in LogFileRepository(conn).get_many(list(log_file_ids))}
    by_norm: dict[str, list[ReferenceHit]] = {}
    text_keys: dict[str, tuple[str, str]] = {}  # 글 항목(소문자) → (code_norm, 문서에 적힌 그대로)
    for row in approved:
        keys = row.keys()
        by_norm.setdefault(row["code_norm"], []).append(ReferenceHit(
            int(row["id"]), int(row["document_id"]), row["document_title"], row["document_name"], row["code"],
            row["title"], row["description"], row["cause"], row["action"], row["locator"],
            row["judgment"] if "judgment" in keys else None))
        code = row["code"] or ""
        if not looks_like_code(code) and len(row["code_norm"]) >= TEXT_KEY_MIN:
            text_keys.setdefault(_flat(code), (row["code_norm"], " ".join(code.split())))
    message_codes = {norm for norm in by_norm if _message_code(norm)}
    key_list = list(text_keys)
    pattern = _text_pattern(key_list)
    found: dict[str, ReferenceMatch] = {}
    for log_file_id in log_file_ids:
        for event in events.get(log_file_id, []):
            seen_here: set[str] = set()
            candidates: list[tuple[str, str, str]] = []
            text = event.message or event.raw_text or ""
            if event.error_code:
                candidates.append((event.error_code.strip(), normalize_code(event.error_code), "ERROR_CODE"))
            if message_codes:
                for token in TOKEN.findall(text):
                    norm = token.upper()
                    if norm in message_codes:
                        candidates.append((token, norm, "MESSAGE"))
            if pattern is not None:
                for hit in pattern.finditer(_flat(text)):
                    norm, written = text_keys[key_list[int(hit.lastgroup[1:])]]
                    candidates.append((written, norm, "TEXT"))
            for code, norm, via in candidates:
                if norm not in by_norm or norm in seen_here:
                    continue
                seen_here.add(norm)
                match = found.get(norm)
                if match is None:
                    match = found[norm] = ReferenceMatch(code, norm, 0, log_file_id,
                                                         names.get(log_file_id, str(log_file_id)), event.line_no,
                                                         event.id, via, by_norm[norm])
                match.count += 1
                if len(match.event_ids) < EVENT_IDS_KEPT:
                    match.event_ids.append(event.id)
                if log_file_id not in match.log_file_ids:
                    match.log_file_ids.append(log_file_id)
    return list(found.values())
