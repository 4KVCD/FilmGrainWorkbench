@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" -m afgs1wb
) else (
  start "" pyw -3 -m afgs1wb
)
