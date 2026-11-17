"""CR-04 참고 자료 읽기·항목 뽑기 (REF-01·02). 모든 코드·설명은 가상이다."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.reference_reader import ReferenceReadError, Table, format_of, read_reference
from app.core.reference_structurer import (
    entries_from_lines,
    extract_entries,
    guess_column_map,
    looks_like_code,
    normalize_code,
)
from tests.refdata import TEXT_DOC, docx_bytes, pdf_bytes, write_bytes, write_xlsx


def test_formats() -> None:
    assert format_of(Path("a.XLSX")) == "XLSX" and format_of(Path("b.markdown")) == "MD"
    assert format_of(Path("c.xls")) is None
    with pytest.raises(ReferenceReadError, match="새 형식"):
        read_reference(Path("c.xls"), b"x")


def test_code_rules() -> None:
    assert looks_like_code("TEST-E001") and looks_like_code("1001") and looks_like_code("0x1A2B")
    assert not looks_like_code("12")  # 숫자만이면 3자 이상
    assert not looks_like_code("ERROR")  # 숫자가 없으면 코드가 아님
    assert not looks_like_code("2026-11-17")  # 날짜
    assert normalize_code(" test-e001 ") == "TEST-E001" and normalize_code("E 001") == "E001"


def test_xlsx_table_and_header_roles(tmp_path: Path) -> None:
    result = read_reference(write_xlsx(tmp_path / "codes.xlsx"))
    assert result.format == "XLSX" and len(result.tables) == 1
    table = result.tables[0]
    assert table.name == "코드표" and table.row_locators[1] == "코드표 2행"
    mapping = guess_column_map(table)
    assert mapping == {"header_row": 0, "code": 0, "title": 1, "description": 2, "cause": 3, "action": 4}
    entries, used = extract_entries(result)
    assert [e.code for e in entries] == ["TEST-E001", "TEST-E002", "TEST-W010"]
    first = entries[0]
    assert (first.title, first.description, first.cause, first.action) == ("가상 오류 A", "가상 설명 A", "가상 원인 A", "가상 조치 A")
    assert entries[1].cause is None and first.locator == "코드표 2행" and "TEST-E001 | 가상 오류 A" in first.source_text
    assert used == {"코드표": mapping}


def test_header_word_priority() -> None:
    table = Table("t", [["에러코드", "알람명", "조치 내용", "상세 설명"], ["TEST-E001", "a", "b", "c"]], ["1", "2"])
    assert guess_column_map(table) == {"header_row": 0, "code": 0, "title": 1, "action": 2, "description": 3}
    headless = Table("t", [["TEST-E001", "가상 설명"], ["TEST-E002", "가상 설명"], ["TEST-E003", "가상"]], ["1", "2", "3"])
    assert guess_column_map(headless) == {"header_row": -1, "code": 0, "description": 1}
    prose = Table("t", [["가상 문장", "다른 문장"], ["또 문장", "끝"]], ["1", "2"])
    assert guess_column_map(prose) is None


def test_user_column_map_overrides(tmp_path: Path) -> None:
    result = read_reference(write_xlsx(tmp_path / "codes.xlsx"))
    entries, _ = extract_entries(result, {"코드표": {"header_row": 0, "code": 0, "description": 1}})
    assert entries[0].description == "가상 오류 A" and entries[0].action is None


def test_text_lines_and_heading_blocks(tmp_path: Path) -> None:
    result = read_reference(write_bytes(tmp_path / "codes.txt", TEXT_DOC.encode("utf-8")))
    entries = entries_from_lines(result.lines)
    assert [e.code for e in entries] == ["TEST-E001", "TEST-E002", "TEST-E003"]
    assert entries[0].description == "가상 설명 A" and entries[0].locator == "3행"
    block = entries[2]
    assert block.title == "가상 오류 C" and block.description == "가상 설명 C 첫 줄 가상 설명 C 둘째 줄"
    assert (block.cause, block.action) == ("가상 원인 C", "가상 조치 C") and block.locator == "7행"


def test_markdown_table(tmp_path: Path) -> None:
    text = "# 가상 표\n\n| 코드 | 설명 | 조치 |\n|---|---|---|\n| TEST-E001 | 가상 설명 A | 가상 조치 A |\n\nTEST-E009: 표 밖 줄\n"
    result = read_reference(write_bytes(tmp_path / "codes.md", text.encode("utf-8")))
    assert len(result.tables) == 1 and result.tables[0].row_locators == ["3행", "5행"]
    entries, _ = extract_entries(result)
    assert [(e.code, e.description, e.action) for e in entries] == [
        ("TEST-E001", "가상 설명 A", "가상 조치 A"), ("TEST-E009", "표 밖 줄", None)]


def test_docx_paragraphs_and_table(tmp_path: Path) -> None:
    data = docx_bytes(["가상 문서", "TEST-E005: 가상 설명 E"], table=[("코드", "설명"), ("TEST-E006", "가상 설명 F")])
    result = read_reference(write_bytes(tmp_path / "codes.docx", data))
    assert result.format == "DOCX" and result.tables[0].name == "표 1"
    entries, _ = extract_entries(result)
    assert {(e.code, e.description) for e in entries} == {("TEST-E005", "가상 설명 E"), ("TEST-E006", "가상 설명 F")}
    assert next(e for e in entries if e.code == "TEST-E005").locator == "문단 2"


def test_pdf_text(tmp_path: Path) -> None:
    result = read_reference(write_bytes(tmp_path / "codes.pdf", pdf_bytes(["TEST-E007: virtual meaning G", "plain"])))
    assert result.format == "PDF" and any("표가 깨질 수" in w for w in result.warnings)
    entries, _ = extract_entries(result)
    assert [(e.code, e.description, e.locator) for e in entries] == [("TEST-E007", "virtual meaning G", "1쪽")]


def test_bad_files(tmp_path: Path) -> None:
    with pytest.raises(ReferenceReadError, match="워드"):
        read_reference(write_bytes(tmp_path / "bad.docx", b"not a zip"))
    with pytest.raises(ReferenceReadError, match="엑셀"):
        read_reference(write_bytes(tmp_path / "bad.xlsx", b"not a zip"))
    with pytest.raises(ReferenceReadError, match="PDF"):
        read_reference(write_bytes(tmp_path / "bad.pdf", b"%PDF-1.4 broken"))
    with pytest.raises(ReferenceReadError, match="글자를 찾지"):
        read_reference(write_bytes(tmp_path / "empty.txt", b"   \n"))
    with pytest.raises(ReferenceReadError, match="바이너리"):
        read_reference(write_bytes(tmp_path / "bin.txt", b"\x00\x01\x02" * 10))
