"""참고 자료에서 코드 항목 뽑기 (변경 요청 CR-04 REF-02). 결정론.

- 표: 머리글로 열 역할(코드·명칭·설명·원인·조치·판정)을 추정한다. 사용자가 화면에서 바꿀 수 있다(column_map).
- 글: `코드: 설명`, `코드 - 설명`, `코드<탭>설명` 줄과 `## 코드 제목` 머리글 블록(아래 줄은 설명, '원인:'·'조치:' 줄은 따로).
항목마다 원문 위치와 원문을 남긴다. **뜻을 만들지 않는다**: 값은 언제나 문서의 글자 그대로다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.reference_reader import ReadResult, Table, TextLine

ROLES = ("code", "title", "description", "cause", "action", "judgment")
ROLE_LABELS = {"code": "코드", "title": "명칭", "description": "설명", "cause": "원인", "action": "조치", "judgment": "판정"}
HEADER_WORDS = {  # 정규화(소문자·공백 제거)한 머리글에 들어 있으면 그 역할. 위에서부터 먼저 맞는 것.
    # CR-16 RJ-01: 문서가 '이 로그는 에러/정상'이라고 적은 열. 값은 글자 그대로 저장하고 judgment_side로만 읽는다.
    "judgment": ("판정", "판단", "구분", "분류", "정상여부", "이상여부", "에러여부", "오류여부", "심각도", "등급", "severity",
                 "verdict", "judg", "classif"),
    "cause": ("원인", "cause", "reason"),
    "action": ("조치", "대처", "대응", "해결", "처리방법", "action", "solution", "remedy", "countermeasure"),
    "title": ("명칭", "이름", "제목", "에러명", "오류명", "알람명", "name", "title"),
    "code": ("코드", "code", "알람", "alarm", "에러번호", "오류번호", "errorno", "errno"),
    "description": ("설명", "내용", "의미", "상세", "현상", "증상", "메시지", "description", "desc", "meaning",
                    "detail", "message"),
}
CODE_TOKEN = r"[A-Za-z0-9][A-Za-z0-9_\-]{0,31}"
_LINE = re.compile(rf"^\s*(?:[-*•·]\s*)?(?P<code>{CODE_TOKEN})\s*(?:[:：|]|\s[-–—]\s|\t|\s{{2,}})\s*(?P<rest>\S.*)$")
_HEADING = re.compile(rf"^\s*#{{1,6}}\s+(?P<code>{CODE_TOKEN})(?:\s*[:：\-–—]\s*|\s+|$)(?P<title>.*)$")
_LABELED = re.compile(r"^\s*(?:[-*•·]\s*)?(?P<label>원인|조치|대처|해결|설명|명칭|판정|구분|cause|action|solution|description)"
                      r"\s*[:：]\s*(?P<rest>.*)$",
                      re.IGNORECASE)
_DATE = re.compile(r"^\d{4}[-_]\d{1,2}[-_]\d{1,2}$")
LABEL_ROLES = {"원인": "cause", "cause": "cause", "조치": "action", "대처": "action", "해결": "action",
               "action": "action", "solution": "action", "설명": "description", "description": "description",
               "명칭": "title", "판정": "judgment", "구분": "judgment"}


@dataclass
class EntryDraft:
    code: str
    title: str | None = None
    description: str | None = None
    cause: str | None = None
    action: str | None = None
    locator: str | None = None
    source_text: str | None = None
    judgment: str | None = None  # 문서의 판정 낱말 그대로(CR-16 RJ-01)


def normalize_code(code: str) -> str:
    return "".join(code.split()).upper()


# CR-16 RJ-02: 판정 칸의 낱말 → 판정 쪽. 문서의 분류 낱말을 읽기만 한다(뜻을 만들지 않음). 목록에 없는 낱말(경고·주의 등),
# 두 쪽이 섞임, 부정은 판정에 쓰지 않는다.
JUDGMENT_ABNORMAL = ("비정상", "에러", "오류", "이상", "고장", "불량", "알람", "점검필요", "조치필요", "error", "fault", "fail",
                     "abnormal", "alarm", "critical", "fatal", "ng")
JUDGMENT_NORMAL = ("정상", "문제없음", "무시", "무해", "정보", "normal", "ok", "ignore", "benign", "info")
_NEGATION = ("아님", "아니", "not", "non")
_ASCII_WORD = re.compile(r"[a-z]+")


def judgment_side(text: str | None) -> str | None:
    """'ABNORMAL'·'NORMAL'·None. 영어 낱말은 낱말 단위로(예: 'ok'가 'token' 안에서 맞지 않게), 한국어는 글자 그대로 찾는다."""
    if not text:
        return None
    flat = re.sub(r"\s+", "", text.casefold())
    words = set(_ASCII_WORD.findall(text.casefold()))

    def has(word: str, body: str) -> bool:
        return word in words if word.isascii() else word in body

    if any(has(n, flat) for n in _NEGATION):
        return None
    abnormal = any(has(w, flat) for w in JUDGMENT_ABNORMAL)
    rest = flat
    for w in JUDGMENT_ABNORMAL:  # '비정상' 안의 '정상'을 정상으로 세지 않게 비정상 낱말을 지우고 본다
        if not w.isascii():
            rest = rest.replace(w, " ")
    normal = any(has(w, rest) for w in JUDGMENT_NORMAL)
    if "문제없음" in flat or "이상없음" in flat:  # '문제 없음'·'이상 없음'은 정상('이상'이 들어 있어도)
        return "NORMAL" if not any(w in flat for w in ("에러", "오류", "고장", "불량")) else None
    if abnormal == normal:
        return None
    return "ABNORMAL" if abnormal else "NORMAL"


def looks_like_code(token: str) -> bool:
    """숫자가 있고, 숫자만이면 3자 이상. 날짜 모양은 뺀다."""
    token = token.strip()
    if not token or len(token) > 32 or not any(ch.isdigit() for ch in token) or _DATE.match(token):
        return False
    return any(ch.isalpha() for ch in token) or len(token) >= 3


def _norm_header(text: str) -> str:
    return re.sub(r"[\s_\-()/·.:]", "", text).lower()


def guess_column_map(table: Table, scan_rows: int = 10) -> dict | None:
    """머리글 행과 열 역할 추정. {'header_row': i, 'code': c, ...}. 못 찾으면 첫 열이 코드처럼 보일 때만 코드·설명."""
    for row_index, row in enumerate(table.rows[:scan_rows]):
        mapping: dict = {"header_row": row_index}
        used: set[int] = set()
        for column, cell in enumerate(row):
            header = _norm_header(cell)
            if not header:
                continue
            for role, words in HEADER_WORDS.items():
                if role not in mapping and any(word in header for word in words):
                    mapping[role] = column
                    used.add(column)
                    break
        if "code" in mapping and len(used) >= 2:
            return mapping
    body = table.rows[:50]
    width = max((len(r) for r in body), default=0)
    if width >= 2:
        codes = [r for r in body if r and looks_like_code(r[0])]
        if len(codes) >= max(2, len(body) // 2):
            return {"header_row": -1, "code": 0, "description": 1}
    return None


def entries_from_table(table: Table, column_map: dict) -> list[EntryDraft]:
    code_column = column_map.get("code")
    if code_column is None:
        return []
    start = int(column_map.get("header_row", -1)) + 1
    out: list[EntryDraft] = []
    for row, locator in zip(table.rows[start:], table.row_locators[start:]):
        code = row[code_column].strip() if code_column < len(row) else ""
        if not code:
            continue
        values = {role: (row[column_map[role]].strip() or None)
                  for role in ROLES[1:] if column_map.get(role) is not None and column_map[role] < len(row)}
        out.append(EntryDraft(code, locator=locator, source_text=" | ".join(c for c in row if c), **values))
    return out


def entries_from_lines(lines: list[TextLine]) -> list[EntryDraft]:
    out: list[EntryDraft] = []
    current: EntryDraft | None = None
    body: list[str] = []

    def close() -> None:
        nonlocal current, body
        if current is not None:
            if body:
                extra = " ".join(body).strip()
                current.description = f"{current.description} {extra}".strip() if current.description else extra
            out.append(current)
        current, body = None, []

    for line in lines:
        text = line.text
        heading = _HEADING.match(text)
        if heading and looks_like_code(heading.group("code")):
            close()
            current = EntryDraft(heading.group("code"), title=heading.group("title").strip() or None,
                                 locator=line.locator, source_text=text.strip())
            continue
        if text.lstrip().startswith("#"):
            close()  # 코드가 아닌 머리글은 블록을 끝낸다
            continue
        if not text.strip():
            continue
        labeled = _LABELED.match(text) if current is not None else None
        if labeled:
            current.source_text = f"{current.source_text}\n{text.strip()}"
            setattr(current, LABEL_ROLES[labeled.group("label").lower()], labeled.group("rest").strip() or None)
            continue
        match = _LINE.match(text)
        if match and looks_like_code(match.group("code")):
            close()  # 코드 줄은 머리글 블록 안에 있어도 새 항목이다
            out.append(EntryDraft(match.group("code"), description=match.group("rest").strip(), locator=line.locator,
                                  source_text=text.strip()))
            continue
        if current is not None:
            current.source_text = f"{current.source_text}\n{text.strip()}"
            body.append(text.strip())
    close()
    return out


def extract_entries(result: ReadResult, column_maps: dict[str, dict] | None = None) -> tuple[list[EntryDraft], dict]:
    """(항목, 쓴 열 역할 {표 이름: map}). column_maps를 주면 그 표는 그 역할로 읽는다."""
    entries: list[EntryDraft] = []
    used: dict[str, dict] = {}
    for table in result.tables:
        mapping = (column_maps or {}).get(table.name) or guess_column_map(table)
        if mapping:
            used[table.name] = mapping
            entries.extend(entries_from_table(table, mapping))
    entries.extend(entries_from_lines(result.lines))
    return entries, used


PASSAGE_MAX = 600
PASSAGE_MIN = 10


def build_passages(result: ReadResult, entries: list[EntryDraft]) -> list[tuple[str | None, str]]:
    """일반 문서 문단(CR-07 REF-07): 코드 항목으로 뽑히지 않은 글 줄을 빈 줄·머리글 단위로 묶는다.
    (원문 위치, 글). 긴 문단은 600자 안쪽으로 나눈다. 표는 넣지 않는다(코드 항목으로 이미 쓰임)."""
    consumed = {line.strip() for e in entries for line in (e.source_text or "").splitlines() if line.strip()}
    passages: list[tuple[str | None, str]] = []
    current: list[str] = []
    locator: str | None = None

    def flush() -> None:
        nonlocal current, locator
        body = " ".join(current).strip()
        while body:
            piece, body = body[:PASSAGE_MAX], body[PASSAGE_MAX:]
            if body:  # 낱말 중간에서 자르지 않게 마지막 공백까지
                cut = piece.rfind(" ")
                if cut > PASSAGE_MAX // 2:
                    piece, body = piece[:cut], piece[cut + 1:] + body
            if len(piece.strip()) >= PASSAGE_MIN:
                passages.append((locator, piece.strip()))
        current, locator = [], None

    for line in result.lines:
        text = line.text.strip()
        if not text or text in consumed:
            flush()
            continue
        if text.startswith("#") and current:
            flush()
        if not current:
            locator = line.locator
        current.append(text.lstrip("#").strip() if text.startswith("#") else text)
    flush()
    return passages
