"""보고서 데이터 조립. 화면의 메모리 결과가 아니라 **DB에 저장된 분석 실행**에서 다시 만든다.
그래서 분석 이력에서 다시 열어도, 보고서로 저장해도 같은 내용이다(UI-13 "분석 결과 재확인")."""

from __future__ import annotations

import json
import re
from contextlib import closing
from typing import Any

from app import APP_NAME, __version__
from app.models import utc_now_iso
from app.storage.database import Database
from app.analysis.error_messages import attach_context, presumed_errors, reference_texts, should_show
from app.storage.repositories import AnalysisRepository, CaseRepository, EventRepository, LogFileRepository, ModeRepository

AXIS_TEXT = {
    "normal_match": ("정상 패턴 일치", {"MATCH": "일치", "PARTIAL": "일부 일치", "NO_MATCH": "불일치", "NOT_EVALUATED": "판단 못 함"}),
    "anomaly_level": ("이상 수준", {"NONE_FOUND": "찾은 이상 없음(정상 판정 아님)", "SUSPECTED": "비정상 의심",
                                   "RULE_VIOLATION": "승인된 규칙 위반", "NOT_EVALUATED": "판단 못 함"}),
    "similar_abnormal": ("기존 비정상 사례", {"FOUND": "유사 사례 있음", "NOT_FOUND": "유사 사례 없음", "NOT_EVALUATED": "승인된 비정상 사례 없음"}),
    "evidence_sufficiency": ("근거 충분성", {"SUFFICIENT": "충분", "PARTIAL": "일부 부족", "INSUFFICIENT": "판단 불가(근거 부족)", "NOT_EVALUATED": "-"}),
    "response_guidance": ("권장 대응 근거", {"MANUAL": "승인된 대응 매뉴얼", "GUIDE_ONLY": "진단 가이드 해결 방법만",
                                        "NONE": "등록된 대응 방법 없음", "NOT_EVALUATED": "-"}),
}
SECTION_TEXT = {"PRECAUTION": "주의", "RESPONSE_STEP": "절차", "VERIFICATION": "조치 후 확인", "ESCALATION": "상위 지원",
                "RESOLUTION": "해결 방법", "CHECK_METHOD": "원인 확인 방법", "CAUSE": "원인", "SYMPTOM": "증상",
                "CRITERIA": "판정 기준"}
AI_STATUS_TEXT = {"NOT_RUN": "실행 안 함", "OK": "검증 통과", "UNAVAILABLE": "AI 사용 불가",
                  "VALIDATION_FAILED": "검증 실패(저장 안 함)", "CANCELLED": "취소"}
NOTICE = ("이 보고서는 로컬에서 만든 분석 결과다. 유사도 점수는 순위 점수이며 진단 확률이 아니다. "
          "AI 가설은 근거 키가 유효해도 해석이 옳다는 보장이 없으므로 사용자가 검토해야 한다. "
          "권장 대응은 승인된 대응 매뉴얼·진단 가이드 항목에서만 가져왔다.")


