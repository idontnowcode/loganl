"""비슷한 정상 로그와 비교 (CR-08 DT-01~04). 데이터는 모두 가상 값(TEST- 이름)이다.

잡아야 할 것(누락·연속 반복·간격·값 범위)과, 정상에서 흔한 차이(가끔 나오는 이벤트·값만 다른 이벤트·카운터)를
잘못 잡지 않는지 함께 본다.
"""

from __future__ import annotations

from app.analysis.reference_checks import ReferenceFile, compare_with_normals
from app.core.event_normalizer import EventTyper
from app.core.feature_extractor import extract
from app.models.enums import EventTimeKind, OriginPurpose, TimestampBasis
from app.models.records import EventRecord, LogFileRecord

TYPER = EventTyper()


def make_file(file_id: int, lines: list[tuple], *, role: str | None = None, step_ms: int = 100):
    """lines: (레벨, 구성요소, 메시지) 또는 (앞 줄과의 간격 ms, 레벨, 구성요소, 메시지)."""
    events, now = [], 0
    for seq, item in enumerate(lines):
        gap, level, component, message = item if len(item) == 4 else (step_ms, *item)
        now += gap * 1000
        events.append(EventRecord(
            id=file_id * 1000 + seq, log_file_id=file_id, seq=seq, line_no=seq + 1, ts_raw=str(now),
            ts_value_us=now, ts_kind=EventTimeKind.ABSOLUTE_NO_TZ, level=level, component=component,
            event_type=TYPER.event_type(message, component), message=message, raw_text=message))
    record = LogFileRecord(id=file_id, session_id=file_id, original_name=f"TEST-{file_id}.log", stored_path="x",
                           sha256="0" * 64, size_bytes=1, log_role=role, timestamp_basis=TimestampBasis.ABSOLUTE_NO_TZ,
                           origin_purpose=OriginPurpose.NORMAL_CASE, imported_at="2026-11-18T00:00:00.000Z")
    return extract(record, events), events


def run_lines(cycles: int, *, mode: str = "A", values=(41, 43, 42, 44, 40), heartbeats=(), waits=0, gap_ms=100):
    out = [("INFO", "TEST-CTRL", f"TEST-START mode={mode} step=1"), ("INFO", "TEST-SUB", "TEST-READY slot=2")]
    for cycle in range(1, cycles + 1):
        out.append((gap_ms, "INFO", "TEST-CTRL", f"TEST-RUN cycle={cycle} value={values[(cycle - 1) % len(values)]}"))
        if cycle in heartbeats:
            out.append(("DEBUG", "TEST-SUB", "TEST-HEARTBEAT seq=4821"))
        out += [("WARN", "TEST-SUB", "TEST-WAIT busy retry=1")] * (waits if cycle == 1 else 0)
        out.append(("INFO", "TEST-SUB", f"TEST-CHECK cycle={cycle} result=OK"))
    return out + [("INFO", "TEST-CTRL", "TEST-STOP step=9"), ("INFO", "TEST-CTRL", "TEST-SUMMARY result=OK")]


def references(*line_sets: list[tuple], role: str | None = None) -> list[ReferenceFile]:
    return [ReferenceFile(*make_file(100 + i, lines, role=role), case_id=100 + i) for i, lines in enumerate(line_sets)]


REFS = (run_lines(2), run_lines(3), run_lines(4), run_lines(5))


def check(target_lines: list[tuple], refs: list[ReferenceFile] | None = None, *, role: str | None = None):
    features, events = make_file(1, target_lines, role=role)
    return compare_with_normals(features, events, refs if refs is not None else references(*REFS))


def test_normal_run_has_no_findings() -> None:
    assert check(run_lines(6, heartbeats=(2,), waits=1)) == []


def test_truncated_log_misses_steady_events() -> None:  # DT-01
    findings = check(run_lines(3)[:-2])
    assert {f.finding_type for f in findings} == {"MISSING_EVENT"}
    assert all(f.stance == "SUPPORTS" and f.severity == "SUSPECT" for f in findings)  # 승인 규칙이 아니므로 '의심'
    assert [f.detail["event_type"] for f in findings] == ["TEST-CTRL|TEST-STOP", "TEST-CTRL|TEST-SUMMARY"]  # 이벤트 이름 단위
    assert findings[0].detail["basis"] == "REFERENCE_NORMAL" and findings[0].detail["reference_case_ids"] == [100, 101, 102, 103]


def test_occasional_event_and_value_variant_are_not_missing() -> None:  # DT-01 오탐 방지
    refs = references(run_lines(2, mode="C", heartbeats=(1,)), run_lines(3, mode="C", heartbeats=(1, 2)),
                      run_lines(4, mode="C", heartbeats=(3,)), run_lines(5, mode="C", heartbeats=(2, 4)))
    # 상태 알림은 기준 4개 모두에 있지만 횟수가 1·2·1·2로 달라 '늘 있는 것'이 아니고, 시작은 mode 값만 다르다
    assert check(run_lines(2, mode="A"), refs) == []


