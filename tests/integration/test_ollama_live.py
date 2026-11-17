"""실제 로컬 Ollama (선택). `LLA_LIVE_OLLAMA=1`일 때만 돈다. loopback이라 네트워크 차단 장치를 통과한다.
모델은 `LLA_OLLAMA_MODEL`(없으면 설치된 첫 모델). 실제 모델의 답 내용은 매번 다를 수 있어 '형식·검증 경로'만 본다."""

import os
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.ai.ollama_client import OllamaClient

pytestmark = [
    pytest.mark.ollama_live,
    pytest.mark.skipif(os.environ.get("LLA_LIVE_OLLAMA") != "1", reason="LLA_LIVE_OLLAMA=1일 때만 실행"),
]
URL = os.environ.get("LLA_OLLAMA_URL", "http://127.0.0.1:11434")


def live_model() -> str:
    with OllamaClient(URL, connect_timeout_s=3, request_timeout_s=30) as client:
        status = client.check_status()
    assert status.reachable, status.error
    chat_models = [m for m in status.models if "embed" not in m]
    return os.environ.get("LLA_OLLAMA_MODEL") or chat_models[0]


def test_live_status() -> None:
    with OllamaClient(URL, connect_timeout_s=3, request_timeout_s=30) as client:
        status = client.check_status()
    assert status.reachable, status.error
    assert status.version


class Echo(BaseModel):
    answer: str


def test_live_generate_json_matches_schema() -> None:
    model = live_model()
    with OllamaClient(URL, connect_timeout_s=3, request_timeout_s=300) as client:
        raw = client.generate_json(model=model, system="JSON으로만 답하라.", prompt="answer에 '가상'이라고 적어라.",
                                   schema=Echo.model_json_schema())
    Echo.model_validate_json(raw)


def test_live_diagnosis_runs_through_validation(tmp_path: Path) -> None:
    """실제 모델로 진단 경로 전체(근거 묶음 → 생성 → V-1~V-9 검증 → 저장)가 오류 없이 끝나는지."""
    from app.analysis.diagnostic_engine import AnalysisRequest, DiagnosticEngine
    from app.bootstrap import initialize
    from app.core.importer import LogImporter
    from app.models.enums import OriginPurpose
    from app.models.records import ImportRequest
    from tests.logdata import abnormal_log

    model = live_model()
    ctx = initialize(tmp_path / "home", configure_logging=False)
    try:
        ctx.settings = ctx.settings.model_copy(update={"ollama": ctx.settings.ollama.model_copy(update={"model": model})})
        ids = LogImporter(ctx.database, ctx.paths, ctx.settings.import_).import_logs(
            ImportRequest(paths=[abnormal_log(tmp_path / "bad.log")], purpose=OriginPurpose.ANALYSIS)).log_file_ids
        result = DiagnosticEngine(ctx.database, ctx.paths, ctx.settings).run(AnalysisRequest(ids))
        print(f"\n실제 모델 {model}: ai_status={result.ai.status}, 오류={result.ai.errors[:3]}")
        assert result.ai.status in ("OK", "VALIDATION_FAILED")  # 둘 다 정상 경로. 검증 실패면 저장 안 함
    finally:
        ctx.close()
