"""참고 자료 올리기·고치기·승인·보관 (변경 요청 CR-04 REF-01·03).

- 원본은 attachments/references/에 읽기 전용 사본과 SHA-256으로 둔다. 같은 파일을 다시 올리면 기존 문서를 연다.
- 항목은 결정론으로 뽑는다(core.reference_structurer). 초안일 때만 고치기·더하기·제외·열 역할 바꾸기가 된다.
- **문서 단위로 승인**한다. 승인 전 문서는 분석·AI에 쓰이지 않는다(approved_reference_entries 뷰).
- 같은 제목의 새 버전을 승인하면 이전 승인 버전은 보관된다. 승인된 문서는 바꿀 수 없다(DB 트리거).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import ImportSettings
from app.core.importer import remove_stored, sha256_bytes, store_original
from app.core.reference_reader import ReadResult, ReferenceReadError, format_of, read_reference
from app.core.reference_structurer import ROLES, build_passages, extract_entries, normalize_code
from app.models import utc_now_iso
from app.models.enums import AuditAction
from app.storage.database import Database, transaction
from app.storage.paths import AppPaths
from app.storage.repositories import (
    REFERENCE_ENTRY_FIELDS,
    AuditRepository,
    ReferencePassageRepository,
    ReferenceRepository,
)

STATUS_LABELS = {"DRAFT": "초안", "APPROVED": "승인됨", "ARCHIVED": "보관"}


class ReferenceError(ValueError):
    """참고 자료 작업을 할 수 없음. 메시지는 화면에 그대로 보여 준다."""


@dataclass
class ImportOutcome:
    document_id: int
    duplicate: bool
    entries: int
    warnings: list[str]
    passages: int = 0  # 일반 문서 문단 수 (CR-07 REF-07)


class ReferenceLibrary:
    def __init__(self, database: Database, paths: AppPaths, settings: ImportSettings) -> None:
        self.database = database
        self.paths = paths
        self.settings = settings

    # ── 올리기 ──
    def import_file(self, path: Path, title: str | None = None) -> ImportOutcome:
        if format_of(path) is None:
            raise ReferenceError(f"지원하지 않는 형식입니다: {path.name} (TXT·MD·XLSX·PDF·DOCX)")
        size = path.stat().st_size
        if size > self.settings.max_file_mb * 1024 * 1024:
            raise ReferenceError(f"파일이 너무 큽니다: {size / 1024 / 1024:.1f}MB (한도 {self.settings.max_file_mb}MB)")
        data = path.read_bytes()
        try:
            result = read_reference(path, data)
        except ReferenceReadError as exc:
            raise ReferenceError(str(exc)) from exc
        digest = sha256_bytes(data)
        with closing(self.database.connect()) as conn:
            existing = ReferenceRepository(conn).by_sha256(digest)
        if existing is not None:
            return ImportOutcome(int(existing["id"]), True, 0, ["이미 올린 파일입니다. 기존 문서를 엽니다."])
        stored = store_original(data, path.name, self.paths.references, self.paths.root)
        entries, column_maps = extract_entries(result)
        passages = build_passages(result, entries)
        try:
            with closing(self.database.connect()) as conn, transaction(conn):
                refs = ReferenceRepository(conn)
                document_id = refs.add_document(
                    title=(title or path.stem).strip() or path.name, original_name=path.name, source_path=str(path),
                    stored_path=stored.stored_path, sha256=digest, size_bytes=len(data), fmt=result.format,
                    column_map=column_maps or None)
                self._add_extracted(refs, document_id, entries)
                passage_repo = ReferencePassageRepository(conn)
                for order, (locator, body) in enumerate(passages):
                    passage_repo.add(document_id, body=body, locator=locator, sort_order=order)
                AuditRepository(conn).add("reference_document", document_id, AuditAction.IMPORT,
                                          after={"name": path.name, "format": result.format, "entries": len(entries),
                                                 "passages": len(passages)})
        except Exception:
            if stored.created:
                remove_stored(stored.absolute)
            raise
        warnings = list(result.warnings)
        if not entries and passages:
            warnings.append("코드 항목은 없고 일반 문서로 읽었습니다. 승인하면 분석할 때 로그와 관련 있는 문단을 참고합니다.")
        elif not entries:
            warnings.append("코드 항목을 찾지 못했습니다. 표라면 열 역할을 고르고, 아니면 항목을 직접 더하세요.")
        return ImportOutcome(document_id, False, len(entries), warnings, len(passages))

    @staticmethod
    def _add_extracted(refs: ReferenceRepository, document_id: int, entries) -> None:
        for order, entry in enumerate(entries):
            refs.add_entry(document_id, code=entry.code.strip(), code_norm=normalize_code(entry.code),
                           source_kind="EXTRACTED", sort_order=order,
                           **{name: getattr(entry, name) for name in REFERENCE_ENTRY_FIELDS if name != "code"})

    # ── 조회 ──
    def documents(self, include_archived: bool = False) -> list[sqlite3.Row]:
        statuses = None if include_archived else ("DRAFT", "APPROVED")
        with closing(self.database.connect()) as conn:
            return ReferenceRepository(conn).documents(statuses)

    def document(self, document_id: int) -> sqlite3.Row:
        with closing(self.database.connect()) as conn:
            row = ReferenceRepository(conn).document(document_id)
        if row is None:
            raise ReferenceError(f"참고 자료 {document_id}이(가) 없습니다.")
        return row

    def entries(self, document_id: int) -> list[sqlite3.Row]:
        with closing(self.database.connect()) as conn:
            return ReferenceRepository(conn).entries(document_id)

    def read(self, document_id: int) -> ReadResult:
        """보관한 사본을 다시 읽는다(미리보기·열 역할 바꾸기)."""
        row = self.document(document_id)
        path = self.paths.root / row["stored_path"]
        return read_reference(Path(row["original_name"]), path.read_bytes())

    def passages(self, document_id: int) -> list[sqlite3.Row]:
        with closing(self.database.connect()) as conn:
            return ReferencePassageRepository(conn).for_document(document_id)

    def column_maps(self, document_id: int) -> dict[str, dict]:
        raw = self.document(document_id)["column_map_json"]
        return json.loads(raw) if raw else {}

    # ── 초안 고치기 ──
    def _draft(self, conn, document_id: int) -> sqlite3.Row:
        row = ReferenceRepository(conn).document(document_id)
        if row is None:
            raise ReferenceError(f"참고 자료 {document_id}이(가) 없습니다.")
        if row["status"] != "DRAFT":
            raise ReferenceError("승인·보관된 참고 자료는 고칠 수 없습니다. 고친 파일을 새로 올리세요(새 버전).")
        return row

    def rename(self, document_id: int, title: str) -> None:
        if not title.strip():
            raise ReferenceError("제목을 입력하세요.")
        with closing(self.database.connect()) as conn, transaction(conn):
            self._draft(conn, document_id)
            ReferenceRepository(conn).update_document(document_id, title=title.strip())

    def set_column_map(self, document_id: int, table_name: str, mapping: dict[str, Any]) -> int:
        """표 하나의 열 역할을 바꾸고 그 문서의 뽑은 항목을 다시 만든다(직접 더한 항목은 남김). 항목 수를 돌려준다."""
        if mapping.get("code") is None:
            raise ReferenceError("코드 열을 골라 주세요.")
        result = self.read(document_id)
        maps = self.column_maps(document_id)
        maps[table_name] = {k: v for k, v in mapping.items() if k == "header_row" or (k in ROLES and v is not None)}
        entries, used = extract_entries(result, maps)
        with closing(self.database.connect()) as conn, transaction(conn):
            self._draft(conn, document_id)
            refs = ReferenceRepository(conn)
            refs.delete_entries(document_id, "EXTRACTED")
            self._add_extracted(refs, document_id, entries)
            refs.update_document(document_id, column_map=used or None)
            AuditRepository(conn).add("reference_document", document_id, AuditAction.UPDATE,
                                      after={"column_map": maps[table_name], "table": table_name, "entries": len(entries)})
        return len(entries)

    def update_entry(self, entry_id: int, **fields: Any) -> None:
        allowed = {k: (v.strip() if isinstance(v, str) else v) or None for k, v in fields.items()
                   if k in REFERENCE_ENTRY_FIELDS or k == "status"}
        if "code" in allowed:
            if not allowed["code"]:
                raise ReferenceError("코드는 비울 수 없습니다.")
            allowed["code_norm"] = normalize_code(allowed["code"])
        if "status" in allowed and allowed["status"] not in ("INCLUDED", "EXCLUDED"):
            raise ReferenceError("포함·제외만 고를 수 있습니다.")
        with closing(self.database.connect()) as conn, transaction(conn):
            entry = ReferenceRepository(conn).entry(entry_id)
            if entry is None:
                raise ReferenceError(f"항목 {entry_id}이(가) 없습니다.")
            self._draft(conn, entry["document_id"])
            ReferenceRepository(conn).update_entry(entry_id, **allowed)

    def add_entry(self, document_id: int, code: str, **fields: Any) -> int:
        if not code.strip():
            raise ReferenceError("코드를 입력하세요.")
        with closing(self.database.connect()) as conn, transaction(conn):
            self._draft(conn, document_id)
            refs = ReferenceRepository(conn)
            order = len(refs.entries(document_id))
            return refs.add_entry(document_id, code=code.strip(), code_norm=normalize_code(code), source_kind="USER",
                                  sort_order=order, **{k: (v.strip() or None) if isinstance(v, str) else v
                                                       for k, v in fields.items() if k in REFERENCE_ENTRY_FIELDS})

    def delete_entry(self, entry_id: int) -> None:
        with closing(self.database.connect()) as conn, transaction(conn):
            entry = ReferenceRepository(conn).entry(entry_id)
            if entry is None:
                return
            self._draft(conn, entry["document_id"])
            ReferenceRepository(conn).delete_entry(entry_id)

    # ── 승인·보관·삭제 ──
    def approve(self, document_id: int) -> list[int]:
        """문서를 승인한다. 같은 제목의 이전 승인 버전은 보관하고 그 id 목록을 돌려준다."""
        with closing(self.database.connect()) as conn, transaction(conn):
            row = self._draft(conn, document_id)
            if not row["included_count"] and not row["passage_count"]:
                raise ReferenceError("포함한 코드 항목도, 읽은 문단도 없습니다. 항목을 더하거나 '포함'으로 바꾼 뒤 승인하세요.")
            refs = ReferenceRepository(conn)
            older = [int(r["id"]) for r in refs.approved_with_title(row["title"]) if int(r["id"]) != document_id]
            now = utc_now_iso()
            for old_id in older:
                refs.update_document(old_id, status="ARCHIVED", archived_at=now)
                AuditRepository(conn).add("reference_document", old_id, AuditAction.ARCHIVE,
                                          after={"replaced_by": document_id})
            refs.update_document(document_id, status="APPROVED", approved_at=now,
                                 replaces_id=older[0] if older else None)
            AuditRepository(conn).add("reference_document", document_id, AuditAction.APPROVE,
                                      after={"entries": row["included_count"], "passages": row["passage_count"],
                                             "replaces": older})
            return older

    def archive(self, document_id: int) -> None:
        with closing(self.database.connect()) as conn, transaction(conn):
            row = ReferenceRepository(conn).document(document_id)
            if row is None or row["status"] == "ARCHIVED":
                return
            ReferenceRepository(conn).update_document(document_id, status="ARCHIVED", archived_at=utc_now_iso())
            AuditRepository(conn).add("reference_document", document_id, AuditAction.ARCHIVE)

    def delete_draft(self, document_id: int) -> None:
        """초안만 지운다. 보관한 사본도 함께 지운다(아직 분석에 쓰인 적 없음)."""
        with closing(self.database.connect()) as conn, transaction(conn):
            row = self._draft(conn, document_id)
            ReferenceRepository(conn).delete_document(document_id)
            AuditRepository(conn).add("reference_document", document_id, AuditAction.DELETE,
                                      before={"name": row["original_name"]})
        path = self.paths.root / row["stored_path"]
        if path.exists():
            remove_stored(path)
