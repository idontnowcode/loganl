# 아키텍처 — Local Log Analyzer

- 기준: [DEV_PLAN_v0.2_R01.md](DEV_PLAN_v0.2_R01.md) §6·§10·§13, [REQUIREMENTS.md](REQUIREMENTS.md)
- 상태: 2026-11-16 확정(Phase 1 착수). 바꿀 때는 이 문서를 먼저 고친다.

## 1. 한눈에 보기

```text
┌──────────────────────────── app/ui (PySide6) ────────────────────────────┐
│ main_window · normal_learning · abnormal_learning · log_analysis · …     │
│ 화면은 아래 계층의 함수만 부른다. 긴 작업은 workers.py로 백그라운드 실행. │
└───────────────┬───────────────────────────┬──────────────────────────────┘
                │                           │
      ┌─────────▼─────────┐       ┌─────────▼─────────┐
      │ app/learning      │       │ app/analysis      │   응용 계층(흐름 조립)
      │ 학습·질문·승인    │       │ 검색·탐지·진단    │
      └──┬──────┬─────┬───┘       └──┬──────┬─────┬───┘
         │      │     │              │      │     │
   ┌─────▼──┐ ┌─▼─────▼──┐     ┌─────▼──┐   │  ┌──▼──────┐
   │app/core│ │ app/ai   │     │app/core│   │  │app/ai   │
   │결정론  │ │ Ollama   │     │        │   │  │         │
   └──┬─────┘ └────┬─────┘     └────────┘   │  └─────────┘
      │            │                        │
   ┌──▼────────┐ ┌─▼──────────────┐   ┌─────▼─────────┐   ┌──────────────┐
   │app/parsers│ │ app/storage    │   │ app/reports   │   │ app/models   │
   │플러그인   │ │ SQLite·경로    │   │ JSON·HTML     │   │ 열거형·모델  │
   └───────────┘ └────────────────┘   └───────────────┘   └──────────────┘
```

- **계층 1 결정론**(`core`, `parsers`)은 AI를 모른다. 같은 입력이면 항상 같은 결과를 낸다.
- **계층 2 검색**(`analysis/case_retriever`)은 승인된 지식만 본다(DB 승인 뷰).
- **계층 3 추론**(`ai`)은 계층 1·2가 만든 근거 묶음만 받는다. 원본 로그 전체나 DB 연결을 받지 않는다.
- Ollama가 꺼져 있거나 응답이 잘못돼도 계층 1·2의 결과는 그대로 저장·표시된다(NA-14).

## 2. 의존 규칙 (자동 검사: `tests/unit/test_architecture.py`)

| 모듈 | import해도 되는 것 | import하면 안 되는 것 |
|---|---|---|
| `app/models` | 표준 라이브러리, pydantic | app의 다른 모든 패키지 |
| `app/parsers`, `app/core` | models, storage | ai, learning, analysis, reports, ui, PySide6 |
| `app/storage` | models | core, parsers, ai, learning, analysis, reports, ui, PySide6 |
| `app/ai` | models, storage(읽기 전용 조회만), security | learning, analysis, reports, ui, PySide6 |
| `app/learning`, `app/analysis` | models, core, parsers, storage, ai | ui, PySide6 |
| `app/reports` | models, storage | ai, ui, PySide6 |
| `app/ui` | 모든 하위 계층, PySide6 | — |

모든 `app/` 모듈에 공통으로 금지하는 것(SEC-03·SEC-09·PRN-09):

- 프로세스·장비 접근: `subprocess`, `os.system`, `os.popen`, `os.spawn*`, `os.exec*`, `serial`, `can`, `ctypes`
- 네트워크: `socket`, `requests`, `urllib.request`, `http.client`, `ftplib`, `smtplib`, `telnetlib`, `paramiko`
- 외부 LLM SDK: `openai`, `anthropic`, `google.generativeai`
- `httpx`는 `app/ai/ollama_client.py`에서만 쓴다.

