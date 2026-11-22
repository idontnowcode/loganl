"""정답표가 있는 가상 세트 하나를 학습용·시험용 두 폴더로 나눈다(2026-11-22 사용자 지정 시험 방법: 학습 100 → 시험 50).

정답표 순서대로 앞 N개는 학습용, 나머지는 시험용. 로그 파일을 복사하고 폴더마다 정답표.txt를 새로 쓴다.
실행: uv run python samples/split_train_test.py <세트 폴더> --train 100 --out <나눈 세트를 둘 폴더>
      → <out>/<세트 이름>_학습, <out>/<세트 이름>_시험
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def split(folder: Path, train: int, out: Path) -> tuple[Path, Path]:
    lines = (folder / "정답표.txt").read_text(encoding="utf-8-sig").splitlines()
    head, rows = lines[0], [line for line in lines[1:] if line.strip()]
    parts = {"학습": rows[:train], "시험": rows[train:]}
    made = []
    for name, part in parts.items():
        target = out / f"{folder.name}_{name}"
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        for row in part:
            shutil.copy2(folder / row.split("\t", 1)[0], target)
        (target / "정답표.txt").write_bytes(("\r\n".join([head, *part]) + "\r\n").encode("utf-8-sig"))
        made.append(target)
    return made[0], made[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path)
    parser.add_argument("--train", type=int, default=100)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    train, test = split(args.folder, args.train, args.out)
    for path in (train, test):
        rows = (path / "정답표.txt").read_text(encoding="utf-8-sig").splitlines()[1:]
        abnormal = sum(row.split("\t")[1] == "비정상" for row in rows if row.strip())
        print(f"{path}: {len(rows)}개 (정상 {len(rows) - abnormal}, 비정상 {abnormal})")


if __name__ == "__main__":
    main()
