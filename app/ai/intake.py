"""자유 글 정리 (변경 요청 CR-05).

사용자가 칸 대신 편하게 쓴 글에서, AI는 **칸에 맞는 부분을 글에서 그대로 복사해 답하기만 한다.**
프로그램이 복사한 글자가 사용자 글에 실제로 있는지 검사하고, 없으면 버린다(AI_POLICY V-10).
그래서 칸에 들어가는 값은 언제나 사용자가 쓴 글자 그대로다. AI가 지어낸 값은 들어갈 수 없다.

(처음에는 문장 조각 키를 고르게 했으나, 실제 gemma3:4b가 키 자리에 글을 복사해 넣어 거의 다 버려졌다.
작은 모델에게 자연스러운 '그대로 복사'로 바꾸고 검사는 프로그램이 한다. 키를 답해도 그 조각 글로 받아 준다.)
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from app.ai.context_builder import DATA_RULES, EvidencePackage
from app.ai.ollama_client import OllamaClient
from app.ai.response_validator import ask_structured
from app.core.cancel import NEVER_CANCEL, CancelToken

FIELD_HELP = {
    "operating_conditions": "운전 조건·상황: 언제, 어떤 설정·환경·단계에서 돌렸는지 (운전 모드 이름만 따로 넣지는 않는다)",
    "fw_version": "FW(펌웨어) 버전 글자",
    "device_config": "장비 구성: 연결된 장치·옵션·모듈",
    "normal_rationale": "정상 판정 근거: 정상이라고 본 이유(문제가 없었다는 말)",
    "symptom": "오류 증상: 무엇이 잘못됐는지(화면·동작에 나타난 문제를 말한 부분)",
    "abnormal_criteria": "판정 기준: 무엇이면 비정상·불량으로 보는지(조건과 판단을 말한 부분)",
}
REQUIRED_FIELDS = {"NORMAL": ("normal_rationale",), "ABNORMAL": ("symptom", "abnormal_criteria"), "ANALYSIS": ()}
KIND_FIELDS = {
    "NORMAL": ("operating_conditions", "fw_version", "device_config", "normal_rationale"),
    "ABNORMAL": ("operating_conditions", "fw_version", "device_config", "symptom", "abnormal_criteria"),
    "ANALYSIS": ("operating_conditions",),
}
CAUSE_VALUES = ("CONFIRMED", "SUSPECTED", "UNKNOWN")
NOT_SAID = "NOT_SAID"
UNKNOWN_MODE = "UNKNOWN"
MAX_SEGMENTS = 60
_BOUNDARY = re.compile(r"[.!?。]+(?=\s|$)")
_KEY = re.compile(r"^U-\d{4}$")
_QUOTES = "\"'“”‘’「」『』`"


@dataclass
class Segment:
    text: str
    span_start: int
    span_end: int


class FieldPick(BaseModel):
    field: str = Field(description="칸 이름")
    text: str = Field(description="사용자 글에서 한 글자도 바꾸지 않고 그대로 복사한 부분")


class IntakeAnswer(BaseModel):
    picks: list[FieldPick] = Field(default_factory=list)
    mode: str = Field(default=UNKNOWN_MODE, description="등록된 모드 이름 중 하나 또는 UNKNOWN")
    mode_text: str = Field(default="", description="글에서 모드를 말한 부분 그대로")
    cause_status: str = Field(default=NOT_SAID, description="CONFIRMED / SUSPECTED / UNKNOWN / NOT_SAID")
    cause_text: str = Field(default="", description="글에서 원인 확정 여부를 말한 부분 그대로")


@dataclass
class IntakePicks:
    status: str  # OK / NO_MODEL / AI_UNAVAILABLE / VALIDATION_FAILED / NO_SEGMENTS
    values: dict[str, str] = field(default_factory=dict)
    mode_name: str | None = None
    cause_status: str | None = None
    dropped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def split_story(text: str) -> list[Segment]:
    """줄과 문장 끝(. ! ? 。 뒤 공백)으로 나눈다. '2.3' 같은 소수점은 나누지 않는다."""
    segments: list[Segment] = []
    line_start = 0
    for line in text.split("\n"):
        start = 0
        for match in _BOUNDARY.finditer(line):
            _add(segments, line, line_start, start, match.end())
            start = match.end()
        _add(segments, line, line_start, start, len(line))
        line_start += len(line) + 1
    return segments[:MAX_SEGMENTS]


def _add(segments: list[Segment], line: str, line_start: int, a: int, b: int) -> None:
    piece = line[a:b]
    stripped = piece.strip()
    if not stripped:
        return
    begin = line_start + a + (len(piece) - len(piece.lstrip()))
    segments.append(Segment(stripped, begin, begin + len(stripped)))


def _collapse(text: str) -> str:
    return " ".join(text.split())


class Story:
    """사용자 글. AI가 복사했다는 글자가 정말 글에 있는지 찾는다."""

    def __init__(self, text: str, segments: Sequence[Segment]) -> None:
        self.text = text
        self.collapsed = _collapse(text)
        self.by_key = {f"U-{i + 1:04d}": s.text for i, s in enumerate(segments)}

    def find(self, copied: str) -> tuple[str, int] | None:
        """(글에 있는 그대로의 값, 글 안의 위치). 없으면 None. 공백 차이와 감싼 따옴표는 봐준다."""
        wanted = copied.strip().strip(_QUOTES).strip()
        if not wanted:
            return None
        if _KEY.match(wanted) and wanted in self.by_key:  # 조각 키로 답하면 그 조각 글
            wanted = self.by_key[wanted]
        position = self.text.find(wanted)
        if position >= 0:
            return wanted, position
        collapsed = _collapse(wanted)
        position = self.collapsed.find(collapsed)
        return (collapsed, position) if collapsed and position >= 0 else None


def pick_fields(client: OllamaClient, model: str | None, story_text: str, kind: str,
                mode_names: Sequence[str], cancel: CancelToken = NEVER_CANCEL) -> IntakePicks:
    segments = split_story(story_text)
    if not segments:
        return IntakePicks("NO_SEGMENTS")
    fields = KIND_FIELDS[kind]
    package = EvidencePackage(prefix="U")
    for segment in segments:
        package.add("STORY", segment.text, span_start=segment.span_start, span_end=segment.span_end)

    fields_help = "\n".join(f"- {name}: {FIELD_HELP[name]}" for name in fields)
    modes = "\n".join(f"- {name}" for name in mode_names) or "(등록된 모드 없음 — mode는 UNKNOWN)"
    cause_help = ("원인 확정 여부: 글에서 원인이 확정됐다고 말하면 CONFIRMED, 추정이라고 말하면 SUSPECTED, "
                  "원인을 모른다고 말하면 UNKNOWN, 말하지 않았으면 NOT_SAID. NOT_SAID가 아니면 그렇게 말한 부분을 "
                  "cause_text에 글자 그대로 복사.\n"
                  if kind == "ABNORMAL" else "원인 확정 여부는 이 화면에서 쓰지 않는다. cause_status는 NOT_SAID.\n")
    required = ", ".join(REQUIRED_FIELDS[kind])
    must = (f"특히 {required} 칸은 승인에 꼭 필요하니, 글에 그런 말이 있으면 빠뜨리지 마라. " if required else "")
    system = ("너는 사용자가 쓴 글에서 칸에 맞는 부분을 찾아 그대로 복사하는 보조자다. " + DATA_RULES +
              " picks의 text에는 사용자 글의 글자를 한 글자도 바꾸지 말고 복사하라. 요약하거나 고쳐 쓰지 마라. "
              "글에 없는 내용은 쓰지 마라. 맞는 부분이 없는 칸은 picks에 넣지 마라(UNKNOWN 같은 자리표시도 쓰지 마라). "
              + must + "한 문장이 여러 칸에 맞으면 칸마다 넣어도 된다. field에는 '쓸 수 있는 칸'의 이름만 쓴다. "
              "예: {\"picks\":[{\"field\":\"fw_version\",\"text\":\"1.0\"}],\"mode\":\"UNKNOWN\","
              "\"mode_text\":\"\",\"cause_status\":\"NOT_SAID\",\"cause_text\":\"\"}")
    prompt = (f"쓸 수 있는 칸:\n{fields_help}\n\n"
              f"등록된 운전 모드(글에서 이 중 하나를 말했을 때만 mode에 그 이름, mode_text에 말한 부분):\n{modes}\n\n"
              f"{cause_help}\n사용자 글(문장별로 나눔):\n{package.render(12_000)}")

    result = ask_structured(client, model, system=system, prompt=prompt, schema=IntakeAnswer, package=None,
                            cancel=cancel)
    if result.value is None:
        return IntakePicks(result.status, errors=result.errors)
    return filter_answer(result.value, fields, Story(story_text, segments), mode_names, kind)


def filter_answer(answer: IntakeAnswer, fields: Sequence[str], story: Story, mode_names: Sequence[str],
                  kind: str) -> IntakePicks:
    """V-10: 글에 없는 글자·쓸 수 없는 칸·등록되지 않은 모드는 버리고 나머지를 값으로 만든다."""
    picks = IntakePicks("OK")
    collected: dict[str, list[tuple[int, str]]] = {}
    mode_words = {name.casefold() for name in mode_names}
    for pick in answer.picks:
        if pick.field not in fields:
            if pick.field not in ("cause_status", "cause_text", "mode", "mode_text"):  # 칸 이름을 헷갈린 것은 조용히 넘김
                picks.dropped.append(f"쓸 수 없는 칸 {pick.field}")
            continue
        found = story.find(pick.text)
        if found is None:
            picks.dropped.append(f"{pick.field}: 글에 없는 글자 '{pick.text[:40]}'")
            continue
        value, position = found
        if pick.field == "operating_conditions" and value.strip(" .,").casefold() in mode_words:
            picks.dropped.append("운전 조건에 모드 이름만 들어 있어 뺌")  # 모드는 따로 다룬다
            continue
        collected.setdefault(pick.field, []).append((position, value))
    for name, parts in collected.items():
        values: list[str] = []
        for _position, value in sorted(parts, key=lambda p: p[0]):
            if not any(value in kept for kept in values):  # 같은 글·이미 들어간 글의 일부는 다시 넣지 않는다
                values = [kept for kept in values if kept not in value] + [value]
        picks.values[name] = " ".join(values)

    canonical = {name.casefold(): name for name in mode_names}
    mode = answer.mode.strip()
    if mode and mode != UNKNOWN_MODE:
        if mode.casefold() not in canonical:
            picks.dropped.append(f"등록되지 않은 모드 {mode}")
        elif story.find(answer.mode_text) is None:
            picks.dropped.append("모드를 말한 부분이 글에 없음")
        else:
            picks.mode_name = canonical[mode.casefold()]

    cause = answer.cause_status.strip().upper()
    if kind == "ABNORMAL" and cause in CAUSE_VALUES:
        if story.find(answer.cause_text) is not None:
            picks.cause_status = cause
        else:
            picks.dropped.append("원인 확정 여부를 말한 부분이 글에 없음")
    elif kind == "ABNORMAL" and cause not in (NOT_SAID, ""):
        picks.dropped.append(f"쓸 수 없는 원인 확정 여부 {cause}")
    return picks
