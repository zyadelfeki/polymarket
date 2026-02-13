@echo off
setlocal

cd /d "%~dp0"
if not exist logs mkdir logs
if not exist runtime mkdir runtime

echo Starting Polymarket Phase 6 paper bot...
start "paper-bot" cmd /k "call .venv\Scripts\activate && python run_production_bot.py --mode paper --db-path data\phase6_paper.db >> logs\paper_bot.log 2>&1"

echo Starting monitoring dashboard...
start "paper-dashboard" cmd /k "call .venv\Scripts\activate && python monitoring_dashboard.py --db-path data\phase6_paper.db"

echo Starting log tail...
start "paper-log-tail" cmd /k "powershell -NoProfile -Command Get-Content logs\paper_bot.log -Wait"

echo Heartbeat file: runtime\heartbeat.txt
echo Use emergency_kill_switch.py to stop trading safely if needed.

endlocal
