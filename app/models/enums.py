"""DB CHECK 제약과 같은 값을 갖는 열거형 (docs/DATABASE.md).

값을 바꾸려면 마이그레이션을 새로 만들고 이 파일을 함께 고친다.
`tests/unit/test_schema.py`가 두 곳이 같은지 검사한다.
"""

from __future__ import annotations

from enum import StrEnum


# ── 수집 세션 · 원본 파일 · 이벤트 ──
class TimeSyncStatus(StrEnum):
    SYNCED = "SYNCED"
    OFFSET_KNOWN = "OFFSET_KNOWN"
    UNSYNCED = "UNSYNCED"
    UNKNOWN = "UNKNOWN"


class TimestampBasis(StrEnum):
    ABSOLUTE = "ABSOLUTE"
    ABSOLUTE_NO_TZ = "ABSOLUTE_NO_TZ"
    RELATIVE = "RELATIVE"
    NONE = "NONE"
    UNKNOWN = "UNKNOWN"


class EventTimeKind(StrEnum):
    ABSOLUTE = "ABSOLUTE"
    ABSOLUTE_NO_TZ = "ABSOLUTE_NO_TZ"
    RELATIVE = "RELATIVE"
    NONE = "NONE"


class OriginPurpose(StrEnum):
    NORMAL_CASE = "NORMAL_CASE"
    ABNORMAL_CASE = "ABNORMAL_CASE"
    ANALYSIS = "ANALYSIS"


# ── 운전 모드 · 사례 ──
class ModeSource(StrEnum):
    USER = "USER"
    AI_PROPOSED = "AI_PROPOSED"


class ModeStatus(StrEnum):
    PROPOSED = "PROPOSED"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class CaseType(StrEnum):
    NORMAL = "NORMAL"
    ABNORMAL = "ABNORMAL"


class CaseStatus(StrEnum):
    DRAFT = "DRAFT"
    NEEDS_INFORMATION = "NEEDS_INFORMATION"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    ARCHIVED = "ARCHIVED"


class CauseStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    SUSPECTED = "SUSPECTED"
    UNKNOWN = "UNKNOWN"


class CaseEventRole(StrEnum):
    SYMPTOM = "SYMPTOM"
    ERROR = "ERROR"
    CONTEXT = "CONTEXT"
    PATTERN_MEMBER = "PATTERN_MEMBER"
    REFERENCE = "REFERENCE"


class ProducedBy(StrEnum):
    """사례-이벤트 연결과 패턴을 누가 만들었나."""

    USER = "USER"
    RULE = "RULE"
    AI = "AI"


# ── 진단 가이드 · 대응 매뉴얼 · 패턴 ──
class AttachmentKind(StrEnum):
    DIAGNOSTIC_GUIDE = "DIAGNOSTIC_GUIDE"  # 필수 (§3.2)
    RESPONSE_MANUAL = "RESPONSE_MANUAL"  # 선택 (변경 요청 CR-01)


class GuideFormat(StrEnum):
    TXT = "TXT"
    MD = "MD"


class GuideSection(StrEnum):
    # 진단 가이드 권장 8항목 (§3.2)
    SYMPTOM = "SYMPTOM"
    CONDITION = "CONDITION"
    CRITERIA = "CRITERIA"
    CAUSE = "CAUSE"
    CHECK_METHOD = "CHECK_METHOD"
    RESOLUTION = "RESOLUTION"
    VERIFICATION = "VERIFICATION"
    NOTE = "NOTE"
    # 대응 매뉴얼 항목 (CR-01)
    RESPONSE_STEP = "RESPONSE_STEP"
    PRECAUTION = "PRECAUTION"
    ESCALATION = "ESCALATION"


class GuideSourceKind(StrEnum):
    GUIDE_TEXT = "GUIDE_TEXT"
    USER = "USER"
    AI_PROPOSED = "AI_PROPOSED"


class ItemStatus(StrEnum):
    """가이드 항목·패턴의 확정 상태."""

    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    RETIRED = "RETIRED"


class PatternType(StrEnum):
    REQUIRED = "REQUIRED"
    OPTIONAL = "OPTIONAL"
    ORDER_CONSTRAINT = "ORDER_CONSTRAINT"
    INDEPENDENT = "INDEPENDENT"
    REPEATED = "REPEATED"
    UNCLASSIFIED = "UNCLASSIFIED"


class PatternBasis(StrEnum):
    OBSERVED = "OBSERVED"
    SPECIFICATION = "SPECIFICATION"
    USER_DEFINED = "USER_DEFINED"


# ── 확인 질문 ──
class QuestionTrigger(StrEnum):
    MODE_UNKNOWN = "MODE_UNKNOWN"
    NORMAL_RATIONALE_MISSING = "NORMAL_RATIONALE_MISSING"
    SESSION_RELATION_UNKNOWN = "SESSION_RELATION_UNKNOWN"
    TIMESTAMP_BASIS_UNKNOWN = "TIMESTAMP_BASIS_UNKNOWN"
    GUIDE_INFO_MISSING = "GUIDE_INFO_MISSING"
    CAUSE_STATUS_UNKNOWN = "CAUSE_STATUS_UNKNOWN"
    CRITERIA_DETAIL_UNKNOWN = "CRITERIA_DETAIL_UNKNOWN"
    USER_LOG_CONFLICT = "USER_LOG_CONFLICT"
    CONFLICTING_KNOWLEDGE = "CONFLICTING_KNOWLEDGE"


