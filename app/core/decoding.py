"""로그 바이트 → 줄 목록 (계획서 §11 Phase 2 "인코딩 처리", REQUIREMENTS G-2).

판정 순서: BOM(UTF-8·UTF-16) → UTF-8 → CP949 → 바이트 손실 없는 대체(latin-1, 경고).
원본 바이트는 따로 보관하므로 여기서 무엇을 고르든 원본은 바뀌지 않는다.
줄마다 원본에서의 바이트 위치(byte_offset)를 함께 돌려준다(원본 행 이동용).
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass, field

from app.models.records import RawLine

BINARY_PROBE_BYTES = 8192


class BinaryFileError(ValueError):
    """텍스트 로그가 아님(바이너리)."""


@dataclass
class DecodedText:
    encoding: str
    lines: list[RawLine]
    warnings: list[str] = field(default_factory=list)

    @property
    def line_count(self) -> int:
        return len(self.lines)

    def text(self) -> str:
        """줄바꿈을 \\n으로 맞춘 전체 텍스트(마지막 줄바꿈 제외)."""
        return "\n".join(line.text for line in self.lines)


def decode_log_bytes(data: bytes) -> DecodedText:
    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        return _decode_utf16(data)

    offset_base = 0
    body = data
    if data.startswith(codecs.BOM_UTF8):
        offset_base = len(codecs.BOM_UTF8)
        body = data[offset_base:]
        encoding, label = "utf-8", "utf-8-sig"
    else:
        if b"\x00" in data[:BINARY_PROBE_BYTES]:
            raise BinaryFileError("텍스트 로그가 아닙니다(바이너리 파일). TXT/LOG 텍스트 파일만 가져올 수 있습니다.")
        encoding = label = _detect_ascii_compatible(body)

    warnings: list[str] = []
    if encoding == "latin-1":
        warnings.append("인코딩을 판정하지 못해 바이트를 그대로 옮겼습니다(latin-1). 한글이 깨져 보일 수 있습니다.")
    return DecodedText(label, _split_lines(body, encoding, offset_base), warnings)


def _detect_ascii_compatible(body: bytes) -> str:
    for candidate in ("utf-8", "cp949"):
        try:
            body.decode(candidate)
            return candidate
        except UnicodeDecodeError:
            continue
    return "latin-1"


def _split_lines(body: bytes, encoding: str, offset_base: int) -> list[RawLine]:
    """ASCII 호환 인코딩(UTF-8, CP949, latin-1)은 다중 바이트 안에 0x0A가 나오지 않으므로 바이트에서 바로 나눈다."""
    lines: list[RawLine] = []
    position = 0
    line_no = 0
    length = len(body)
    while position < length:
        end = body.find(b"\n", position)
        chunk_end = length if end == -1 else end
        chunk = body[position:chunk_end]
        if chunk.endswith(b"\r"):
            chunk = chunk[:-1]
        line_no += 1
        lines.append(RawLine(line_no=line_no, byte_offset=offset_base + position, text=chunk.decode(encoding)))
        if end == -1:
            break
        position = end + 1
    return lines


def _decode_utf16(data: bytes) -> DecodedText:
    text = data.decode("utf-16")
    big_endian = data.startswith(codecs.BOM_UTF16_BE)
    unit_codec = "utf-16-be" if big_endian else "utf-16-le"
    lines: list[RawLine] = []
    offset = 2  # BOM
    for line_no, piece in enumerate(text.split("\n"), start=1):
        raw_len = len((piece + "\n").encode(unit_codec))
        visible = piece[:-1] if piece.endswith("\r") else piece
        lines.append(RawLine(line_no=line_no, byte_offset=offset, text=visible))
        offset += raw_len
    if lines and lines[-1].text == "" and text.endswith("\n"):
        lines.pop()
    return DecodedText("utf-16", lines, [])
