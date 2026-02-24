@echo off

REM Kook Bot One-Click Start Script
REM Author: Kook Bot Team
REM Date: 2026-01-21

echo ===========================================
echo Kook Bot One-Click Start Script
echo ===========================================

REM Set Python interpreter path
set PYTHON_PATH="C:\Program Files\Python311\python.exe"

REM Check if Python exists
if not exist %PYTHON_PATH% (
    echo ERROR: Python interpreter not found
    echo Please make sure Python 3.11 is installed at %PYTHON_PATH%
    pause
    exit /b 1
)

REM Change to script directory
cd /d %~dp0

echo Current Directory: %CD%
echo Python Path: %PYTHON_PATH%
echo.
echo Starting bot scripts...
echo ===========================================
echo.

REM Start the script
%PYTHON_PATH% start_bots.py

REM Check start result
if %errorlevel% neq 0 (
    echo.
echo ===========================================
echo ERROR: Bot script failed to start
    echo Error Code: %errorlevel%
echo Please check log files for detailed error information
echo ===========================================
pause
    exit /b %errorlevel%
)

echo.
echo ===========================================
echo SUCCESS: Bot scripts started successfully
echo Press any key to exit...
echo ===========================================
pause

exit /b 0