def test_missing_needs_four_references() -> None:
    assert check(run_lines(3)[:-2], references(*REFS[:3])) == []


def test_burst_of_same_event() -> None:  # DT-02
    findings = check(run_lines(1, waits=6))
    assert [f.finding_type for f in findings] == ["REPETITION"]
    assert findings[0].detail["run"] == 6 and findings[0].detail["reference_max_run"] == 0
    assert check(run_lines(1, waits=2)) == []  # 2번 연속은 넘어간다


def test_long_gap_between_events() -> None:  # DT-03
    lines = run_lines(2)
    lines[3] = (45_000, *lines[3][-3:])  # 첫 사이클 점검 줄 앞에 45초 공백
    findings = check(lines)
    assert [f.finding_type for f in findings] == ["LATENCY"]
    assert findings[0].detail["gap_us"] == 45_000_000 and findings[0].detail["reference_max_gap_us"] == 100_000
    assert check(run_lines(2, gap_ms=300)) == []  # 정상 최대 간격의 5배 이하


def test_value_far_outside_observed_range() -> None:  # DT-04
    findings = check(run_lines(3, values=(42, 418, 41)))
    assert [f.finding_type for f in findings] == ["VALUE_OUT_OF_RANGE"]
    detail = findings[0].detail
    assert detail["field"] == "value" and detail["value"] == 418 and (detail["observed_min"], detail["observed_max"]) == (40, 44)
    assert findings[0].line_start == 5


def test_counters_and_small_differences_are_not_values_out_of_range() -> None:
    # cycle은 기준에서 1~4지만 1씩 오르는 카운터라 12까지 가도 범위로 보지 않는다. value 46은 허용 폭 안이다.
    assert check(run_lines(12, values=(46, 40, 41))) == []


def test_only_same_role_references_are_used() -> None:
    assert check(run_lines(3)[:-2], references(*REFS, role="TEST-ROLE-B"), role="TEST-ROLE-A") == []


def by_kind(findings) -> dict[str, list]:
    out: dict[str, list] = {}
    for f in findings:
        out.setdefault(f"{f.finding_type}/{f.detail.get('kind', '')}", []).append(f)
    return out


def test_steady_event_repeated_more_often() -> None:  # 재시작·재작업 반복
    lines = run_lines(3)
    lines[4:4] = [("INFO", "TEST-CTRL", "TEST-START mode=A step=1")]
    lines[8:8] = [("INFO", "TEST-CTRL", "TEST-START mode=A step=1")]
    found = by_kind(check(lines))
    assert [(f.detail["count"], f.detail["reference_count"]) for f in found["REPETITION/count"]] == [(3, 1)]


def test_first_order_swapped() -> None:
    lines = run_lines(3)
    lines[0], lines[1] = lines[1], lines[0]  # 준비가 시작보다 먼저
    found = by_kind(check(lines))
    assert [(f.detail["before"], f.detail["after"]) for f in found["ORDER_VIOLATION/"]] == [
        ("TEST-CTRL|TEST-START", "TEST-SUB|TEST-READY")]


def test_share_drop_of_regular_event() -> None:
    lines = [line for line in run_lines(40) if "TEST-CHECK" not in line[-1]]  # 정상 비율이면 10번 넘게 나왔을 이벤트가 0번
    found = by_kind(check(lines))
    assert [f.detail["event_type"] for f in found["MISSING_EVENT/share"]] == ["TEST-SUB|TEST-CHECK"]
    short = [line for line in run_lines(5) if "TEST-CHECK" not in line[-1]]  # 짧아도 핵심 이벤트(20% 이상)가 아예 없으면
    assert "MISSING_EVENT/share" in by_kind(check(short))


def test_missing_stage_is_not_hidden_by_same_text_elsewhere() -> None:
    refs = references(*[[("INFO", f"TEST-ST{s}", "TEST-ENTER") for s in range(1, 5)] for _ in range(4)])
    found = check([("INFO", f"TEST-ST{s}", "TEST-ENTER") for s in (1, 2, 4)], refs)
    assert [f.detail["event_type"] for f in found if f.finding_type == "MISSING_EVENT"] == ["TEST-ST3|TEST-ENTER"]


