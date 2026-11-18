"""샘플 세트로 비정상 검출률·정상 오탐률을 잰다 (CR-08 DT-05). AI는 쓰지 않는다(결정론 분석만).

방법: 형식(파일 이름 앞 3글자)마다 정상 로그 하나를 남기고 나머지 정상만 '정상'으로 학습 →
남긴 정상과 그 형식의 비정상을 운전 모드 없이 분석. 남기는 정상을 바꿔 가며 폴드 수만큼 반복한다.
- 비정상 검출: 분석 결과 '이상 정도'가 SUSPECTED(의심) 또는 RULE_VIOLATION(규칙 위반)
- 정상 오탐: 남긴 정상 로그가 위처럼 나옴

매번 새 임시 데이터 폴더를 쓴다(사용 중인 앱 데이터와 섞이지 않음).

실행: uv run python samples/evaluate_detection.py samples/새형식_시험_50 [--folds 4] [--data 임시폴더] [--verbose]
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from app.analysis.diagnostic_engine import AnalysisRequest, DiagnosticEngine  # noqa: E402
from app.bootstrap import initialize  # noqa: E402
from app.core.importer import LogImporter  # noqa: E402
from app.learning.log_entry import LogEntryService  # noqa: E402
from app.models.enums import OriginPurpose  # noqa: E402
from app.models.records import ImportRequest, SessionMode  # noqa: E402

DETECTED = {"SUSPECTED", "RULE_VIOLATION"}


NOTES: dict[str, str] = {}  # 파일 이름 → 정답표 셋째 칸(고장 종류·메모)


def read_answers(folder: Path) -> list[tuple[Path, str, str]]:
    """(파일 경로, 구분, 묶음). 묶음 = 폴더 이름 + 파일 이름 앞 3글자(형식·제품군)."""
    lines = (folder / "정답표.txt").read_text(encoding="utf-8-sig").splitlines()[1:]
    rows = [row.split("\t") for row in lines if row.strip()]
    for cells in rows:
        NOTES[cells[0]] = cells[2] if len(cells) > 2 else ""
    return [(folder / cells[0], cells[1], f"{folder.name}/{cells[0][:3]}") for cells in rows]


def run_fold(answers: list[tuple[Path, str, str]], fold: int, folds: int, home: Path) -> list[dict]:
    ctx = initialize(home, configure_logging=False)
    try:
        importer = LogImporter(ctx.database, ctx.paths, ctx.settings.import_)
        entries = LogEntryService(ctx.database)
        engine = DiagnosticEngine(ctx.database, ctx.paths, ctx.settings)
        ids = {}
        for path, _label, _group in answers:
            result = importer.import_logs(ImportRequest(paths=[path], purpose=OriginPurpose.NORMAL_CASE,
                                                        session_mode=SessionMode.SEPARATE))
            if result.rejected:
                raise RuntimeError(f"가져오기 거부: {result.rejected}")
            ids[path] = result.log_file_ids[0]
        normals: dict[str, list[Path]] = defaultdict(list)
        for path, label, group in answers:
            if label == "정상":
                normals[group].append(path)
        # 묶음마다 정상의 1/folds를 남겨 분석하고 나머지를 학습한다(정상 4개면 1개, 40개면 10개)
        held = {path for names in normals.values() for i, path in enumerate(names) if i % folds == fold % folds}
        for path, label, _group in answers:
            if label == "정상" and path not in held:
                entries.save(ids[path], "NORMAL", "")
        rows = []
        for path, label, group in answers:
            if label != "비정상" and path not in held:
                continue
            outcome = engine.run(AnalysisRequest([ids[path]], use_ai=False))
            supports = [f for f in outcome.findings if f.stance == "SUPPORTS" and f.layer == "DETERMINISTIC"]
            rows.append({
                "fold": fold, "name": path.name, "group": group, "label": label,
                "level": outcome.axes["anomaly_level"], "detected": outcome.axes["anomaly_level"] in DETECTED,
                "reasons": [f"{f.finding_type}: {f.summary[:140]}" for f in supports],
            })
        return rows
    finally:
        ctx.close()


def remove_tree(path: Path) -> None:
    def make_writable(func, target, _exc):  # 원본 사본은 읽기 전용으로 저장된다
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onerror=make_writable)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folders", type=Path, nargs="+", help="정답표.txt가 있는 폴더. 여러 개면 한 데이터에 섞어서 잰다")
    parser.add_argument("--folds", type=int, default=4)
    parser.add_argument("--data", type=Path, default=None, help="임시 데이터 폴더(기본: 시스템 임시 폴더)")
    parser.add_argument("--verbose", action="store_true", help="검출·오탐 근거를 모두 보임")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    answers = [row for folder in args.folders for row in read_answers(folder)]
    base = Path(tempfile.mkdtemp(prefix="lla_eval_", dir=args.data))
    rows: list[dict] = []
    try:
        for fold in range(args.folds):
            rows.extend(run_fold(answers, fold, args.folds, base / f"fold{fold}"))
    finally:
        remove_tree(base)

    print(f"세트 {', '.join(f.name for f in args.folders)} · 폴드 {args.folds}")
    by_file: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["label"] == "비정상":
            by_file[f"{r['group']}/{r['name']}"].append(r)
    for name, items in sorted(by_file.items()):
        count = sum(i["detected"] for i in items)
        mark = "검출" if count == len(items) else ("일부" if count else "놓침")
        if args.verbose or count < len(items):
            print(f"  [비정상 {mark} {count}/{len(items)}] {name} — {NOTES.get(items[0]['name'], '')[:40]}")
            reasons = items[0]["reasons"]
            for reason in reasons[: (len(reasons) if args.verbose else 2)]:
                print(f"      - {reason}")
    for r in rows:
        if r["label"] == "정상" and (r["detected"] or args.verbose):
            print(f"  [정상 {'오탐' if r['detected'] else '통과'} 폴드{r['fold']}] {r['group']}/{r['name']} ({r['level']})")
            for reason in r["reasons"][:3]:
                print(f"      - {reason}")
    kinds: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        note = NOTES.get(r["name"], "")
        if r["label"] == "비정상" and note and len(note) <= 12:  # 무작위 제품군 정답표의 '고장 종류'
            kinds[note].append(r["detected"])
    if kinds:
        print("고장 종류별: " + " · ".join(f"{k} {sum(v)}/{len(v)}" for k, v in sorted(kinds.items())))
    groups = sorted({r["group"].split("/")[0] for r in rows}) if len(args.folders) > 1 else []
    for group in groups + ["전체"]:
        part = [r for r in rows if group == "전체" or r["group"].startswith(group + "/")]
        abnormal = [r for r in part if r["label"] == "비정상"]
        normal = [r for r in part if r["label"] == "정상"]
        hit, false_alarm = sum(r["detected"] for r in abnormal), sum(r["detected"] for r in normal)
        print(f"{group}: 비정상 검출률 {hit}/{len(abnormal)} = {hit / max(1, len(abnormal)):.0%} · "
              f"정상 오탐률 {false_alarm}/{len(normal)} = {false_alarm / max(1, len(normal)):.0%}")


if __name__ == "__main__":
    main()