## 3. 모듈 책임

| 패키지 | 책임 | 처음 생기는 Phase |
|---|---|---|
| `main.py` | 진입점. `--check`(GUI 없는 자체 점검)와 GUI 실행 | P1 |
| `app/config.py` | 설정 모델(Pydantic), 기본값 병합, 원자적 저장, loopback 검증 | P1 |
| `app/security.py` | loopback 주소 판정 같은 보안 공통 함수 | P1 |
| `app/bootstrap.py` | 설정 → 경로 → DB 마이그레이션 → 로그 설정 순서로 앱 문맥(`AppContext`) 조립. Qt 없음 | P1 |
| `app/models` | 열거형(DB CHECK 값과 일치), 이후 Phase의 Pydantic 레코드 | P1(열거형) |
| `app/storage/paths.py` | 데이터 폴더 구조(§9)와 생성 | P1 |
| `app/storage/database.py` | 연결·PRAGMA·마이그레이션(체크섬)·SQLite 기능 검사 | P1 |
| `app/storage/repositories.py` | 테이블별 저장·조회. SQL은 이 파일에만 | P2 |
| `app/parsers` | 파서 프로토콜, 프로파일 기반 범용 파서(줄 형식 regex·json·logfmt·delimited, CR-08), 레지스트리 | P2, CR-08 |
| `app/core` | 가져오기·정규화·특징 추출·부분 순서·패턴 후보·근거 ID 존재 검증. 참고 자료 읽기(`reference_reader`: TXT·MD·XLSX·PDF·DOCX → 표·글 덩어리)와 항목 뽑기(`reference_structurer`) | P2~P7, CR-04 |
| `app/learning` | 정상·비정상 학습 흐름, 질문 엔진, 승인·개정. 로그 입력: 파일 하나 = 사례 하나, 저장 = 승인(`log_entry`). 자유 글 정리를 사례·분석 칸에 반영(`intake`). 참고 자료 올리기·수정·승인·보관(`reference_library`) | P3~P5, CR-04, CR-05, CR-06 |
| `app/analysis` | 사례 검색, 이상 탐지, 진단 조립, 결과 모델. 로그 코드와 승인된 참고 자료 맞추기(`reference_matcher`) | P6~P8, CR-04 |
| `app/ai` | Ollama 클라이언트, 모드 추정, 초안 생성, 근거 묶음, 출력 검증. 자유 글 조각 고르기(`intake`, V-10) | P1(상태 확인)~P7, CR-05 |
| `app/reports` | JSON·HTML(자체 포함) 보고서 | P8 |
| `app/ui` | 화면. 로직 없음 | P1(틀)~P8 |

## 4. 모듈 간 인터페이스

아래 시그니처가 Phase 사이의 약속이다. Phase 1에서는 **표시된 P1 항목만 코드로 존재**하고, 나머지는 해당 Phase에서 이 모양으로 만든다. 모양을 바꿀 때는 이 문서와 호출부 테스트를 같이 고친다.

### 4.1 설정·문맥 (P1, 구현됨)

```python
# app/config.py
def bootstrap_dir() -> Path                      # LLA_HOME 또는 %LOCALAPPDATA%\LocalLogAnalyzer
def load_settings(settings_file: Path) -> LoadedSettings      # .settings, .warnings, .created
def save_settings(settings: AppSettings, settings_file: Path) -> None   # 임시 파일 → 교체
def resolve_data_root(settings: AppSettings, bootstrap: Path) -> Path

# app/bootstrap.py
def initialize(home: Path | None = None, *, configure_logging: bool = True) -> AppContext
class AppContext:            # bootstrap_dir, settings_file, settings, paths, database,
    def close(self) -> None  # capabilities, warnings
```

### 4.2 저장소 (P1 일부 구현)