def test_flow_per_number_missing_duplicate_and_swapped() -> None:
    lines = [line for line in run_lines(4) if line[-1] != "TEST-CHECK cycle=3 result=OK"]  # 3번 사이클 점검 없음
    found = by_kind(check(lines))
    assert [(f.detail["field"], f.detail["values"]) for f in found["MISSING_EVENT/flow"]] == [("cycle", ["3"])]

    lines = run_lines(4)
    lines.insert(9, ("INFO", "TEST-SUB", "TEST-CHECK cycle=2 result=OK"))  # 2번 사이클 점검이 두 번
    assert [f.detail["values"] for f in by_kind(check(lines))["REPETITION/flow"]] == [["2"]]

    lines = run_lines(4)
    lines[4], lines[5] = lines[5], lines[4]  # 2번 사이클 점검이 동작보다 먼저
    assert [f.detail["values"] for f in by_kind(check(lines))["ORDER_VIOLATION/flow"]] == [["2"]]


FLOATS = (41.2, 43.7, 40.9, 42.4, 44.1, 41.8, 43.0, 40.3, 42.9, 44.6, 41.5, 43.3)


def test_value_that_stopped_changing() -> None:
    refs = references(run_lines(6, values=FLOATS), run_lines(7, values=FLOATS[3:] + FLOATS[:3]),
                      run_lines(8, values=FLOATS[6:] + FLOATS[:6]))
    found = by_kind(check(run_lines(7, values=(42.4,) * 7), refs))
    assert [(f.detail["field"], f.detail["run"]) for f in found["VALUE_STUCK/"]] == [("value", 7)]
    assert check(run_lines(7, values=FLOATS[2:]), refs) == []  # 늘 변하면 넘어간다


def test_status_word_that_was_always_the_same_changed() -> None:  # DT-10
    many = references(*[run_lines(2 + i % 4) for i in range(8)])  # 기준 8개: 점검 결과가 늘 OK
    lines = run_lines(3)
    lines[5] = ("INFO", "TEST-SUB", "TEST-CHECK cycle=2 result=NG")  # 레벨은 INFO 그대로
    found = by_kind(check(lines, many))
    assert [(f.detail["field"], f.detail["value"], f.detail["expected"]) for f in found["VALUE_OUT_OF_RANGE/category"]] == [
        ("result", "NG", "OK")]
    assert "VALUE_OUT_OF_RANGE/category" not in by_kind(check(lines))  # 기준 4개로는 상태값을 판단하지 않는다
    varied = references(*[run_lines(3, mode="ABC"[i % 3]) for i in range(9)])
    assert "VALUE_OUT_OF_RANGE/category" not in by_kind(check(run_lines(3, mode="B"), varied))  # 여러 값이 나오던 칸


def channel_lines(cycles: int, *, drop_after: int | None = None) -> list[tuple]:
    out = [("INFO", "TEST-AGG", "TEST-START")]
    for cycle in range(1, cycles + 1):
        for ch in (1, 2, 3):
            if drop_after is None or ch != 3 or cycle <= drop_after:
                out.append(("INFO", f"TEST-CH{ch}", f"TEST-SAMPLE val={40 + ch + cycle % 3}"))
    return out + [("INFO", "TEST-AGG", "TEST-STOP")]


def test_counts_that_always_matched_now_differ() -> None:  # DT-14 (채널 하나 끊김)
    refs = references(*[channel_lines(n) for n in (5, 7, 9, 11)])
    found = by_kind(check(channel_lines(10, drop_after=3), refs))
    detail = found["MISSING_EVENT/equal_counts"][0].detail
    assert detail["event_type"] == "TEST-CH3|TEST-SAMPLE" and detail["usual"] == 10
    assert check(channel_lines(8), refs) == []  # 횟수는 달라도 서로 같으면 정상


def test_event_present_in_every_reference_is_absent() -> None:  # DT-13
    def run(n: int, worker3: bool = True) -> list[tuple]:
        lines = [("INFO", "TEST-Q", "TEST-OPEN")]
        for job in range(n):
            worker = 1 + job % (3 if worker3 else 2)
            lines += [("INFO", "TEST-Q", f"TEST-ACCEPT job={job}"), ("INFO", f"TEST-W{worker}", f"TEST-DONE job={job}")]
        return lines + [("INFO", "TEST-Q", "TEST-CLOSE")]
    refs = references(*[run(6 + i) for i in range(10)])
    found = by_kind(check(run(9, worker3=False), refs))
    assert [f.detail["event_type"] for f in found["MISSING_EVENT/absent"]] == ["TEST-W3|TEST-DONE"]
    assert "MISSING_EVENT/absent" not in by_kind(check(run(9, worker3=False), refs[:9]))  # 기준 9개로는 보지 않는다


