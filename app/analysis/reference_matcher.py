"""계층 2 — 로그에 나온 코드와 승인된 참고 자료 맞추기 (변경 요청 CR-04 REF-04). 결정론.

- 이벤트의 오류 코드 칸이 승인된 항목의 코드와 같으면 맞춘다.
- 메시지 안 글자는 단어 단위로, 글자와 숫자가 섞인 3자 이상 코드만 맞춘다(숫자만인 코드는 오류 코드 칸에서만).
- 승인된 문서·포함 항목만 본다(approved_reference_entries 뷰). 뜻은 문서 글자 그대로 보여 준다.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field

from app.core.reference_structurer import normalize_code
from app.storage.database import Database
from app.storage.repositories import EventRepository, LogFileRepository, ReferenceRepository

TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]*")


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

    def source(self) -> str:
        return f"{self.document_title} · {self.locator}" if self.locator else self.document_title

    def as_dict(self) -> dict:
        return {"entry_id": self.entry_id, "document_id": self.document_id, "document_title": self.document_title,
                "document_name": self.document_name, "code": self.code, "title": self.title,
                "description": self.description, "cause": self.cause, "action": self.action, "locator": self.locator}


@dataclass
class ReferenceMatch:
    code: str  # 로그에 나온 그대로(처음 본 것)
    code_norm: str
    count: int
    first_log_file_id: int
    first_file_name: str
    first_line: int
    first_event_id: int
    via: str  # ERROR_CODE / MESSAGE
    entries: list[ReferenceHit] = field(default_factory=list)

    def summary(self) -> str:
        first = self.entries[0] if self.entries else None
        meaning = (first.title or first.description or "") if first else ""
        return (f"참고 자료 코드 {self.code} ×{self.count} (처음 {self.first_file_name} {self.first_line}행)"
                + (f": {meaning[:120]}" if meaning else "") + (f" [출처 {first.source()}]" if first else ""))


def _message_code(code_norm: str) -> bool:
    return len(code_norm) >= 3 and any(c.isalpha() for c in code_norm) and any(c.isdigit() for c in code_norm)


def match_references(database: Database, log_file_ids: Sequence[int]) -> list[ReferenceMatch]:
    with closing(database.connect()) as conn:
        approved = ReferenceRepository(conn).approved_entries()
        if not approved:
            return []
        events = EventRepository(conn).for_files(list(log_file_ids))
        names = {r.id: r.original_name for r in LogFileRepository(conn).get_many(list(log_file_ids))}
    by_norm: dict[str, list[ReferenceHit]] = {}
    for row in approved:
        by_norm.setdefault(row["code_norm"], []).append(ReferenceHit(
            int(row["id"]), int(row["document_id"]), row["document_title"], row["document_name"], row["code"],
            row["title"], row["description"], row["cause"], row["action"], row["locator"]))
    message_codes = {norm for norm in by_norm if _message_code(norm)}
    found: dict[str, ReferenceMatch] = {}
    for log_file_id in log_file_ids:
        for event in events.get(log_file_id, []):
            seen_here: set[str] = set()
            candidates: list[tuple[str, str, str]] = []
            if event.error_code:
                candidates.append((event.error_code.strip(), normalize_code(event.error_code), "ERROR_CODE"))
            if message_codes:
                for token in TOKEN.findall(event.message or event.raw_text or ""):
                    norm = token.upper()
                    if norm in message_codes:
                        candidates.append((token, norm, "MESSAGE"))
            for code, norm, via in candidates:
                if norm not in by_norm or norm in seen_here:
                    continue
                seen_here.add(norm)
                match = found.get(norm)
                if match is None:
                    found[norm] = ReferenceMatch(code, norm, 1, log_file_id, names.get(log_file_id, str(log_file_id)),
                                                 event.line_no, event.id, via, by_norm[norm])
                else:
                    match.count += 1
    return list(found.values())
