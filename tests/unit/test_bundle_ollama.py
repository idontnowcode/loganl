"""휴대용 Ollama 동봉 (CR-03, packaging/bundle_ollama.py). 가짜 Ollama 폴더와 가짜 모델로 확인한다."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("bundle_ollama", ROOT / "packaging" / "bundle_ollama.py")
bo = importlib.util.module_from_spec(_spec)
sys.modules["bundle_ollama"] = bo
_spec.loader.exec_module(bo)


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.fixture
def fake(tmp_path: Path):
    ollama = tmp_path / "Ollama"
    for rel in ("ollama.exe", "lib/ollama/ggml-base.dll", "lib/ollama/LLAMA_CPP_LICENSE",
                "lib/ollama/cuda_v12/ggml-cuda.dll", "lib/ollama/rocm_v7_1/ggml-hip.dll", "lib/ollama/vulkan/ggml-vulkan.dll"):
        (ollama / rel).parent.mkdir(parents=True, exist_ok=True)
        (ollama / rel).write_bytes(b"fake " + rel.encode())
    (ollama / "ollama app.exe").write_bytes(b"tray app")  # 동봉하지 않아야 함

    models = tmp_path / "models"
    contents = {"model": b"GGUF fake weights", "license": "가상 라이선스 TEST".encode(), "config": b"{}"}
    layers = [{"mediaType": "application/vnd.ollama.image.model", "digest": digest(contents["model"]), "size": 17},
              {"mediaType": bo.LICENSE_MEDIA, "digest": digest(contents["license"]), "size": len(contents["license"])}]
    manifest = {"schemaVersion": 2, "config": {"digest": digest(contents["config"]), "size": 2}, "layers": layers}
    (models / "blobs").mkdir(parents=True)
    for data in contents.values():
        (models / "blobs" / digest(data).replace(":", "-")).write_bytes(data)
    mf = models / "manifests" / "registry.ollama.ai" / "library" / "testmodel" / "4b"
    mf.parent.mkdir(parents=True)
    mf.write_text(json.dumps(manifest), encoding="utf-8")

    dest = tmp_path / "dist" / "LocalLogAnalyzer"
    (dest / "_internal" / "configs").mkdir(parents=True)
    (dest / "LocalLogAnalyzer.exe").write_bytes(b"exe")
    (dest / "_internal" / "configs" / "settings.json").write_text(
        json.dumps({"ollama": {"base_url": "http://127.0.0.1:11434", "model": None}}), encoding="utf-8")
    return ollama, models, dest, contents


@pytest.mark.parametrize("text,expected", [
    ("gemma3:4b", ("library", "gemma3", "4b")),
    ("gemma3", ("library", "gemma3", "latest")),
    ("someone/model:q4", ("someone", "model", "q4")),
])
def test_parse_model(text, expected) -> None:
    ref = bo.parse_model(text)
    assert (ref.namespace, ref.name, ref.tag) == expected


@pytest.mark.parametrize("text", ["", "../evil:1", "a/b/c:1", "https://x/y:1", "a\\b:1", ":tag"])
def test_parse_model_rejects_paths_and_registries(text) -> None:
    with pytest.raises(bo.BundleError):
        bo.parse_model(text)


@pytest.mark.parametrize("gpu,present,absent", [
    ("all", {"cuda_v12", "rocm_v7_1", "vulkan"}, set()),
    ("nvidia", {"cuda_v12", "vulkan"}, {"rocm_v7_1"}),
    ("cpu", {"vulkan"}, {"cuda_v12", "rocm_v7_1"}),
])
def test_gpu_profiles(fake, gpu, present, absent) -> None:
    ollama, _, _, _ = fake
    dirs = {p.parts[2] for p in bo.runtime_files(ollama, gpu) if len(p.parts) > 3}
    assert present <= dirs and not (absent & dirs)
    names = {p.name for p in bo.runtime_files(ollama, gpu)}
    assert "ollama.exe" in names and "ggml-base.dll" in names and "ollama app.exe" not in names


def test_bundle_copies_runtime_model_launchers_and_sets_default(fake) -> None:
    ollama, models, dest, contents = fake
    result = bo.bundle(dest, "testmodel:4b", "cpu", ollama, models, log=lambda _m: None)
    out = dest / "ollama"
    assert (out / "ollama.exe").is_file() and not (out / "ollama app.exe").exists()
    assert not (out / "lib" / "ollama" / "cuda_v12").exists()
    for data in contents.values():
        assert (out / "models" / "blobs" / digest(data).replace(":", "-")).read_bytes() == data
    assert (out / "models" / "manifests" / "registry.ollama.ai" / "library" / "testmodel" / "4b").is_file()
    assert (out / "LICENSE_testmodel_4b.txt").read_text(encoding="utf-8") == "가상 라이선스 TEST"
    for name in bo.LAUNCHERS:
        assert (dest / name).is_file()
    shipped = json.loads((dest / "_internal" / "configs" / "settings.json").read_text(encoding="utf-8"))
    assert shipped["ollama"]["model"] == "testmodel:4b" and result["default_model_set"]


def test_existing_default_model_is_kept(fake) -> None:
    ollama, models, dest, _ = fake
    cfg = dest / "_internal" / "configs" / "settings.json"
    cfg.write_text(json.dumps({"ollama": {"model": "other:7b"}}), encoding="utf-8")
    result = bo.bundle(dest, "testmodel:4b", "cpu", ollama, models, log=lambda _m: None)
    assert json.loads(cfg.read_text(encoding="utf-8"))["ollama"]["model"] == "other:7b"
    assert not result["default_model_set"]


def test_corrupted_blob_is_rejected(fake) -> None:
    ollama, models, dest, contents = fake
    (models / "blobs" / digest(contents["model"]).replace(":", "-")).write_bytes(b"tampered weights!")
    with pytest.raises(bo.BundleError, match="해시"):
        bo.bundle(dest, "testmodel:4b", "cpu", ollama, models, log=lambda _m: None)


def test_missing_model_and_wrong_dest_fail_clearly(fake, tmp_path: Path) -> None:
    ollama, models, dest, _ = fake
    with pytest.raises(bo.BundleError, match="없습니다"):
        bo.bundle(dest, "nomodel:1b", "cpu", ollama, models, log=lambda _m: None)
    with pytest.raises(bo.BundleError, match="배포 폴더"):
        bo.bundle(tmp_path / "empty", "testmodel:4b", "cpu", ollama, models, log=lambda _m: None)


def test_launchers_keep_server_local_and_offline() -> None:
    """시작 파일은 CP949·CRLF(한국어 Windows cmd), loopback 바인딩, 클라우드 끔, 앱 폴더 모델만."""
    start = (ROOT / "packaging" / "launchers" / "AI와 함께 시작.cmd").read_bytes()
    stop = (ROOT / "packaging" / "launchers" / "AI 서버 끄기.cmd").read_bytes()
    for raw in (start, stop):
        assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
        raw.decode("cp949")
    text = start.decode("cp949")
    assert 'set "OLLAMA_HOST=127.0.0.1:%LLA_PORT%"' in text
    assert 'set "OLLAMA_NO_CLOUD=1"' in text
    assert 'set "OLLAMA_MODELS=%~dp0ollama\\models"' in text
    assert "0.0.0.0" not in text and "http://" in text and "https://" not in text
    assert "WINDOWTITLE eq LLA AI*" in stop.decode("cp949")  # 따로 설치된 Ollama는 끄지 않음
