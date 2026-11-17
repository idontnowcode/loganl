"""결과 조립 — 권장 대응 (변경 요청 CR-01) 과 결과 축 계산.

권장 대응은 유사한 **승인된** 비정상 사례의 **승인된** 대응 매뉴얼 항목에서만 가져온다.
매뉴얼이 없으면 진단 가이드의 해결 방법(RESOLUTION), 그것도 없으면 '등록된 대응 방법 없음'이다.
AI는 이 목록을 고르고 순서를 정리할 수만 있다(AI_POLICY V-9).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from typing import Sequence

from app.analysis.case_retriever import SimilarCase
from app.storage.database import Database
from app.storage.repositories import GuideRepository

MANUAL_ORDER = {"PRECAUTION": 0, "RESPONSE_STEP": 1, "VERIFICATION": 2, "ESCALATION": 3}


@dataclass
class ResponseItem:
    section: str
    content: str
    case_id: int
    case_label: str
    item_id: int
    source_name: str | None
    source_kind: str | None
    order: int = 0


@dataclass
class ResponseGuidance:
    level: str  # MANUAL / GUIDE_ONLY / NONE
    items: list[ResponseItem] = field(default_factory=list)
    check_methods: list[ResponseItem] = field(default_factory=list)
    source_cases: list[str] = field(default_factory=list)
    message: str = ""


def build_response_guidance(database: Database, similar_abnormal: Sequence[SimilarCase]) -> ResponseGuidance:
    relevant = [s for s in similar_abnormal if s.relevant]
    if not relevant:
        return ResponseGuidance("NONE", message="비슷한 승인 비정상 사례가 없어 등록된 대응 방법이 없습니다.")
    with closing(database.connect()) as conn:
        rows = GuideRepository(conn).approved_items_for_cases([s.case.id for s in relevant])
    by_case: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        by_case.setdefault(row["case_id"], []).append(row)

    def item(row: sqlite3.Row, s: SimilarCase, order: int) -> ResponseItem:
        return ResponseItem(row["section"], row["content"], s.case.id, s.case.label, row["id"],
                            row["attachment_name"], row["attachment_kind"], order)

    for s in relevant:  # 점수가 높은 사례부터, 매뉴얼이 있는 첫 사례
        manual = [r for r in by_case.get(s.case.id, []) if r["attachment_kind"] == "RESPONSE_MANUAL"
                  and r["section"] in MANUAL_ORDER]
        if manual:
            manual.sort(key=lambda r: (MANUAL_ORDER[r["section"]], r["sort_order"], r["id"]))
            checks = [item(r, s, i) for i, r in enumerate(by_case[s.case.id]) if r["section"] == "CHECK_METHOD"]
            return ResponseGuidance(
                "MANUAL", [item(r, s, i) for i, r in enumerate(manual)], checks, [s.case.label],
                f"비슷한 승인 사례 {s.case.label}의 승인된 대응 매뉴얼에서 가져왔습니다({s.explain()}).")
    for s in relevant:
        resolution = [r for r in by_case.get(s.case.id, []) if r["section"] == "RESOLUTION"]
        if resolution:
            checks = [item(r, s, i) for i, r in enumerate(by_case[s.case.id]) if r["section"] == "CHECK_METHOD"]
            return ResponseGuidance(
                "GUIDE_ONLY", [item(r, s, i) for i, r in enumerate(resolution)], checks, [s.case.label],
                f"대응 매뉴얼은 없고, 비슷한 승인 사례 {s.case.label}의 진단 가이드 해결 방법만 있습니다({s.explain()}).")
    return ResponseGuidance("NONE", source_cases=[s.case.label for s in relevant],
                            message="비슷한 승인 사례는 있지만 승인된 대응 매뉴얼·해결 방법이 없습니다. 등록된 대응 방법 없음.")
