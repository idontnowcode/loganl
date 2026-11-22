# 데이터베이스 설계 — Local Log Analyzer

- 기준: [DEV_PLAN_v0.2_R01.md](DEV_PLAN_v0.2_R01.md) §4·§5, 실제 정의는 [`app/storage/migrations/0001_initial_schema.sql`](../app/storage/migrations/0001_initial_schema.sql)
- 파일 위치: `<data_root>\database\knowledge.db` (기본 `%LOCALAPPDATA%\LocalLogAnalyzer\database\knowledge.db`)
- 상태: 2026-11-16 스키마 v1 확정. 이 문서와 SQL이 다르면 SQL이 맞고, 문서를 고친다.

## 1. 공통 규칙

| 항목 | 규칙 |
|---|---|
| 테이블 | 전부 `STRICT`(SQLite 3.37 이상). 열 타입이 다르면 저장되지 않는다. |
| 키 | `INTEGER PRIMARY KEY`. 화면에는 `C-0001`(사례), `R-0001`(분석)처럼 접두어를 붙여 보여 준다. |
| 시각 | UTC ISO-8601 문자열 `YYYY-MM-DDTHH:MM:SS.fffZ` |
| JSON 열 | 이름이 `_json`으로 끝나고 `json_valid()` CHECK가 걸려 있다. 내용 구조는 Pydantic 모델로 검증한다. |
| 연결 설정 | `foreign_keys=ON`, `journal_mode=WAL`, `busy_timeout=5000`, `synchronous=NORMAL` |
| 열거형 | DB CHECK 값과 `app/models/enums.py` 열거형이 같아야 한다(`test_schema`가 검사). |
| 변경 | 번호 붙은 SQL 파일로만 바꾼다. 적용된 파일을 고치면 앱이 시작을 거부한다(체크섬). DB가 앱보다 새 버전이어도 거부한다. |

## 2. 관계 (§5.2)

```text
collection_sessions 1───N log_files 1───N events
                              │               │
                              N               N
cases 1───N case_log_files ───┘   cases 1───N case_events
  │
  ├──1───N case_notes
  ├──1───N guide_attachments 1───N diagnostic_guides (구조화된 항목)
  │        (진단 가이드 필수 · 대응 매뉴얼 선택)
  ├──1───N learning_questions 1───N learning_answers
  ├──1───N case_revisions          (추가 전용)
  ├──N───1 mode_definitions 1───N event_patterns ───N──1 cases(source_case_id)
  └──1───N knowledge_claims 1───N claim_evidence ──→ events / log_files 행 / 가이드 구간 / 답변 / 사례 / 메모

analysis_runs 1───N analysis_run_files N───1 log_files
analysis_runs 1───N analysis_evidence ──→ log_files / events / cases / event_patterns / diagnostic_guides
analysis_runs 1───N knowledge_claims

audit_history (모든 엔터티의 승인·수정·삭제 기록, 추가 전용)
```

계획서 §5.2의 관계 8개가 어디서 성립하는지:

| 계획서 관계 | 구현 |
|---|---|
| Case → 여러 LogFile | `case_log_files` (사례 쪽 CASCADE, 파일 쪽 RESTRICT) |
| LogFile → 여러 Event | `events.log_file_id` |
| CollectionSession → 여러 LogFile | `log_files.session_id` (NOT NULL) |
| Case → 여러 세션의 로그 | `case_log_files`로 묶인 파일들의 `session_id`가 서로 달라도 된다 |
| Case → 여러 Note·Guide | `case_notes`, `guide_attachments`, `diagnostic_guides` |
| Case → 여러 Question·Answer | `learning_questions` → `learning_answers` |
| KnowledgeClaim → 여러 Evidence | `claim_evidence` |
| AnalysisRun → 여러 Evidence·Case | `analysis_evidence` (`case_id`로 관련 사례 참조), 입력은 `analysis_run_files` |

## 3. 테이블

계획서 §5.1의 19개 + 추가 2개(`analysis_run_files`, `schema_migrations`).

### 3.1 log_files — 원본 파일 정보

§4.2 파일 메타데이터 13항목과의 대응:

