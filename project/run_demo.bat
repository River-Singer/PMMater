@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m pm_core.cli demo --out demo_run
) else (
  py -3 -m pm_core.cli demo --out demo_run
)
pause
