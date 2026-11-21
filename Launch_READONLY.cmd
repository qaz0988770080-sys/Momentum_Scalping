@echo off
setlocal
set "MOMENTUM_PYTHON=python"
if exist "%~dp0python_path.txt" set /p "MOMENTUM_PYTHON="<"%~dp0python_path.txt"
"%MOMENTUM_PYTHON%" "%~dp0taishin_readonly_ui.py"
if errorlevel 1 pause
