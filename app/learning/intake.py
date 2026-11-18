"""자유 글 정리를 사례·분석 칸에 반영 (변경 요청 CR-05).

- 규칙(AI 없이): 제목은 글 첫 줄, 운전 모드는 등록된 이름이 글에 그대로 있을 때만.
- AI(모델이 있으면): 칸에 맞는 부분을 글에서 그대로 복사한다(app.ai.intake). 글에 없는 글자는 버린다(V-10).
- 찾은 칸만 바꾸고 못 찾은 칸은 그대로 둔다. 모드는 바꾸면 '확인 전'으로 둔다(사용자가 확인, 계획서 §3.1 5단계).
- 글은 정리하기 전에 먼저 저장한다. AI가 실패해도 사용자가 쓴 글은 남는다.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from app.ai.intake import KIND_FIELDS, pick_fields, split_story
from app.ai.ollama_client import OllamaClient
from app.config import AppSettings
from app.core.cancel import NEVER_CANCEL, CancelToken
from app.learning.case_manager import CaseError, CaseManager
from app.models.records import ModeRecord
from app.storage.database import Database

TITLE_MAX = 40
UNTITLED = "제목 없는 사례"
FIELD_LABELS = {
    "operating_conditions": "운전 조건", "fw_version": "FW 버전", "device_config": "장비 구성",
    "normal_rationale": "정상 판정 근거", "symptom": "오류 증상", "abnormal_criteria": "판정 기준",
}
CAUSE_LABELS = {"CONFIRMED": "원인 확정", "SUSPECTED": "원인 추정", "UNKNOWN": "원인 모름"}
AI_FAIL_LABELS = {
    "NO_MODEL": "설정에서 AI 모델을 고르지 않았습니다",
    "AI_UNAVAILABLE": "AI 서버에 연결되지 않았습니다",
    "VALIDATION_FAILED": "AI 답이 형식에 맞지 않았습니다",
}


def title_from_story(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped if len(stripped) <= TITLE_MAX else stripped[:TITLE_MAX].rstrip() + "…"
    return None


def match_mode_name(text: str, modes: Sequence[ModeRecord]) -> ModeRecord | None:
    """등록된 모드 이름이 글에 그대로 있으면 그 모드. 서로 다른 모드가 둘 이상 나오면 정하지 않는다."""
    lowered = text.casefold()
    found = []
    for mode in modes:
        name = mode.name.strip().casefold()
        if name and re.search(r"(?<![0-9a-z])" + re.escape(name) + r"(?![0-9a-z])", lowered):
            found.append(mode)
    if not found:
        return None
    found.sort(key=lambda m: len(m.name), reverse=True)
    longest = found[0]
    others = [m for m in found[1:] if m.name.strip().casefold() not in longest.name.strip().casefold()]
    return None if others else longest


CAUSE_WORDS = {
    "UNKNOWN": ("모르", "모름", "미상", "불명", "파악 안", "파악하지 못", "못 찾"),
    "SUSPECTED": ("추정", "의심", "같", "듯", "아마", "보임", "보인다"),
    "CONFIRMED": ("확정", "확인됨", "확인했", "밝혀"),
}


def match_cause_words(text: str) -> str | None:
    """'원인'이 들어간 문장 하나에서 원인 확정 여부를 읽는다. 문장이 둘 이상이거나 뜻이 섞이면 정하지 않는다."""
    sentences = [s.text for s in split_story(text) if "원인" in s.text]
    if len(sentences) != 1:
        return None
    found = {status for status, words in CAUSE_WORDS.items() if any(w in sentences[0] for w in words)}
    return found.pop() if len(found) == 1 else None


@dataclass
class IntakeResult:
    status: str  # OK / RULES_ONLY (AI 없이 규칙만) / NO_TEXT
    ai_status: str = "SKIPPED"  # OK / NO_MODEL / AI_UNAVAILABLE / VALIDATION_FAILED / NO_SEGMENTS / SKIPPED
    fields: dict[str, str] = field(default_factory=dict)
    title: str | None = None
    mode_id: int | None = None
    mode_name: str | None = None
    mode_source: str | None = None  # NAME(이름 일치) / AI
    cause_status: str | None = None
    applied: list[str] = field(default_factory=list)  # 사례에 실제로 바꾼 칸
    text_saved: bool = False  # 글을 사례에 저장했는지(신규 분석은 저장하지 않음)
    dropped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def found_labels(self) -> list[str]:
        labels = [FIELD_LABELS[name] for name in self.fields]
        if self.mode_name:
            labels.append(f"운전 모드({self.mode_name}, 확인 필요)")
        if self.cause_status:
            labels.append(CAUSE_LABELS[self.cause_status])
        return labels

    def message(self) -> str:
        if self.status == "NO_TEXT":
            return "정리할 글이 없습니다. 위 칸에 상황을 편하게 적어 주세요."
        found = self.found_labels()
        head = f"내 글에서 옮겼습니다: {', '.join(found)}." if found else "내 글에서 칸에 옮길 내용을 찾지 못했습니다."
        if self.status == "RULES_ONLY":
            reason = AI_FAIL_LABELS.get(self.ai_status, "AI를 쓸 수 없었습니다")
            saved = "글은 저장했고 " if self.text_saved else ""
            head = (f"{reason}. {saved}" + (f"{', '.join(found)}만 채웠습니다." if found else "칸은 채우지 못했습니다.")
                    + " 아래 '칸으로 직접 고치기'에서 빠진 칸을 채우세요.")
        invented = [d for d in self.dropped if "글에 없는" in d]
        if invented:
            head += f" (AI가 고른 것 중 {len(invented)}건은 내 글에 없는 말이라 버렸습니다.)"
        return head


ClientFactory = Callable[[], OllamaClient]


class IntakeService:
    def __init__(self, database: Database, settings: AppSettings, client_factory: ClientFactory) -> None:
        self.database = database
        self.settings = settings
        self.client_factory = client_factory

    def organize_text(self, text: str, kind: str, cancel: CancelToken = NEVER_CANCEL) -> IntakeResult:
        """DB를 쓰지 않고 글만 정리한다(신규 분석 화면이 직접 칸에 넣는다)."""
        text = text.strip()
        if not text:
            return IntakeResult("NO_TEXT")
        modes = []  # CR-09: 운전 모드로 나누지 않으므로 글에서 모드를 찾지 않는다
        result = IntakeResult("OK", title=title_from_story(text))
        by_name = match_mode_name(text, modes)
        if by_name is not None:
            result.mode_id, result.mode_name, result.mode_source = by_name.id, by_name.name, "NAME"
        if kind == "ABNORMAL":
            result.cause_status = match_cause_words(text)
        picks = pick_fields(self.client_factory(), self.settings.ollama.model, text, kind,
                            [m.name for m in modes], cancel)
        result.ai_status = picks.status
        if picks.status != "OK":
            result.status = "RULES_ONLY"
            result.errors = picks.errors
            return result
        result.fields = {name: value for name, value in picks.values.items() if name in KIND_FIELDS[kind]}
        result.dropped = picks.dropped
        if result.mode_id is None and picks.mode_name:
            chosen = next(m for m in modes if m.name == picks.mode_name)
            result.mode_id, result.mode_name, result.mode_source = chosen.id, chosen.name, "AI"
        if kind == "ABNORMAL" and picks.cause_status:
            result.cause_status = picks.cause_status  # 근거 글이 확인된 AI 판단이 규칙보다 우선
        return result

    def organize_case(self, case_id: int, text: str, cancel: CancelToken = NEVER_CANCEL) -> IntakeResult:
        cases = CaseManager(self.database)
        case = cases.get(case_id)
        if case.status in ("APPROVED", "ARCHIVED"):
            raise CaseError("승인·보관된 사례는 바꿀 수 없습니다. '수정 시작'을 먼저 누르세요.")
        cases.update(case_id, user_description=text.strip() or None)  # 정리가 실패해도 글은 남게 먼저 저장
        result = self.organize_text(text, case.case_type, cancel)
        result.text_saved = bool(text.strip())
        if result.status == "NO_TEXT":
            return result
        changes: dict[str, object] = dict(result.fields)
        if result.cause_status:
            changes["cause_status"] = result.cause_status
        if result.mode_id is not None and result.mode_id != case.mode_id:
            changes.update(mode_id=result.mode_id, mode_confirmed=False)
        if result.title and (not case.title.strip() or case.title == UNTITLED):
            changes["title"] = result.title
        current = {name: getattr(case, name, None) for name in changes}
        result.applied = [name for name, value in changes.items() if current.get(name) != value]
        if result.applied:
            cases.update(case_id, **changes)
        return result
