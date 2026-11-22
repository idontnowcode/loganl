"""정상 로그 학습 흐름 (계획서 §3.1 처리 절차, §6.1 처리 알고리즘).

결정론으로 특징·패턴 후보를 만들고, AI는 (모델이 설정돼 있으면) 모드 추정과 초안 설명만 한다.
AI가 없거나 실패해도 패턴 후보 만들기·승인은 그대로 된다.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field

from app.ai.context_builder import EvidencePackage
from app.ai.draft_generator import NormalDraft, generate_normal_draft
from app.ai.mode_classifier import ModeGuess, classify_mode
from app.ai.ollama_client import OllamaClient
from app.config import AppSettings
from app.core.cancel import NEVER_CANCEL, CancelToken
from app.core.feature_extractor import FileFeatures, extract, first_occurrence_order
from app.core.pattern_analyzer import propose_patterns
from app.core.reparse import current_parse
from app.models.enums import AuditAction, Actor
from app.models.records import EvidenceRef, PatternRecord
from app.storage.database import Database, transaction
from app.storage.repositories import (
    AuditRepository,
    CaseRepository,
    ClaimRepository,
    EventRepository,
    LogFileRepository,
    ModeRepository,
    PatternRepository,
    QuestionRepository,
)
from app.learning.case_manager import CaseError

ClientFactory = Callable[[], OllamaClient]
MAX_TYPES_IN_PACKAGE = 25


def default_client_factory(settings: AppSettings) -> ClientFactory:
    def make() -> OllamaClient:
        return OllamaClient(settings.ollama.base_url, connect_timeout_s=settings.ollama.connect_timeout_s,
                            request_timeout_s=settings.ollama.request_timeout_s)
    return make


@dataclass
class DraftOutcome:
    status: str  # OK / NO_MODEL / AI_UNAVAILABLE / VALIDATION_FAILED
    draft: NormalDraft | None = None
    errors: list[str] = field(default_factory=list)
    claim_ids: list[int] = field(default_factory=list)


# 로그 파일별 특징 기억(CR-13 PF-01). 저장된 이벤트는 바뀌지 않으므로(규칙 5) 앱이 켜져 있는 동안 다시 계산하지 않는다.
# 정상 로그 수십 개(각 1~2만 줄)를 분석마다 모두 읽던 시간을 줄인다. 키에 DB 파일을 넣어 데이터 폴더가 달라도 섞이지 않게.
_FEATURE_CACHE: dict[tuple[str, int], FileFeatures] = {}
_FEATURE_CACHE_LIMIT = 400


def case_file_features(database: Database, case_id: int) -> list[FileFeatures]:
    with closing(database.connect()) as conn:
        ids = CaseRepository(conn).log_file_ids(case_id)
        records = LogFileRepository(conn).get_many(ids)
        db = str(database.db_file)
        missing = [r.id for r in records if (db, r.id) not in _FEATURE_CACHE]
        events = EventRepository(conn).for_files(missing)
    todo = [r for r in records if r.id in events]
    todo, events = current_parse(database, todo, events)  # 예전 버전이 읽은 로그는 지금 파서로(DT-16)
    for record in todo:
        if len(_FEATURE_CACHE) >= _FEATURE_CACHE_LIMIT:
            _FEATURE_CACHE.pop(next(iter(_FEATURE_CACHE)))  # 가장 오래 기억한 것부터
        _FEATURE_CACHE[(db, record.id)] = extract(record, events[record.id])
    return [_FEATURE_CACHE[(db, r.id)] for r in records]


def build_package(features: list[FileFeatures], package: EvidencePackage | None = None) -> tuple[EvidencePackage, str]:
    """특징을 근거 묶음과 요약 문장으로. 이벤트 근거에는 원본 행 정보가 붙는다."""
    package = package or EvidencePackage()
    summary_lines: list[str] = []
    for f in features:
        order = first_occurrence_order(f)
        summary_lines.append(
            f"- 파일 {f.log_file_id} (역할: {f.role_key}, 세션 {f.session_id}, 타임스탬프 기준 {f.timestamp_basis}): "
            f"이벤트 {len(f.sequence)}개, 유형 {len(f.type_counts)}개, 오류 코드 {dict(f.error_codes) or '없음'}, "
            f"레벨 {dict(f.level_counts) or '없음'}"
        )
        for key in order[:MAX_TYPES_IN_PACKAGE]:
            event = f.first_event[key]
            package.add(
                "EVENT",
                f"파일 {f.log_file_id} {event.line_no}행 (유형 {key}, {f.type_counts[key]}회): {event.raw_text[:300]}",
                event_id=event.id, log_file_id=f.log_file_id, line_no=event.line_no, session_id=f.session_id,
                event_type=key, error_code=event.error_code, timestamp_basis=f.timestamp_basis,
            )
    return package, "\n".join(summary_lines)


def evidence_refs(package: EvidencePackage, keys: list[str]) -> list[EvidenceRef]:
    refs: list[EvidenceRef] = []
    for key in keys:
        if key not in package:
            continue
        ref = package.get(key).ref
        if ref.get("event_id"):
            refs.append(EvidenceRef(evidence_type="EVENT", event_id=ref["event_id"]))
        elif ref.get("log_file_id") and ref.get("line_no"):
            refs.append(EvidenceRef(evidence_type="LOG_LINES", log_file_id=ref["log_file_id"],
                                    line_start=ref["line_no"], line_end=ref["line_no"]))
        elif ref.get("case_id"):
            refs.append(EvidenceRef(evidence_type="CASE", ref_case_id=ref["case_id"]))
    return refs


class NormalLearner:
    def __init__(self, database: Database, settings: AppSettings, client_factory: ClientFactory | None = None) -> None:
        self.database = database
        self.settings = settings
        self.client_factory = client_factory or default_client_factory(settings)

    def estimate_mode(self, case_id: int, cancel: CancelToken = NEVER_CANCEL) -> ModeGuess:
        features = case_file_features(self.database, case_id)
        if not features:
            return ModeGuess("UNKNOWN", reason="로그가 없습니다.")
        package, summary = build_package(features)
        with closing(self.database.connect()) as conn:
            modes = ModeRepository(conn).list(active_only=True)
        with self.client_factory() as client:
            return classify_mode(client, self.settings.ollama.model, modes, package, summary, cancel)

    def propose(self, case_id: int) -> list[PatternRecord]:
        """같은 모드의 승인된 정상 사례 로그 + 이 사례 로그로 패턴 후보를 다시 만든다(이전 '제안' 후보는 지움)."""
        with closing(self.database.connect()) as conn:
            case = CaseRepository(conn).get(case_id)
            if case is None:
                raise CaseError(f"사례 {case_id}이(가) 없습니다.")
            if case.case_type != "NORMAL":
                raise CaseError("정상 사례에서만 패턴 후보를 만듭니다.")
            if case.mode_id is None:
                raise CaseError("운전 모드를 먼저 고르세요. 정상 패턴은 운전 모드마다 따로 만듭니다. "
                                "목록이 비어 있으면 '관리 → 운전 모드 관리'에서 먼저 등록하세요.")
            if case.status in ("APPROVED", "ARCHIVED"):
                raise CaseError("승인·보관된 사례는 '수정 시작' 후 다시 만들 수 있습니다.")
            others = [c.id for c in CaseRepository(conn).approved(case_type="NORMAL", mode_id=case.mode_id)
                      if c.id != case_id]
        features = case_file_features(self.database, case_id)
        if not features:
            raise CaseError("로그를 먼저 올리세요.")
        seen = {f.log_file_id for f in features}
        for other in others:
            features.extend(f for f in case_file_features(self.database, other) if f.log_file_id not in seen)
            seen.update(f.log_file_id for f in features)
        candidates = propose_patterns(features)
        with closing(self.database.connect()) as conn, transaction(conn):
            patterns = PatternRepository(conn)
            patterns.delete_proposed_for_case(case_id)
            for candidate in candidates:
                patterns.add(case.mode_id, candidate, source_case_id=case_id)
            AuditRepository(conn).add("case", case_id, AuditAction.UPDATE, Actor.SYSTEM,
                                      after={"pattern_candidates": len(candidates), "compared_cases": others})
            return [p for p in patterns.for_case(case_id)]

    def generate_draft(self, case_id: int, cancel: CancelToken = NEVER_CANCEL) -> DraftOutcome:
        with closing(self.database.connect()) as conn:
            case = CaseRepository(conn).get(case_id)
            mode = ModeRepository(conn).get(case.mode_id) if case and case.mode_id else None
            patterns = PatternRepository(conn).for_case(case_id)
        if case is None:
            raise CaseError(f"사례 {case_id}이(가) 없습니다.")
        features = case_file_features(self.database, case_id)
        package, summary = build_package(features)
        for p in patterns[:40]:
            package.add("PATTERN", f"{p.pattern_type} 후보: {p.describe()} ({p.basis_note or ''})", pattern_id=p.id)
        context = (
            f"제목: {case.title}\n운전 모드: {mode.name if mode else '미정'}\n운전 조건: {case.operating_conditions or '없음'}\n"
            f"정상 판정 근거(사용자): {case.normal_rationale or '없음'}\n로그 요약:\n{summary}"
        )
        with self.client_factory() as client:
            result = generate_normal_draft(client, self.settings.ollama.model, package, context, cancel)
        if result.value is None:
            return DraftOutcome(result.status, errors=result.errors)
        draft = result.value
        claim_ids: list[int] = []
        with closing(self.database.connect()) as conn, transaction(conn):
            claims = ClaimRepository(conn)
            claims.delete_proposed_for_case(case_id, "AI")
            claim_ids.append(claims.add(claim_text=f"[AI 초안 요약] {draft.summary}", claim_kind="HYPOTHESIS",
                                        source="AI", case_id=case_id))
            for obs in draft.observations:
                claim_ids.append(claims.add(claim_text=obs.text, claim_kind=obs.kind, source="AI", case_id=case_id,
                                            evidence=evidence_refs(package, obs.evidence)))
            questions = QuestionRepository(conn)
            for item in draft.missing_info:
                claim_ids.append(claims.add(claim_text=f"[확인 필요] {item.text}", claim_kind="UNKNOWN", source="AI",
                                            case_id=case_id))
                # AI가 짚은 빠진 정보는 비필수 질문으로 남긴다(필수 여부 판정은 규칙이 한다, Phase 5)
                questions.upsert(case_id, trigger_type=item.trigger, question_text=item.text,
                                 reason="AI 학습 초안이 짚은 빠진 정보", priority=10, is_required=False,
                                 dedup_key=f"ai:{hashlib.sha1(item.text.encode('utf-8')).hexdigest()[:12]}",
                                 generated_by="AI")
            AuditRepository(conn).add("case", case_id, AuditAction.UPDATE, Actor.AI,
                                      after={"ai_draft_claims": len(claim_ids)})
        return DraftOutcome("OK", draft, [], claim_ids)
