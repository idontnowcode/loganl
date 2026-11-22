"""학습 뒤 처음 보는 로그로 판단 정답률이 쌓일수록 오르는지 잰다 (CR-14 CM-06). 가상 세트로만 잰다.

시험 방법(2026-11-22 사용자 지정): 샘플을 학습용 100개·시험용 50개로 따로 만든다.
  1) 학습: 학습용 100개를 모두 사례로 저장(정상은 동작 메모, 비정상은 판단 가이드와 함께). 사례 지문을 미리 만든다.
  2) 시험: 시험용 50개를 하나씩 ① 분석 ② 엔지니어가 정답으로 '확인 결과 남기기'를 되풀이한다.
앱 코드는 그대로이고 쌓이는 것은 확인된 사례뿐이다. 판단마다 맞았는지 기록해 10개마다 누적 정답률을 보인다.
- 규칙만: 결정론 규칙 판정('의심'·'규칙 위반'이면 비정상이라고 한 것)
- 최종: 화면의 최종 판정(정상 / 비정상 / 판단 보류, 사례 기억·확인 이력 보정 AI 반영). 보류는 정답률에서 빼고 따로 센다
- AI: --ai일 때 Ollama가 낸 비정상 가능성 50% 이상이면 비정상(가능성을 매기지 않았거나 AI 의견이 실패하면 보류)
- AI 보정: 확인 이력으로 고른 문턱 이상이면 비정상(CM-07, 이력이 적으면 50%), 가능성이 없으면 보류
정답률은 판단한 것(보류 제외) 중 맞힌 비율이고, 보류 수를 함께 보인다(리뷰 2026-11-22).
--answer-questions: 분석 뒤 '판단을 돕는 질문'(CR-15)에 정답표를 아는 가상 엔지니어가 답한다. 정상 로그면 '정상에서도 나옴',
비정상 로그면 물은 근거(특징·원문 줄·값 칸)가 판단 가이드의 낱말과 겹칠 때만 '문제의 표시', 아니면 '모름'(저장 안 함).
--reference 표 파일: 시험 전에 참고 자료로 올려 승인한다(CR-16 RJ-07, '판정' 열이 있으면 판정에 쓰임).
--train-limit N: 학습용 앞 N개만 저장한다(학습이 적을 때).

실행: uv run python samples/evaluate_learning.py <학습용 폴더> <시험용 폴더> [--ai] [--model gemma3:4b] [--no-memory]
      [--seed 순서 씨앗] [--json 결과 파일] [--data 임시 폴더]
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analysis.diagnostic_engine import AnalysisRequest, DiagnosticEngine  # noqa: E402
from app.bootstrap import initialize  # noqa: E402
from app.core.importer import LogImporter  # noqa: E402
from app.learning.criteria import ABNORMAL_SIGN, BENIGN, CriteriaService  # noqa: E402
from app.learning.log_entry import LogEntryService  # noqa: E402
from app.models.enums import OriginPurpose  # noqa: E402
from app.models.records import ImportRequest, SessionMode  # noqa: E402

DETECTED = {"SUSPECTED", "RULE_VIOLATION"}


STOP_WORDS = {"TEST", "INFO", "WARN", "ERROR", "sub", "unit", "step", "code"}


def _tokens(text: str | None) -> set[str]:
    return {t for t in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}", text or "") if t not in STOP_WORDS}


def oracle(question, cells: list[str]) -> str | None:
    """정답표를 아는 가상 엔지니어의 답(CR-15 측정용)."""
    if cells[1] == "정상":
        return BENIGN
    guide = cells[3] if len(cells) > 3 else ""
    asked = _tokens(question.feature) | _tokens(question.example) | ({question.field} if question.field else set())
    return ABNORMAL_SIGN if asked & _tokens(guide) else None


def read_rows(folder: Path) -> list[list[str]]:
    lines = (folder / "정답표.txt").read_text(encoding="utf-8-sig").splitlines()[1:]
    return [line.split("\t") for line in lines if line.strip()]


def accuracy(rows: list[dict], key: str) -> str:
    """판단한 것(보류 제외) 중 맞힌 비율 + 보류 수."""
    judged = [r for r in rows if r[key] is not None]
    held = len(rows) - len(judged)
    if not judged:
        return f"  -  (보류 {held})" if held else "  -  "
    rate = f"{sum(r[key] == r['truth'] for r in judged) / len(judged):5.0%}"
    return rate + (f"(보류 {held})" if held else "")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("train", type=Path)
    parser.add_argument("test", type=Path)
    parser.add_argument("--ai", action="store_true")
    parser.add_argument("--model", default="gemma3:4b")
    parser.add_argument("--no-memory", action="store_true", help="사례 기억을 끄고(CR-14 이전과 같게) 잰다")
    parser.add_argument("--answer-questions", action="store_true", help="판단 질문에 정답표로 답한다(CR-15)")
    parser.add_argument("--seed", default="", help="시험 순서를 섞을 씨앗(없으면 파일 순서)")
    parser.add_argument("--reference", type=Path, action="append", default=[], help="참고 자료로 올려 승인할 파일(CR-16)")
    parser.add_argument("--train-limit", type=int, default=None, help="학습용 앞 N개만 저장")
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--data", type=Path, default=Path(tempfile.gettempdir()))
    args = parser.parse_args()
    args.data.mkdir(parents=True, exist_ok=True)
    train, test = read_rows(args.train), read_rows(args.test)
    if args.train_limit is not None:
        train = train[:args.train_limit]
    if args.seed:
        random.Random(args.seed).shuffle(test)
    home = Path(tempfile.mkdtemp(prefix="learn_", dir=args.data))
    (home / "config").mkdir(parents=True)
    (home / "config" / "settings.json").write_text(
        json.dumps({"settings_version": 1, "ollama": {"model": args.model if args.ai else None}}), encoding="utf-8")
    ctx = initialize(home, configure_logging=False)
    results: list[dict] = []
    try:
        importer = LogImporter(ctx.database, ctx.paths, ctx.settings.import_)
        entries = LogEntryService(ctx.database)
        engine = DiagnosticEngine(ctx.database, ctx.paths, ctx.settings, use_case_memory=not args.no_memory)
        criteria = CriteriaService(ctx.database)

        def load(folder: Path, cells: list[str]) -> int:
            result = importer.import_logs(ImportRequest(paths=[folder / cells[0]], purpose=OriginPurpose.NORMAL_CASE,
                                                        session_mode=SessionMode.SEPARATE))
            return result.log_file_ids[0]

        def confirm(log_id: int, cells: list[str]) -> None:
            if cells[1] == "정상":
                entries.save(log_id, "NORMAL", cells[2])
            else:
                entries.save(log_id, "ABNORMAL", cells[2], cells[3] if len(cells) > 3 and cells[3] else "비정상")

        if args.reference:
            from app.learning.reference_library import ReferenceLibrary

            library = ReferenceLibrary(ctx.database, ctx.paths, ctx.settings.import_)
            for path in args.reference:
                outcome = library.import_file(path)
                library.approve(outcome.document_id)
                print(f"참고 자료: {path.name} 항목 {outcome.entries}개 승인", flush=True)
        started = time.perf_counter()
        for cells in train:
            confirm(load(args.train, cells), cells)
        ready, total = (0, 0) if args.no_memory else engine.prepare_case_memory()
        print(f"학습: {args.train.name} {len(train)}개 저장(정상 {sum(c[1] == '정상' for c in train)}·비정상 "
              f"{sum(c[1] == '비정상' for c in train)}), 사례 지문 {ready}/{total}, {time.perf_counter() - started:.0f}초"
              f" · 시험: {args.test.name} {len(test)}개 · 사례 기억 {'끔' if args.no_memory else '켬'} · AI {'켬' if args.ai else '끔'}",
              flush=True)
        for index, cells in enumerate(test, start=1):
            log_id = load(args.test, cells)
            began = time.perf_counter()
            result = engine.run(AnalysisRequest([log_id], use_ai=args.ai))
            memory = result.memory
            rule_level = memory.rule_level if memory is not None else result.axes["anomaly_level"]
            ai = result.ai
            answer = getattr(ai, "answer", None)
            likelihood = getattr(answer, "abnormal_likelihood", None) if answer is not None else None
            row = {
                "i": index, "name": cells[0], "truth": cells[1] == "비정상", "guide": cells[3] if len(cells) > 3 else "",
                "rule": rule_level in DETECTED,
                "final": {"ABNORMAL": True, "NORMAL": False}.get(result.verdict.state),
                "final_reasons": result.verdict.reasons,
                "ai": (likelihood >= 50) if getattr(ai, "status", None) == "OK" and likelihood is not None else None,
                "ai_cal": ({"ABNORMAL": True, "NORMAL": False}.get(result.ai_judgment.state)
                           if result.ai_judgment is not None else None),
                "ai_threshold": result.ai_judgment.threshold if result.ai_judgment is not None else None,
                "likelihood": likelihood,
                "ai_status": getattr(ai, "status", None), "seconds": round(time.perf_counter() - began, 1),
                "neighbors": [(round(s, 2), c.case.case_type) for s, c, _f in (memory.neighbors if memory else [])][:3],
                "memory_on": bool(memory and memory.calibration and memory.calibration.tau is not None),
                "hypotheses": [h.text for h in answer.hypotheses][:2] if answer is not None else [],
                "reference": bool(result.reference_judgment is not None and result.reference_judgment.used),
            }
            asked, answered = len(result.questions), 0
            if args.answer_questions:
                for question in result.questions:
                    kind = oracle(question, cells)
                    if kind is not None:
                        criteria.save_answer(question, kind, log_ids=[log_id], run_id=result.run_id)
                        answered += 1
            row["asked"], row["answered"] = asked, answered
            results.append(row)
            confirm(log_id, cells)
            mark = lambda said: "-" if said is None else ("O" if said == row["truth"] else "X")  # noqa: E731
            print(f"  {index:>2} {cells[0]} {'비정상' if row['truth'] else '정상  '} 규칙 {mark(row['rule'])} 최종 {mark(row['final'])}"
                  + (f" AI {mark(row['ai'])}({likelihood}) 보정 {mark(row['ai_cal'])}(문턱 {row['ai_threshold']})" if args.ai else "")
                  + (" 참고자료" if row["reference"] else "")
                  + (f" 질문 {asked}(답 {answered})" if asked else "") + f" {row['seconds']}초", flush=True)
            if index % 10 == 0 or index == len(test):
                print(f"  ── 1~{index} 누적: 규칙만 {accuracy(results, 'rule')} · 최종 {accuracy(results, 'final')}"
                      + (f" · AI {accuracy(results, 'ai')} · AI 보정 {accuracy(results, 'ai_cal')}" if args.ai else "")
                      + f" | 최근 {min(10, index)}개: 최종 {accuracy(results[-10:], 'final')}"
                      + (f" · AI {accuracy(results[-10:], 'ai')} · AI 보정 {accuracy(results[-10:], 'ai_cal')}" if args.ai else ""),
                      flush=True)
        print(f"판단 질문: {sum(r['asked'] for r in results)}개 · 답 {sum(r['answered'] for r in results)}개"
              f" · 질문이 나온 분석 {sum(1 for r in results if r['asked'])}건", flush=True)
        half = len(results) // 2
        for title, part in (("전체", results), ("앞 절반", results[:half]), ("뒤 절반", results[half:])):
            print(f"{title}: 규칙만 {accuracy(part, 'rule')} · 최종 {accuracy(part, 'final')}"
                  + (f" · AI {accuracy(part, 'ai')} · AI 보정 {accuracy(part, 'ai_cal')}" if args.ai else ""))
        if args.json:
            args.json.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    finally:
        ctx.close()
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    main()
