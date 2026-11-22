"""결과 조립 — 권장 대응 (변경 요청 CR-01) 과 결과 축 계산.

권장 대응은 유사한 **승인된** 비정상 사례의 **승인된** 대응 매뉴얼 항목에서만 가져온다.
매뉴얼이 없으면 진단 가이드의 해결 방법(RESOLUTION), 그것도 없으면 '등록된 대응 방법 없음'이다.
AI는 이 목록을 고르고 순서를 정리할 수만 있다(AI_POLICY V-9).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any, Sequence

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


@dataclass
class Applicable:
    """이 로그에 대응을 적용할 근거가 확인된 비정상 사례(CR-14 리뷰 2026-11-22). 이벤트 종류가 비슷한 것만으로는 아님."""
    case: Any  # CaseRecord
    reason: str  # 같은 오류 코드 … / 근거 지문이 비슷함 …
    recheck: bool = False  # 같은 코드지만 다른 단계에서 난 사례 → 적용 전 재확인


def build_response_guidance(database: Database, candidates: Sequence[Applicable], abnormal: bool) -> ResponseGuidance:
    """권장 대응은 (1) 최종 판정이 비정상 쪽이고 (2) 이 로그에 적용할 근거(같은 오류 코드·비슷한 근거 지문)가 확인된
    비정상 사례의 승인된 조치 문구만 보인다. 정상 로그에 승인된 대응이 함께 뜨던 문제(2026-11-22 리뷰)."""
    if not abnormal:
        return ResponseGuidance("NONE", message="이상 근거가 없거나 판단 보류라 대응을 붙이지 않았습니다. "
                                                "비슷한 비정상 사례는 아래 근거에 참고로만 있습니다.")
    if not candidates:
        return ResponseGuidance("NONE", message="이 로그에 적용할 근거(같은 오류 코드·비슷한 근거 지문)가 확인된 비정상 사례가 없어 "
                                                "등록된 대응을 붙이지 않았습니다.")
    with closing(database.connect()) as conn:
        rows = GuideRepository(conn).approved_items_for_cases([c.case.id for c in candidates])
    by_case: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        by_case.setdefault(row["case_id"], []).append(row)

    def item(row: sqlite3.Row, c: Applicable, order: int) -> ResponseItem:
        return ResponseItem(row["section"], row["content"], c.case.id, c.case.label, row["id"],
                            row["attachment_name"], row["attachment_kind"], order)

    def why(c: Applicable) -> str:
        return c.reason + (" — 다른 단계에서 난 사례라 적용 전에 같은 상황인지 재확인하세요" if c.recheck else "")

    for c in candidates:  # 적용 근거가 강한 사례부터, 매뉴얼이 있는 첫 사례
        manual = [r for r in by_case.get(c.case.id, []) if r["attachment_kind"] == "RESPONSE_MANUAL"
                  and r["section"] in MANUAL_ORDER]
        if manual:
            manual.sort(key=lambda r: (MANUAL_ORDER[r["section"]], r["sort_order"], r["id"]))
            checks = [item(r, c, i) for i, r in enumerate(by_case[c.case.id]) if r["section"] == "CHECK_METHOD"]
            return ResponseGuidance(
                "MANUAL", [item(r, c, i) for i, r in enumerate(manual)], checks, [c.case.label],
                f"이 로그에 적용할 근거가 확인된 승인 사례 {c.case.label}의 승인된 대응 매뉴얼입니다({why(c)}).")
    for c in candidates:
        resolution = [r for r in by_case.get(c.case.id, []) if r["section"] == "RESOLUTION"]
        if resolution:
            checks = [item(r, c, i) for i, r in enumerate(by_case[c.case.id]) if r["section"] == "CHECK_METHOD"]
            return ResponseGuidance(
                "GUIDE_ONLY", [item(r, c, i) for i, r in enumerate(resolution)], checks, [c.case.label],
                f"대응 매뉴얼은 없고, 적용할 근거가 확인된 승인 사례 {c.case.label}의 진단 가이드 해결 방법만 있습니다({why(c)}).")
    return ResponseGuidance("NONE", source_cases=[c.case.label for c in candidates],
                            message="적용할 근거가 확인된 승인 사례는 있지만 승인된 대응 매뉴얼·해결 방법이 없습니다. 등록된 대응 방법 없음.")
