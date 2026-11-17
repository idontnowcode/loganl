# 제3자 소프트웨어 고지 — Local Log Analyzer

이 프로그램 배포본에는 다음 오픈소스 소프트웨어가 들어 있다. 각 라이선스 전문은 해당 프로젝트에서 확인한다.

| 소프트웨어 | 용도 | 라이선스 |
|---|---|---|
| Python 3.11 (CPython) | 실행 환경 | PSF License |
| Qt 6 / PySide6-Essentials, shiboken6 | 화면 | LGPLv3 (Qt for Python) |
| pydantic, pydantic-core | 데이터 검증 | MIT |
| httpx, httpcore, h11, anyio, idna, certifi | 로컬 Ollama 통신 | BSD-3 / MIT / MPL-2.0(certifi) |
| SQLite (Python 내장) | 데이터베이스 | Public Domain |
| openpyxl, et_xmlfile | 참고 자료 엑셀(.xlsx) 읽기 (CR-04) | MIT |
| pypdf | 참고 자료 PDF 글자 읽기 (CR-04) | BSD-3-Clause |
| Ollama (휴대용 동봉 시, `ollama\`) | 로컬 AI 서버 | MIT. 함께 든 llama.cpp·ggml(MIT)·Go 런타임(BSD-3) 고지는 `ollama\lib\ollama\*_LICENSE` |
| AI 모델 (휴대용 동봉 시) | 로컬 추론 | 모델별 라이선스. 전문은 `ollama\LICENSE_<모델>.txt` (예: Gemma Terms of Use) |
| NVIDIA CUDA·AMD ROCm 런타임 (동봉 GPU 선택에 따라) | GPU 가속 | 각 제조사 재배포 조건 |

- PySide6(Qt)는 LGPLv3다. 이 배포본은 라이브러리를 수정하지 않았으며, Qt 라이브러리는 실행 파일과 분리된 DLL로 들어 있어 사용자가 교체할 수 있다(onedir 배포).
- 사내 배포 전에 이 목록과 버전(`uv.lock`)을 사내 승인 패키지 목록과 대조한다(REQUIREMENTS Q3).
- 모델 라이선스(예: Gemma 사용 약관)는 사용 목적 제한이 있을 수 있다. 사내 사용 전에 법무·IT 승인을 받는다.
