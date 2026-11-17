@echo off
rem 'AI와 함께 시작'으로 켠 휴대용 AI 서버만 끈다. 이 PC에 따로 설치된 Ollama는 건드리지 않는다.
tasklist /fi "WINDOWTITLE eq LLA AI*" 2>nul | find /i "ollama" >nul
if errorlevel 1 (
  echo 켜져 있는 휴대용 AI 서버가 없습니다.
) else (
  taskkill /fi "WINDOWTITLE eq LLA AI*" /t /f >nul 2>&1
  echo 휴대용 AI 서버를 껐습니다.
)
ping -n 3 127.0.0.1 >nul