```python
# app/storage/database.py  (P1)
class Database:
    def __init__(self, db_file: Path, migrations_dir: Path = MIGRATIONS_DIR)
    def connect(self) -> sqlite3.Connection        # foreign_keys=ON, WAL, busy_timeout, Row
    def migrate(self) -> list[int]                 # 새로 적용한 버전 목록
    def schema_version(self) -> int
    def capabilities(self) -> SqliteCapabilities   # version, fts5, json1, strict
@contextmanager
def transaction(conn) -> Iterator[sqlite3.Connection]   # BEGIN IMMEDIATE … COMMIT / ROLLBACK

# app/storage/repositories.py  (P2~)
class LogFileRepository:
    def find_by_sha256(self, sha256: str) -> LogFileRecord | None
    def add(self, record: NewLogFile) -> LogFileRecord
class EventRepository:
    def add_many(self, log_file_id: int, events: Iterable[ParsedEvent]) -> int
    def lines(self, log_file_id: int, start: int, end: int) -> list[EventRecord]
class CaseRepository: ...                # P3: create_draft, update_draft, get, list(status, type)
class KnowledgeRepository: ...           # P3~P6: approved_patterns(mode_id), approved_guides(...)
```

### 4.3 파서 (P2, DEV-12)

```python
# app/parsers/base_parser.py
class LogParser(Protocol):
    name: str
    def sniff(self, head_lines: Sequence[str]) -> float      # 0~1, 결정론 규칙으로만 판단
    def parse(self, lines: Iterable[RawLine]) -> Iterator[ParsedEvent]

# app/parsers/generic_parser.py
class RegexProfileParser(LogParser):     # configs/parser_profiles/*.json 하나 = 파서 하나
    def __init__(self, profile: ParserProfile)

# app/parsers/parser_registry.py
class ParserRegistry:
    def register(self, parser: LogParser) -> None
    def load_profiles(self, directory: Path) -> list[str]
    def select(self, head_lines: Sequence[str], profile: str | None = None) -> LogParser
    # 맞는 파서가 없으면 "한 줄 = 이벤트 하나, 타임스탬프 NONE"인 원문 파서로 떨어진다.
```

- 파서는 **오류 코드 문자열을 그대로** `error_code`에 담을 뿐 뜻을 붙이지 않는다(DEV-11).
- `event_type`은 프로파일 규칙이나 사용자가 정한 이름이다. AI가 만들지 않는다.

### 4.4 결정론 분석 (P2~P7)

```python
# app/core/cancel.py (P2)
class CancelToken:          # threading.Event 기반. Qt 없음
    def cancel(self) -> None
    def raise_if_cancelled(self) -> None     # Cancelled 예외

# app/core/importer.py (P2)
def import_logs(request: ImportRequest, *, paths: AppPaths, repo: LogFileRepository,
                settings: ImportSettings, cancel: CancelToken) -> ImportResult
    # 확장자·크기·총량 검사 → SHA-256 → 중복이면 기존 행 재사용 → 사본 저장(읽기 전용 속성)
    # → 인코딩 판정 → 파싱 → 이벤트 저장. 사본은 다시 해시해 원본과 같은지 확인한다.

# app/core/sequence_analyzer.py (P6)
def build_timeline(files: Sequence[FileEvents]) -> Timeline
    # 세션이 둘 이상이면 SessionMismatchError (MLG-03)
    # 시계 기준이 다르고 오프셋 근거가 없으면 파일별 순서만 유지 (MLG-07)
def check_order(events: Sequence[EventRecord], constraints: Sequence[OrderConstraint]) -> list[Finding]

# app/core/pattern_analyzer.py (P3)
def propose_patterns(cases: Sequence[CaseEvents], mode_id: int) -> list[PatternCandidate]
    # 같은 모드의 정상 사례들에서 공통·선택·반복·부분 순서 후보. 출처 RULE, 근거 basis=OBSERVED

# app/core/evidence_validator.py (P3·P7)
def unknown_evidence_keys(keys: Iterable[str], package: EvidencePackage) -> set[str]
```

