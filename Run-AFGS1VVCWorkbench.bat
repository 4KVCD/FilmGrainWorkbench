@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" -m afgs1vvc.app
) else (
  py -3 -m afgs1vvc.app
)
if errorlevel 1 pause
