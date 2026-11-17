"""로그·가이드 파일 가져오기 (계획서 §3, §4.2, §9.1, §11 Phase 2).

로그 한 파일의 순서:
  1) 확장자·크기 검사(파일당·총량)  2) SHA-256  3) 중복이면 기존 행 재사용
  4) 인코딩 판정·줄 나누기  5) 사본 저장(읽기 전용) 후 사본을 다시 해시해 원본과 같은지 확인
  6) 파싱  7) 한 트랜잭션으로 파일·이벤트·감사 이력 저장
원본 파일은 읽기만 한다. 실패하거나 취소하면 이번에 만든 사본을 지운다.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import stat
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.config import ImportSettings
from app.core.cancel import NEVER_CANCEL, Cancelled, CancelToken
from app.core.decoding import BinaryFileError, DecodedText, decode_log_bytes
from app.models.enums import Actor, AuditAction, EventTimeKind, OriginPurpose, TimestampBasis
from app.models.records import ImportedFile, ImportRequest, ImportResult, RejectedFile, SessionMode
from app.parsers.parser_registry import ParserRegistry, UnknownProfileError
from app.storage.database import Database, transaction
from app.storage.paths import AppPaths
from app.storage.repositories import (
    AuditRepository,
    EventRepository,
    GuideRepository,
    LogFileRepository,
    SessionRepository,
)

logger = logging.getLogger("app.core.importer")
ProgressFn = Callable[[int, int, str], None]
MB = 1024 * 1024


class ImportRejected(ValueError):
    """요청 전체를 받을 수 없음(총량 초과, 세션 없음 등)."""


@dataclass(frozen=True)
class StoredFile:
    stored_path: str  # data_root 기준 상대 경로
    absolute: Path
    sha256: str
    size_bytes: int
    created: bool  # 이번 호출에서 사본을 새로 만들었나


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def destination_dir(paths: AppPaths, purpose: OriginPurpose) -> Path:
    day = datetime.now().strftime("%Y%m%d")
    base = {
        OriginPurpose.NORMAL_CASE: paths.cases_normal,
        OriginPurpose.ABNORMAL_CASE: paths.cases_abnormal,
        OriginPurpose.ANALYSIS: paths.analysis / "inputs",
    }[purpose]
    return base / day


def store_original(data: bytes, original_name: str, dest_dir: Path, root: Path) -> StoredFile:
    """원본 바이트를 `<sha 앞 12자>_<원래 이름>`으로 저장하고 읽기 전용으로 만든다. 저장 후 다시 해시해 확인한다."""
    digest = sha256_bytes(data)
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(ch if ch not in '<>:"/\\|?*' else "_" for ch in original_name)
    target = dest_dir / f"{digest[:12]}_{safe_name}"
    created = False
    if target.exists():
        if sha256_bytes(target.read_bytes()) != digest:
            raise OSError(f"보관 위치에 다른 내용의 파일이 이미 있습니다: {target}")
    else:
        with open(target, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        created = True
        os.chmod(target, stat.S_IREAD)
        if sha256_bytes(target.read_bytes()) != digest:
            remove_stored(target)
            raise OSError("보관한 사본의 해시가 원본과 다릅니다. 저장을 취소했습니다.")
    return StoredFile(target.relative_to(root).as_posix(), target, digest, len(data), created)


def remove_stored(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("사본을 지우지 못했습니다: %s", path)


class LogImporter:
    def __init__(self, database: Database, paths: AppPaths, settings: ImportSettings,
                 registry: ParserRegistry | None = None) -> None:
        self.database = database
        self.paths = paths
        self.settings = settings
        self.registry = registry or ParserRegistry.default()

    def import_logs(self, request: ImportRequest, *, cancel: CancelToken = NEVER_CANCEL,
                    progress: ProgressFn | None = None) -> ImportResult:
        result = ImportResult()
        candidates = self._validate(request, result)
        total = len(candidates)
        session_for_all: int | None = request.existing_session_id if request.session_mode == SessionMode.EXISTING else None
        if request.session_mode == SessionMode.EXISTING:
            self._require_session(request.existing_session_id)

        for index, (path, data) in enumerate(candidates):
            if progress:
                progress(index, total, f"{path.name} 가져오는 중")
            try:
                cancel.raise_if_cancelled()
                imported, session_for_all = self._import_one(path, data, request, session_for_all, cancel)
                result.files.append(imported)
            except Cancelled:
                result.cancelled = True
                break
            except (BinaryFileError, UnknownProfileError, UnicodeDecodeError, OSError, sqlite3.Error) as exc:
                result.rejected.append(RejectedFile(path=str(path), reason=str(exc)))
                logger.warning("가져오기 실패 %s: %s", path.name, exc)
        if progress:
            progress(total if not result.cancelled else len(result.files), total,
                     "취소했습니다" if result.cancelled else "가져오기를 마쳤습니다")
        return result

    # ── 검사 ──
    def _validate(self, request: ImportRequest, result: ImportResult) -> list[tuple[Path, bytes]]:
        allowed = set(self.settings.log_extensions)
        max_file = self.settings.max_file_mb * MB
        accepted: list[tuple[Path, bytes]] = []
        for raw_path in request.paths:
            path = Path(raw_path)
            if path.suffix.lower() not in allowed:
                result.rejected.append(RejectedFile(path=str(path), reason=f"허용하지 않는 확장자입니다({', '.join(sorted(allowed))})"))
                continue
            try:
                size = path.stat().st_size
            except OSError as exc:
                result.rejected.append(RejectedFile(path=str(path), reason=f"파일을 열 수 없습니다: {exc}"))
                continue
            if size == 0:
                result.rejected.append(RejectedFile(path=str(path), reason="빈 파일입니다"))
                continue
            if size > max_file:
                result.rejected.append(RejectedFile(
                    path=str(path), reason=f"파일이 {self.settings.max_file_mb:g}MB를 넘습니다({size / MB:.1f}MB)"))
                continue
            accepted.append((path, path.read_bytes()))
        total_bytes = sum(len(data) for _, data in accepted)
        if total_bytes > self.settings.max_total_mb * MB:
            raise ImportRejected(
                f"한 번에 가져올 총 용량 {self.settings.max_total_mb:g}MB를 넘습니다({total_bytes / MB:.1f}MB). "
                "나눠서 가져오거나 설정에서 한도를 바꾸세요."
            )
        return accepted

    def _require_session(self, session_id: int | None) -> None:
        if session_id is None:
            raise ImportRejected("추가할 세션을 고르지 않았습니다.")
        with closing(self.database.connect()) as conn:
            if SessionRepository(conn).get(session_id) is None:
                raise ImportRejected(f"세션 {session_id}이(가) 없습니다.")

    # ── 한 파일 ──
    def _import_one(self, path: Path, data: bytes, request: ImportRequest, session_for_all: int | None,
                    cancel: CancelToken) -> tuple[ImportedFile, int | None]:
        digest = sha256_bytes(data)
        conn = self.database.connect()
        try:
            existing = LogFileRepository(conn).find_by_sha256(digest)
            if existing is not None:
                warnings = ["같은 내용의 파일이 이미 있어 기존 기록을 그대로 씁니다."]
                if session_for_all is not None and existing.session_id != session_for_all:
                    warnings.append(f"이 파일은 이미 세션 '{existing.session_label}'에 속해 있어 세션을 바꾸지 않았습니다.")
                return ImportedFile(
                    log_file_id=existing.id, session_id=existing.session_id, original_name=existing.original_name,
                    sha256=digest, size_bytes=existing.size_bytes, encoding=existing.encoding,
                    line_count=existing.line_count, event_count=EventRepository(conn).count(existing.id),
                    parser_profile=existing.parser_profile, timestamp_basis=existing.timestamp_basis,
                    duplicate=True, warnings=warnings,
                ), session_for_all
        finally:
            conn.close()

        decoded = decode_log_bytes(data)
        parser = self.registry.select([line.text for line in decoded.lines[:200]], request.profile)
        stored = store_original(data, path.name, destination_dir(self.paths, request.purpose), self.paths.root)
        try:
            events = list(parser.parse(decoded.lines, cancel))
            cancel.raise_if_cancelled()
            basis = self._timestamp_basis(parser.timestamp_basis, events)
            conn = self.database.connect()
            try:
                with transaction(conn):
                    session_id = session_for_all
                    if session_id is None:
                        session_id = self._new_session(conn, request, path)
                        if request.session_mode == SessionMode.SAME:
                            session_for_all = session_id
                    files = LogFileRepository(conn)
                    log_file_id = files.add(
                        session_id=session_id, original_name=path.name, source_path=str(path),
                        stored_path=stored.stored_path, sha256=digest, size_bytes=len(data),
                        encoding=decoded.encoding, line_count=decoded.line_count,
                        log_role=request.meta.log_role, collected_at=request.meta.collected_at,
                        timestamp_basis=basis.value, fw_version=request.meta.fw_version,
                        user_memo=request.meta.user_memo, parser_profile=parser.name,
                        origin_purpose=request.purpose.value,
                    )
                    count = EventRepository(conn).add_many(log_file_id, events, parser.name)
                    AuditRepository(conn).add(
                        "log_file", log_file_id, AuditAction.IMPORT, Actor.USER,
                        after={"name": path.name, "sha256": digest, "events": count, "parser": parser.name},
                    )
            finally:
                conn.close()
        except BaseException:
            if stored.created:
                remove_stored(stored.absolute)
            raise
        warnings = list(decoded.warnings)
        if parser.name == self.registry.fallback.name:
            warnings.append("맞는 파서 프로파일이 없어 한 줄을 이벤트 하나로 읽었습니다. 타임스탬프 기준은 '모름'입니다.")
        unparsed = sum(1 for e in events if e.attrs and e.attrs.get("unparsed"))
        if unparsed:
            warnings.append(f"형식에 맞지 않는 줄 묶음 {unparsed}개를 원문 그대로 이벤트로 남겼습니다.")
        return ImportedFile(
            log_file_id=log_file_id, session_id=session_id, original_name=path.name, sha256=digest,
            size_bytes=len(data), encoding=decoded.encoding, line_count=decoded.line_count, event_count=count,
            parser_profile=parser.name, timestamp_basis=basis, warnings=warnings,
        ), session_for_all

    def _new_session(self, conn: sqlite3.Connection, request: ImportRequest, path: Path) -> int:
        label = request.session_label or datetime.now().strftime("%Y-%m-%d %H:%M 수집")
        if request.session_mode != SessionMode.SAME:
            label = f"{label} · {path.name}"
        return SessionRepository(conn).create(
            label, relation_confirmed=request.session_mode in (SessionMode.SAME, SessionMode.SEPARATE))

    @staticmethod
    def _timestamp_basis(declared: TimestampBasis, events: list) -> TimestampBasis:
        if declared in (TimestampBasis.UNKNOWN, TimestampBasis.NONE):
            return declared
        kinds = {e.ts_kind for e in events if e.ts_kind != EventTimeKind.NONE}
        if not kinds:
            return TimestampBasis.UNKNOWN  # 형식은 맞았지만 시각을 하나도 읽지 못함
        if declared == TimestampBasis.ABSOLUTE and EventTimeKind.ABSOLUTE_NO_TZ in kinds:
            return TimestampBasis.ABSOLUTE_NO_TZ
        return declared


@dataclass(frozen=True)
class AttachmentResult:
    attachment_id: int
    text: str
    encoding: str
    duplicate: bool


def import_attachment(database: Database, paths: AppPaths, settings: ImportSettings, case_id: int, path: Path,
                      kind: str) -> AttachmentResult:
    """진단 가이드(DIAGNOSTIC_GUIDE, 필수)·대응 매뉴얼(RESPONSE_MANUAL, 선택, CR-01) 첨부.
    원본은 attachments/guides/에 읽기 전용 사본으로 두고, 같은 사례에 같은 파일이면 기존 첨부를 쓴다."""
    path = Path(path)
    allowed = settings.guide_extensions if kind == "DIAGNOSTIC_GUIDE" else settings.manual_extensions
    if path.suffix.lower() not in allowed:
        raise ImportRejected(f"허용하지 않는 확장자입니다({', '.join(allowed)}): {path.name}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ImportRejected(f"파일을 열 수 없습니다: {exc}") from exc
    if not data:
        raise ImportRejected("빈 파일입니다.")
    if len(data) > settings.max_file_mb * MB:
        raise ImportRejected(f"파일이 {settings.max_file_mb:g}MB를 넘습니다.")
    decoded = decode_log_bytes(data)  # 바이너리면 BinaryFileError
    digest = sha256_bytes(data)
    with closing(database.connect()) as conn:

        for row in GuideRepository(conn).attachments(case_id, kind):
            if row["sha256"] == digest:
                return AttachmentResult(int(row["id"]), decoded.text(), decoded.encoding, True)
    stored = store_original(data, path.name, paths.guides, paths.root)
    try:
        with closing(database.connect()) as conn, transaction(conn):
            attachment_id = GuideRepository(conn).add_attachment(
                case_id, kind, original_name=path.name, source_path=str(path), stored_path=stored.stored_path,
                sha256=digest, size_bytes=len(data), encoding=decoded.encoding,
                format="MD" if path.suffix.lower() == ".md" else "TXT",
            )
            AuditRepository(conn).add("guide_attachment", attachment_id, AuditAction.IMPORT, Actor.USER,
                                      after={"case_id": case_id, "kind": kind, "name": path.name, "sha256": digest})
    except BaseException:
        if stored.created:
            remove_stored(stored.absolute)
        raise
    return AttachmentResult(attachment_id, decoded.text(), decoded.encoding, False)


def read_attachment_text(paths: AppPaths, stored_path: str) -> str:
    return decode_log_bytes((paths.root / stored_path).read_bytes()).text()


def read_decoded(paths: AppPaths, stored_path: str) -> DecodedText:
    """보관한 사본을 다시 읽어 줄 목록으로. 화면의 원본 보기에 쓴다."""
    return decode_log_bytes((paths.root / stored_path).read_bytes())