| §4.2 항목 | 열 |
|---|---|
| 파일 ID | `id` |
| 원본 파일명 | `original_name` (가져온 위치는 `source_path`, 보관 사본은 `stored_path`) |
| SHA-256 | `sha256` (소문자 16진수 64자, UNIQUE → 중복 감지) |
| 파일 크기 | `size_bytes` |
| 인코딩 | `encoding` |
| 로그 역할 | `log_role` (자유 텍스트) |
| 수집 세션 ID | `session_id` |
| 수집 시각 | `collected_at` |
| 타임스탬프 기준 | `timestamp_basis`: ABSOLUTE / ABSOLUTE_NO_TZ / RELATIVE / NONE / UNKNOWN |
| 시간 오차 및 불확실성 | `time_uncertainty_ms`, `clock_offset_ms` + `offset_basis` |
| 관련 오류 구간 | `error_ranges_json` (구간 목록) |
| FW 버전 | `fw_version` |
| 사용자 메모 | `user_memo` |

- 원본 보존: 파일명·저장 경로·SHA-256·크기·용도·가져온 시각은 트리거 때문에 바꿀 수 없다. 메모·역할·세션 정보는 고칠 수 있다.
- `clock_offset_ms`는 `offset_basis`(동기화 근거)가 없으면 저장되지 않는다(§4.3).
- `origin_purpose`: 처음 가져온 용도(NORMAL_CASE / ABNORMAL_CASE / ANALYSIS). 사본 폴더를 정한다(REQUIREMENTS D-5).

### 3.2 collection_sessions — 수집 세션

- 같은 세션 = 같은 오류 시점에 여러 컴포넌트에서 수집한 로그. 다른 세션 = 다른 시점의 재현·반복 수집.
- `time_sync_status`: SYNCED / OFFSET_KNOWN / UNSYNCED / UNKNOWN. 세션 안의 파일끼리 시간을 맞출 수 있는지 나타낸다.
- 세션이 다른 파일은 한 시간축으로 합치지 않는다. 이 규칙은 Phase 2·6의 `build_timeline`이 강제한다(DB 제약으로 표현할 수 없음).

### 3.3 events — 파싱 이벤트

- `seq`(파일 안 순서, 0부터)와 `line_no`(원본 행, 1부터)를 모두 둔다. 여러 줄 이벤트는 `line_end`, 원본 위치 이동은 `byte_offset`.
- 시간: `ts_raw`(원문 그대로), `ts_value_us`(정규화한 마이크로초), `ts_kind`. `ts_kind`가 NONE이 아니면 `ts_value_us`가 있어야 한다.
- `error_code`는 로그에 적힌 문자열 그대로다. **뜻을 담는 열은 없다**(§13-11).
- `raw_text`에 원문 줄을 그대로 둔다. 정규화한 값은 다른 열에 둔다.

### 3.4 cases — 정상·비정상 학습 사례

- `case_type`: NORMAL / ABNORMAL. `status`: 아래 §4.
- 정상 사례 입력: `operating_conditions`, `normal_rationale`, `mode_id` + `mode_confirmed`, `fw_version`, `device_config`.
- 비정상 사례 입력: `symptom`, `abnormal_criteria`, `cause_status`(CONFIRMED / SUSPECTED / UNKNOWN). 정상 사례에서는 이 세 열이 비어 있어야 한다.
- `current_revision`: 승인할 때마다 1씩 오른다. 0이면 한 번도 승인되지 않은 사례다.

### 3.5 case_log_files · case_events · case_notes

- `case_events.role`: SYMPTOM / ERROR / CONTEXT / PATTERN_MEMBER / REFERENCE. `source`: USER / RULE / AI.
- 메모는 여러 개, 각각 수정 시각을 가진다.

### 3.6 guide_attachments · diagnostic_guides — 진단 가이드 · 대응 매뉴얼

- `guide_attachments`: TXT/MD 원본. `attachment_kind`로 종류를 나눈다.
  - DIAGNOSTIC_GUIDE: 진단 가이드. 비정상 사례에 **필수**(§3.2). Phase 4 승인 검사에서 강제한다.
  - RESPONSE_MANUAL: 대응 매뉴얼. **선택**(변경 요청 CR-01).