### 4.5 AI (P1 일부 구현)

```python
# app/ai/ollama_client.py
class OllamaClient:
    def __init__(self, base_url: str, *, connect_timeout_s: float, request_timeout_s: float,
                 transport: httpx.BaseTransport | None = None)    # loopback 아니면 ValueError
    def check_status(self, model: str | None = None) -> OllamaStatus            # P1 구현
    def generate_json(self, *, model: str, system: str, prompt: str,
                      schema: dict, cancel: CancelToken) -> str                # P3
    def close(self) -> None

# app/ai/mode_classifier.py (P3)
def classify_mode(features: LogFeatures, modes: Sequence[ModeDefinition],
                  client: OllamaClient) -> ModeGuess   # 등록된 mode_id 또는 UNKNOWN + 근거 키

# app/ai/context_builder.py (P7)
def build_evidence_package(findings, similar_cases, guides, *, budget_chars: int) -> EvidencePackage
    # 근거마다 EV-0001 형식 키. 로그 원문은 구분자 안에 "데이터"로만 넣는다 (AI_POLICY §4)

# app/ai/response_validator.py (P3·P7)
def validate_response(raw: str, schema: type[BaseModel],
                      package: EvidencePackage | None) -> ValidationResult
    # JSON Schema → 근거 키 존재 → 표현 규칙(AI_POLICY §3). 실패 사유를 목록으로 돌려준다.
```

### 4.6 응용 계층 (P3~P8)

```python
# app/learning/approval_workflow.py
def check_approvable(case_id: int) -> ApprovalCheck     # blocking 사유 목록 (AL-09, QE-08)
def approve(case_id: int) -> CaseRevision               # 스냅샷·audit·상태를 한 트랜잭션으로
def reopen_for_edit(case_id: int) -> None               # 승인 사례 수정 시작 (DATABASE.md §5)

# app/learning/question_engine.py (P5)
def detect_gaps(context: CaseContext) -> list[QuestionCandidate]   # 9가지 조건은 코드가 판정
def next_questions(case_id: int, limit: int = 3) -> list[Question] # 문장은 AI가 다듬어도 됨

# app/analysis/case_retriever.py (P6)
def find_similar(query: AnalysisQuery, case_type: CaseType, limit: int = 5) -> list[SimilarCase]
    # approved_* 뷰만 조회. similarity는 순위용 점수이며 확률이 아니다 (NA-13)

# app/analysis/anomaly_detector.py (P6)
def detect(events: Sequence[EventRecord], patterns: Sequence[ApprovedPattern]) -> list[Finding]

# app/analysis/reference_checks.py (CR-08 DT-01~09)
def compare_with_normals(target: FileFeatures, events: Sequence[EventRecord],
                         references: Sequence[ReferenceFile]) -> list[Finding]
    # 점수 0.4 이상 비슷한 승인 정상 로그(최대 5개)와 비교: 누락·횟수 초과·순서·비율·번호별 흐름·
    # 연속 반복·간격·값 범위·값 멈춤. 모두 SUSPECT(의심), detail.basis = REFERENCE_NORMAL. 장비 사양을 쓰지 않는다.

# app/core/reparse.py (CR-08 DT-16)
def current_parse(database: Database, records: Sequence[LogFileRecord],
                  events: Mapping[int, list[EventRecord]]) -> tuple[list[LogFileRecord], dict[int, list[EventRecord]]]
    # 마이그레이션 0007 적용 전에 가져온 로그만 보관한 원본 사본을 지금 파서로 다시 읽어 돌려준다(메모리에서만, 캐시).
    # 이벤트 id는 그 줄을 담은 DB 이벤트의 id. DB는 바꾸지 않는다. 사본을 못 읽으면 저장된 이벤트 그대로.

# app/analysis/error_messages.py (CR-11 EM-01~04)
def presumed_errors(findings, keys=None, raw_lines=None, details=None, references=None, limit=20) -> list[ErrorMessage]
    # 결정론 근거(Finding 또는 저장된 근거 행)에서 문제로 보이는 원문 줄을 묶음별로(에러 기록·정상과 다름·
    # 처음 보는 경고·실패·빠진 메시지). 뜻은 만들지 않고 승인된 참고 자료 글만 붙인다. 화면과 보고서가 같이 쓴다.
def file_error_candidates(database: Database, log_file_id: int, limit=15) -> list[ErrorMessage]
    # 정상 로그와 비교하지 않은 한 파일의 후보(에러 레벨·오류 코드, 경고·실패 낱말). 로그 입력 탭.
def should_show(anomaly_level: str | None, ai_likelihood: int | None) -> bool  # 의심·규칙 위반 또는 AI 50% 이상

# app/analysis/diagnostic_engine.py (P7)
def run_analysis(run_id: int, cancel: CancelToken) -> AnalysisResult
    # 계층 1 → 계층 2 → (가능하면) 계층 3. 각 계층 결과를 따로 저장한다.

# app/analysis/result_builder.py (P6·P8, 변경 요청 CR-01)
def build_response_guidance(similar_abnormal: Sequence[SimilarCase],
                            approved_items: Sequence[ApprovedGuideItem]) -> ResponseGuidance
    # 승인된 매뉴얼 항목(RESPONSE_STEP·PRECAUTION·ESCALATION) → 없으면 가이드 RESOLUTION → 없으면 NONE.
    # 결정론으로 후보를 만든 뒤 AI는 고르기·순서·요약만 한다(AI_POLICY V-9).
```

