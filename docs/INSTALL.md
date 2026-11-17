# 설치 및 실행 가이드 — Local Log Analyzer

## 1. 사내 PC (인터넷 없음)

**다른 PC에는 아무것도 설치하지 않는다(CR-03).** 앱 폴더 하나에 실행 환경(Python·Qt·Visual C++ 런타임)과 휴대용 AI 서버(Ollama)·모델이 모두 들어 있다. 필요한 것은 Windows 10(1809 이상)·11 64비트뿐이다.

### 준비물 (빌드 PC에서 마련)

| 배포본 | 만드는 법 | 크기 |
|---|---|---|
| AI 포함 (권장) | `packaging\build.ps1 -OllamaModel gemma3:4b` | 약 6.5GB (GPU 라이브러리 전부) |
| AI 포함, CPU만 | `packaging\build.ps1 -OllamaModel gemma3:4b -OllamaGpu cpu` | 약 3.4GB |
| AI 포함, NVIDIA만 | `... -OllamaGpu nvidia` | 약 5.5GB |
| 앱만 | `packaging\build.ps1` | 약 110MB (AI는 그 PC에 Ollama가 있을 때만) |

- 동봉은 빌드 PC에 **이미 설치된 Ollama**(`%LOCALAPPDATA%\Programs\Ollama`)와 **이미 받아 둔 모델**(`%USERPROFILE%\.ollama\models`)을 복사한다. 인터넷에서 내려받지 않는다. 트레이 앱·설치 프로그램·자동 업데이트는 넣지 않는다. 모델 파일은 복사 뒤 SHA-256을 확인한다(`packaging\bundle_ollama.py`).
- GPU 종류를 모르면 `all`로 둔다. 사내 PC에 GPU가 없다고 확인되면 `cpu`로 줄인다.

- 모델은 계획서 §2.2대로 7~8B 양자화 모델을 사내 PC에서 비교해 정한다(속도·메모리·한국어 설명·로그 해석).
- 이 PC에서는 4B 모델(gemma3:4b)로 진단 경로가 검증을 통과했다(TEST_PLAN Phase 7). 사내 PC 결과로 다시 확인할 것.

### 설치 (복사만)

1. `LocalLogAnalyzer\` 폴더를 원하는 곳(예: `D:\LocalLogAnalyzer`)에 통째로 복사한다. 한글·공백이 든 경로도 된다.
2. 폴더 안 `AI와 함께 시작.cmd`를 두 번 누른다. 휴대용 AI 서버가 `127.0.0.1:11434`에서만, 클라우드 기능을 끈 채로(`OLLAMA_NO_CLOUD=1`), 앱 폴더의 모델만 쓰게(`OLLAMA_MODELS`) 켜지고 앱이 열린다. 작업 표시줄에 최소화된 “LLA AI 서버” 창이 생긴다.
3. 다 쓰면 `AI 서버 끄기.cmd`(또는 그 창 닫기). PC에 따로 설치된 Ollama는 끄지 않는다.
4. AI 없이 쓸 때는 `LocalLogAnalyzer.exe`를 바로 실행한다.
5. 동봉한 모델은 배포 기본 설정에 미리 들어 있다(처음 실행하는 PC). 이미 이 앱을 쓰던 PC는 **관리 → 설정**에서 모델을 고른다.

- 이미 이 PC에서 다른 Ollama가 11434를 쓰고 있으면 시작 파일은 그것을 그대로 쓴다. 그 Ollama에 같은 모델이 없으면 설정에서 모델을 고른다.
- 서명하지 않은 실행 파일이라 처음 실행 때 SmartScreen 안내가 뜰 수 있다(“추가 정보 → 실행”). 사내 배포 전 IT 부서의 서명·백신 예외 정책을 확인한다.

### 처음 확인

- 명령 프롬프트에서 실행한다.

```bash
LocalLogAnalyzer.exe --check
```

- 결과는 `%LOCALAPPDATA%\LocalLogAnalyzer\logs\check.txt`에도 남는다. 다음 줄이 보이면 기반이 정상이다.
  - `결과: 기반 정상`
  - `FTS5 사용 가능`
  - `파서 프로파일 N개`
- Ollama가 꺼져 있으면 `Ollama: 연결 안 됨`으로 나온다. 이때는 AI 기능만 쓸 수 없다. 휴대용 AI가 든 배포본이면 상태 표시줄에 “앱 폴더의 'AI와 함께 시작'으로 켜면 AI를 쓸 수 있습니다”가 함께 나온다.

### 오프라인 확인 (사용자가 사내 PC에서 직접)

| # | 확인 | 기대 |
|---|---|---|
| 1 | 네트워크를 끊은 상태에서 앱 실행 | 정상 실행, 상태 표시줄에 DB 스키마 v4 |
| 2 | 정상 로그 2개로 사례 등록 → 승인 | 승인됨 · 개정본 1 |
| 3 | 비정상 로그 + 가이드 + 매뉴얼 → 승인 | 승인됨 |
| 4 | 신규 비정상 로그 분석 | 맨 위 '권장 대응 — 승인된 대응 매뉴얼 근거' |
| 5 | Ollama 끈 채로 분석 | AI '사용 불가', 나머지 결과는 그대로 |
| 6 | 보고서 저장 → HTML 열기 | 인터넷 없이 열림 |
| 7 | 앱 종료 후 재실행 | 승인 사례 유지 |

## 2. 데이터 위치

| 무엇 | 위치 |
|---|---|
| 설정 | `%LOCALAPPDATA%\LocalLogAnalyzer\config\settings.json` (항상 여기) |
| DB·원본 사본·분석·백업·앱 로그 | 기본 `%LOCALAPPDATA%\LocalLogAnalyzer\`. 설정의 데이터 폴더로 바꿀 수 있다(다시 시작 후 적용) |

- 앱 폴더를 지우거나 새 버전으로 바꿔도 데이터는 남는다(§9.1 "재설치 후 학습 데이터 유지").
- 디스크: 로그 100MB를 가져오면 DB가 약 580MB가 된다. 원문, 정규화 값, 검색 색인을 함께 저장하기 때문이다(T-PERF-01).

## 3. 개발 PC

```bash
uv sync
```

```bash
uv run python main.py
```

```bash
uv run pytest
```

- 실제 Ollama 시험: PowerShell에서 `$env:LLA_LIVE_OLLAMA = "1"; uv run pytest tests/integration/test_ollama_live.py`
- 성능 시험(약 40초): `$env:LLA_PERF = "1"; uv run pytest tests/integration/test_performance.py -s`
- 실행 파일 빌드: `powershell -File packaging\build.ps1` (AI 포함: `-OllamaModel gemma3:4b`, GPU: `-OllamaGpu all|nvidia|cpu`)
