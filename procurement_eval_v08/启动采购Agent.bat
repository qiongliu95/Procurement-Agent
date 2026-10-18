@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\procurement_agent_control.ps1" -Action Start
if errorlevel 1 (
  echo.
  echo 启动失败，请根据上方中文提示处理。
  pause
  exit /b 1
)
endlocal
