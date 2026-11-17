# Local Log Analyzer

단일 장비 로그를 PC 안에서만 분석하는 데스크톱 앱. 정상·비정상 로그를 사용자 승인 아래 학습 사례로 쌓고, 신규 로그를 그 사례와 비교해 이상 여부·원인 가설·권장 대응을 근거와 함께 보여 준다. 인터넷과 외부 AI 서비스를 쓰지 않고, AI는 이 PC의 Ollama만 쓴다.

- 최상위 요구사항: 개발 계획서 LLA-DEV-PLAN-001 v0.2 R01(`docs/DEV_PLAN_v0.2_R01.md`, 공개 저장소에는 올리지 않음) + 변경 요청 CR-01~03([docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) §1.16~1.18)
- 현재 단계: **Phase 1~9 + CR-01~03 구현(2026-11-17)**, 테스트 527 통과. 남은 일은 사내 PC 확인(오프라인·모델 평가·실제 로그 형식)이다. 설치는 [docs/INSTALL.md](docs/INSTALL.md), 사용법은 [docs/USER_MANUAL.md](docs/USER_MANUAL.md).

## 다른 PC에서 실행 (설치 없음)

빌드한 `dist\LocalLogAnalyzer\` 폴더를 통째로 복사하고 `AI와 함께 시작.cmd`를 두 번 누른다. Python·Ollama·런타임을 따로 설치하지 않는다(휴대용 Ollama와 모델이 폴더 안에 있다). 자세한 것은 [docs/INSTALL.md](docs/INSTALL.md) §1.

```bash
powershell -File packaging/build.ps1 -OllamaModel gemma3:4b
```

## 개발 환경에서 실행

필요한 것: [uv](https://docs.astral.sh/uv/), Windows 10/11. Ollama는 없어도 앱은 뜬다(AI 기능만 쓸 수 없음).

```bash
uv sync
```

```bash
uv run python main.py
```

```bash
uv run python main.py --check
```

- `uv sync`는 처음 한 번만 한다. Python 3.11과 패키지(PySide6-Essentials, pydantic, httpx, pytest, pytest-qt)를 프로젝트 폴더의 `.venv`에 설치한다.
- `--check`는 화면 없이 설정 파일·데이터 폴더·DB·SQLite 기능·Ollama 상태를 출력한다. 사내 PC에서 처음 확인할 때 쓴다.

## 테스트

```bash
uv run pytest
```

- 모든 테스트는 임시 폴더를 데이터 폴더로 쓰고, PC 밖으로 나가는 네트워크 연결을 막은 채 돈다.
- 실제 로컬 Ollama 연결 시험은 따로 켠다: PowerShell에서 `$env:LLA_LIVE_OLLAMA = "1"; uv run pytest tests/integration/test_ollama_live.py`

## 데이터 위치

| 무엇 | 기본 위치 |
|---|---|
| 설정 파일 | `%LOCALAPPDATA%\LocalLogAnalyzer\config\settings.json` (항상 여기) |
| DB·사례·분석·백업·앱 로그 | `%LOCALAPPDATA%\LocalLogAnalyzer\` 아래. 설정의 `data_root`로 바꿀 수 있다(다시 시작 후 적용) |

- `LLA_HOME` 환경 변수를 주면 `%LOCALAPPDATA%\LocalLogAnalyzer` 대신 그 폴더를 쓴다(테스트·시험용).
- 원본 로그는 고치지 않는다. 가져온 파일은 사본과 SHA-256으로 보관한다(Phase 2).

## 폴더

```text
main.py                 진입점 (--check 지원)
app/                    소스 (config, security, bootstrap, models, storage, ai, ui …)
app/storage/migrations  DB 마이그레이션 SQL (적용된 파일은 고치지 않는다)
configs/settings.json   배포 기본 설정
tests/                  unit / integration / ui
docs/                   계획서 원문, 요구사항 추적표, 아키텍처, DB, AI 정책, 테스트 계획
```

## 문서

- [REQUIREMENTS.md](docs/REQUIREMENTS.md) — 요구사항 추적표(ID별 Phase·상태·검증), 모순·누락·위험 검토, 미결 질문
- [ARCHITECTURE.md](docs/ARCHITECTURE.md) — 계층·의존 규칙·모듈 인터페이스·Phase별 파일 계획
- [DATABASE.md](docs/DATABASE.md) — 스키마 v1, 관계, 상태 규칙
- [AI_POLICY.md](docs/AI_POLICY.md) — AI가 하는 일·하지 않는 일, 출력 검증 규칙
- [TEST_PLAN.md](docs/TEST_PLAN.md) — 테스트 전략, 계획서 §12 시험 ID, Phase별 결과
- [USER_GUIDE.html](docs/USER_GUIDE.html) — 처음 쓰는 사람용 사용 가이드(앱에서 F1)
- [PRODUCT.md](PRODUCT.md)·[DESIGN.md](DESIGN.md) — 사용자·화면 원칙과 디자인 토큰
