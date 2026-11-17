"""배포 폴더에 휴대용 Ollama와 모델을 넣는다 (CR-03, 빌드 PC에서만 실행).

다른 PC에서 아무것도 설치하지 않고 AI 기능까지 쓰게 하려는 것이다. 인터넷에서 내려받지 않고,
빌드 PC에 이미 설치된 Ollama 실행 파일과 이미 받아 둔 모델 파일을 복사한다.

    uv run python packaging/bundle_ollama.py --dest dist/LocalLogAnalyzer --model gemma3:4b --gpu all

결과(배포 폴더 안):
    ollama/ollama.exe, ollama/lib/ollama/...   Ollama 서버(설치 프로그램·트레이 앱·자동 업데이트 제외)
    ollama/models/manifests/..., blobs/...       선택한 모델 하나
    ollama/LICENSE_<모델>.txt, ollama/읽어보기.txt
    AI와 함께 시작.cmd, AI 서버 끄기.cmd        더블클릭용(앱 코드는 프로세스를 실행하지 않는다)
    _internal/configs/settings.json              배포 기본 모델을 이 모델로(값이 비어 있을 때만)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

REGISTRY = "registry.ollama.ai"
GPU_PROFILES = {
    "all": (),  # 어떤 PC에서도 GPU를 쓸 수 있게 전부
    "nvidia": ("rocm",),  # NVIDIA(CUDA)와 CPU·Vulkan
    "cpu": ("cuda", "rocm"),  # CPU·Vulkan만 (가장 작음)
}
LAUNCHERS = ("AI와 함께 시작.cmd", "AI 서버 끄기.cmd")
LAUNCHER_DIR = Path(__file__).resolve().parent / "launchers"
LICENSE_MEDIA = "application/vnd.ollama.image.license"


class BundleError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelRef:
    namespace: str
    name: str
    tag: str

    @property
    def manifest_rel(self) -> Path:
        return Path("manifests", REGISTRY, self.namespace, self.name, self.tag)

    @property
    def label(self) -> str:
        return f"{self.name}:{self.tag}" if self.namespace == "library" else f"{self.namespace}/{self.name}:{self.tag}"


def parse_model(text: str) -> ModelRef:
    """'gemma3:4b', 'gemma3'(=latest), 'user/model:tag'. 다른 레지스트리 주소는 받지 않는다(빌드 PC 로컬 모델만)."""
    text = text.strip()
    if not text or "\\" in text or ".." in text or text.count("/") > 1 or "://" in text:
        raise BundleError(f"모델 이름 형식이 아닙니다: {text!r}")
    path, _, tag = text.partition(":")
    namespace, _, name = path.rpartition("/")
    if not name:
        raise BundleError(f"모델 이름 형식이 아닙니다: {text!r}")
    return ModelRef(namespace or "library", name, tag or "latest")


def default_ollama_dir() -> Path:
    env = os.environ.get("LLA_OLLAMA_DIR", "").strip()
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA", "").strip() or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Programs" / "Ollama"


def default_models_dir() -> Path:
    env = os.environ.get("OLLAMA_MODELS", "").strip()
    return Path(env) if env else Path.home() / ".ollama" / "models"


def model_blobs(models_dir: Path, ref: ModelRef) -> tuple[dict, list[tuple[str, str, int]]]:
    """매니페스트를 읽어 (매니페스트, [(digest, mediaType, size)])를 돌려준다."""
    manifest_file = models_dir / ref.manifest_rel
    if not manifest_file.is_file():
        raise BundleError(f"빌드 PC에 모델 {ref.label}이(가) 없습니다: {manifest_file}")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    entries = [manifest["config"], *manifest.get("layers", [])]
    blobs = []
    for entry in entries:
        digest = entry["digest"]
        if not digest.startswith("sha256:") or len(digest) != 71:
            raise BundleError(f"매니페스트의 digest 형식이 이상합니다: {digest}")
        blobs.append((digest, entry.get("mediaType", ""), int(entry.get("size", 0))))
    return manifest, blobs


def blob_path(models_dir: Path, digest: str) -> Path:
    return models_dir / "blobs" / digest.replace(":", "-")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def runtime_files(ollama_dir: Path, gpu: str) -> list[Path]:
    """ollama.exe와 lib/ollama 아래에서 GPU 선택에 맞는 파일(빌드 PC 기준 상대 경로)."""
    if gpu not in GPU_PROFILES:
        raise BundleError(f"--gpu는 {', '.join(GPU_PROFILES)} 중 하나입니다: {gpu}")
    exe = ollama_dir / "ollama.exe"
    lib = ollama_dir / "lib" / "ollama"
    if not exe.is_file() or not lib.is_dir():
        raise BundleError(f"Ollama 실행 파일을 찾지 못했습니다(ollama.exe, lib\\ollama): {ollama_dir}")
    skipped = GPU_PROFILES[gpu]
    files = [Path("ollama.exe")]
    for path in sorted(lib.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ollama_dir)
        top = path.relative_to(lib).parts[0].lower()
        if len(path.relative_to(lib).parts) > 1 and top.startswith(skipped):
            continue
        files.append(rel)
    return files


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_size == src.stat().st_size and dst.stat().st_mtime >= src.stat().st_mtime:
        return
    shutil.copy2(src, dst)


def set_default_model(dest: Path, ref: ModelRef) -> bool:
    """배포 기본 설정의 모델이 비어 있으면 동봉한 모델로 채운다. 이미 값이 있으면 건드리지 않는다."""
    candidates = [dest / "_internal" / "configs" / "settings.json", dest / "configs" / "settings.json"]
    shipped = next((p for p in candidates if p.is_file()), None)
    if shipped is None:
        raise BundleError(f"배포 기본 설정(configs\\settings.json)을 찾지 못했습니다: {dest}")
    data = json.loads(shipped.read_text(encoding="utf-8"))
    if data.get("ollama", {}).get("model"):
        return False
    data.setdefault("ollama", {})["model"] = ref.label
    shipped.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return True


def bundle(dest: Path, model: str, gpu: str = "all", ollama_dir: Path | None = None,
           models_dir: Path | None = None, verify: bool = True, log=print) -> dict:
    ref = parse_model(model)
    ollama_dir = ollama_dir or default_ollama_dir()
    models_dir = models_dir or default_models_dir()
    if not (dest / "LocalLogAnalyzer.exe").is_file() and not (dest / "main.py").is_file():
        raise BundleError(f"배포 폴더가 아닙니다(LocalLogAnalyzer.exe 없음): {dest}")
    manifest, blobs = model_blobs(models_dir, ref)
    files = runtime_files(ollama_dir, gpu)
    out = dest / "ollama"

    runtime_bytes = 0
    for rel in files:
        src = ollama_dir / rel
        copy_file(src, out / rel)
        runtime_bytes += src.stat().st_size
    log(f"Ollama 실행 파일 {len(files)}개 복사 ({runtime_bytes / 2**30:.2f} GB, GPU 선택: {gpu})")

    model_bytes = 0
    for digest, media, size in blobs:
        src = blob_path(models_dir, digest)
        if not src.is_file():
            raise BundleError(f"모델 파일이 빠져 있습니다: {src}")
        dst = blob_path(out / "models", digest)
        copy_file(src, dst)
        if verify and sha256_of(dst) != digest:
            dst.unlink(missing_ok=True)
            raise BundleError(f"복사한 모델 파일의 해시가 다릅니다: {dst.name}")
        if media == LICENSE_MEDIA:
            license_text = dst.read_text(encoding="utf-8", errors="replace")
            (out / f"LICENSE_{ref.name}_{ref.tag}.txt").write_text(license_text, encoding="utf-8")
        model_bytes += src.stat().st_size
    manifest_dst = out / "models" / ref.manifest_rel
    manifest_dst.parent.mkdir(parents=True, exist_ok=True)
    manifest_dst.write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")
    log(f"모델 {ref.label} 복사 ({model_bytes / 2**30:.2f} GB, 해시 확인 {'함' if verify else '안 함'})")

    for name in LAUNCHERS:
        src = LAUNCHER_DIR / name
        if not src.is_file():
            raise BundleError(f"시작 파일 원본이 없습니다: {src}")
        shutil.copyfile(src, dest / name)
    changed = set_default_model(dest, ref)

    (out / "읽어보기.txt").write_text(
        "이 폴더는 Local Log Analyzer가 쓰는 휴대용 Ollama입니다. 설치하지 않고 이 자리에서 실행됩니다.\n"
        f"- 모델: {ref.label}\n- GPU 선택: {gpu}\n"
        "- 켜기: 앱 폴더의 'AI와 함께 시작.cmd' (이 PC 안 주소 127.0.0.1에서만 받고, 클라우드 기능은 끈 채로 켭니다)\n"
        "- 끄기: 'AI 서버 끄기.cmd' 또는 작업 표시줄의 'LLA AI 서버' 창 닫기\n"
        "- 라이선스: Ollama(MIT), 모델은 같은 폴더의 LICENSE_*.txt\n",
        encoding="utf-8")
    log(f"시작 파일 {len(LAUNCHERS)}개, 배포 기본 모델 {'설정함' if changed else '이미 있어 그대로 둠'}")
    return {"model": ref.label, "gpu": gpu, "runtime_files": len(files),
            "runtime_bytes": runtime_bytes, "model_bytes": model_bytes, "default_model_set": changed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="배포 폴더에 휴대용 Ollama와 모델을 넣는다.")
    parser.add_argument("--dest", required=True, type=Path, help="배포 폴더 (dist\\LocalLogAnalyzer)")
    parser.add_argument("--model", required=True, help="빌드 PC에 받아 둔 모델 (예: gemma3:4b)")
    parser.add_argument("--gpu", default="all", choices=sorted(GPU_PROFILES), help="넣을 GPU 라이브러리")
    parser.add_argument("--ollama-dir", type=Path, help="Ollama 설치 폴더 (기본: %%LOCALAPPDATA%%\\Programs\\Ollama)")
    parser.add_argument("--models-dir", type=Path, help="모델 폴더 (기본: OLLAMA_MODELS 또는 %%USERPROFILE%%\\.ollama\\models)")
    parser.add_argument("--no-verify", action="store_true", help="복사한 모델 파일의 해시 확인을 건너뜀")
    args = parser.parse_args(argv)
    try:
        bundle(args.dest, args.model, args.gpu, args.ollama_dir, args.models_dir, verify=not args.no_verify)
    except (BundleError, OSError, ValueError, KeyError) as exc:
        print(f"동봉 실패: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
