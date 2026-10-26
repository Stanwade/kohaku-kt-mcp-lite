@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\pythonw.exe" (
    start "" "%~dp0.venv\Scripts\pythonw.exe" -m kt_mcp.gui
) else (
    echo Please install first: python -m venv .venv
    echo Then: .venv\Scripts\python.exe -m pip install -e .
    pause
    exit /b 1
)
