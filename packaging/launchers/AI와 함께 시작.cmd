@echo off
setlocal EnableExtensions
rem Local Log Analyzer: 앱 폴더의 휴대용 AI 서버(Ollama)를 켜고 앱을 연다. 아무것도 설치하지 않는다.
cd /d "%~dp0"
set "LLA_PORT=11434"
if defined LLA_OLLAMA_PORT set "LLA_PORT=%LLA_OLLAMA_PORT%"
rem 이 PC 안 주소에서만 받는다. 모델은 앱 폴더 안의 것만 쓴다. 클라우드 기능은 끈다.
set "OLLAMA_HOST=127.0.0.1:%LLA_PORT%"
set "OLLAMA_MODELS=%~dp0ollama\models"
set "OLLAMA_NO_CLOUD=1"
set "LLA_URL=http://127.0.0.1:%LLA_PORT%/api/version"

curl.exe -s -m 2 "%LLA_URL%" >nul 2>&1
if not errorlevel 1 (
  echo 이 PC에서 이미 AI 서버가 실행 중입니다. 그대로 사용합니다.
  goto start_app
)
if not exist "%~dp0ollama\ollama.exe" (
  echo 앱 폴더에 ollama 폴더가 없습니다. AI 기능 없이 앱만 엽니다.
  ping -n 4 127.0.0.1 >nul
  goto start_app
)
echo AI 서버를 켜는 중입니다. 잠시 기다려 주세요.
start "LLA AI 서버 - 이 창을 닫으면 AI가 꺼집니다" /min "%~dp0ollama\ollama.exe" serve
for /l %%i in (1,1,30) do (
  ping -n 2 127.0.0.1 >nul
  curl.exe -s -m 1 "%LLA_URL%" >nul 2>&1 && goto start_app
)
echo AI 서버가 30초 안에 준비되지 않았습니다. 앱은 그대로 엽니다. 잠시 뒤 앱 아래쪽의 '다시 확인'을 누르세요.
ping -n 6 127.0.0.1 >nul

:start_app
start "" "%~dp0LocalLogAnalyzer.exe"
endlocal
