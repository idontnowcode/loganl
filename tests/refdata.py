"""참고 자료 시험용 파일 만들기 (CR-04). 모든 코드·설명은 가상이다(TEST- 접두어, '가상 설명')."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

ROWS = [
    ("코드", "명칭", "설명", "원인", "조치"),
    ("TEST-E001", "가상 오류 A", "가상 설명 A", "가상 원인 A", "가상 조치 A"),
    ("TEST-E002", "가상 오류 B", "가상 설명 B", "", "가상 조치 B"),
    ("TEST-W010", "가상 경고", "가상 설명 W", "", ""),
]

TEXT_DOC = """가상 코드 사전 (시험용, 실제 장비 의미 아님)

TEST-E001: 가상 설명 A
TEST-E002 - 가상 설명 B
1. 번호 목록은 코드가 아님

## TEST-E003 가상 오류 C
가상 설명 C 첫 줄
가상 설명 C 둘째 줄
원인: 가상 원인 C
조치: 가상 조치 C

## 참고
2026-11-17: 날짜는 코드가 아님
"""


def write_xlsx(path: Path, rows=ROWS, sheet: str = "코드표") -> Path:
    from openpyxl import Workbook

    workbook = Workbook()
    ws = workbook.active
    ws.title = sheet
    for row in rows:
        ws.append(list(row))
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    return path


def docx_bytes(paragraphs: list[str], table: list[tuple[str, ...]] | None = None) -> bytes:
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(f"<w:p><w:r><w:t xml:space=\"preserve\">{escape(p)}</w:t></w:r></w:p>" for p in paragraphs)
    if table:
        rows = "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{escape(c)}</w:t></w:r></w:p></w:tc>" for c in row)
                       + "</w:tr>" for row in table)
        body += f"<w:tbl>{rows}</w:tbl>"
    document = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {w}><w:body>{body}</w:body></w:document>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def pdf_bytes(lines: list[str]) -> bytes:
    """글자(ASCII)가 든 최소 PDF. pypdf가 글자를 뽑을 수 있다."""
    stream_lines = ["BT", "/F1 12 Tf", "14 TL", "50 750 Td"]
    for line in lines:
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream_lines.append(f"({safe}) Tj T*")
    stream_lines.append("ET")
    stream = "\n".join(stream_lines).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def write_bytes(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path