- 식별 정보(종류 포함)는 바꿀 수 없다. 가이드·매뉴얼을 고치면 새 행을 만든다(이전 행 보존).
- `diagnostic_guides`: 가이드·매뉴얼을 구조화한 **항목 하나가 행 하나**다. 어느 파일에서 나왔는지는 `attachment_id` → `attachment_kind`로 안다. `section`은 가이드 권장 8항목과 매뉴얼 항목 3개에 대응한다.

| 권장 항목 (§3.2) | section |
|---|---|
| 오류 증상 | SYMPTOM |
| 발생 조건 | CONDITION |
| 비정상 판정 기준 | CRITERIA |
| 가능한 원인 | CAUSE |
| 원인 확인 방법 | CHECK_METHOD |
| 해결 방법 | RESOLUTION (없어도 됨) |
| 조치 후 검증 방법 | VERIFICATION |
| 추가 참고사항 | NOTE |
| (매뉴얼) 대응 절차 한 단계 | RESPONSE_STEP (`sort_order`가 단계 순서) |
| (매뉴얼) 주의·안전 사항 | PRECAUTION |
| (매뉴얼) 상위 지원 요청 기준 | ESCALATION |

- 매뉴얼 항목 3개는 매뉴얼 샘플을 받기 전의 기본값이다(REQUIREMENTS Q5). 샘플을 보고 바꾸면 새 마이그레이션으로 고친다.
- `source_kind`: GUIDE_TEXT(가이드 원문에서 옮김, 원문 구간 `span_start`/`span_end`) / USER / AI_PROPOSED.
- 오류 코드와 해결책을 묶는 열이 없다. 연결은 `knowledge_claims` + `claim_evidence`의 다대다로만 한다(§3.2 "일대일 고정 금지").

### 3.7 mode_definitions · event_patterns

- 운전 모드: `source` USER / AI_PROPOSED, `status` PROPOSED / ACTIVE / RETIRED. AI가 제안한 모드는 `approved_at` 없이 ACTIVE가 될 수 없다.
- 패턴: `mode_id` 필수(패턴은 모드에 종속). `pattern_type`은 §3.1의 6유형.
- `basis`: OBSERVED(사례에서 관찰됨) / SPECIFICATION(장비 사양 근거, `basis_note` 필수) / USER_DEFINED. 관찰된 공통 패턴과 사양상 필수 조건을 구분한다(§6.1).
- `definition_json` 예: `{"events": ["<event_type>"], "before": "<a>", "after": "<b>", "min": 1, "max": 3}`. 구조는 Phase 3에서 Pydantic 모델로 고정한다.

### 3.8 learning_questions · learning_answers

- `trigger_type`은 §7.2의 9가지 조건이다.

| §7.2 조건 | trigger_type |
|---|---|
| 운전 모드 불명 | MODE_UNKNOWN |
| 정상 판정 근거 부족 | NORMAL_RATIONALE_MISSING |
| 로그 수집 세션 관계 불명 | SESSION_RELATION_UNKNOWN |
| 타임스탬프 기준 불명 | TIMESTAMP_BASIS_UNKNOWN |
| 진단 가이드 정보 누락 | GUIDE_INFO_MISSING |
| 오류 원인 확정 여부 불명 | CAUSE_STATUS_UNKNOWN |
| 진단 기준의 수치 또는 조건 불명 | CRITERIA_DETAIL_UNKNOWN |
| 사용자 설명과 로그 간 충돌 | USER_LOG_CONFLICT |
| 동일 오류 코드에 상충하는 진단 지식 | CONFLICTING_KNOWLEDGE |

- `reason`(질문 이유)은 비울 수 없다. `is_required=1`인 질문이 열려 있으면 Phase 5부터 승인이 막힌다.
- 같은 사례에 같은 `dedup_key`의 질문은 두 번 만들 수 없다(반복 방지).
- 답변 종류: TEXT / CHOICE / UNKNOWN(모름) / SKIPPED(건너뛰기).

### 3.9 knowledge_claims · claim_evidence

