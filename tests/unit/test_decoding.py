"""인코딩 처리와 줄 나누기 (Phase 2 "인코딩 처리", "데이터 손실 없이")."""

import codecs

import pytest

from app.core.decoding import BinaryFileError, decode_log_bytes

KOREAN = ["가상 로그 첫 줄", "두 번째 줄 TEST-E001", "", "마지막"]


@pytest.mark.parametrize(
    "encoding, prefix, label",
    [("utf-8", b"", "utf-8"), ("utf-8", codecs.BOM_UTF8, "utf-8-sig"), ("cp949", b"", "cp949")],
)
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_text_encodings_roundtrip(encoding: str, prefix: bytes, label: str, newline: str) -> None:
    data = prefix + (newline.join(KOREAN) + newline).encode(encoding)
    decoded = decode_log_bytes(data)
    assert decoded.encoding == label
    assert [line.text for line in decoded.lines] == KOREAN
    assert [line.line_no for line in decoded.lines] == [1, 2, 3, 4]
    assert decoded.warnings == []
    for line in decoded.lines:  # 바이트 위치가 원본에서 그 줄을 가리킨다
        assert data[line.byte_offset:].startswith(line.text.encode(encoding))


def test_no_trailing_newline() -> None:
    decoded = decode_log_bytes("a\nb".encode())
    assert [line.text for line in decoded.lines] == ["a", "b"]


@pytest.mark.parametrize("bom_codec, codec", [(codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")])
def test_utf16_with_bom(bom_codec: bytes, codec: str) -> None:
    data = bom_codec + ("\r\n".join(KOREAN) + "\r\n").encode(codec)
    decoded = decode_log_bytes(data)
    assert decoded.encoding == "utf-16"
    assert [line.text for line in decoded.lines] == KOREAN
    for line in decoded.lines:
        assert data[line.byte_offset:].startswith(line.text.encode(codec))


def test_binary_is_rejected() -> None:
    with pytest.raises(BinaryFileError, match="바이너리"):
        decode_log_bytes(b"\x01\x02\x00\x00binary\x00data")


def test_undecodable_falls_back_without_loss() -> None:
    data = b"ok line\n\xff\xfe\xfd broken\n"
    decoded = decode_log_bytes(data)
    assert decoded.encoding == "latin-1"
    assert decoded.warnings and "latin-1" in decoded.warnings[0]
    assert decoded.lines[1].text.encode("latin-1") == b"\xff\xfe\xfd broken"


def test_empty_bytes_have_no_lines() -> None:
    assert decode_log_bytes(b"").lines == []
