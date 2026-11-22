"""릴리스용 갱신 zip 만들기 (CR-13 RL-01).

이미 쓰고 있는 LocalLogAnalyzer 폴더를 갱신하는 묶음: 안내 글(UPDATE_HOW_TO.txt) + LocalLogAnalyzer.exe + _internal/**.
휴대용 AI(ollama 폴더)는 넣지 않는다(약 6GB라 GitHub에 올릴 수 없음). 안내 글은 릴리스마다 달라 인자로 받는다.

실행: uv run python packaging/make_update_zip.py dist <안내 글.txt> LocalLogAnalyzer-app-update-v<버전>.zip
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path


def build(dist: Path, how_to: Path, name: str) -> Path:
    app = dist / "LocalLogAnalyzer"
    out = dist / name
    text = how_to.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\n", "\r\n")  # 메모장용 줄바꿈
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("UPDATE_HOW_TO.txt", "﻿" + text)
        archive.write(app / "LocalLogAnalyzer.exe", "LocalLogAnalyzer/LocalLogAnalyzer.exe")
        for path in sorted((app / "_internal").rglob("*")):
            if path.is_file():
                archive.write(path, "LocalLogAnalyzer/" + path.relative_to(app).as_posix())
    names = zipfile.ZipFile(out).namelist()
    if any("ollama" in n.lower() for n in names):
        raise SystemExit("갱신 zip에 ollama가 들어갔습니다.")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dist", type=Path, help="dist 폴더(LocalLogAnalyzer 폴더가 든 곳)")
    parser.add_argument("how_to", type=Path, help="zip 안에 넣을 안내 글(UTF-8)")
    parser.add_argument("name", help="만들 zip 이름")
    args = parser.parse_args()
    out = build(args.dist, args.how_to, args.name)
    count = len(zipfile.ZipFile(out).namelist())
    print(f"{out.name}: {out.stat().st_size / 1e6:.1f} MB, 파일 {count}개")


if __name__ == "__main__":
    main()
