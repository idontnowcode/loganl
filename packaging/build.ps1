# Local Log Analyzer 실행 파일 빌드 (인터넷 되는 개발 PC에서 실행)
# 결과: dist\LocalLogAnalyzer\ 폴더. 이 폴더를 통째로 다른 PC에 복사하면 설치 없이 실행된다(docs\INSTALL.md).
#
#   packaging\build.ps1                                  앱만 (AI는 그 PC의 Ollama가 있을 때만)
#   packaging\build.ps1 -OllamaModel gemma3:4b           휴대용 Ollama와 모델까지 동봉 (CR-03)
#   packaging\build.ps1 -OllamaModel gemma3:4b -OllamaGpu cpu   GPU 라이브러리를 빼서 작게
#
# 동봉은 이 PC에 설치된 Ollama와 이미 받아 둔 모델 파일을 복사한다. 인터넷에서 내려받지 않는다.
# uv는 진행 상황을 stderr로 쓴다. 출력을 파일로 돌리면 PowerShell 5.1이 그것을 오류로 바꾸므로
# "Stop" 대신 각 명령의 종료 코드로 성공 여부를 판단한다.
param(
    [string]$OllamaModel = "",
    [ValidateSet("all", "nvidia", "cpu")]
    [string]$OllamaGpu = "all"
)
$ErrorActionPreference = "Continue"
Set-Location (Split-Path $PSScriptRoot -Parent)

uv sync
if ($LASTEXITCODE -ne 0) { throw "uv sync가 실패했습니다." }
uv run pytest -q
if ($LASTEXITCODE -ne 0) { throw "테스트가 실패해서 빌드하지 않습니다." }
uv run pyinstaller packaging\local_log_analyzer.spec --noconfirm --distpath dist --workpath build
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 빌드가 실패했습니다." }

if ($OllamaModel) {
    uv run python packaging\bundle_ollama.py --dest dist\LocalLogAnalyzer --model $OllamaModel --gpu $OllamaGpu
    if ($LASTEXITCODE -ne 0) { throw "Ollama 동봉이 실패했습니다." }
}
Write-Host "완료: dist\LocalLogAnalyzer\LocalLogAnalyzer.exe"