- 변경 요청 CR-01: 대응 매뉴얼은 진단 가이드와 같은 가져오기 경로(`importer`, P4)로 들어오고 `attachment_kind`만 다르다. 신규 분석 화면은 결과 맨 위에 권장 대응을 보여 준다(P8).
- 변경 요청 CR-05: 자유 글 정리.

```python
# app/ai/intake.py
def split_story(text: str) -> list[Segment]                       # 문장·줄 조각 (결정론)
def pick_fields(client, model, segments, kind, modes) -> IntakePicks   # AI는 조각 키·글자 일부만 고름, V-10으로 거름

# app/learning/intake.py
class IntakeService:
    def organize_text(text, kind) -> IntakeResult        # 규칙(제목·모드 이름 일치) + AI 고르기. DB 쓰지 않음(신규 분석)
    def organize_case(case_id, text) -> IntakeResult     # 글 저장 → 정리 → 찾은 칸만 사례에 반영(모드는 확인 전으로)
```

## 5. 실행 모델

- **스레드**: UI 스레드는 화면만 그린다. DB 쓰기, 파싱, Ollama 호출은 `app/ui/workers.py`의 `FunctionWorker`(QThreadPool)로 돌린다. 워커 함수 자체는 Qt를 모르는 일반 함수다.
- **DB 연결**: 스레드마다 자기 연결을 연다(`Database.connect()`). WAL 모드라 읽기와 쓰기가 서로 막지 않는다. 쓰기 트랜잭션은 짧게 유지한다.
- **취소**: 긴 작업은 `CancelToken`을 받는다. 화면의 취소 버튼이 `cancel()`을 부르면, 작업은 다음 확인 지점에서 멈추고 진행 중이던 트랜잭션을 되돌린다(P2부터).
- **진행률**: 워커가 `progress(int, str)` 신호를 보낸다(P2부터).
- **오류**: 예상한 오류(마이그레이션 실패, 파일 거부, Ollama 미실행)는 한국어 메시지로 보여 준다. 예상하지 못한 오류는 `logs/app.log`에 기록하고 일반 안내를 띄운다. 앱 로그에는 분석 대상 로그의 원문을 남기지 않는다.