class QuestionStatus(StrEnum):
    OPEN = "OPEN"
    ANSWERED = "ANSWERED"
    SKIPPED = "SKIPPED"
    UNKNOWN = "UNKNOWN"
    OBSOLETE = "OBSOLETE"


class QuestionGenerator(StrEnum):
    RULE = "RULE"
    AI = "AI"


class AnswerKind(StrEnum):
    TEXT = "TEXT"
    CHOICE = "CHOICE"
    UNKNOWN = "UNKNOWN"
    SKIPPED = "SKIPPED"


# ── 지식 주장 · 근거 ──
class ClaimKind(StrEnum):
    FACT = "FACT"
    HYPOTHESIS = "HYPOTHESIS"
    UNKNOWN = "UNKNOWN"


class ClaimSource(StrEnum):
    LOG_OBSERVATION = "LOG_OBSERVATION"
    USER = "USER"
    GUIDE = "GUIDE"
    AI = "AI"


class ClaimStatus(StrEnum):
    PROPOSED = "PROPOSED"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class EvidenceType(StrEnum):
    EVENT = "EVENT"
    LOG_LINES = "LOG_LINES"
    GUIDE_SPAN = "GUIDE_SPAN"
    USER_ANSWER = "USER_ANSWER"
    CASE = "CASE"
    NOTE = "NOTE"
    ANALYSIS_EVIDENCE = "ANALYSIS_EVIDENCE"


class ClaimStance(StrEnum):
    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"


# ── 분석 실행 ──
class AnalysisStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class NormalMatch(StrEnum):
    MATCH = "MATCH"
    PARTIAL = "PARTIAL"
    NO_MATCH = "NO_MATCH"
    NOT_EVALUATED = "NOT_EVALUATED"


class AnomalyLevel(StrEnum):
    NONE_FOUND = "NONE_FOUND"
    SUSPECTED = "SUSPECTED"
    RULE_VIOLATION = "RULE_VIOLATION"
    NOT_EVALUATED = "NOT_EVALUATED"


class SimilarAbnormal(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    NOT_EVALUATED = "NOT_EVALUATED"


class EvidenceSufficiency(StrEnum):
    SUFFICIENT = "SUFFICIENT"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"
    NOT_EVALUATED = "NOT_EVALUATED"


class ResponseGuidance(StrEnum):
    """사용자에게 보여 줄 권장 대응의 근거 (CR-01)."""

    MANUAL = "MANUAL"
    GUIDE_ONLY = "GUIDE_ONLY"
    NONE = "NONE"
    NOT_EVALUATED = "NOT_EVALUATED"


class AiStatus(StrEnum):
    NOT_RUN = "NOT_RUN"
    OK = "OK"
    UNAVAILABLE = "UNAVAILABLE"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    CANCELLED = "CANCELLED"


class AnalysisLayer(StrEnum):
    DETERMINISTIC = "DETERMINISTIC"
    RETRIEVAL = "RETRIEVAL"
    AI = "AI"


class FindingType(StrEnum):
    EXPLICIT_ERROR = "EXPLICIT_ERROR"
    MISSING_EVENT = "MISSING_EVENT"
    EXTRA_EVENT = "EXTRA_EVENT"
    ORDER_VIOLATION = "ORDER_VIOLATION"
    LATENCY = "LATENCY"
    REPETITION = "REPETITION"
    SIMILAR_NORMAL_CASE = "SIMILAR_NORMAL_CASE"
    SIMILAR_ABNORMAL_CASE = "SIMILAR_ABNORMAL_CASE"
    GUIDE_REFERENCE = "GUIDE_REFERENCE"
    RESPONSE_REFERENCE = "RESPONSE_REFERENCE"
    TEXT_MATCH = "TEXT_MATCH"
    AI_OBSERVATION = "AI_OBSERVATION"
    AI_HYPOTHESIS = "AI_HYPOTHESIS"
    AI_COUNTER_EVIDENCE = "AI_COUNTER_EVIDENCE"
    AI_VERIFICATION_STEP = "AI_VERIFICATION_STEP"
    AI_UNKNOWN = "AI_UNKNOWN"


class FindingStance(StrEnum):
    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    NEUTRAL = "NEUTRAL"


# ── 감사 이력 ──
class AuditAction(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    STATUS_CHANGE = "STATUS_CHANGE"
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    ARCHIVE = "ARCHIVE"
    RESTORE = "RESTORE"
    DELETE = "DELETE"
    IMPORT = "IMPORT"


class Actor(StrEnum):
    USER = "USER"
    AI = "AI"
    SYSTEM = "SYSTEM"
