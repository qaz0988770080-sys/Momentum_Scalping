@echo off
setlocal
set MOMENTUM_PYTHON=python
if exist "%~dp0python_path.txt" set /p "MOMENTUM_PYTHON="<"%~dp0python_path.txt"
title Taishin Futures - VALIDATION LOGIN ONLY
echo Open http://127.0.0.1:8790 after FUTURES_SELF_CHECK_OK
echo Keep this window open. No orders are enabled.
"%MOMENTUM_PYTHON%" -u "%~dp0taishin_futures_web.py"
echo Service stopped.
pause