- 주장 하나는 사례·가이드 항목·패턴·분석 실행 중 **정확히 하나**에 속한다(CHECK).
- `claim_kind`: FACT / HYPOTHESIS / UNKNOWN. `source`: LOG_OBSERVATION / USER / GUIDE / AI. AI가 만든 사실·추론과 사용자 정보를 이 두 열로 구분한다(§1.3).
- 근거 종류마다 필요한 참조가 CHECK로 강제된다. `stance`로 지지·반대 근거를 나눈다.

### 3.10 analysis_runs · analysis_run_files · analysis_evidence

- 결과 분류 네 축(§3.3)을 독립 열로 둔다.

| 계획서 분류 | 열 | 값 |
|---|---|---|
| 정상 패턴과 일치 | `normal_match` | MATCH / PARTIAL / NO_MATCH / NOT_EVALUATED |
| 비정상 의심 | `anomaly_level` | NONE_FOUND / SUSPECTED / RULE_VIOLATION / NOT_EVALUATED |
| 기존 비정상 사례와 유사 | `similar_abnormal` | FOUND / NOT_FOUND / NOT_EVALUATED |
| 판단 불가 | `evidence_sufficiency` | SUFFICIENT / PARTIAL / INSUFFICIENT / NOT_EVALUATED |

- 다섯 번째 열 `response_guidance`는 사용자(CS 엔지니어 등)에게 보여 줄 **권장 대응의 근거 수준**이다(CR-01). MANUAL(유사한 승인 사례의 승인된 매뉴얼 항목) / GUIDE_ONLY(매뉴얼 없이 진단 가이드의 해결 방법만) / NONE(등록된 대응 없음) / NOT_EVALUATED. 권장 대응 항목 하나하나는 `analysis_evidence`의 `finding_type=RESPONSE_REFERENCE` 행이고, `guide_item_id`로 승인된 항목을 가리킨다.
- `anomaly_level`에 "정상"이라는 값은 없다. 이상을 찾지 못한 것(NONE_FOUND)과 정상이라는 판정은 다르기 때문이다.
- `ai_status`로 AI 단계 결과를 따로 남긴다. AI가 실패해도 계층 1·2의 근거 행은 남는다.
- `analysis_evidence.evidence_key`(예: `EV-0001`)는 AI에게 넘긴 근거 ID이고 실행마다 고유하다. AI 출력의 근거 키는 이 값과 대조한다.
- `similarity`는 순위용 점수다. 화면·보고서에서 확률로 표시하지 않는다.

### 3.11 case_revisions · audit_history

- 둘 다 **추가 전용**이다. UPDATE·DELETE가 트리거로 막혀 있다.
- `case_revisions.snapshot_json`: 승인 시점의 사례와 연결 항목(메모·가이드 항목·패턴·주장) 전체.
- `audit_history`: 행위자 종류(USER / AI / SYSTEM)와 전후 값.

### 3.12 schema_migrations (마이그레이션 실행기가 만듦)

`version`, `name`, `checksum`(줄바꿈을 LF로 맞춘 SQL의 SHA-256), `applied_at`.

## 4. 사례 상태

```text
            ┌──────────────┐  필수 정보 부족   ┌────────────────────┐
  생성 ───▶ │    DRAFT     │ ───────────────▶ │ NEEDS_INFORMATION  │
            └──────┬───────┘ ◀─────────────── └─────────┬──────────┘
                   │ 검토 요청          답변 반영         │
                   ▼                                     │
            ┌──────────────────┐ ◀─────────────────────┘
            │ READY_FOR_REVIEW │ ── 반려 ──▶ REJECTED ──(다시 작성)──▶ DRAFT
            └──────┬───────────┘
                   │ 사용자 승인 (개정본 +1, 스냅샷, audit)
                   ▼
            ┌──────────────┐ ── 보관 ──▶ ARCHIVED ──(복원)──▶ 이전 상태
            │   APPROVED   │
            └──────────────┘
```

- 전이 규칙은 Phase 3의 `approval_workflow`가 강제한다. DB는 다음만 보장한다.
  - 상태 값은 6개 중 하나다.
  - APPROVED에는 승인 시각과 개정본(1 이상)이 있다.
  - 비정상 사례의 APPROVED에는 증상과 판정 기준이 있다.
  - DRAFT·REJECTED만 지울 수 있다. 한 번이라도 승인된 사례는 지울 수 없다.
