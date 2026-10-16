@echo off
set "PYTHONPATH=%~dp0"
"%~dp0.venv\Scripts\python.exe" -P -m llamadas %*
