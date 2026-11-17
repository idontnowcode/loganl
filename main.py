"""Local Log Analyzer 진입점.

    uv run python main.py           # 화면 실행
    uv run python main.py --check   # 화면 없이 설정·폴더·DB·Ollama 상태 점검
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from app import APP_NAME, __version__
from app.ai.ollama_client import OllamaClient
from app.bootstrap import AppContext, StartupError, initialize

ClientFactory = Callable[..., OllamaClient]


def run_check(context: AppContext, *, client_factory: ClientFactory | None = None, out=print) -> int:
    """GUI 없이 기반 상태를 출력한다. 기반(설정·폴더·DB)이 정상이면 0. Ollama가 꺼져 있어도 0이다
    (AI 장애 시에도 결정론 분석은 동작해야 하므로, §12.5)."""
    client_factory = client_factory or OllamaClient
    caps = context.capabilities
    settings = context.settings
    out(f"{APP_NAME} {__version__} 점검")
    out(f"- Python: {sys.version.split()[0]}")
    out(f"- 설정 파일: {context.settings_file}" + (" (이번에 새로 만듦)" if context.settings_created else ""))
    out(f"- 데이터 폴더: {context.paths.root}")
    out(f"- DB: {context.paths.db_file} (스키마 v{context.schema_version})")
    out(
        f"- SQLite {caps.version}: JSON {'사용 가능' if caps.json1 else '없음'}, "
        f"FTS5 {'사용 가능' if caps.fts5 else '없음'}"
    )
    from app.parsers.parser_registry import ParserRegistry

    registry = ParserRegistry.default()
    out(f"- 파서 프로파일 {len(registry.names())}개: {', '.join(registry.names()) or '없음'}"
        + (f" (읽기 실패 {len(registry.load_errors)}개)" if registry.load_errors else ""))
    with client_factory(
        settings.ollama.base_url,
        connect_timeout_s=settings.ollama.connect_timeout_s,
        request_timeout_s=settings.ollama.request_timeout_s,
    ) as client:
        status = client.check_status(settings.ollama.model)
    out(f"- {status.summary()}")
    for line in status.details().splitlines():
        out(f"    {line}")
    for line in network_safety_lines(settings.ollama.base_url, status):
        out(line)
    for warning in context.warnings:
        out(f"- 주의: {warning}")
    out("결과: 기반 정상" + ("" if status.reachable else " (Ollama 연결 안 됨 — AI 기능만 쓸 수 없음)"))
    return 0


def network_safety_lines(base_url: str, status) -> list[str]:
    """외부 인터넷과 연결되지 않게 쓰이는지 점검한 줄들 (§9.2). 이 앱이 볼 수 있는 것만 판정한다."""
    import os

    from app.security import is_loopback_host, is_loopback_url

    lines = ["- 네트워크 안전 점검"]
    ok = "[안전]"
    warn = "[주의]"
    lines.append(f"    {ok if is_loopback_url(base_url) else warn} 이 앱의 Ollama 주소: {base_url} "
                 "(이 PC 안 주소만 허용, 프록시 환경 변수 무시, 리디렉션 안 따라감)")
    host = os.environ.get("OLLAMA_HOST", "").strip()
    if host:
        bare = host.split("://")[-1].rsplit(":", 1)[0].strip("[]")
        lines.append(f"    {ok if is_loopback_host(bare) else warn} OLLAMA_HOST={host}"
                     + ("" if is_loopback_host(bare) else " — Ollama가 다른 PC에서 접속을 받을 수 있습니다. 127.0.0.1로 두세요."))
    else:
        lines.append(f"    {ok} OLLAMA_HOST 없음 → Ollama 기본값 127.0.0.1:11434 (이 PC 안에서만 받음)")
    no_cloud = os.environ.get("OLLAMA_NO_CLOUD", "").strip().lower() in ("1", "true", "yes")
    lines.append(f"    {ok if no_cloud else warn} OLLAMA_NO_CLOUD={'1' if no_cloud else '없음'}"
                 + ("" if no_cloud else " — Ollama 클라우드 기능이 허용된 상태입니다. 이 앱은 클라우드 모델을 거부하지만, "
                    "Ollama 자체 설정도 끄는 것을 권장합니다(사용 가이드 '외부 연결 차단')."))
    if status.reachable:
        if status.remote_models:
            lines.append(f"    {warn} 클라우드·원격 모델이 등록돼 있습니다(이 앱은 쓰지 않음): {', '.join(status.remote_models)}")
        else:
            lines.append(f"    {ok} 등록된 모델은 모두 로컬 모델입니다.")
    proxies = [k for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy") if os.environ.get(k)]
    if proxies:
        lines.append(f"    {ok} 프록시 환경 변수 {', '.join(proxies)}가 있지만 이 앱은 무시합니다.")
    lines.append("    [참고] Ollama 트레이 앱의 자동 업데이트·클라우드 설정은 이 앱이 확인할 수 없습니다. 사용 가이드의 확인 방법을 따르세요.")
    return lines


def run_gui(argv: Sequence[str]) -> int:
    from PySide6.QtWidgets import QApplication, QMessageBox

    from app.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication(list(argv))
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    from app.ui.theme import apply_theme

    apply_theme(app)

    try:
        context = initialize()
    except StartupError as exc:
        QMessageBox.critical(None, f"{APP_NAME} — 시작 실패", str(exc))
        return 1

    window = MainWindow(context)

    def report_unexpected(exc_type, exc, tb) -> None:
        """예상하지 못한 오류: 기록하고 알린 뒤 앱은 계속 돈다(§8.5 오류 메시지)."""
        import logging

        logging.getLogger("app").error("예상하지 못한 오류", exc_info=(exc_type, exc, tb))
        QMessageBox.warning(window, f"{APP_NAME} — 오류",
                            f"작업 중 예상하지 못한 오류가 났습니다. 자세한 내용은 logs/app.log에 남겼습니다.\n\n{exc}")

    sys.excepthook = report_unexpected
    window.show()
    try:
        return app.exec()
    finally:
        context.close()


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else [sys.argv[0], *argv])
    parser = argparse.ArgumentParser(prog="main.py", description=f"{APP_NAME} {__version__}")
    parser.add_argument("--check", action="store_true", help="화면 없이 설정·폴더·DB·Ollama 상태를 점검한다")
    args = parser.parse_args(argv[1:])

    if args.check:
        # 창 모드 실행 파일(콘솔 없음)에서는 stdout이 없으므로 결과를 <데이터>/logs/check.txt에도 쓴다.
        lines: list[str] = []
        console = sys.stdout is not None
        if console and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")

        def out(text: str) -> None:
            lines.append(text)
            if console:
                print(text)

        try:
            context = initialize()
        except StartupError as exc:
            if console:
                print(f"시작 실패: {exc}", file=sys.stderr)
            return 1
        try:
            code = run_check(context, out=out)
            (context.paths.logs / "check.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
            return code
        finally:
            context.close()

    return run_gui(argv)


if __name__ == "__main__":
    sys.exit(main())