## 6. 파일 생성 계획 (Phase별)

| Phase | 새로 만드는 파일 | 고치는 파일 | 테스트 |
|---|---|---|---|
| **P1** | `main.py`, `pyproject.toml`, `.python-version`, `README.md`, `CLAUDE.md`, `configs/settings.json`, `app/{__init__,config,security,bootstrap}.py`, `app/models/enums.py`, `app/storage/{paths,database}.py`, `app/storage/migrations/0001_initial_schema.sql`, `app/ai/ollama_client.py`, `app/ui/{main_window,settings_dialog,workers}.py` | — | `tests/unit/test_{paths,config,security,database,schema,ollama_client,architecture}.py`, `tests/integration/test_{app_startup,ollama_live}.py`, `tests/ui/test_main_window.py` |
| P2 | `app/parsers/{base_parser,generic_parser,parser_registry}.py`, `configs/parser_profiles/*.json`, `app/core/{cancel,importer,event_normalizer}.py`, `app/storage/repositories.py`, `app/models/records.py`, `app/ui/log_viewer.py`(공용 위젯) | `main_window.py`(가져오기·드래그 앤 드롭), 필요하면 `migrations/0002_*.sql` | 가상 형식 테스트 로그(`tests/fixtures/logs/`), 인코딩·중복·크기·세션·원본 행 연결, 10개 파일 성능 |
| P3 | `app/core/{feature_extractor,pattern_analyzer,evidence_validator}.py`, `app/learning/{case_manager,normal_learner,approval_workflow}.py`, `app/ai/{mode_classifier,draft_generator,response_validator}.py`, `app/ui/normal_learning.py` | `ollama_client.py`(generate_json), `repositories.py` | 패턴 후보·승인·개정·스키마 검증 실패 처리, 가짜 Ollama 응답 |
| P4 | `app/learning/abnormal_learner.py`, `app/ui/abnormal_learning.py` | `importer.py`(TXT/MD 가이드·대응 매뉴얼), `repositories.py` | 가이드 필수·매뉴얼 선택·원인 미확정·해결 방법 빈 값·세션 구분 |
| P5 | `app/learning/question_engine.py`, `app/ui/question_panel.py`(공용 위젯) | `approval_workflow.py`, 학습 화면 2개 | 9가지 조건·최대 3개·반복 방지·모름·승인 보류 |
| P6 | `app/core/sequence_analyzer.py`, `app/analysis/{case_retriever,anomaly_detector}.py`, `migrations/000N_fts5.sql`, `app/ui/log_analysis.py` | `repositories.py` | 부분 순서·누락·반복·지연·세션 병합 거부·승인 뷰만 검색 |
| P7 | `app/ai/context_builder.py`, `app/analysis/diagnostic_engine.py` | `response_validator.py`, `evidence_validator.py` | 없는 근거 키·오류 코드 의미 생성·확정 표현·Ollama 장애 시 계층 1 유지 |
| P8 | `app/analysis/result_builder.py`(권장 대응 포함), `app/reports/{json_exporter,html_exporter}.py`, `app/ui/{case_review,knowledge_manager}.py`, `app/storage/backup.py` | `main_window.py`(메뉴 활성화) | 권장 대응 표시·보고서·백업·복원·전체 시나리오 |
| P9 | `packaging/local_log_analyzer.spec`, `packaging/build.ps1`, `docs/USER_MANUAL.md`, `docs/INSTALL.md` | — | 오프라인 실행·성능·실행 파일 시험 |

- Phase 1은 §10 구조 중 Phase 1에 필요한 파일만 만든다. 빈 자리 파일(stub)을 미리 만들지 않는다.
- §10에 없는 파일을 더한 것: `app/security.py`, `app/bootstrap.py`, `app/storage/paths.py`, `app/ui/settings_dialog.py`, `app/ui/workers.py`. 모두 위 의존 규칙을 지키기 위한 분리다.
