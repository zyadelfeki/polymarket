#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Production-grade launcher for Polymarket Trading Bot
    
.DESCRIPTION
    Activates virtual environment and runs the bot with proper error handling
    
.PARAMETER Mode
    Trading mode: 'paper' or 'live'
    
.PARAMETER Capital
    Initial capital in USD
    
.EXAMPLE
    .\run.ps1 -Mode paper -Capital 10000
#>

param(
    [Parameter(Mandatory=$false)]
    [ValidateSet('paper', 'live')]
    [string]$Mode = 'paper',
    
    [Parameter(Mandatory=$false)]
    [ValidateRange(100, 1000000)]
    [int]$Capital = 10000
)

$ErrorActionPreference = "Stop"

Write-Host "=" * 60 -ForegroundColor Cyan
Write-Host "POLYMARKET TRADING BOT - PRODUCTION LAUNCHER" -ForegroundColor Cyan
Write-Host "=" * 60 -ForegroundColor Cyan
Write-Host ""

# Check if venv exists
if (-not (Test-Path "venv\Scripts\python.exe")) {
    Write-Host "[ERROR] Virtual environment not found!" -ForegroundColor Red
    Write-Host "Run: python -m venv venv" -ForegroundColor Yellow
    exit 1
}

Write-Host "[1/3] Activating virtual environment..." -ForegroundColor Green
& ".\venv\Scripts\Activate.ps1"

Write-Host "[2/3] Verifying dependencies..." -ForegroundColor Green
$required = @("structlog", "aiohttp", "aiosqlite", "websockets", "pydantic")
foreach ($pkg in $required) {
    $check = & ".\venv\Scripts\python.exe" -c "import $pkg" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [MISSING] $pkg - Installing..." -ForegroundColor Yellow
        & ".\venv\Scripts\pip.exe" install -q $pkg
    } else {
        Write-Host "  [OK] $pkg" -ForegroundColor Green
    }
}

Write-Host "[3/3] Launching bot..." -ForegroundColor Green
Write-Host ""

# Launch bot with full path to venv Python
& ".\venv\Scripts\python.exe" main_v2.py --mode $Mode --capital $Capital

$exitCode = $LASTEXITCODE
Write-Host ""
if ($exitCode -eq 0) {
    Write-Host "Bot exited successfully" -ForegroundColor Green
} else {
    Write-Host "Bot exited with code $exitCode" -ForegroundColor Red
}

exit $exitCode
