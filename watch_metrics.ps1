# Watch metrics in real-time
Write-Host "`n📊 LIVE TRADING BOT MONITOR" -ForegroundColor Cyan
Write-Host "Updates every 30 seconds. Press Ctrl+C to exit.`n" -ForegroundColor Gray

while ($true) {
    python check_performance.py
    Start-Sleep -Seconds 30
}
