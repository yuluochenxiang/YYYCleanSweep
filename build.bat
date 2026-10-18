@echo off
REM Do not switch console codepage here: it can kill the batch instantly.
REM This file stays pure ASCII; all Chinese output comes from build.py.
setlocal
set "PY=E:\work\WB_\YYYCleanSweep\venv\Scripts\python.exe"
set "LOG=%~dp0build_bat.log"
if not exist "%PY%" (
    echo [ERROR] Cannot find venv python: %PY%
    echo Please create the venv and install deps (PySide6 6.12 + PyInstaller 6.22).
    echo See build.py header for the exact commands.
    pause
    exit /b 1
)
echo [%date% %time%] build.bat start > "%LOG%"
"%PY%" "%~dp0build.py" %* >> "%LOG%" 2>&1
echo [%date% %time%] build.bat exit code = %errorlevel% >> "%LOG%"
echo.
echo Build log written to: %LOG%
echo (open it to see details; last line shows the exit code)
echo.
pause
