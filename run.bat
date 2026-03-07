@echo off
REM Windows batch launcher for Polymarket Trading Bot

echo ============================================================
echo POLYMARKET TRADING BOT - PRODUCTION LAUNCHER
echo ============================================================
echo.

REM Check if venv exists
if not exist "venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found!
    echo Run: python -m venv venv
    exit /b 1
)

echo [1/2] Activating virtual environment...
call venv\Scripts\activate.bat

echo [2/2] Launching bot...
echo.

echo [INFO] main.py uses config-defined capital; launcher ignores explicit capital overrides.

REM Launch bot with the supported entrypoint
venv\Scripts\python.exe main.py --config config/production.yaml --mode paper


echo.
echo Bot shutdown complete.
pause
