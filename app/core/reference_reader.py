"""참고 자료 읽기 (변경 요청 CR-04 REF-01). TXT·MD·XLSX·PDF·DOCX → 표와 글 줄.

결정론만 쓴다. 네트워크·프로세스 실행 없음. 원본 바이트는 바꾸지 않는다(읽기만).
- TXT·MD: 로그와 같은 인코딩 판정(decode_log_bytes). MD의 `| 표 |`는 표로 읽는다.
- XLSX: openpyxl(읽기 전용, 저장된 값). 시트마다 표 하나.
- DOCX: 표준 라이브러리(zip + XML). 문단은 글 줄, 표는 표.
- PDF: pypdf 글자 추출. 쪽마다 줄. 글자가 없으면(스캔 이미지) 알려 준다. 암호 PDF는 읽지 않는다.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from app.core.decoding import BinaryFileError, decode_log_bytes

FORMATS = {".txt": "TXT", ".md": "MD", ".markdown": "MD", ".xlsx": "XLSX", ".pdf": "PDF", ".docx": "DOCX"}
MAX_XML_BYTES = 64 * 1024 * 1024  # DOCX 본문 XML 상한(압축 폭탄 방지)
MAX_ROWS = 20_000
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MD_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


class ReferenceReadError(ValueError):
    """읽을 수 없는 참고 자료. 메시지는 화면에 그대로 보여 준다."""


@dataclass
class Table:
    name: str
    rows: list[list[str]]
    row_locators: list[str]


@dataclass
class TextLine:
    text: str
    locator: str


@dataclass
class ReadResult:
    format: str
    tables: list[Table] = field(default_factory=list)
    lines: list[TextLine] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def preview(self, limit: int = 40_000) -> str:
        """미리보기용 글: 표는 ' | '로 이은 행, 글은 줄 그대로."""
        parts: list[str] = []
        for table in self.tables:
            parts.append(f"[{table.name}]")
            parts.extend(" | ".join(cell for cell in row) for row in table.rows)
            parts.append("")
        parts.extend(line.text for line in self.lines)
        text = "\n".join(parts)
        return text if len(text) <= limit else text[:limit] + "\n…(미리보기는 여기까지)"


def format_of(path: Path) -> str | None:
    return FORMATS.get(path.suffix.lower())


def read_reference(path: Path, data: bytes | None = None) -> ReadResult:
    fmt = format_of(path)
    if fmt is None:
        raise ReferenceReadError(f"지원하지 않는 형식입니다: {path.suffix or '(확장자 없음)'} "
                                 "(TXT·MD·XLSX·PDF·DOCX만 받습니다. 옛 형식 .xls·.doc은 새 형식으로 저장해 올리세요.)")
    raw = path.read_bytes() if data is None else data
    reader = {"TXT": _read_text, "MD": _read_markdown, "XLSX": _read_xlsx, "DOCX": _read_docx, "PDF": _read_pdf}[fmt]
    result = reader(raw)
    result.format = fmt
    if not result.tables and not any(line.text.strip() for line in result.lines):
        raise ReferenceReadError("문서에서 글자를 찾지 못했습니다." +
                                 (" 스캔한 이미지 PDF는 글자가 없어 읽을 수 없습니다." if fmt == "PDF" else ""))
    return result


# ── TXT·MD ──
def _decode(raw: bytes) -> tuple[list[str], list[str]]:
    try:
        decoded = decode_log_bytes(raw)
    except BinaryFileError as exc:
        raise ReferenceReadError("텍스트 파일이 아닙니다(바이너리).") from exc
    return [line.text for line in decoded.lines], decoded.warnings


def _read_text(raw: bytes) -> ReadResult:
    lines, warnings = _decode(raw)
    return ReadResult("TXT", lines=[TextLine(text, f"{n}행") for n, text in enumerate(lines, start=1)],
                      warnings=warnings)


def _split_md_row(text: str) -> list[str]:
    inner = text.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [cell.strip() for cell in inner.split("|")]


def _read_markdown(raw: bytes) -> ReadResult:
    lines, warnings = _decode(raw)
    result = ReadResult("MD", warnings=warnings)
    index = 0
    while index < len(lines):
        text = lines[index]
        nxt = lines[index + 1] if index + 1 < len(lines) else ""
        if text.strip().startswith("|") and _MD_SEPARATOR.match(nxt):
            rows, locators = [_split_md_row(text)], [f"{index + 1}행"]
            index += 2
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(_split_md_row(lines[index]))
                locators.append(f"{index + 1}행")
                index += 1
            result.tables.append(Table(f"표 {len(result.tables) + 1}", rows, locators))
            continue
        result.lines.append(TextLine(text, f"{index + 1}행"))
        index += 1
    return result


# ── XLSX ──
def _cell_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _read_xlsx(raw: bytes) -> ReadResult:
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl은 여러 종류의 예외를 낸다(손상·암호·형식)
        raise ReferenceReadError(f"엑셀 파일을 열지 못했습니다(손상되었거나 암호가 걸렸을 수 있음): {exc}") from exc
    result = ReadResult("XLSX")
    try:
        for sheet in workbook.worksheets:
            rows, locators = [], []
            for number, values in enumerate(sheet.iter_rows(values_only=True), start=1):
                if number > MAX_ROWS:
                    result.warnings.append(f"시트 '{sheet.title}'가 {MAX_ROWS:,}행을 넘어 뒤쪽은 읽지 않았습니다.")
                    break
                cells = [_cell_text(v) for v in values]
                if any(cells):
                    rows.append(cells)
                    locators.append(f"{sheet.title} {number}행")
            if rows:
                width = max(len(r) for r in rows)
                while width > 0 and not any(len(r) >= width and r[width - 1] for r in rows):
                    width -= 1
                result.tables.append(Table(sheet.title, [(r + [""] * width)[:width] for r in rows], locators))
    finally:
        workbook.close()
    return result


# ── DOCX ──
def _paragraph_text(element: ElementTree.Element) -> str:
    parts = []
    for node in element.iter():
        if node.tag == f"{_W}t" and node.text:
            parts.append(node.text)
        elif node.tag == f"{_W}tab":
            parts.append("\t")
        elif node.tag in (f"{_W}br", f"{_W}cr"):
            parts.append("\n")
    return "".join(parts)


def _read_docx(raw: bytes) -> ReadResult:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > MAX_XML_BYTES:
                raise ReferenceReadError("워드 문서 본문이 너무 큽니다.")
            xml = archive.read(info)
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ReferenceReadError("워드(.docx) 파일을 열지 못했습니다(손상되었거나 다른 형식).") from exc
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise ReferenceReadError("워드 문서 본문을 읽지 못했습니다.") from exc
    body = root.find(f"{_W}body")
    result = ReadResult("DOCX")
    paragraph_no = 0
    for child in list(body) if body is not None else []:
        if child.tag == f"{_W}p":
            paragraph_no += 1
            for piece in _paragraph_text(child).split("\n"):
                result.lines.append(TextLine(piece, f"문단 {paragraph_no}"))
        elif child.tag == f"{_W}tbl":
            name = f"표 {len(result.tables) + 1}"
            rows, locators = [], []
            for number, row in enumerate(child.findall(f"{_W}tr"), start=1):
                cells = [" ".join(_paragraph_text(p) for p in cell.findall(f"{_W}p")).strip()
                         for cell in row.findall(f"{_W}tc")]
                if any(cells):
                    rows.append(cells)
                    locators.append(f"{name} {number}행")
            if rows:
                width = max(len(r) for r in rows)
                result.tables.append(Table(name, [r + [""] * (width - len(r)) for r in rows], locators))
    return result


# ── PDF ──
def _read_pdf(raw: bytes) -> ReadResult:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ReferenceReadError("암호가 걸린 PDF는 읽을 수 없습니다. 암호를 푼 사본을 올리세요.")
        result = ReadResult("PDF")
        for number, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            for line in text.splitlines():
                result.lines.append(TextLine(line, f"{number}쪽"))
    except ReferenceReadError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError) as exc:
        raise ReferenceReadError(f"PDF를 읽지 못했습니다(손상되었을 수 있음): {exc}") from exc
    if result.lines:
        result.warnings.append("PDF는 글자만 뽑으므로 표가 깨질 수 있습니다. 항목과 원문을 확인하고 고친 뒤 승인하세요.")
    return result
