"""다중 챔버 가상 세트용 참고 자료 예시 — '판정' 열이 있는 엑셀 표 (CR-16 RJ-07). 모두 가상 값이다.

코드·알림 이름은 samples/make_cluster_tool_samples.py가 만드는 가상 이름(TEST-E7xx, NOTICE flag=TEST-F?)이고, 설명·판정도
그 생성기가 정한 가상 판정 그대로다. 실제 장비의 코드 뜻·사양이 아니다.

실행: uv run python samples/make_judgment_table.py <저장할 .xlsx>
"""

from __future__ import annotations

import sys
from pathlib import Path

from openpyxl import Workbook

ROWS = [
    ("코드/메시지", "판정", "명칭", "설명"),
    ("TEST-E710", "에러", "가상 로봇 이송 오류", "가상: 로봇이 기판을 옮기다 멈춤"),
    ("TEST-E720", "에러", "가상 배기 실패", "가상: 챔버 압력이 내려가지 않음"),
    ("TEST-E730", "에러", "가상 RF 아크", "가상: 처리 중 아크 감지"),
    ("TEST-E750", "에러", "가상 배정 시간 초과", "가상: 스케줄러가 기판을 보내지 않고 로트 중단"),
    ("NOTICE flag=TEST-FK", "점검 필요", "가상 점검 알림", "가상: INFO로 찍히지만 점검이 필요한 알림"),
    ("NOTICE flag=TEST-FA", "정상", "가상 안내 알림", "가상: 문제없는 안내 알림"),
]


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("참고자료_판정표_예시.xlsx")
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "가상 판정표"
    for row in ROWS:
        sheet.append(list(row))
    for column, width in zip("ABCD", (24, 12, 22, 44)):
        sheet.column_dimensions[column].width = width
    out.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(out)
    print(f"{out} — 항목 {len(ROWS) - 1}개")


if __name__ == "__main__":
    main()