# ── 2026-11-22 CT 유사 세트 오탐: 설정값·다시 세는 번호·횟수가 원래 들쭉날쭉한 이벤트를 '겹침'으로 오해 ──
def scan_lines(series: list[tuple[str, int]], kvs: tuple[int, ...] = (100, 120, 80, 140)) -> list[tuple]:
    """가상 촬영 흐름: 촬영(series)마다 설정 kv, 축은 단계(step=1부터 다시), 나선은 회전 줄(TEST-ROT) n번."""
    out = [("INFO", "TEST-SYS", "TEST-SESSION-START")]
    for number, (kind, n) in enumerate(series, start=1):
        kv = kvs[(number - 1) % len(kvs)]
        out += [("INFO", "TEST-SYS", f"TEST-SERIES-START series={number}"),
                ("INFO", "TEST-HVG", f"TEST-PREP kv={kv}"), ("INFO", "TEST-HVG", f"TEST-READY kv={kv}")]
        if kind == "axial":
            for step in range(1, n + 1):
                out += [("INFO", "TEST-HVG", f"TEST-EXPOSE series={number} step={step}"),
                        ("INFO", "TEST-DET", f"TEST-ACQ series={number} step={step}")]
        else:
            out.append(("INFO", "TEST-HVG", f"TEST-EXPOSE series={number}"))
            out += [("INFO", "TEST-DET", f"TEST-ROT series={number}")] * n
            out.append(("INFO", "TEST-DET", f"TEST-ACQ series={number}"))
        out.append(("INFO", "TEST-SYS", f"TEST-SERIES-END series={number}"))
    return out + [("INFO", "TEST-SYS", "TEST-SESSION-END")]


def flow_findings(found) -> list:
    return [f for f in found if f.detail.get("kind") == "flow"]


def test_reused_setting_value_is_not_a_duplicate_flow() -> None:
    refs = references(*[scan_lines([("helical", 5), ("helical", 6), ("helical", 7)]) for _ in range(4)])
    target = scan_lines([("helical", 5), ("helical", 6), ("helical", 7), ("helical", 6)], kvs=(100, 120, 100, 80))
    assert flow_findings(check(target, refs)) == []  # kv=100을 두 번 써도 PREP·READY 겹침이 아니다


def test_counter_restarting_per_group_is_not_a_duplicate_flow() -> None:
    refs = references(*[scan_lines([("axial", n)]) for n in (4, 5, 6, 4)])
    found = check(scan_lines([("axial", 4), ("axial", 5)]), refs)  # 촬영마다 step=1부터 다시
    assert [f for f in flow_findings(found) if f.detail["field"] == "step"] == []


def test_naturally_varying_count_is_not_a_duplicate_but_real_one_is() -> None:
    refs = references(*[scan_lines([("helical", 4 + i), ("helical", 9 + 2 * i), ("helical", 15 + i)]) for i in range(4)])
    long_scan = scan_lines([("helical", 5), ("helical", 40), ("helical", 10)])  # 회전 줄은 길이에 따라 원래 다르다
    assert flow_findings(check(long_scan, refs)) == []
    doubled = scan_lines([("helical", 5), ("helical", 10), ("helical", 12)])
    doubled.insert(doubled.index(("INFO", "TEST-DET", "TEST-ACQ series=2")), ("INFO", "TEST-DET", "TEST-ACQ series=2"))
    found = by_kind(check(doubled, refs))  # 늘 한 번이던 마지막 단계가 두 번 → 여전히 겹침
    assert [(f.detail["field"], f.detail["values"]) for f in found["REPETITION/flow"]] == [("series", ["2"])]


# ── CR-13: 정상에 없던 경고(DT-18), 통째로 빠진 구성요소(DT-19) — 기준 10개 이상일 때만 ──
TEN_REFS = tuple(run_lines(2 + i % 4) for i in range(10))


def test_new_warning_absent_from_all_references() -> None:
    lines = run_lines(4)
    lines.insert(5, ("WARN", "TEST-SUB", "TEST-HEAT-HIGH level=93 wait_s=180"))  # 정상 10개 어디에도 없던 경고
    found = by_kind(check(lines, references(*TEN_REFS)))
    assert [f.detail["event_type"] for f in found["EXTRA_EVENT/new_warning"]] == ["TEST-SUB|TEST-HEAT-HIGH level=<N> wait_s=<N>"]
    assert found["EXTRA_EVENT/new_warning"][0].stance == "SUPPORTS"
    assert "EXTRA_EVENT/new_warning" not in by_kind(check(lines))  # 기준 4개면 쓰지 않는다(가끔 나오는 경고일 수 있음)


def test_component_missing_entirely() -> None:
    lines = [line for line in run_lines(4) if line[-2] != "TEST-SUB"]  # TEST-SUB 구성요소가 한 줄도 없음
    found = by_kind(check(lines, references(*TEN_REFS)))
    assert [f.detail["component"] for f in found["MISSING_EVENT/component_absent"]] == ["TEST-SUB"]
    assert "MISSING_EVENT/component_absent" not in by_kind(check(run_lines(4), references(*TEN_REFS)))