def load_report(database: Database, run_id: int) -> dict[str, Any]:
    with closing(database.connect()) as conn:
        analysis = AnalysisRepository(conn)
        run = analysis.get(run_id)
        if run is None:
            raise ValueError(f"분석 실행 R-{run_id:04d}이(가) 없습니다.")
        rows = analysis.evidence(run_id)
        files = LogFileRepository(conn).get_many(analysis.files(run_id))
        mode = ModeRepository(conn).get(run["mode_id"]) if run["mode_id"] else None
        case_ids = sorted({r["case_id"] for r in rows if r["case_id"]})
        cases = {c: CaseRepository(conn).get(c) for c in case_ids}

    def detail(row) -> dict[str, Any]:
        return json.loads(row["detail_json"]) if row["detail_json"] else {}

    response = [r for r in rows if r["finding_type"] == "RESPONSE_REFERENCE"]
    response.sort(key=lambda r: detail(r).get("order", 0))
    guidance_items = []
    step = 0
    for r in response:
        section = detail(r).get("section")
        label = SECTION_TEXT.get(section, section)
        if section in ("RESPONSE_STEP", "RESOLUTION"):
            step += 1
            label = f"{step}. {label}"
        case = cases.get(r["case_id"])
        text = r["summary"].split("] ", 1)[-1].split(": ", 1)[-1]
        text = re.sub(r"^\s*(?:\d{1,3}[.)]|[-*•]|[①-⑳])\s*", "", text, count=1)  # 표시용 번호·글머리 제거
        guidance_items.append({"key": r["evidence_key"], "label": label, "text": text,
                               "case": case.label if case else None, "source": detail(r).get("source")})
    checks = [{"key": r["evidence_key"], "text": r["summary"]} for r in rows
              if r["finding_type"] == "GUIDE_REFERENCE" and detail(r).get("section") == "CHECK_METHOD"]
    names = {f.id: f.original_name for f in files}
    passages = [{"key": r["evidence_key"], **detail(r)} for r in rows
                if r["finding_type"] == "TEXT_MATCH" and detail(r).get("role") == "REFERENCE_PASSAGE"]
    references = [{"key": r["evidence_key"], "code": detail(r).get("code"), "count": detail(r).get("count"),
                   "file": names.get(r["log_file_id"]), "line": r["line_start"], "entries": detail(r).get("entries", [])}
                  for r in rows if r["finding_type"] == "TEXT_MATCH" and detail(r).get("role") == "REFERENCE_MATCH"]
    ai_rows = [r for r in rows if r["layer"] == "AI"]

    def ai_items(kind: str, role: str | None = None) -> list[dict[str, Any]]:
        out = []
        for r in ai_rows:
            d = detail(r)
            wanted = d.get("role") == role if role is not None else d.get("role") != "ABNORMAL_LIKELIHOOD"
            if r["finding_type"] == kind and wanted:
                out.append({"key": r["evidence_key"], "text": r["summary"], "cites": d.get("cites", []),
                            "counter": d.get("counter", []), "manual_based": d.get("manual_based"),
                            "likelihood": d.get("likelihood")})
        return out

    overall = ai_items("AI_HYPOTHESIS", "ABNORMAL_LIKELIHOOD")
    # CR-11 EM-03: 비정상으로 보인 로그는 예상 에러 메시지(화면과 같은 규칙, 저장된 근거에서 다시 고름)
    errors = None
    if should_show(run["anomaly_level"], overall[0]["likelihood"] if overall else None):
        deterministic = [r for r in rows if r["layer"] == "DETERMINISTIC"]
        with closing(database.connect()) as conn:
            raw = {e.id: e.raw_text for e in EventRepository(conn).get_many(
                [r["event_id"] for r in deterministic if r["event_id"]])}
        picked = presumed_errors(deterministic, {i: r["evidence_key"] for i, r in enumerate(deterministic)}, raw,
                                 details=[detail(r) for r in deterministic], references=reference_texts(references))
        attach_context(database, picked)  # CR-13 SQ-01
        errors = [m.as_dict() for m in picked]

    return {
        "app": f"{APP_NAME} {__version__}",
        "plan": "LLA-DEV-PLAN-001 v0.2 R01 + CR-01·04·06",
        "generated_at": utc_now_iso(),
        "run": {"id": run_id, "label": f"R-{run_id:04d}", "status": run["status"], "started_at": run["started_at"],
                "finished_at": run["finished_at"], "mode": mode.name if mode else None,
                "mode_confirmed": bool(run["mode_confirmed"]), "operating_conditions": run["operating_conditions"],
                "user_description": run["user_description"], "error_message": run["error_message"]},
        "files": [{"id": f.id, "name": f.original_name, "sha256": f.sha256, "session": f.session_label,
                   "timestamp_basis": str(f.timestamp_basis), "role": f.log_role} for f in files],
        "axes": {axis: {"title": title, "value": run[axis], "text": labels.get(run[axis], run[axis])}
                 for axis, (title, labels) in AXIS_TEXT.items()},
        "guidance": {"level": run["response_guidance"], "items": guidance_items, "checks": checks},
        "errors": errors,  # None이면 비정상으로 보이지 않아 고르지 않음
        "references": references,
        "passages": passages,
        "findings": [{"key": r["evidence_key"], "layer": r["layer"], "type": r["finding_type"], "summary": r["summary"],
                      "file_id": r["log_file_id"], "line": r["line_start"], "case": cases[r["case_id"]].label
                      if r["case_id"] in cases else None, "score": r["similarity"]}
                     for r in rows if r["layer"] != "AI"],
        "ai": {"status": run["ai_status"], "status_text": AI_STATUS_TEXT.get(run["ai_status"], run["ai_status"]),
               "model": run["ai_model"], "observations": ai_items("AI_OBSERVATION"),
               "hypotheses": ai_items("AI_HYPOTHESIS"), "response_plan": ai_items("AI_VERIFICATION_STEP", "RESPONSE_PLAN"),
               "verification_steps": ai_items("AI_VERIFICATION_STEP", "VERIFICATION"), "unknowns": ai_items("AI_UNKNOWN"),
               "abnormal_likelihood": overall[0] if overall else None},
        "notice": NOTICE,
    }


