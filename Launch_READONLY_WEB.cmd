@echo off
setlocal
set "MOMENTUM_PYTHON=python"
if exist "%~dp0python_path.txt" set /p "MOMENTUM_PYTHON="<"%~dp0python_path.txt"
title Momentum Taishin - local read-only web server
echo Starting the local read-only web server...
echo Keep this window open while using http://127.0.0.1:8788
"%MOMENTUM_PYTHON%" -u "%~dp0taishin_web_start.py" --port 8788
echo.
echo Server stopped. Exit code: %ERRORLEVEL%
echo Diagnostics: %~dp0private\web-startup.jsonl
pause
