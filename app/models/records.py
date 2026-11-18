"""계층 사이에서 주고받는 레코드 (Pydantic). DB 행을 그대로 옮기거나, 파서·가져오기 결과를 담는다."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import EventTimeKind, OriginPurpose, TimestampBasis


class _Record(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ── 파서 ──
class RawLine(_Record):
    line_no: int
    byte_offset: int | None = None
    text: str


class ParsedEvent(_Record):
    seq: int
    line_no: int
    line_end: int | None = None
    byte_offset: int | None = None
    ts_raw: str | None = None
    ts_value_us: int | None = None
    ts_kind: EventTimeKind = EventTimeKind.NONE
    level: str | None = None
    component: str | None = None
    event_type: str | None = None
    error_code: str | None = None
    message: str
    raw_text: str
    attrs: dict[str, Any] | None = None


class TimestampSpec(_Record):
    """iso: ISO-8601 비슷한 절대시각 / strptime: formats 목록 / relative: 기준 없는 경과 시간 /
    epoch: 유닉스 시간(unit 단위, UTC).

    strptime 보조(CR-08 PA-01): ampm은 '오전'·'PM' 같은 낱말 → AM/PM(낱말을 떼고 12시간제를 바꿈, 로캘과 무관),
    no_year는 연도 없는 시각(지어낸 연도를 붙이지 않고 파일 첫 시각부터의 경과로 둠),
    date_line_regex는 (?P<date>...) 날짜 머리 줄(뒤따르는 시각에 그 날짜를 붙여 읽음).
    """

    kind: str = Field(pattern="^(iso|strptime|relative|epoch)$")
    formats: list[str] = Field(default_factory=list)
    unit: str = Field(default="s", pattern="^(s|ms|us|auto)$")  # auto(epoch만): 정수 자릿수로 초·밀리초·마이크로초
    ampm: dict[str, str] = Field(default_factory=dict)
    no_year: bool = False
    date_line_regex: str | None = None

    @field_validator("ampm")
    @classmethod
    def _ampm_values(cls, value: dict[str, str]) -> dict[str, str]:
        if any(v.upper() not in ("AM", "PM") for v in value.values()):
            raise ValueError("ampm 값은 AM 또는 PM이어야 합니다")
        return {k: v.upper() for k, v in value.items()}


class EventRule(_Record):
    """사용자가 이름 붙인 이벤트 유형. 메시지가 regex에 맞으면 event_type = name."""

    name: str
    regex: str


class FieldAliases(_Record):
    """구조형 줄(json·logfmt·delimited)에서 각 칸으로 읽을 이름들. 대소문자는 가리지 않는다."""

    ts: list[str] = Field(default_factory=lambda: ["ts", "time", "timestamp", "@timestamp", "datetime",
                                                   "date and time", "date", "t", "when"])
    level: list[str] = Field(default_factory=lambda: ["level", "lvl", "severity", "loglevel", "sev", "priority"])
    component: list[str] = Field(default_factory=lambda: ["component", "comp", "module", "logger", "source",
                                                          "service", "src", "unit"])
    message: list[str] = Field(default_factory=lambda: ["msg", "message", "text", "what", "event"])
    code: list[str] = Field(default_factory=lambda: ["code", "error_code", "err_code"])


class ParserProfile(_Record):
    """line_format(CR-08 PA-01): regex(line_regex의 이름 그룹) · json(한 줄에 JSON 객체 하나) ·
    logfmt(key=value) · delimited(머리 줄이 있는 CSV·탭). 구조형에서 line_regex는 비워도 되고, 적으면 먼저 거르는 데 쓴다.
    level_map은 레벨 별칭표(대소문자 무시, 예: I → INFO), level_from=syslog_pri는 '<14>' 우선순위에서 레벨을 읽는다.
    regex의 rest 그룹은 메시지 뒤에 붙인다(따옴표 안 메시지 + 뒤따르는 key=value 같은 경우)."""

    name: str = Field(pattern=r"^[A-Za-z0-9_\-]+$")
    description: str = ""
    line_format: str = Field(default="regex", pattern="^(regex|json|logfmt|delimited)$")
    line_regex: str = ""
    timestamp: TimestampSpec | None = None
    continuation: bool = True
    sniff_threshold: float = Field(default=0.6, gt=0, le=1)
    code_regex: str | None = None
    event_rules: list[EventRule] = Field(default_factory=list)
    fields: FieldAliases = Field(default_factory=FieldAliases)
    delimiter: str = Field(default=",", min_length=1, max_length=1)
    level_map: dict[str, str] = Field(default_factory=dict)
    level_from: str | None = Field(default=None, pattern="^syslog_pri$")
    catch_all: bool = False  # 무엇이든 받는 포괄형(점수 절반으로 겨룸, CR-08 PA-04)

    @model_validator(mode="after")
    def _needs_message_group(self) -> "ParserProfile":
        if self.line_format == "regex" and "(?P<message>" not in self.line_regex:
            raise ValueError("line_regex에는 (?P<message>...) 그룹이 있어야 합니다")
        return self


# ── 가져오기 ──
class SessionMode(StrEnum):
    SAME = "SAME"  # 같은 시점에 여러 컴포넌트에서 수집
    SEPARATE = "SEPARATE"  # 서로 다른 시점의 재현·반복 수집
    UNKNOWN = "UNKNOWN"  # 사용자가 모름 → 파일마다 세션, relation_confirmed = 0
    EXISTING = "EXISTING"  # 이미 있는 세션에 추가


class FileMeta(_Record):
    log_role: str | None = None
    fw_version: str | None = None
    user_memo: str | None = None
    collected_at: str | None = None


class ImportRequest(_Record):
    paths: list[Path]
    purpose: OriginPurpose
    session_mode: SessionMode = SessionMode.UNKNOWN
    existing_session_id: int | None = None
    session_label: str | None = None
    profile: str | None = None  # None이면 자동 감지
    meta: FileMeta = Field(default_factory=FileMeta)


class ImportedFile(_Record):
    log_file_id: int
    session_id: int
    original_name: str
    sha256: str
    size_bytes: int
    encoding: str | None
    line_count: int | None
    event_count: int
    parser_profile: str | None
    timestamp_basis: TimestampBasis
    duplicate: bool = False
    warnings: list[str] = Field(default_factory=list)


class RejectedFile(_Record):
    path: str
    reason: str


class ImportResult(_Record):
    files: list[ImportedFile] = Field(default_factory=list)
    rejected: list[RejectedFile] = Field(default_factory=list)
    cancelled: bool = False

    @property
    def log_file_ids(self) -> list[int]:
        return [f.log_file_id for f in self.files]


# ── DB 행 ──
class SessionRecord(_Record):
    id: int
    label: str
    time_sync_status: str
    relation_confirmed: bool
    note: str | None = None
    created_at: str


class LogFileRecord(_Record):
    id: int
    session_id: int
    session_label: str | None = None
    original_name: str
    stored_path: str
    sha256: str
    size_bytes: int
    encoding: str | None = None
    line_count: int | None = None
    log_role: str | None = None
    collected_at: str | None = None
    timestamp_basis: TimestampBasis
    clock_offset_ms: int | None = None
    offset_basis: str | None = None
    fw_version: str | None = None
    user_memo: str | None = None
    parser_profile: str | None = None
    origin_purpose: OriginPurpose
    imported_at: str


class EventRecord(_Record):
    id: int
    log_file_id: int
    seq: int
    line_no: int
    line_end: int | None = None
    byte_offset: int | None = None
    ts_raw: str | None = None
    ts_value_us: int | None = None
    ts_kind: EventTimeKind
    level: str | None = None
    component: str | None = None
    event_type: str | None = None
    error_code: str | None = None
    message: str
    raw_text: str


# ── 사례·모드·패턴·지식 (Phase 3~) ──
class ModeRecord(_Record):
    id: int
    name: str
    description: str | None = None
    conditions: str | None = None
    source: str
    status: str
    approved_at: str | None = None


class CaseRecord(_Record):
    id: int
    case_type: str
    title: str
    status: str
    mode_id: int | None = None
    mode_confirmed: bool = False
    operating_conditions: str | None = None
    normal_rationale: str | None = None
    symptom: str | None = None
    abnormal_criteria: str | None = None
    cause_status: str | None = None
    device_config: str | None = None
    fw_version: str | None = None
    user_description: str | None = None
    current_revision: int = 0
    approved_at: str | None = None
    created_at: str
    updated_at: str

    @property
    def label(self) -> str:
        return f"C-{self.id:04d} {self.title}"


class NoteRecord(_Record):
    id: int
    case_id: int
    body: str
    created_at: str
    updated_at: str


class PatternCandidate(_Record):
    pattern_type: str
    definition: dict[str, Any]
    observed_count: int = 0
    note: str | None = None


class PatternRecord(_Record):
    id: int
    mode_id: int
    source_case_id: int | None = None
    pattern_type: str
    definition: dict[str, Any]
    conditions: str | None = None
    basis: str
    basis_note: str | None = None
    source: str
    status: str
    observed_count: int = 0
    approved_at: str | None = None

    def describe(self) -> str:
        d = self.definition
        role = f"[{d['log_role']}] " if d.get("log_role") else ""
        events = d.get("events") or []
        if self.pattern_type == "ORDER_CONSTRAINT":
            return f"{role}{d.get('before')} → {d.get('after')}"
        if self.pattern_type == "REPEATED":
            return f"{role}{', '.join(events)} ({d.get('min')}~{d.get('max')}회)"
        return role + ", ".join(events)


class EvidenceRef(_Record):
    """주장 근거 하나. claim_evidence 열과 같은 이름을 쓴다."""

    evidence_type: str
    stance: str = "SUPPORTS"
    event_id: int | None = None
    log_file_id: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    guide_attachment_id: int | None = None
    span_start: int | None = None
    span_end: int | None = None
    answer_id: int | None = None
    ref_case_id: int | None = None
    note_id: int | None = None
    analysis_evidence_id: int | None = None


class ClaimRecord(_Record):
    id: int
    case_id: int | None = None
    guide_item_id: int | None = None
    pattern_id: int | None = None
    analysis_run_id: int | None = None
    claim_text: str
    claim_kind: str
    source: str
    status: str
    evidence: list[EvidenceRef] = Field(default_factory=list)
