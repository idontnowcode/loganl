"""계층 의존 규칙과 금지 모듈 (docs/ARCHITECTURE.md §2, DEV-03·04, NA-15, SEC-02·09, PRN-09)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"

LAYER_FORBIDDEN: dict[str, tuple[str, ...]] = {
    "app.models": ("app.config", "app.security", "app.bootstrap", "app.storage", "app.parsers", "app.core",
                   "app.ai", "app.learning", "app.analysis", "app.reports", "app.ui"),
    "app.storage": ("app.core", "app.parsers", "app.ai", "app.learning", "app.analysis", "app.reports",
                    "app.ui", "app.bootstrap"),
    "app.parsers": ("app.ai", "app.learning", "app.analysis", "app.reports", "app.ui", "app.bootstrap"),
    "app.core": ("app.ai", "app.learning", "app.analysis", "app.reports", "app.ui", "app.bootstrap"),
    "app.ai": ("app.learning", "app.analysis", "app.reports", "app.ui", "app.bootstrap"),
    "app.learning": ("app.ui", "app.bootstrap"),
    "app.analysis": ("app.ui", "app.bootstrap"),
    "app.reports": ("app.ai", "app.ui", "app.bootstrap"),
}

FORBIDDEN_EVERYWHERE = (
    "subprocess", "socket", "serial", "can", "ctypes", "requests", "urllib.request", "http.client",
    "ftplib", "smtplib", "telnetlib", "paramiko", "openai", "anthropic", "google.generativeai",
)
FORBIDDEN_OS_CALLS = {"system", "popen", "startfile", "execv", "execve", "execl", "execlp", "spawnl", "spawnv"}
HTTPX_ALLOWED = {"app.ai.ollama_client"}


def module_name(path: Path) -> str:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def package_of(path: Path) -> list[str]:
    parts = module_name(path).split(".")
    return parts if path.name == "__init__.py" else parts[:-1]


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package = package_of(path)
                base = package[: len(package) - node.level + 1]
                prefix = ".".join(base + ([node.module] if node.module else []))
            else:
                prefix = node.module or ""
            found.add(prefix)
            found.update(f"{prefix}.{alias.name}" for alias in node.names)
    return found


def os_calls_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
        and (node.attr in FORBIDDEN_OS_CALLS or node.attr.startswith(("spawn", "exec")))
    }


def matches(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


APP_FILES = sorted(APP.rglob("*.py"))


def test_app_files_found() -> None:
    assert len(APP_FILES) >= 10


@pytest.mark.parametrize("path", APP_FILES, ids=lambda p: module_name(p))
def test_layer_rules(path: Path) -> None:
    name = module_name(path)
    imported = imports_of(path)
    for layer, forbidden in LAYER_FORBIDDEN.items():
        if matches(name, layer):
            bad = sorted(i for i in imported for f in forbidden if matches(i, f))
            assert not bad, f"{name}은(는) {bad}를 import하면 안 된다"
    if not matches(name, "app.ui"):
        qt = sorted(i for i in imported if matches(i, "PySide6"))
        assert not qt, f"화면 밖 모듈 {name}이(가) PySide6를 import한다: {qt}"


@pytest.mark.parametrize("path", APP_FILES, ids=lambda p: module_name(p))
def test_forbidden_modules(path: Path) -> None:
    name = module_name(path)
    imported = imports_of(path)
    bad = sorted(i for i in imported for f in FORBIDDEN_EVERYWHERE if matches(i, f))
    assert not bad, f"{name}: 금지 모듈 {bad}"
    if name not in HTTPX_ALLOWED:
        assert not any(matches(i, "httpx") for i in imported), f"{name}: httpx는 ollama_client에서만 쓴다"
    assert not os_calls_of(path), f"{name}: 프로세스 실행 함수 {os_calls_of(path)}"


def test_scanner_detects_violations(tmp_path: Path) -> None:
    """검사기 자체가 동작하는지 확인한다."""
    source = "import subprocess\nfrom app.ai import ollama_client\nimport os\nos.system('x')\n"
    sample = tmp_path / "probe.py"
    sample.write_text(source, encoding="utf-8")
    found = imports_of(sample)
    assert "subprocess" in found and "app.ai.ollama_client" in found
    assert os_calls_of(sample) == {"system"}
