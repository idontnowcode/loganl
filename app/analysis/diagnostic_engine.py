"""신규 로그 분석 실행 (계획서 §3.3 처리 절차, §6.3 세 계층).

계층 1(결정론) → 계층 2(승인된 사례 검색) → 계층 3(Ollama, Phase 7)을 차례로 돌리고, 계층마다 근거를
analysis_evidence에 근거 키(EV-0001…)와 함께 저장한다. AI가 없거나 실패해도 계층 1·2 결과는 그대로 남는다(§12.5).
결과 분류는 독립된 다섯 축이다(§3.3, CR-01): 정상 일치·이상 수준·유사 비정상·근거 충분성·권장 대응.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any

from app import __version__
from app.ai.ollama_client import OllamaClient
from app.analysis.anomaly_detector import Finding, detect
from app.analysis.case_retriever import (
    REFERENCE_POOL,
    SimilarCase,
    approved_normal_types,
    comparison_pool,
    find_similar,
    normal_types_for_cases,
    reference_normals,
)
from app.analysis.reference_checks import ReferenceFile, compare_with_normals
from app.analysis.reference_judgment import JudgedMatch, apply_reference
from app.analysis.reference_matcher import PassageHit, ReferenceMatch, match_passages, match_references
from app.analysis.result_builder import Applicable, ResponseGuidance, build_response_guidance
from app.config import AppSettings
from app.core.cancel import NEVER_CANCEL, Cancelled, CancelToken
from app.core.feature_extractor import FileFeatures, extract
from app.core.reparse import current_parse
from app.core.sequence_analyzer import build_timeline
from app.learning.case_memory import fingerprint_of
from app.learning.criteria import ABNORMAL_SIGN, BENIGN, CriteriaService, Criterion, apply_criteria
from app.learning.normal_learner import default_client_factory
from app.models import utc_now_iso
from app.storage.database import Database, transaction
from app.storage.paths import AppPaths
from app.storage.repositories import (
    AnalysisRepository,
    CaseRepository,
    CriteriaRepository,
    EventRepository,
    LogFileRepository,
    PatternRepository,
    SessionRepository,
)

logger = logging.getLogger("app.analysis")
ProgressFn = Callable[[int, int, str], None]
STEPS = 5
SHORT_LOG_EVENTS = 20  # 이보다 이벤트가 적은 로그는 '비교 근거가 적음'을 메모에(DT-20)
REFERENCE_EVENT_BUDGET = 200_000  # 정상 로그 대비 비교에 읽는 기준 이벤트 합계 상한(PF-01)
REFERENCE_MIN_KEEP = 10  # 상한을 넘어도 기준은 이만큼은 쓴다('통째로 없음' 등 기준 10개 규칙)
LOW_PRIORITY = 3
DETECTED_LEVELS = frozenset({"SUSPECTED", "RULE_VIOLATION"})
APPLICABLE_SIMILARITY = 0.5  # 근거 지문으로 대응을 붙이려면 이만큼 비슷해야 한다


def anomaly_level_of(findings: list[Finding], has_normal_knowledge: bool) -> str:
    """결정론 근거의 무게로 나눈 규칙 판정: 승인된 규칙을 어긴 것(VIOLATION)과 관찰된 것과 다른 것(SUSPECT, CR-08 비교 포함)."""
    deterministic = [f for f in findings if f.layer == "DETERMINISTIC" and f.stance == "SUPPORTS"]
    if any(f.severity == "VIOLATION" for f in deterministic):
        return "RULE_VIOLATION"
    if any(f.severity == "SUSPECT" for f in deterministic):
        return "SUSPECTED"
    return "NONE_FOUND" if has_normal_knowledge else "NOT_EVALUATED"


@dataclass
class NormalComparison:
    findings: list[Finding]
    normal_pool: list
    reference: list
    normal_types: Any
    pool: list


def memory_findings(memory) -> list[Finding]:
    """사례 기억의 이웃 확인 로그를 근거로(CR-14 CM-02). 같았던 근거 특징을 함께 적는다."""
    out = []
    for sim, item, shared in memory.neighbors:
        label = "비정상" if item.abnormal else "정상"
        same = ", ".join(shared[:3]) + (f" 외 {len(shared) - 3}개" if len(shared) > 3 else "")
        out.append(Finding(
            "RETRIEVAL", "SIMILAR_ABNORMAL_CASE" if item.abnormal else "SIMILAR_NORMAL_CASE",
            f"근거 지문이 비슷한 확인 로그 {item.case.label}({label}, 유사도 {sim:.2f})" + (f": 같은 근거 {same}" if same else ""),
            stance="SUPPORTS" if item.abnormal else "CONTRADICTS", case_id=item.case.id, similarity=round(sim, 4),
            detail={"role": "CASE_MEMORY", "label": item.case.case_type, "shared": shared[:10]}))
    return out


def _ai_priority(finding: Finding) -> int:
    """AI 근거 목록에 넣는 순서. -1 근거 지문이 비슷한 확인 로그(CR-14) → 0 이상 쪽 결정론 근거 → 1 승인된 대응·가이드
    → 2 사례·참고 자료 → 3 참고용 결정론(본 적 없는 이벤트 등). 근거 키(EV-…)는 저장 순서 그대로이고 프롬프트 안 순서만 바뀐다."""
    if finding.detail.get("role") == "CASE_MEMORY":
        return -1
    if finding.layer == "DETERMINISTIC" and finding.stance == "SUPPORTS":
        return 0
    if finding.finding_type in ("RESPONSE_REFERENCE", "GUIDE_REFERENCE"):
        return 1
    if finding.layer == "RETRIEVAL":
        return 2
    return LOW_PRIORITY


@dataclass
class AnalysisRequest:
    log_file_ids: list[int]
    mode_id: int | None = None
    mode_confirmed: bool = False
    operating_conditions: str | None = None
    user_description: str | None = None
    use_ai: bool = True


@dataclass
class AnalysisResult:
    run_id: int
    axes: dict[str, str]
    findings: list[Finding]
    similar_normal: list[SimilarCase]
    similar_abnormal: list[SimilarCase]
    guidance: ResponseGuidance
    keys: dict[int, str] = field(default_factory=dict)  # findings 인덱스 → 근거 키
    notes: list[str] = field(default_factory=list)
    merged_files: set[int] = field(default_factory=set)  # 시각으로 한 시간축에 합친 파일
    ai: Any = None  # DiagnosisOutcome
    memory: Any = None  # app.learning.case_memory.MemoryDecision (CR-14, 단일 로그 분석일 때)
    ai_judgment: Any = None  # app.learning.case_memory.AiJudgment (CR-14 CM-07, AI를 켰을 때)
    short_log: bool = False  # 이벤트가 적어 비교 근거가 적음(DT-20) — 최종 판정 '판단 보류' 사유
    criteria: Any = None  # app.learning.criteria.CriteriaDecision (CR-15, 엔지니어가 답한 판단 기준)
    questions: list = field(default_factory=list)  # app.learning.criteria.Question — 판단을 돕는 질문(CR-15 JQ-01·02)
    reference_judgment: Any = None  # app.analysis.reference_judgment.ReferenceDecision (CR-16, 참고 자료의 판정 설명)

    @property
    def verdict(self):
        """최종 판정 하나: 정상 / 비정상 / 판단 보류(app.analysis.verdict)."""
        from app.analysis.verdict import verdict_of

        return verdict_of(self)
    references: list[ReferenceMatch] = field(default_factory=list)  # 참고 자료 — 로그에 나온 코드 (CR-04)
    passages: list[PassageHit] = field(default_factory=list)  # 참고 자료 — 관련 문서 내용 (CR-07)


@dataclass
class DiagnosisOutcome:
    status: str  # OK / NOT_RUN / UNAVAILABLE / VALIDATION_FAILED / CANCELLED
    answer: Any = None  # app.ai.diagnosis.DiagnosisAnswer
    errors: list[str] = field(default_factory=list)
    input_path: str | None = None
    kinds: dict[str, str] = field(default_factory=dict)  # 근거 키 → 종류(FINDING/CASE/GUIDE/RESPONSE)


class DiagnosticEngine:
    def __init__(self, database: Database, paths: AppPaths, settings: AppSettings,
                 client_factory: Callable[[], OllamaClient] | None = None, use_case_memory: bool = True) -> None:
        self.database = database
        self.paths = paths
        self.settings = settings
        self.client_factory = client_factory or default_client_factory(settings)
        self.use_case_memory = use_case_memory  # 끄면 CR-14 이전과 같다(비교 측정용)

    def prepare_case_memory(self, budget_s: float | None = None) -> tuple[int, int]:
        """확인된 사례 로그의 지문을 미리 만든다(학습 직후·한꺼번에 저장한 뒤). (지문 있는 사례 수, 전체 단일 로그 사례 수)."""
        import math
        import time

        from app.learning.case_memory import FingerprintStore, load_cases

        with closing(self.database.connect()) as conn:
            repo = CaseRepository(conn)
            approved = repo.approved()
            links = {case.id: repo.log_file_ids(case.id) for case in approved}
            normals_count = len([c for c in approved if c.case_type == "NORMAL"])
        cases = load_cases(approved, lambda case_id: links[case_id], FingerprintStore(self.paths.analysis), normals_count,
                           lambda case, ids: self._case_fingerprint(case, ids, normals_count),
                           math.inf if budget_s is None else budget_s, time.perf_counter)
        return len(cases), sum(1 for ids in links.values() if ids)

    def run(self, request: AnalysisRequest, cancel: CancelToken = NEVER_CANCEL,
            progress: ProgressFn | None = None) -> AnalysisResult:
        if not request.log_file_ids:
            raise ValueError("분석할 로그가 없습니다.")
        report = progress or (lambda *_: None)
        with closing(self.database.connect()) as conn, transaction(conn):
            run_id = AnalysisRepository(conn).create(
                app_version=__version__, log_file_ids=request.log_file_ids, mode_id=request.mode_id,
                mode_confirmed=bool(request.mode_confirmed and request.mode_id is not None),
                operating_conditions=request.operating_conditions, user_description=request.user_description)
        try:
            result = self._run(run_id, request, cancel, report)
        except Cancelled:
            self._finish(run_id, status="CANCELLED", error_message="사용자가 취소했습니다.")
            raise
        except Exception as exc:
            logger.exception("분석 실패")
            self._finish(run_id, status="FAILED", error_message=str(exc)[:500])
            raise
        return result

    def _run(self, run_id: int, request: AnalysisRequest, cancel: CancelToken, report: ProgressFn) -> AnalysisResult:
        notes: list[str] = []
        report(0, STEPS, "로그 특징 추출")
        with closing(self.database.connect()) as conn:
            records = LogFileRepository(conn).get_many(request.log_file_ids)
            events = EventRepository(conn).for_files(request.log_file_ids)
            sessions = {r.session_id: SessionRepository(conn).get(r.session_id) for r in records}
            patterns = PatternRepository(conn).approved_for_mode(request.mode_id) if request.mode_id else []
            normals_count = len(CaseRepository(conn).approved(case_type="NORMAL"))
            any_normal = bool(normals_count)
            any_abnormal = bool(CaseRepository(conn).approved(case_type="ABNORMAL"))
        records, events = current_parse(self.database, records, events)  # 예전 버전이 읽은 로그는 지금 파서로(DT-16)
        features = [extract(r, events[r.id]) for r in records]
        short = [r.original_name for r, f in zip(records, features) if len(f.sequence) < SHORT_LOG_EVENTS]
        if short:  # CR-13 DT-20
            notes.append(f"로그가 짧아(이벤트 {SHORT_LOG_EVENTS}개 미만) 비교 근거가 적습니다: {', '.join(short)}. "
                         "'찾은 이상 없음'이어도 정상이라는 뜻이 아닙니다.")
        merged_files: set[int] = set()
        for session_id in sorted({r.session_id for r in records}):  # 세션마다 따로 시간축(§4.1)
            group = [(r, events[r.id]) for r in records if r.session_id == session_id]
            synced = sessions[session_id].time_sync_status in ("SYNCED", "OFFSET_KNOWN")
            timeline = build_timeline(group, session_synced=synced)
            notes.extend(timeline.notes)
            if timeline.merged_by_time:
                merged_files.update(r.id for r, _ in group)
        if len(sessions) > 1:
            notes.append(f"수집 세션 {len(sessions)}개는 서로 시간축을 합치지 않고 따로 분석했습니다.")
        cancel.raise_if_cancelled()

        report(1, STEPS, "비슷한 정상 로그 찾기 · 결정론 분석(명시적 오류·누락·순서·반복·지연)")
        compared = self._compare_normals(features, events, request.mode_id, patterns)
        findings, normal_types, reference = compared.findings, compared.normal_types, compared.reference
        similar_normal = compared.normal_pool[:5]
        if request.mode_id is None:
            if reference:
                notes.append("운전 모드를 고르지 않아 가장 비슷한 정상 로그 " + ", ".join(
                    f"{s.case.label}(점수 {s.similarity:.2f})" for s in reference) + "와 비교했습니다. 점수는 확률이 아닙니다.")
            elif any_normal:
                notes.append("운전 모드를 고르지 않았고, 점수 0.4 이상인 비슷한 정상 로그가 없어 정상 패턴과 비교하지 못했습니다.")
        if compared.pool and not reference:
            notes.append(f"점수 0.4 이상인 정상 로그는 없지만, 이 로그의 이벤트 종류를 대부분 담은 정상 로그 {len(compared.pool)}개와 "
                         "비교했습니다(일부가 빠진 로그일 수 있음).")
        cancel.raise_if_cancelled()

        report(2, STEPS, "승인된 사례 검색")
        similar_abnormal = find_similar(self.database, features, "ABNORMAL", None)  # 비정상은 모드가 달라도 찾는다
        # 같은 제품 로그는 이벤트 종류가 거의 같아 점수만으로는 정상 로그에도 비정상 사례가 '관련 있음'이 된다. 오류 코드가 같거나
        # 가장 비슷한 정상 사례보다 더 비슷할 때만 이상 쪽 근거로 쓴다(2026-11-22 AI 측정: 정상 로그에 붙어 AI가 끌려감).
        best_normal = max((s.similarity for s in similar_normal), default=0.0)
        closer = {s.case.id for s in similar_abnormal if s.relevant and (s.shared_codes or s.similarity > best_normal)}
        for s in similar_normal:
            findings.append(Finding("RETRIEVAL", "SIMILAR_NORMAL_CASE", f"비슷한 정상 사례 {s.case.label}: {s.explain()}",
                                    stance="CONTRADICTS" if s.relevant else "NEUTRAL", case_id=s.case.id,
                                    similarity=s.similarity, detail={"shared_types": s.shared_types[:10]}))
        for s in similar_abnormal:
            findings.append(Finding("RETRIEVAL", "SIMILAR_ABNORMAL_CASE",
                                    f"비슷한 비정상 사례 {s.case.label}: {s.explain()}"
                                    + ("" if s.case.id in closer or not s.relevant else " (가장 비슷한 정상 사례보다 덜 비슷함)"),
                                    stance="SUPPORTS" if s.case.id in closer else "NEUTRAL", case_id=s.case.id,
                                    similarity=s.similarity,
                                    detail={"shared_codes": s.shared_codes, "text_hit": s.text_hit,
                                            "cause_status": s.case.cause_status}))
        cancel.raise_if_cancelled()

        report(3, STEPS, "사례 판단 · 권장 대응 조립")
        # 규칙 판정 → 사례 기억 보정(CR-14) → 최종 판정이 비정상 쪽이고 이 로그에 적용할 근거가 확인된 사례일 때만 승인된 조치(리뷰 2026-11-22)
        rule_level = anomaly_level_of(findings, self._normal_knowledge(request, patterns, normal_types, any_normal))
        memory = self._case_memory(features, findings, rule_level, normals_count) if self.use_case_memory else None
        level = memory.level if memory is not None and memory.total else rule_level
        if memory is not None and memory.total:
            findings.extend(memory_findings(memory))
            notes.append(memory.note())
        fp = memory.fp if memory is not None and memory.fp is not None else fingerprint_of(findings, rule_level, normals_count)
        unit_events = [e for f in features for e in events[f.log_file_id]]
        # CR-16 RJ-04: 승인된 참고 자료에 판정(에러·정상 등)이 적힌 항목이 로그에 나오면 판정에 쓴다. 확인 로그와 자주 어긋나면 건너뜀
        references = match_references(self.database, request.log_file_ids)
        judged = apply_reference(references, findings, fp.strong, level, self._reference_counts(request.log_file_ids))
        if judged.used or judged.skipped:
            notes.append(judged.note())
        if judged.used:
            level = judged.level
            first = judged.used[0].match
            findings.append(Finding("RETRIEVAL", "TEXT_MATCH", judged.note()[:400],
                                    stance="SUPPORTS" if level in DETECTED_LEVELS else "CONTRADICTS",
                                    log_file_id=first.first_log_file_id, event_id=first.first_event_id,
                                    line_start=first.first_line,
                                    detail={"role": "REFERENCE_JUDGMENT", "entries": [i.hit.entry_id for i in judged.used],
                                            "reasons": judged.reasons}))
        # CR-15 JQ-04: 엔지니어가 판단 질문에 답해 만든 기준을 덮어 적용(가장 강함). 해로운 기준은 확인 로그로 점검해 끈다
        active = CriteriaService(self.database).checked(memory.cases if memory is not None else [])
        criteria = apply_criteria(frozenset(fp.strong | judged.sign_features), fp.features, active, unit_events, level)
        if criteria.fired:
            level = criteria.level
            notes.append(criteria.note())
            findings.append(Finding("RETRIEVAL", "TEXT_MATCH", criteria.note()[:400],
                                    stance="SUPPORTS" if criteria.level in DETECTED_LEVELS else "CONTRADICTS",
                                    detail={"role": "CRITERION", "criteria": [c.id for c in criteria.fired],
                                            "reasons": criteria.reasons + criteria.hold}))
        candidates = self._applicable(similar_abnormal, memory)
        guidance = build_response_guidance(self.database, candidates, level in DETECTED_LEVELS)
        for item in guidance.items:
            findings.append(Finding("RETRIEVAL", "RESPONSE_REFERENCE", f"[{item.case_label}] {item.section}: {item.content[:200]}",
                                    case_id=item.case_id, guide_item_id=item.item_id,
                                    detail={"section": item.section, "source": item.source_name, "order": item.order,
                                            "content": item.content}))
        for item in guidance.check_methods:
            findings.append(Finding("RETRIEVAL", "GUIDE_REFERENCE", f"[{item.case_label}] 원인 확인 방법: {item.content[:200]}",
                                    case_id=item.case_id, guide_item_id=item.item_id, detail={"section": item.section}))
        applicable_ids = {c.case.id for c in candidates}
        findings.extend(self._knowledge_refs([s for s in similar_abnormal if s.case.id in applicable_ids]))
        # CR-04 REF-04: 승인된 참고 자료에 있는 코드가 로그에 나오면 따로 보여 준다(권장 대응과 섞지 않음).
        for match in references:
            findings.append(Finding(
                "RETRIEVAL", "TEXT_MATCH", match.summary(), log_file_id=match.first_log_file_id,
                event_id=match.first_event_id, line_start=match.first_line,
                detail={"role": "REFERENCE_MATCH", "code": match.code, "count": match.count, "via": match.via,
                        "entries": [hit.as_dict() for hit in match.entries]}))
        passages = match_passages(self.database, request.log_file_ids, request.user_description)
        for hit in passages:
            findings.append(Finding("RETRIEVAL", "TEXT_MATCH", hit.summary(),
                                    detail={"role": "REFERENCE_PASSAGE", **hit.as_dict()}))

        axes = self._axes(request, features, findings, patterns, similar_normal, similar_abnormal, guidance,
                          any_normal, any_abnormal, normal_types, reference_found=bool(reference))
        axes["anomaly_level"] = level
        keys = self._store(run_id, findings)
        self._finish(run_id, status="COMPLETED", **axes)
        result = AnalysisResult(run_id, axes, findings, similar_normal, similar_abnormal, guidance, keys, notes,
                                merged_files, references=references, passages=passages, memory=memory,
                                short_log=bool(short), criteria=criteria, reference_judgment=judged)
        result.questions = self._questions(result, fp, active)
        report(4, STEPS, "결정론·검색 단계 완료")
        if request.use_ai:
            result.ai = self.ai_layer(result, request, cancel)
            self._ai_memory(result)
        self._save_verdict(result)
        report(STEPS, STEPS, "분석을 마쳤습니다")
        return result

    def estimate_mode(self, log_file_ids: list[int], cancel: CancelToken = NEVER_CANCEL):
        """분석할 로그의 운전 모드를 AI로 추정한다(§3.3 3단계). 결정은 사용자가 확인한다."""
        from app.ai.mode_classifier import ModeGuess, classify_mode
        from app.learning.normal_learner import build_package
        from app.storage.repositories import ModeRepository

        with closing(self.database.connect()) as conn:
            records = LogFileRepository(conn).get_many(log_file_ids)
            events = EventRepository(conn).for_files(log_file_ids)
            modes = ModeRepository(conn).list(active_only=True)
        features = [extract(r, events[r.id]) for r in records]
        if not features:
            return ModeGuess("UNKNOWN", reason="로그가 없습니다.")
        package, summary = build_package(features)
        with self.client_factory() as client:
            return classify_mode(client, self.settings.ollama.model, modes, package, summary, cancel)

    def _knowledge_refs(self, relevant: list[SimilarCase]) -> list[Finding]:
        """비슷한 승인 비정상 사례의 승인된 증상·원인 항목(관련 진단 가이드, §6.3 계층 3 입력)."""
        if not relevant:
            return []
        from app.storage.repositories import GuideRepository

        labels = {s.case.id: s.case for s in relevant}
        with closing(self.database.connect()) as conn:
            rows = GuideRepository(conn).approved_items_for_cases(list(labels))
        out = []
        for row in rows:
            if row["section"] in ("SYMPTOM", "CAUSE", "CRITERIA"):
                case = labels[row["case_id"]]
                out.append(Finding("RETRIEVAL", "GUIDE_REFERENCE",
                                   f"[{case.label} · 원인 {case.cause_status or '미정'}] {row['section']}: {row['content'][:200]}",
                                   case_id=case.id, guide_item_id=row["id"],
                                   detail={"section": row["section"], "cause_status": case.cause_status}))
        return out

    def ai_layer(self, result: AnalysisResult, request: AnalysisRequest, cancel: CancelToken) -> DiagnosisOutcome:
        """계층 3 (Phase 7). 실패해도 계층 1·2 결과는 이미 저장돼 있다(NA-14)."""
        import json

        from app.ai.context_builder import EvidencePackage
        from app.ai.diagnosis import DiagnosisContext, diagnose

        model = self.settings.ollama.model
        if not model:
            self._finish(result.run_id, ai_status="NOT_RUN", error_message="Ollama 모델 미지정으로 AI 단계를 건너뜀")
            return DiagnosisOutcome("NOT_RUN", errors=["설정에서 사용할 Ollama 모델을 고르지 않았습니다."])
        from app.learning.ai_questions import case_knowledge, knowledge_texts

        package = EvidencePackage()
        kinds: dict[str, str] = {}
        confirmed: set[str] = set()
        similar_cases = {s.case.id: s.case for s in result.similar_normal + result.similar_abnormal}
        if result.memory is not None:  # CR-14: 근거 지문이 비슷한 확인 로그도 판정·메모·판단 가이드를 붙인다
            similar_cases.update({item.case.id: item.case for _s, item, _f in result.memory.neighbors})
        event_ids = [f.event_id for f in result.findings if f.event_id]
        with closing(self.database.connect()) as conn:
            raw = {e.id: e.raw_text for e in EventRepository(conn).get_many(event_ids)}
        # 중요한 근거부터 넣는다. 프롬프트 길이 제한으로 뒤쪽이 빠질 때 '본 적 없는 이벤트'(참고) 30개가
        # 값 범위 이탈·대응 근거를 밀어내지 않게 한다(2026-11-19 실사용).
        for index in sorted(range(len(result.findings)), key=lambda i: _ai_priority(result.findings[i])):
            finding = result.findings[index]
            key = result.keys[index]
            kind = {"SIMILAR_NORMAL_CASE": "CASE", "SIMILAR_ABNORMAL_CASE": "CASE", "GUIDE_REFERENCE": "GUIDE",
                    "RESPONSE_REFERENCE": "RESPONSE"}.get(finding.finding_type, "FINDING")
            text = f"[{finding.finding_type}] {finding.summary}"
            if finding.detail.get("role") == "REFERENCE_PASSAGE":  # CR-07: 승인된 일반 문서 문단
                kind = "REFERENCE"
                text = f"[참고 자료 문단] {finding.detail['document_title']}" + (
                    f" · {finding.detail['locator']}" if finding.detail.get("locator") else "") + f": {finding.detail['body']}"
            if finding.detail.get("role") == "REFERENCE_MATCH":
                # 승인된 참고 자료(사용자 코드 사전): 코드 뜻의 근거로만 쓴다(V-5). 관측 사실·대응 순서 근거는 아님.
                kind = "REFERENCE"
                text = f"[참고 자료] 코드 {finding.detail['code']} (로그에 {finding.detail['count']}회)" + "".join(
                    f"\n- 출처 {e['document_title']}{(' · ' + e['locator']) if e.get('locator') else ''}: "
                    + "; ".join(f"{label} {e[name]}" for name, label in (("title", "명칭"), ("description", "설명"),
                                                                        ("cause", "원인"), ("action", "조치"))
                                if e.get(name))
                    for e in finding.detail["entries"][:3])
            if finding.event_id in raw:
                width = 160 if _ai_priority(finding) == LOW_PRIORITY else 300
                text += f"\n원문({finding.line_start}행): {raw[finding.event_id][:width]}"
            if kind == "CASE" and finding.case_id in similar_cases:  # CR-07 AO-02: 사용자 판정·메모·판단 가이드
                text += "\n" + case_knowledge(similar_cases[finding.case_id])
            package.add_with_key(key, kind, text, log_file_id=finding.log_file_id, event_id=finding.event_id,
                                 case_id=finding.case_id, guide_item_id=finding.guide_item_id,
                                 content=finding.detail.get("content") if kind == "RESPONSE" else None)
            kinds[key] = kind
            if finding.detail.get("cause_status") == "CONFIRMED":
                confirmed.add(key)
        # CR-14 CM-04: 이 로그 근거마다 확인된 로그에서의 실적(비정상 몇·정상 몇). 쌓인 확인 결과로 근거의 무게를 정한다.
        if result.memory is not None and result.memory.records:
            lines = [f"- {feature}: 확인된 비정상 {abn}개·정상 {nor}개에서 나옴" for feature, abn, nor in result.memory.records[:8]]
            package.add_with_key("CM-0001", "CASE", "[근거별 실적 — 확인된 로그 기준]\n" + "\n".join(lines))
            kinds["CM-0001"] = "CASE"
        # CR-07 QA-03: 비슷한 승인 로그에 사용자가 답한 내용(맞음·틀림·글)은 승인 지식 ANSWER로 준다.
        relevant = [s.case.id for s in result.similar_normal + result.similar_abnormal if s.relevant]
        for number, text in enumerate(knowledge_texts(self.database, relevant), start=1):
            key = f"QA-{number:04d}"
            package.add_with_key(key, "ANSWER", text)
            kinds[key] = "ANSWER"
        axes_text = ", ".join(f"{k}={v}" for k, v in result.axes.items())
        summary = (f"결과 축: {axes_text}\n운전 모드 확인: {'예' if request.mode_confirmed else '아니오'}\n"
                   f"운전 조건: {request.operating_conditions or '없음'}\n사용자 설명: {request.user_description or '없음'}\n"
                   + "\n".join(result.notes))
        ctx = DiagnosisContext(package, summary, set(result.merged_files), confirmed)
        run_dir = self.paths.analysis / f"R-{result.run_id:04d}"
        run_dir.mkdir(parents=True, exist_ok=True)
        input_path = run_dir / "ai_input.json"  # AI에 무엇을 보냈는지 원본과 따로 남긴다(SEC-08)
        input_path.write_text(json.dumps({"summary": summary, "evidence": package.to_json()}, ensure_ascii=False,
                                         indent=2), encoding="utf-8")
        try:
            with self.client_factory() as client:
                structured = diagnose(client, model, ctx, cancel)
        except Cancelled:
            self._finish(result.run_id, ai_status="CANCELLED", ai_model=model)
            return DiagnosisOutcome("CANCELLED", errors=["AI 단계를 취소했습니다."], input_path=str(input_path), kinds=kinds)
        status = {"OK": "OK", "AI_UNAVAILABLE": "UNAVAILABLE", "VALIDATION_FAILED": "VALIDATION_FAILED",
                  "NO_MODEL": "NOT_RUN"}[structured.status]
        (run_dir / "ai_output.json").write_text(json.dumps({"status": structured.status, "raw": structured.raw,
                                                            "errors": structured.errors}, ensure_ascii=False, indent=2),
                                                encoding="utf-8")
        if structured.value is not None:
            self._store_ai(result.run_id, structured.value, kinds)
        self._finish(result.run_id, ai_status=status, ai_model=model,
                     error_message="; ".join(structured.errors)[:500] or None)
        return DiagnosisOutcome(status, structured.value, structured.errors, str(input_path), kinds)

    def _store_ai(self, run_id: int, answer, kinds: dict[str, str]) -> None:
        from app.models.records import EvidenceRef
        from app.storage.repositories import ClaimRepository

        with closing(self.database.connect()) as conn, transaction(conn):
            repo = AnalysisRepository(conn)
            claims = ClaimRepository(conn)
            ids = {row["evidence_key"]: row["id"] for row in repo.evidence(run_id)}
            counter = 0

            def add(finding_type: str, text: str, cites: list[str], stance: str = "NEUTRAL", **detail) -> None:
                nonlocal counter
                counter += 1
                repo.add_evidence(run_id, evidence_key=f"AI-{counter:04d}", layer="AI", finding_type=finding_type,
                                  summary=text, stance=stance, detail={"cites": cites, **detail})

            def refs(keys: list[str], stance: str = "SUPPORTS") -> list[EvidenceRef]:
                return [EvidenceRef(evidence_type="ANALYSIS_EVIDENCE", analysis_evidence_id=ids[k], stance=stance)
                        for k in keys if k in ids]

            for obs in answer.observations:
                add("AI_OBSERVATION", obs.text, obs.evidence)
                claims.add(claim_text=obs.text, claim_kind="FACT", source="AI", analysis_run_id=run_id,
                           evidence=refs(obs.evidence))
            if answer.abnormal_likelihood is not None:  # CR-07 AO-01: AI 추정치(통계 확률 아님)
                add("AI_HYPOTHESIS", answer.likelihood_reason or "비정상일 가능성", answer.likelihood_evidence,
                    role="ABNORMAL_LIKELIHOOD", likelihood=answer.abnormal_likelihood)
            for hyp in answer.hypotheses:
                add("AI_HYPOTHESIS", hyp.text, hyp.evidence, stance="SUPPORTS", counter=hyp.counter_evidence,
                    likelihood=hyp.likelihood)
                if hyp.counter_evidence:
                    add("AI_COUNTER_EVIDENCE", f"반대 근거: {hyp.text[:80]}", hyp.counter_evidence, stance="CONTRADICTS")
                claims.add(claim_text=hyp.text, claim_kind="HYPOTHESIS", source="AI", analysis_run_id=run_id,
                           evidence=refs(hyp.evidence) + refs(hyp.counter_evidence, "CONTRADICTS"))
            for step in answer.verification_steps:
                manual_based = bool(step.evidence) and all(kinds.get(k) in ("RESPONSE", "GUIDE") for k in step.evidence)
                add("AI_VERIFICATION_STEP", step.text, step.evidence, role="VERIFICATION", manual_based=manual_based)
            for step in answer.response_plan:
                # 대응 순서는 승인된 매뉴얼·가이드 항목을 AI가 고르고 정렬한 것(V-9). 별도 finding_type이 없어 role로 구분한다.
                add("AI_VERIFICATION_STEP", step.text, step.evidence, role="RESPONSE_PLAN", manual_based=True)
            for item in answer.unknowns:
                add("AI_UNKNOWN", item, [])
                claims.add(claim_text=item, claim_kind="UNKNOWN", source="AI", analysis_run_id=run_id)

    @staticmethod
    def _normal_knowledge(request, patterns, normal_types, any_normal: bool) -> bool:
        """정상 비교 기준이 있는지(모드 패턴·모드 정상 유형, 또는 모드 없이 저장된 정상 로그)."""
        return bool(patterns) or normal_types is not None or (request.mode_id is None and any_normal)

    @staticmethod
    def _applicable(similar_abnormal: list[SimilarCase], memory) -> list[Applicable]:
        """이 로그에 대응을 적용할 근거가 확인된 비정상 사례: 근거 지문이 비슷한 확인 로그(유사도 0.5 이상) → 같은 오류 코드.
        같은 코드라도 다른 단계에서 난 사례는 '적용 전 재확인'(리뷰 2026-11-22). 이벤트 종류가 비슷한 것만으로는 넣지 않는다."""
        out: list[Applicable] = []
        seen: set[int] = set()
        for sim, item, _shared in (memory.neighbors if memory is not None else []):
            if item.abnormal and sim >= APPLICABLE_SIMILARITY and item.case.id not in seen:
                out.append(Applicable(item.case, f"근거 지문이 비슷함(유사도 {sim:.2f})"))
                seen.add(item.case.id)
        for s in similar_abnormal:
            if s.shared_codes and s.case.id not in seen:
                recheck = bool(s.other_stage_codes) and not s.same_stage_codes
                out.append(Applicable(s.case, f"같은 오류 코드 {', '.join(s.shared_codes[:3])}", recheck))
                seen.add(s.case.id)
        return out

    def _questions(self, result: AnalysisResult, fp, criteria) -> list:
        """판단을 돕는 질문(CR-15 JQ-01·02): 판단 보류이거나 확인 이력 없는 근거가 있을 때만, 질문 틀은 코드가 만든다."""
        from app.learning.criteria import make_questions

        event_ids = [f.event_id for f in result.findings if f.layer == "DETERMINISTIC" and f.event_id]
        with closing(self.database.connect()) as conn:
            rows = EventRepository(conn).get_many(event_ids)
            known = [Criterion.from_row(r) for r in CriteriaRepository(conn).all()]
        raw = {e.id: e.raw_text for e in rows}
        messages = {e.id: e.message for e in rows}
        cases = result.memory.cases if result.memory is not None else []
        reference = result.reference_judgment  # CR-16 RJ-06: 참고 자료가 판정을 준 근거는 묻지 않는다
        if reference is not None:
            known += [Criterion(0, f, ABNORMAL_SIGN, "참고 자료") for f in reference.sign_features]
            known += [Criterion(0, f, BENIGN, "참고 자료") for f in reference.benign_features]
        return make_questions(result.findings, fp.strong, fp.weak, result.verdict.state, cases, known, raw, messages)

    _REFERENCE_COUNTS: dict[tuple, tuple[int, int]] = {}  # (DB, 사례 상태, 항목) → 개수. 앱이 켜져 있는 동안만

    def _reference_counts(self, mine: Sequence[int]):
        """참고 자료 판정의 스스로 점검(CR-16 RJ-05): 그 항목이 나온 확인된 정상·비정상 사례 수. 이 분석 단위의 로그가 걸린
        사례는 뺀다. 사례가 바뀌지 않았으면 앞에서 센 값을 쓴다."""
        with closing(self.database.connect()) as conn:
            repo = CaseRepository(conn)
            approved = repo.approved()
            links = {case.id: repo.log_file_ids(case.id) for case in approved}
        cases = [c for c in approved if not set(links[c.id]) & set(mine)]
        if not cases:
            return None
        state = (str(self.database.db_file), len(cases), max(c.id for c in cases), sum(c.id for c in cases))
        logs = [i for c in cases for i in links[c.id]]
        case_of = {i: c for c in cases for i in links[c.id]}

        def counts(item: JudgedMatch) -> tuple[int, int]:
            key = (*state, item.hit.entry_id)
            if key not in self._REFERENCE_COUNTS:
                text = " ".join(item.hit.code.split()) if item.match.via == "TEXT" else None
                code = None if text else item.match.code_norm
                with closing(self.database.connect()) as conn:
                    files = EventRepository(conn).files_with(logs, code_norm=code, text=text)
                hit_cases = {case_of[i].id: case_of[i].case_type for i in files}
                self._REFERENCE_COUNTS[key] = (sum(t == "NORMAL" for t in hit_cases.values()),
                                               sum(t == "ABNORMAL" for t in hit_cases.values()))
            return self._REFERENCE_COUNTS[key]

        return counts

    def _save_verdict(self, result: AnalysisResult) -> None:
        """최종 판정(정상·비정상·판단 보류)·이유·사례 판단·AI 판정을 분석 폴더에 남긴다. 보고서가 화면과 같은 판정을 보이게
        (DB 구조는 그대로, 리뷰 2026-11-22). 못 써도 분석은 그대로."""
        import json

        verdict = result.verdict
        data = {"state": verdict.state, "title": verdict.title, "reasons": verdict.reasons,
                "memory": result.memory.note() if result.memory is not None and result.memory.total else None,
                "reference": result.reference_judgment.note() if result.reference_judgment is not None else None,
                "ai": result.ai_judgment.note() if result.ai_judgment is not None else None}
        try:
            folder = self.paths.analysis / f"R-{result.run_id:04d}"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "verdict.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            logger.exception("최종 판정 기록 실패: R-%04d", result.run_id)

    def _compare_normals(self, features: list[FileFeatures], events: dict, mode_id: int | None, patterns,
                         exclude_case_ids: frozenset[int] = frozenset()) -> NormalComparison:
        """결정론 분석: 명시적 오류·본 적 없는 이벤트(detect) + 비슷한 정상 로그와 비교(CR-08).
        exclude_case_ids: 비교 기준에서 뺄 사례(사례 로그의 지문을 다시 만들 때 자기 자신, CR-14)."""
        # 비교 통계(CR-08)는 비슷한 정상 로그를 넓게(최대 20개) 쓰고, 화면·AI에는 위 5개만 보인다
        normal_types_seen: set[str] = set()  # 저장된 정상 로그 전체의 이벤트 유형(DT-18)
        normal_pool = find_similar(self.database, features, "NORMAL", mode_id, limit=REFERENCE_POOL,
                                   seen_types=normal_types_seen, exclude_case_ids=exclude_case_ids)
        reference: list = []
        if mode_id is None:
            # CR-06 LI-06: 모드를 고르지 않으면 가장 비슷한 승인 정상 로그를 기준으로 '본 적 없는 이벤트'를 찾는다.
            # 사용자가 확정한 패턴은 모드에 묶여 있으므로 쓰지 않는다(→ '승인된 규칙 위반'은 나오지 않음).
            reference = reference_normals(normal_pool[:5])
            normal_types = normal_types_for_cases(self.database, [s.case.id for s in reference])
        else:
            normal_types = approved_normal_types(self.database, mode_id)
        findings = detect(features, patterns, normal_types)
        pool = comparison_pool(normal_pool) if mode_id is None else []
        pool = self._within_budget(pool)  # 큰 로그(1~2만 줄)는 비교 기준을 이벤트 합계 안으로(PF-01)
        if pool:
            # CR-08 DT-01~14: 비슷한 정상 로그와 비교한 차이. 모두 '의심'이다. 기준이 많을수록 우연한 공통점(가끔 나오는
            # 이벤트·비슷한 반복 횟수)을 규칙으로 착각하지 않으므로 넓게(최대 20개) 쓴다. 점수가 0.4보다 조금 낮아도
            # 입력 로그의 이벤트 종류를 거의 다 담은 정상 로그는 기준이 된다(끊긴 로그·무언가 통째로 빠진 로그).
            reference_files = self._reference_files(pool)
            for f in features:
                findings.extend(compare_with_normals(f, events[f.log_file_id], reference_files, normal_types_seen))
        return NormalComparison(findings, normal_pool, reference, normal_types, pool)

    def _case_memory(self, features: list[FileFeatures], findings: list[Finding], rule_level: str, normals_count: int):
        """이 분석 단위(로그 하나 또는 같은 수집 세션 묶음)의 근거 지문을 남기고, 확인된 사례들(사례 기억)로 판정을 보정한다
        (CR-14 CM-01~03). 확인 사례 지문이 없거나 오래됐으면 시간 예산 안에서 다시 만든다. 이 단위의 로그가 걸린 사례는
        기억에서 뺀다(자기 자신과 비교 금지)."""
        import time

        from app.learning.case_memory import FINGERPRINT_BUDGET_S, FingerprintStore, decide, fingerprint_of, load_cases, unit_key

        mine_ids = {f.log_file_id for f in features}
        key = unit_key(mine_ids)
        store = FingerprintStore(self.paths.analysis)
        fp = fingerprint_of(findings, rule_level, normals_count)
        with closing(self.database.connect()) as conn:
            repo = CaseRepository(conn)
            approved = repo.approved()
            links = {case.id: repo.log_file_ids(case.id) for case in approved}
        mine = [case for case in approved if mine_ids & set(links[case.id])]
        own = any(unit_key(links[case.id]) == key for case in mine)
        if not own:  # 이미 사례인 단위는 비교 기준에 자기 자신이 들어가 지문이 흐려지므로 사례 지문을 덮어쓰지 않는다
            store.put(key, fp)
        approved = [case for case in approved if case not in mine]
        cases = load_cases(approved, lambda case_id: links[case_id], store, normals_count,
                           lambda case, ids: self._case_fingerprint(case, ids, normals_count),
                           FINGERPRINT_BUDGET_S, time.perf_counter)
        decision = decide(fp, cases)
        decision.log_file_id, decision.own_case, decision.fp = key, own, fp
        return decision

    def _ai_memory(self, result: AnalysisResult) -> None:
        """AI가 낸 가능성을 이 단위의 지문에 남기고(나중에 확인되면 이력이 됨), 확인 이력으로 AI 판정 문턱을 고른다(CM-07).
        AI 의견이 실패했으면 '판단 보류'로 남긴다."""
        from dataclasses import replace

        from app.learning.case_memory import FingerprintStore, ai_judgment

        memory = result.memory
        if memory is None:
            return
        ok = getattr(result.ai, "status", None) == "OK"
        likelihood = getattr(result.ai.answer, "abnormal_likelihood", None) if ok else None
        store = FingerprintStore(self.paths.analysis)
        if ok and memory.log_file_id is not None and not memory.own_case:
            fp = store.get(memory.log_file_id)
            if fp is not None:
                store.put(memory.log_file_id, replace(fp, ai_ran=True, ai_likelihood=likelihood))
        result.ai_judgment = ai_judgment(likelihood, memory.cases, ran=ok)
        result.notes.append(result.ai_judgment.note())

    def _case_fingerprint(self, case, log_file_ids: list[int], normals_count: int):
        """분석한 적 없이 저장된 사례(로그 하나 또는 묶음)의 지문: 그 사례를 뺀 정상 사례들과 결정론 비교(CR-14 CM-01)."""
        from app.learning.case_memory import fingerprint_of

        try:
            with closing(self.database.connect()) as conn:
                records = LogFileRepository(conn).get_many(list(log_file_ids))
                events = EventRepository(conn).for_files(list(log_file_ids))
            records, events = current_parse(self.database, records, events)
            features = [extract(r, events[r.id]) for r in records]
            compared = self._compare_normals(features, events, None, [], frozenset({case.id}))
        except Exception:  # 지문 하나 못 만들어도 분석은 계속(다음 분석 때 다시 시도)
            logger.exception("사례 지문 만들기 실패: case %s", case.id)
            return None
        normals = normals_count - (1 if case.case_type == "NORMAL" else 0)
        return fingerprint_of(compared.findings, anomaly_level_of(compared.findings, normals > 0), normals)

    def _within_budget(self, pool: list) -> list:
        """비슷한 순서대로 기준을 담되 이벤트 합계가 REFERENCE_EVENT_BUDGET을 넘으면 멈춘다(최소 REFERENCE_MIN_KEEP개).
        실제 장비 로그는 정상 1~2만 줄이라 기준 20개를 모두 읽으면 분석마다 30만 줄을 읽는다(2026-11-22 사용자 정보)."""
        from app.learning.normal_learner import case_file_features

        kept, total = [], 0
        for similar in pool:
            if len(kept) >= REFERENCE_MIN_KEEP and total >= REFERENCE_EVENT_BUDGET:
                break
            kept.append(similar)
            total += sum(len(f.sequence) for f in case_file_features(self.database, similar.case.id))  # 기억해 둔 특징
        return kept

    def _reference_files(self, reference) -> list[ReferenceFile]:
        out: list[ReferenceFile] = []
        with closing(self.database.connect()) as conn:
            for s in reference:
                ids = CaseRepository(conn).log_file_ids(s.case.id)
                records = LogFileRepository(conn).get_many(ids)
                events = EventRepository(conn).for_files(ids)
                records, events = current_parse(self.database, records, events)
                out.extend(ReferenceFile(extract(r, events[r.id]), events[r.id], s.case.id) for r in records)
        return out

    @staticmethod
    def _axes(request, features: list[FileFeatures], findings: list[Finding], patterns, similar_normal,
              similar_abnormal, guidance, any_normal: bool, any_abnormal: bool, normal_types,
              reference_found: bool = False) -> dict[str, str]:
        # 결정론 근거의 무게로 나눈다: 승인된 규칙을 어긴 것(VIOLATION)과 관찰된 것과 다른 것(SUSPECT, CR-08 비교 포함)
        deterministic = [f for f in findings if f.layer == "DETERMINISTIC" and f.stance == "SUPPORTS"]
        violations = {f.finding_type for f in deterministic if f.severity == "VIOLATION"}
        suspects = {f.finding_type for f in deterministic if f.severity == "SUSPECT"}
        # 모드 없이 분석하면 승인된 정상 로그 전체와 비교했으므로, 정상 로그가 하나라도 있으면 '판단 못 함'이 아니라
        # 점수대로 일치·일부·불일치로 본다(비슷한 것이 없으면 불일치, CR-07 화면 점검).
        has_normal_knowledge = DiagnosticEngine._normal_knowledge(request, patterns, normal_types, any_normal)
        best_normal = max((s.similarity for s in similar_normal), default=0.0)
        if not has_normal_knowledge:
            normal_match = "NOT_EVALUATED"
        elif not violations and not (suspects - {"EXPLICIT_ERROR"}) and best_normal >= 0.6:
            normal_match = "MATCH"
        elif best_normal >= 0.3:
            normal_match = "PARTIAL"
        else:
            normal_match = "NO_MATCH"
        anomaly = anomaly_level_of(findings, has_normal_knowledge)
        if not any_abnormal:
            similar = "NOT_EVALUATED"
        else:
            similar = "FOUND" if any(s.relevant for s in similar_abnormal) else "NOT_FOUND"
        # 비교 기준: 확인된 운전 모드, 또는 모드 없이 고른 비슷한 정상 로그(CR-06 LI-06)
        mode_known = (request.mode_id is not None and request.mode_confirmed) or reference_found
        timestamps_known = all(f.timestamp_basis not in ("UNKNOWN",) for f in features)
        if not any_normal and not any_abnormal:
            sufficiency = "INSUFFICIENT"
        elif not mode_known and not any_abnormal:
            sufficiency = "INSUFFICIENT"
        elif not mode_known or not has_normal_knowledge or not timestamps_known:
            sufficiency = "PARTIAL"
        else:
            sufficiency = "SUFFICIENT"
        return {"normal_match": normal_match, "anomaly_level": anomaly, "similar_abnormal": similar,
                "evidence_sufficiency": sufficiency, "response_guidance": guidance.level}

    def _store(self, run_id: int, findings: list[Finding]) -> dict[int, str]:
        keys: dict[int, str] = {}
        with closing(self.database.connect()) as conn, transaction(conn):
            repo = AnalysisRepository(conn)
            for index, f in enumerate(findings, start=1):
                key = f"EV-{index:04d}"
                repo.add_evidence(run_id, evidence_key=key, layer=f.layer, finding_type=f.finding_type,
                                  summary=f.summary, stance=f.stance, log_file_id=f.log_file_id, event_id=f.event_id,
                                  line_start=f.line_start, line_end=f.line_end, case_id=f.case_id,
                                  pattern_id=f.pattern_id, guide_item_id=f.guide_item_id, similarity=f.similarity,
                                  detail=f.detail or None)
                keys[index - 1] = key
        return keys

    def _finish(self, run_id: int, **fields: Any) -> None:
        with closing(self.database.connect()) as conn, transaction(conn):
            AnalysisRepository(conn).update(run_id, finished_at=utc_now_iso(), **fields)