- 확정 지식으로 쓰는 것은 `approved_cases`, `approved_event_patterns`, `approved_diagnostic_guides` 뷰뿐이다. 패턴·가이드 항목은 **자기 상태가 APPROVED이고 소속 사례도 APPROVED**일 때만 뷰에 나온다.

## 5. Phase 1에서 정하지 않은 것

| 항목 | 정할 Phase | 메모 |
|---|---|---|
| 승인 사례를 고치는 동안 분석에 무엇을 쓰나 | P3 | 후보: 고치는 동안에도 마지막 승인 개정본(`case_revisions`)을 쓴다. 지금 뷰는 상태 기준이라 고치는 동안 빠진다. |
| 같은 파일을 다른 파서 프로파일로 다시 파싱 | P2 | `case_events`가 이벤트를 RESTRICT로 잡고 있어서, 사례에 연결된 이벤트는 지울 수 없다. 재파싱은 새 이벤트 묶음을 만드는 방식으로 설계한다. |
| FTS5 색인 테이블 | P6 | 시작할 때 FTS5 사용 가능 여부만 검사한다(Phase 1). |
| `definition_json`, `error_ranges_json`, `snapshot_json`의 내부 구조 | P2·P3 | Pydantic 모델로 고정한다. |

## 6. 이후 마이그레이션 (Phase 2~7)

| 번호 | 내용 | 이유 |
|---|---|---|
| 0002_session_relation | `collection_sessions.relation_confirmed` | 가져올 때 세션 관계를 '모름'으로 고른 경우를 기록한다. 질문 엔진의 SESSION_RELATION_UNKNOWN 판정에 쓴다. |
| 0003_search_index | FTS5 `events_fts`(message·event_type·error_code, 외부 내용) + 삽입·삭제·수정 트리거 | §6.3 텍스트 검색, §9.1 색인 재생성(`INSERT INTO events_fts(events_fts) VALUES('rebuild')`) |
| 0004_fts_bulk_index | 삽입 트리거 제거 | 행마다 트리거면 10MB × 10개 가져오기가 32초에서 145초로 느려졌다. `EventRepository.add_many`가 파일 단위로 한 번에 색인한다(36.9초). |

| 0005_reference_library | `reference_documents`·`reference_entries`, 승인 뷰 `approved_reference_entries`, 트리거 | CR-04 참고 자료(사용자 코드 사전) |

### 0005 참고 자료 (CR-04)

- `reference_documents`: 올린 문서 하나. `format` ∈ TXT·MD·XLSX·PDF·DOCX, `status` ∈ DRAFT·APPROVED·ARCHIVED, `sha256` 유일, `replaces_id`(같은 제목의 이전 승인 버전), `column_map_json`(표 열 역할을 사용자가 고른 값).
- `reference_entries`: 코드 항목 하나. `code`(원문 그대로), `code_norm`(공백 제거·대문자, 찾기용 색인), `title`·`description`·`cause`·`action`, `locator`(원문 위치), `source_text`(원문 행·문단), `source_kind` ∈ EXTRACTED·USER, `status` ∈ INCLUDED·EXCLUDED.
- 트리거
  - 문서 식별 정보(원래 이름·사본 경로·해시·크기·형식)는 바꿀 수 없다.
  - 상태는 DRAFT → APPROVED/ARCHIVED, APPROVED → ARCHIVED만 된다. 보관은 마지막 상태다.
  - 승인·보관된 문서는 지울 수 없고, 그 항목은 넣기·고치기·지우기가 막힌다.
- 분석은 `approved_reference_entries`(승인 문서 + INCLUDED 항목)로만 읽는다.
- 분석 결과의 참고 자료 일치는 `analysis_evidence.finding_type = 'TEXT_MATCH'`(이미 있던 값)에 `detail_json.role = 'REFERENCE_MATCH'`로 저장한다. 종류 제약을 바꾸려면 테이블을 다시 만들어야 해서 AI 대응 순서와 같은 방식을 쓴다.

