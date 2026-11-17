"""HTML 보고서. 인터넷 없이 열리도록 CSS를 안에 넣고 외부 링크·스크립트를 쓰지 않는다(REQUIREMENTS G-7).
모든 글자는 HTML 이스케이프한다(로그 원문에 태그가 있어도 실행되지 않음)."""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any

CSS = """
body{font-family:"Malgun Gothic","Apple SD Gothic Neo",sans-serif;margin:24px;color:#1f1f1f;background:#fff;line-height:1.5}
h1{font-size:20px;margin:0 0 4px} h2{font-size:16px;margin:24px 0 8px;border-bottom:1px solid #ddd;padding-bottom:4px}
.meta{color:#5f6368;font-size:13px} table{border-collapse:collapse;width:100%;font-size:13px}
td,th{border:1px solid #ddd;padding:4px 6px;vertical-align:top;text-align:left} th{background:#f5f5f5}
.guidance{border:2px solid #1a73e8;padding:12px;border-radius:6px;background:#f8fbff}
.none{border-color:#999;background:#fafafa} .notice{color:#5f6368;font-size:12px;margin-top:24px}
.key{font-family:Consolas,monospace;color:#5f6368} li{margin:2px 0}
"""


def _e(value: Any) -> str:
    return escape("" if value is None else str(value))


def render_html(data: dict[str, Any]) -> str:
    run = data["run"]
    guidance = data["guidance"]
    level_class = "guidance" + (" none" if guidance["level"] in ("NONE", "NOT_EVALUATED") else "")
    parts = [
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'>",
        f"<title>분석 보고서 {_e(run['label'])}</title><style>{CSS}</style></head><body>",
        f"<h1>로그 분석 보고서 {_e(run['label'])}</h1>",
        f"<div class='meta'>{_e(data['app'])} · {_e(data['plan'])} · 만든 시각 {_e(data['generated_at'])}<br>"
        f"상태 {_e(run['status'])} · 운전 모드 {_e(run['mode'] or '모름')}{' (확인됨)' if run['mode_confirmed'] else ''}</div>",
        f"<h2>권장 대응 — {_e(data['axes']['response_guidance']['text'])}</h2><div class='{level_class}'>",
    ]
    if guidance["items"]:
        parts.append("<ol style='list-style:none;padding-left:0'>")
        for item in guidance["items"]:
            parts.append(f"<li><b>[{_e(item['label'])}]</b> {_e(item['text'])} "
                         f"<span class='key'>({_e(item['key'])} · 출처 {_e(item['case'])} · {_e(item['source'] or '-')})</span></li>")
        parts.append("</ol>")
    else:
        parts.append("<p>등록된 대응 방법이 없습니다.</p>")
    for check in guidance["checks"]:
        parts.append(f"<p>원인 확인: {_e(check['text'])} <span class='key'>{_e(check['key'])}</span></p>")
    parts.append("</div>")
    if data.get("references"):
        parts.append("<h2>참고 자료 — 로그에 나온 코드 (승인된 참고 자료, 권장 대응과 별개)</h2><table>"
                     "<tr><th>코드</th><th>횟수·처음 위치</th><th>명칭</th><th>설명</th><th>원인</th><th>조치</th><th>출처</th></tr>")
        for ref in data["references"]:
            for index, entry in enumerate(ref["entries"]):
                head = (f"<td class='key'>{_e(ref['code'])}</td><td>{_e(ref['count'])}회 · {_e(ref['file'])} {_e(ref['line'])}행</td>"
                        if index == 0 else "<td></td><td></td>")
                source = entry["document_title"] + (f" · {entry['locator']}" if entry.get("locator") else "")
                parts.append(f"<tr>{head}" + "".join(f"<td>{_e(entry.get(name) or '')}</td>"
                                                       for name in ("title", "description", "cause", "action"))
                             + f"<td>{_e(source)}</td></tr>")
        parts.append("</table>")
    parts.append("<h2>결과 분류 (서로 독립)</h2><table>")
    parts += [f"<tr><th>{_e(a['title'])}</th><td>{_e(a['text'])}</td></tr>" for a in data["axes"].values()]
    parts.append("</table><h2>입력 로그</h2><table><tr><th>파일</th><th>세션</th><th>시각 기준</th><th>SHA-256</th></tr>")
    parts += [f"<tr><td>{_e(f['name'])}</td><td>{_e(f['session'])}</td><td>{_e(f['timestamp_basis'])}</td>"
              f"<td class='key'>{_e(f['sha256'])}</td></tr>" for f in data["files"]]
    parts.append("</table><h2>근거</h2><table><tr><th>키</th><th>계층</th><th>종류</th><th>내용</th><th>행</th></tr>")
    parts += [f"<tr><td class='key'>{_e(f['key'])}</td><td>{_e(f['layer'])}</td><td>{_e(f['type'])}</td>"
              f"<td>{_e(f['summary'])}</td><td>{_e(f['line'])}</td></tr>" for f in data["findings"]]
    ai = data["ai"]
    parts.append(f"</table><h2>AI 원인 가설 — {_e(ai['status_text'])} {_e(ai['model'] or '')}</h2>")
    for title, items in (("관측 사실", ai["observations"]), ("원인 가설", ai["hypotheses"]),
                         ("AI가 정리한 대응 순서(승인된 매뉴얼·가이드 근거)", ai["response_plan"]),
                         ("추가 확인 절차", ai["verification_steps"]), ("판단 불가", ai["unknowns"])):
        if not items:
            continue
        parts.append(f"<h3>{_e(title)}</h3><ul>")
        for item in items:
            counter = f" · 반대 근거 {_e(', '.join(item['counter']))}" if item.get("counter") else ""
            tag = "" if item.get("manual_based") or title != "추가 확인 절차" else " (AI 제안 — 매뉴얼 근거 없음)"
            parts.append(f"<li>{_e(item['text'])}{_e(tag)} <span class='key'>[{_e(', '.join(item['cites']))}]{counter}</span></li>")
        parts.append("</ul>")
    parts.append(f"<p class='notice'>{_e(data['notice'])}</p></body></html>")
    return "".join(parts)


def write_html(data: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(data), encoding="utf-8")
    return path
