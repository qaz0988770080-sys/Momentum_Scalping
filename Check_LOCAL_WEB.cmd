@echo off
setlocal
set "MOMENTUM_PYTHON=python"
if exist "%~dp0python_path.txt" set /p "MOMENTUM_PYTHON="<"%~dp0python_path.txt"
title Momentum - local web connection check
"%MOMENTUM_PYTHON%" "%~dp0taishin_web_diagnose.py"
echo.
echo This check does not log in or submit orders.
pause