| 0006_questions_passages | `ai_questions`, `reference_passages`(+승인 뷰·잠금 트리거), `cases`의 '승인된 비정상은 증상·판정 기준 필수' CHECK 제거 | CR-07 |
| 0007_value_out_of_range | `analysis_evidence.finding_type` 허용 값에 `VALUE_OUT_OF_RANGE`·`VALUE_STUCK` 추가, 근거 종류 색인 | CR-08 |

### 0006 LLM 질문·관련 문서 문단 (CR-07)

- `ai_questions`: 사례(로그 하나)마다 LLM 질문. `kind` ∈ GUESS·ASK, `evidence_json`(근거 키·종류·행·글), `status` ∈ OPEN·ANSWERED·SKIPPED·OBSOLETE, `answer` ∈ YES·NO·UNKNOWN·TEXT, `answer_text`, `dedup_key`(같은 사례에서 유일). 뷰 `confirmed_ai_answers`: 승인된 사례의 답(맞음·틀림·글)만.
- `reference_passages`: 참고 자료 문서의 문단(코드 항목으로 뽑히지 않은 글). 승인·보관된 문서의 문단은 넣기·고치기·지우기 막힘. 뷰 `approved_reference_passages`.
- `cases`: 0001의 CHECK 하나를 SQLite 공식 절차(`PRAGMA writable_schema`)로 CREATE TABLE 문에서 뺀다. 표를 다시 만들지 않는다(외래 키 연쇄 삭제 방지). 같은 스크립트의 CREATE TABLE이 스키마 번호를 올려 모든 연결이 새 정의를 읽는다. 0001 파일은 그대로다(체크섬).

### 0007 정상 로그 대비 값 근거 (CR-08)

- `analysis_evidence.finding_type`: 0006과 같은 절차(`PRAGMA writable_schema`)로 CREATE TABLE 문의 허용 값 목록에 `VALUE_OUT_OF_RANGE`(비슷한 정상 로그의 관찰 범위를 크게 벗어난 값)와 `VALUE_STUCK`(늘 변하던 값이 연속으로 같음)을 더한다. 허용 값을 늘리기만 하므로 기존 행은 그대로 맞는다. 같은 스크립트의 `CREATE INDEX`가 스키마 번호를 올린다.
- 정상 로그 대비 다른 판단(누락·횟수·순서·비율·번호별 흐름·연속 반복·간격)은 기존 값(`MISSING_EVENT`·`REPETITION`·`ORDER_VIOLATION`·`LATENCY`)을 쓰고 `detail_json.basis = 'REFERENCE_NORMAL'`(+`kind`)로 구분한다. 심각도는 늘 의심(SUSPECT)이다.

### 결정·관례 (Phase 3~7)

- **승인 사례 수정 중**(수정 시작 → DRAFT)에는 승인 뷰에서 빠진다. 다시 승인하면 개정본이 하나 늘고 이전 개정본은 보존된다(§5 미결 항목을 결정함).
- **AI 대응 순서**(response_plan)는 `analysis_evidence.finding_type = 'AI_VERIFICATION_STEP'`에 `detail_json.role = 'RESPONSE_PLAN'`으로 저장한다. AI 확인 절차는 `role = 'VERIFICATION'`이다. 전용 열거값을 더하려면 CHECK 제약 때문에 테이블을 다시 만들어야 해서 지금은 role로 구분한다.
- AI 진단 근거 키는 `EV-…`(계층 1·2, 저장된 근거)와 `AI-…`(계층 3 출력)로 나눈다. AI 주장은 `knowledge_claims.analysis_run_id`에 묶이고, 근거는 `claim_evidence.analysis_evidence_id`로 가리킨다.

## 0009 참고 자료 판정 칸 (CR-16 RJ-01, 2026-11-23)

- `reference_entries.judgment TEXT` — 문서의 판정 낱말 그대로(예: 에러·정상). 분석은 `judgment_side`로 비정상·정상·안 씀으로만 읽는다.
- `approved_reference_entries` 뷰를 다시 만든다(같은 정의). 승인된 문서 항목 잠금(0005 트리거)은 그대로.
