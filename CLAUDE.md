# Local Log Analyzer — 작업 지침

## 절대 규칙

1. **최상위 요구사항은 [docs/DEV_PLAN_v0.2_R01.md](docs/DEV_PLAN_v0.2_R01.md)다.** 그 뒤 사용자가 더한 요구는 [docs/REQUIREMENTS.md §1.16](docs/REQUIREMENTS.md)의 변경 요청(CR)으로 기록한다. 요구사항을 지우지 않고 상태만 바꾼다(계획서 §13-1).
2. **오류 코드의 뜻, 장비 동작 사양을 만들어 내지 않는다.** 코드·문서·테스트 데이터·프롬프트 예시 모두 해당한다. 테스트 로그는 가상 형식이고 오류 코드는 `TEST-E001`처럼 쓴다.
3. **오프라인.** `app/` 안에서 외부 네트워크·프로세스 실행·장비 접근 모듈을 쓰지 않는다. httpx는 `app/ai/ollama_client.py`에서만, 주소는 loopback만. `tests/unit/test_architecture.py`가 검사한다.
4. **결정론과 AI 분리.** `core`·`parsers`·`storage`는 `ai`를 import하지 않는다. UI 밖에서는 PySide6를 import하지 않는다.
5. **원본 로그 불변, 승인 전 지식은 분석에 쓰지 않는다.** DB 트리거와 `approved_*` 뷰가 지킨다. 우회하는 SQL을 쓰지 않는다.
6. **DB 변경은 새 번호 마이그레이션 파일로만.** 적용된 `0001_initial_schema.sql`은 고치지 않는다(체크섬 검사로 시작이 거부된다).
7. **현재 Phase에 필요 없는 기능은 만들지 않는다.** 빈 자리 파일(stub)도 미리 만들지 않는다. 인터페이스 약속은 [docs/ARCHITECTURE.md §4](docs/ARCHITECTURE.md)에 있다.
8. Phase가 끝나면 테스트 결과·바뀐 파일·미완료 항목을 보고하고 [docs/TEST_PLAN.md §4](docs/TEST_PLAN.md)에 결과를 덧붙인다. 구현하지 않은 것을 완료라고 쓰지 않는다.

## 현재 상태

- 2026-11-16: 1~3단계 문서(추적표·아키텍처·DB·AI 정책·테스트 계획)와 Phase 1.
- 2026-11-17: **Phase 2~9 구현**. 테스트 485 통과, 4 건너뜀(실제 Ollama·성능, 켜서 돌리면 통과). 실행 파일 `dist\LocalLogAnalyzer\`(109MB) 빌드와 점검 확인. 단계별 결과는 docs/TEST_PLAN.md §4.
- 2026-11-17 오후: 자동 저장·세션 타임라인·F1 사용 가이드·외부 연결 점검, **CR-02 화면 디자인**(테마 `app/ui/theme.py`, 기준 `PRODUCT.md`·`DESIGN.md`). 테스트 507 통과.
- 2026-11-17 2차: **CR-03 무설치 실행**(휴대용 Ollama 동봉 `packaging/bundle_ollama.py`, 시작·끄기 cmd), 실제 모델 기능 점검으로 진단 프롬프트 보완(칸별 근거 키·한국어), 가독성(UI-20). 테스트 527 통과. Git 저장소 `idontnowcode/loganl`(공개, 계획서 원문 제외).
- 2026-11-17 3차: **CR-05 칸 대신 자연어로 적기**(글 칸 + 'AI로 정리', AI는 내 글을 그대로 복사만, V-10). 테스트 562 통과. (CR-04는 4차에서 구현.)
- 2026-11-17 4차: **CR-06 '로그 입력' 탭 하나로**(파일마다 정상/비정상·동작 메모·판단 근거, 저장 = 승인, 모드 없으면 가장 비슷한 정상 로그와 비교, 예전 학습 화면은 '자세히 편집' 창), **CR-04 참고 자료 구현**(관리 → 참고 자료 관리, TXT·MD·XLSX·PDF·DOCX, 문서 단위 승인, 분석 '참고 자료' 칸, AI 근거 REFERENCE). 테스트 615 통과.
- 변경 요청 CR-01(대응 매뉴얼·권장 대응), CR-02(디자인), CR-03(무설치), CR-04(참고 자료), CR-05(자연어 입력), CR-06(로그 입력 탭) 반영. 미결 질문 Q1~Q5는 기본값으로 진행(REQUIREMENTS §3).
- 남은 일: 사내 PC 오프라인 확인·7~8B 모델 평가·실제 로그 샘플로 파서 프로파일 추가(docs/INSTALL.md §1).

## 실행

- 설치 `uv sync`, 화면 `uv run python main.py`, 점검 `uv run python main.py --check`, 테스트 `uv run pytest`, 실행 파일 `packaging\build.ps1`(AI 동봉 `-OllamaModel gemma3:4b`)
- 실제 Ollama 시험 `LLA_LIVE_OLLAMA=1`, 성능 시험 `LLA_PERF=1`(약 40초). 기본 테스트에서는 꺼져 있다.
- Python 3.11은 uv가 받는다. **Claude 데스크톱 앱 안에서 uv를 돌릴 때는 `UV_PYTHON_INSTALL_DIR=C:/Users/admin/.uv/python`을 준다.** 앱 안에서 `%APPDATA%`에 설치하면 가상화돼 사용자 터미널에서 `.venv`가 깨진다(지금 `.venv`는 이 경로의 Python을 가리킨다).
- 같은 이유로 Claude 앱 안에서 앱을 직접 실행해 볼 때는 `LLA_HOME`을 스크래치 폴더로 준다. 그러지 않으면 `%LOCALAPPDATA%`에 쓴 데이터가 가상화돼 사용자에게 안 보인다.

## 함정

- **`docs/DEV_PLAN_v0.2_R01.md`는 공개 저장소에 올리지 않는다**(.gitignore). 사용자 결정(2026-11-17).
- 휴대용 Ollama를 켜는 일은 `packaging/launchers/*.cmd`가 한다. `app/`에서 프로세스를 실행하지 않는다(규칙 3). cmd 파일은 CP949·CRLF(한국어 Windows cmd), git에서 `-text`.
- 화면 색·간격은 `app/ui/theme.py` 토큰과 역할 속성(`set_role`)으로만 바꾼다. 위젯에 직접 `setStyleSheet`로 색을 넣지 않는다. 속성을 바꾼 뒤에는 `set_role`이 다시 칠한다.
- `QRunnable` 신호 객체는 신호가 도착할 때까지 참조를 들고 있어야 한다(`MainWindow._pending_signals`).
- `sqlite3` 연결은 `isolation_level=None`이다. 쓰기는 `transaction()` 문맥으로 묶는다.
- 설정 파일은 항상 부트스트랩 폴더에 있다. `data_root`를 바꿔도 설정 파일은 옮겨지지 않는다(REQUIREMENTS D-1).
