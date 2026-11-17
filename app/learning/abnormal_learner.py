"""비정상 로그 학습 흐름 (계획서 §3.2, §6.2, CR-01 대응 매뉴얼).

1) 진단 가이드(필수)·대응 매뉴얼(선택) 첨부 → 원본 보존
2) 결정론 구조화(제목 줄 → 권장 항목). 필요하면 AI가 조각에 항목 이름만 다시 붙인다
3) 로그의 오류 코드가 가이드 원문에 그대로 나오면 '가이드-로그 연결' 주장을 만든다(뜻은 붙이지 않음)
4) 사용자가 항목을 승인·거부하고 사례를 승인한다(ApprovalWorkflow)
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from app.ai.guide_labeler import label_segments, split_segments
from app.ai.ollama_client import OllamaClient
from app.ai.response_validator import StructuredResult
from app.config import AppSettings
from app.core.cancel import NEVER_CANCEL, CancelToken
from app.core.guide_structurer import mentioned_codes, structure_text
from app.core.importer import import_attachment, read_attachment_text
from app.learning.case_manager import LOCKED_STATUSES, CaseError
from app.learning.normal_learner import default_client_factory
from app.models.enums import Actor, AuditAction
from app.models.records import EvidenceRef
from app.storage.database import Database, transaction
from app.storage.paths import AppPaths
from app.storage.repositories import (
    AuditRepository,
    CaseRepository,
    ClaimRepository,
    EventRepository,
    GuideRepository,
)

KIND_LABEL = {"DIAGNOSTIC_GUIDE": "진단 가이드", "RESPONSE_MANUAL": "대응 매뉴얼"}


@dataclass
class AttachOutcome:
    attachment_id: int
    duplicate: bool
    items: int
    headings_found: bool
    linked_codes: list[str] = field(default_factory=list)


class AbnormalLearner:
    def __init__(self, database: Database, paths: AppPaths, settings: AppSettings,
                 client_factory: Callable[[], OllamaClient] | None = None) -> None:
        self.database = database
        self.paths = paths
        self.settings = settings
        self.client_factory = client_factory or default_client_factory(settings)

    def _check_editable(self, case_id: int) -> None:
        with closing(self.database.connect()) as conn:
            case = CaseRepository(conn).get(case_id)
        if case is None:
            raise CaseError(f"사례 {case_id}이(가) 없습니다.")
        if case.case_type != "ABNORMAL":
            raise CaseError("진단 가이드·대응 매뉴얼은 비정상 사례에만 첨부합니다.")
        if case.status in LOCKED_STATUSES:
            raise CaseError("승인·보관된 사례는 '수정 시작' 후 첨부할 수 있습니다.")

    def attach(self, case_id: int, path: Path, kind: str) -> AttachOutcome:
        self._check_editable(case_id)
        result = import_attachment(self.database, self.paths, self.settings.import_, case_id, path, kind)
        items, found = (0, True)
        if not result.duplicate:
            items, found = self._store_structure(case_id, result.attachment_id, result.text, kind)
        codes = self.link_events(case_id)
        return AttachOutcome(result.attachment_id, result.duplicate, items, found, codes)

    def _store_structure(self, case_id: int, attachment_id: int, text: str, kind: str) -> tuple[int, bool]:
        items, found = structure_text(text, kind)
        with closing(self.database.connect()) as conn, transaction(conn):
            guides = GuideRepository(conn)
            for item in items:
                guides.add_item(case_id, section=item.section, content=item.content, source_kind="GUIDE_TEXT",
                                attachment_id=attachment_id, span_start=item.span_start, span_end=item.span_end,
                                sort_order=item.sort_order)
            AuditRepository(conn).add("case", case_id, AuditAction.UPDATE, Actor.SYSTEM,
                                      after={"structured_items": len(items), "attachment_id": attachment_id})
        return len(items), found

    def link_events(self, case_id: int) -> list[str]:
        """로그의 오류 코드 문자열이 가이드·매뉴얼 원문에 그대로 나오면 연결 주장(FACT, 출처 GUIDE)을 만든다."""
        with closing(self.database.connect()) as conn:
            log_ids = CaseRepository(conn).log_file_ids(case_id)
            events = [e for file_events in EventRepository(conn).for_files(log_ids).values() for e in file_events]
            attachments = GuideRepository(conn).attachments(case_id)
        first_by_code = {}
        for event in events:
            if event.error_code and event.error_code not in first_by_code:
                first_by_code[event.error_code] = event
        linked: list[str] = []
        with closing(self.database.connect()) as conn, transaction(conn):
            claims = ClaimRepository(conn)
            claims.delete_proposed_for_case(case_id, "GUIDE")
            for attachment in attachments:
                text = read_attachment_text(self.paths, attachment["stored_path"])
                for code in sorted(mentioned_codes(text, set(first_by_code))):
                    start = text.lower().find(code.lower())
                    event = first_by_code[code]
                    claims.add(
                        claim_text=f"{KIND_LABEL[attachment['attachment_kind']]} '{attachment['original_name']}'이(가) "
                                   f"로그의 오류 코드 {code}를 언급함 (로그 {event.line_no}행)",
                        claim_kind="FACT", source="GUIDE", case_id=case_id,
                        evidence=[
                            EvidenceRef(evidence_type="GUIDE_SPAN", guide_attachment_id=attachment["id"],
                                        span_start=start, span_end=start + len(code)),
                            EvidenceRef(evidence_type="EVENT", event_id=event.id),
                        ],
                    )
                    linked.append(code)
        return sorted(set(linked))

    def suggest_fields(self, case_id: int) -> dict[str, str]:
        """가이드 원문의 증상·판정 기준 항목을 입력 칸 제안값으로(사용자가 고쳐서 저장)."""
        with closing(self.database.connect()) as conn:
            rows = GuideRepository(conn).items(case_id)
        out: dict[str, str] = {}
        for field_name, section in (("symptom", "SYMPTOM"), ("abnormal_criteria", "CRITERIA")):
            texts = [r["content"] for r in rows if r["section"] == section and r["status"] != "REJECTED"
                     and r["attachment_kind"] == "DIAGNOSTIC_GUIDE"]
            if texts:
                out[field_name] = "\n".join(texts)
        return out

    def restructure_with_ai(self, case_id: int, attachment_id: int,
                            cancel: CancelToken = NEVER_CANCEL) -> StructuredResult:
        self._check_editable(case_id)
        with closing(self.database.connect()) as conn:
            attachment = GuideRepository(conn).attachment(attachment_id)
        if attachment is None or attachment["case_id"] != case_id:
            raise CaseError("이 사례의 첨부가 아닙니다.")
        text = read_attachment_text(self.paths, attachment["stored_path"])
        segments = split_segments(text)
        with self.client_factory() as client:
            result, package = label_segments(client, self.settings.ollama.model, segments,
                                             attachment["attachment_kind"], cancel)
        if result.value is None:
            return result
        with closing(self.database.connect()) as conn, transaction(conn):
            guides = GuideRepository(conn)
            guides.delete_proposed_items_for_attachment(attachment_id)
            order: dict[str, int] = {}
            for label in result.value.labels:
                item = package.get(label.segment)
                order[label.section] = order.get(label.section, 0) + 1
                guides.add_item(case_id, section=label.section, content=item.text, source_kind="AI_PROPOSED",
                                attachment_id=attachment_id, span_start=item.ref["span_start"],
                                span_end=item.ref["span_end"], sort_order=order[label.section])
            AuditRepository(conn).add("case", case_id, AuditAction.UPDATE, Actor.AI,
                                      after={"ai_labeled_items": len(result.value.labels), "attachment_id": attachment_id})
        return result

    def attachment_text(self, attachment_id: int) -> str:
        with closing(self.database.connect()) as conn:
            attachment = GuideRepository(conn).attachment(attachment_id)
        return read_attachment_text(self.paths, attachment["stored_path"]) if attachment else ""
