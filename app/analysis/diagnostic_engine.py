"""신규 로그 분석 실행 (계획서 §3.3 처리 절차, §6.3 세 계층).

계층 1(결정론) → 계층 2(승인된 사례 검색) → 계층 3(Ollama, Phase 7)을 차례로 돌리고, 계층마다 근거를
analysis_evidence에 근거 키(EV-0001…)와 함께 저장한다. AI가 없거나 실패해도 계층 1·2 결과는 그대로 남는다(§12.5).
결과 분류는 독립된 다섯 축이다(§3.3, CR-01): 정상 일치·이상 수준·유사 비정상·근거 충분성·권장 대응.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any

from app import __version__
from app.ai.ollama_client import OllamaClient
from app.analysis.anomaly_detector import Finding, detect
from app.analysis.case_retriever import SimilarCase, approved_normal_types, find_similar
from app.analysis.result_builder import ResponseGuidance, build_response_guidance
from app.config import AppSettings
from app.core.cancel import NEVER_CANCEL, Cancelled, CancelToken
from app.core.feature_extractor import FileFeatures, extract
from app.core.sequence_analyzer import build_timeline
from app.learning.normal_learner import default_client_factory
from app.models import utc_now_iso
from app.storage.database import Database, transaction
from app.storage.paths import AppPaths
from app.storage.repositories import (
    AnalysisRepository,
    CaseRepository,
    EventRepository,
    LogFileRepository,
    PatternRepository,
    SessionRepository,
)

logger = logging.getLogger("app.analysis")
ProgressFn = Callable[[int, int, str], None]
STEPS = 5


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


@dataclass
class DiagnosisOutcome:
    status: str  # OK / NOT_RUN / UNAVAILABLE / VALIDATION_FAILED / CANCELLED
    answer: Any = None  # app.ai.diagnosis.DiagnosisAnswer
    errors: list[str] = field(default_factory=list)
    input_path: str | None = None
    kinds: dict[str, str] = field(default_factory=dict)  # 근거 키 → 종류(FINDING/CASE/GUIDE/RESPONSE)


class DiagnosticEngine:
    def __init__(self, database: Database, paths: AppPaths, settings: AppSettings,
                 client_factory: Callable[[], OllamaClient] | None = None) -> None:
        self.database = database
        self.paths = paths
        self.settings = settings
        self.client_factory = client_factory or default_client_factory(settings)

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
            any_normal = bool(CaseRepository(conn).approved(case_type="NORMAL"))
            any_abnormal = bool(CaseRepository(conn).approved(case_type="ABNORMAL"))
        features = [extract(r, events[r.id]) for r in records]
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

        report(1, STEPS, "결정론 분석(명시적 오류·누락·순서·반복·지연)")
        normal_types = approved_normal_types(self.database, request.mode_id)
        findings = detect(features, patterns, normal_types)
        cancel.raise_if_cancelled()

        report(2, STEPS, "승인된 사례 검색")
        similar_normal = find_similar(self.database, features, "NORMAL", request.mode_id)
        similar_abnormal = find_similar(self.database, features, "ABNORMAL", None)  # 비정상은 모드가 달라도 찾는다
        for s in similar_normal:
            findings.append(Finding("RETRIEVAL", "SIMILAR_NORMAL_CASE", f"비슷한 정상 사례 {s.case.label}: {s.explain()}",
                                    stance="CONTRADICTS" if s.relevant else "NEUTRAL", case_id=s.case.id,
                                    similarity=s.similarity, detail={"shared_types": s.shared_types[:10]}))
        for s in similar_abnormal:
            findings.append(Finding("RETRIEVAL", "SIMILAR_ABNORMAL_CASE",
                                    f"비슷한 비정상 사례 {s.case.label}: {s.explain()}",
                                    stance="SUPPORTS" if s.relevant else "NEUTRAL", case_id=s.case.id,
                                    similarity=s.similarity,
                                    detail={"shared_codes": s.shared_codes, "text_hit": s.text_hit,
                                            "cause_status": s.case.cause_status}))
        cancel.raise_if_cancelled()

        report(3, STEPS, "권장 대응 조립")
        guidance = build_response_guidance(self.database, similar_abnormal)
        for item in guidance.items:
            findings.append(Finding("RETRIEVAL", "RESPONSE_REFERENCE", f"[{item.case_label}] {item.section}: {item.content[:200]}",
                                    case_id=item.case_id, guide_item_id=item.item_id,
                                    detail={"section": item.section, "source": item.source_name, "order": item.order}))
        for item in guidance.check_methods:
            findings.append(Finding("RETRIEVAL", "GUIDE_REFERENCE", f"[{item.case_label}] 원인 확인 방법: {item.content[:200]}",
                                    case_id=item.case_id, guide_item_id=item.item_id, detail={"section": item.section}))
        findings.extend(self._knowledge_refs([s for s in similar_abnormal if s.relevant]))

        axes = self._axes(request, features, findings, patterns, similar_normal, similar_abnormal, guidance,
                          any_normal, any_abnormal, normal_types)
        keys = self._store(run_id, findings)
        self._finish(run_id, status="COMPLETED", **axes)
        result = AnalysisResult(run_id, axes, findings, similar_normal, similar_abnormal, guidance, keys, notes,
                                merged_files)
        report(4, STEPS, "결정론·검색 단계 완료")
        if request.use_ai:
            result.ai = self.ai_layer(result, request, cancel)
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
        package = EvidencePackage()
        kinds: dict[str, str] = {}
        confirmed: set[str] = set()
        event_ids = [f.event_id for f in result.findings if f.event_id]
        with closing(self.database.connect()) as conn:
            raw = {e.id: e.raw_text for e in EventRepository(conn).get_many(event_ids)}
        for index, finding in enumerate(result.findings):
            key = result.keys[index]
            kind = {"SIMILAR_NORMAL_CASE": "CASE", "SIMILAR_ABNORMAL_CASE": "CASE", "GUIDE_REFERENCE": "GUIDE",
                    "RESPONSE_REFERENCE": "RESPONSE"}.get(finding.finding_type, "FINDING")
            text = f"[{finding.finding_type}] {finding.summary}"
            if finding.event_id in raw:
                text += f"\n원문({finding.line_start}행): {raw[finding.event_id][:300]}"
            package.add_with_key(key, kind, text, log_file_id=finding.log_file_id, event_id=finding.event_id,
                                 case_id=finding.case_id, guide_item_id=finding.guide_item_id)
            kinds[key] = kind
            if finding.detail.get("cause_status") == "CONFIRMED":
                confirmed.add(key)
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
            for hyp in answer.hypotheses:
                add("AI_HYPOTHESIS", hyp.text, hyp.evidence, stance="SUPPORTS", counter=hyp.counter_evidence)
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
    def _axes(request, features: list[FileFeatures], findings: list[Finding], patterns, similar_normal,
              similar_abnormal, guidance, any_normal: bool, any_abnormal: bool, normal_types) -> dict[str, str]:
        types = {f.finding_type for f in findings}
        violations = types & {"MISSING_EVENT", "ORDER_VIOLATION"}
        suspects = types & {"EXPLICIT_ERROR", "REPETITION", "LATENCY"}
        has_normal_knowledge = bool(patterns) or normal_types is not None
        best_normal = max((s.similarity for s in similar_normal), default=0.0)
        if not has_normal_knowledge:
            normal_match = "NOT_EVALUATED"
        elif not violations and not (types & {"REPETITION", "LATENCY"}) and best_normal >= 0.6:
            normal_match = "MATCH"
        elif best_normal >= 0.3:
            normal_match = "PARTIAL"
        else:
            normal_match = "NO_MATCH"
        if violations:
            anomaly = "RULE_VIOLATION"
        elif suspects:
            anomaly = "SUSPECTED"
        elif has_normal_knowledge:
            anomaly = "NONE_FOUND"
        else:
            anomaly = "NOT_EVALUATED"
        if not any_abnormal:
            similar = "NOT_EVALUATED"
        else:
            similar = "FOUND" if any(s.relevant for s in similar_abnormal) else "NOT_FOUND"
        mode_known = request.mode_id is not None and request.mode_confirmed
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