def save_reports(database: Database, paths, run_id: int) -> tuple[str, str]:
    """`analysis/R-xxxx/report.json`·`report.html`을 쓰고 분석 실행에 경로를 기록한다(data_root 기준 상대 경로)."""
    from app.reports.html_exporter import write_html
    from app.reports.json_exporter import write_json
    from app.storage.database import transaction

    data = load_report(database, run_id)
    folder = paths.analysis / f"R-{run_id:04d}"
    json_path = write_json(data, folder / "report.json")
    html_path = write_html(data, folder / "report.html")
    rel_json = json_path.relative_to(paths.root).as_posix()
    rel_html = html_path.relative_to(paths.root).as_posix()
    with closing(database.connect()) as conn, transaction(conn):
        AnalysisRepository(conn).update(run_id, report_json_path=rel_json, report_html_path=rel_html)
    return rel_json, rel_html


REFERENCE_FIELDS = (("title", "명칭"), ("description", "설명"), ("cause", "원인"), ("action", "조치"))


def reference_lines(ref: dict[str, Any]) -> list[str]:
    """참고 자료 일치 하나를 글 줄로: 코드·횟수·첫 위치, 출처마다 명칭·설명·원인·조치."""
    lines = [f"{ref['code']} ×{ref['count']} (처음 {ref['file']} {ref['line']}행)"]
    for entry in ref["entries"]:
        values = " / ".join(f"{label}: {entry[name]}" for name, label in REFERENCE_FIELDS if entry.get(name))
        source = entry["document_title"] + (f" · {entry['locator']}" if entry.get("locator") else "")
        lines.append(f"  {values or '(설명 없음)'}  [출처 {source}]")
    return lines


def passage_line(passage: dict[str, Any]) -> str:
    source = passage["document_title"] + (f" · {passage['locator']}" if passage.get("locator") else "")
    return f"[{source}] {passage['body'][:300]}  (겹친 낱말: {', '.join(passage.get('matched', [])[:6])})"


def report_text(data: dict[str, Any]) -> str:
    """화면의 '결과 다시 보기'용 짧은 글."""
    lines = [f"{data['run']['label']} · {data['run']['status']} · 시작 {data['run']['started_at']}"]
    lines.append("파일: " + ", ".join(f["name"] for f in data["files"]))
    lines.append("")
    lines.append(f"[권장 대응 — {data['axes']['response_guidance']['text']}]")
    lines += [f"  [{i['label']}] {i['text']}  (출처 {i['case']} · {i['source'] or '-'})" for i in data["guidance"]["items"]]
    if data.get("errors"):
        lines.append("")
        lines.append("[예상 에러 메시지 — 문제로 보이는 원문 줄]")
        for m in data["errors"]:
            where = f"{m['line']}행" if m["line"] is not None else "—"
            lines.append(f"  [{m['group_title']}] {where}  {m['text']}  (이유: {m['reason']})"
                         + (f"  직전 단계: {m['stage_line']}행 {m['stage']}" if m.get("stage") else "")
                         + (f"  참고 자료: {m['reference']}" if m.get("reference") else ""))
    if data.get("references"):
        lines.append("")
        lines.append("[참고 자료 — 로그에 나온 코드]")
        lines += [f"  {line}" for ref in data["references"] for line in reference_lines(ref)]
    if data.get("passages"):
        lines.append("")
        lines.append("[참고 자료 — 관련 문서 내용]")
        lines += [f"  {passage_line(p)}" for p in data["passages"]]
    lines.append("")
    lines += [f"{a['title']}: {a['text']}" for a in data["axes"].values()]
    lines.append("")
    lines.append(f"AI: {data['ai']['status_text']}")
    overall = data["ai"].get("abnormal_likelihood")
    if overall:
        lines.append(f"  비정상일 가능성 {overall['likelihood']}% (AI 추정치 · 통계 확률 아님): {overall['text']}"
                     f" [{', '.join(overall['cites'])}]")
    lines += [f"  가설{(' ' + str(h['likelihood']) + '%') if h.get('likelihood') is not None else ''}: {h['text']}"
              f" [{', '.join(h['cites'])}]" for h in data["ai"]["hypotheses"]]
    lines.append("")
    lines.append(data["notice"])
    return "\n".join(lines)
