@echo off
REM Do not switch console codepage here: it can kill the batch instantly.
REM This file stays pure ASCII; all Chinese output comes from run.py.
setlocal
rem ASCII-only on purpose: Chinese messages are printed by run.py (UTF-8).
rem Double-click this file to run YYYCleanSweep from source on any machine.

set "PYW="
if exist "%~dp0venv\Scripts\python.exe" (
    set "PYW=%~dp0venv\Scripts\python.exe"
) else (
    set "PYW=python"
)

"%PYW%" "%~dp0run.py" %*
if errorlevel 1 pause
