@echo off
if "%~1"=="" if exist "%~dp0desktop-app\Data Analysis Agent.exe" (
    start "" "%~dp0desktop-app\Data Analysis Agent.exe"
    exit /b 0
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
