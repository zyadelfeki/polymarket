# scripts/run_paper.ps1
# ---------------------------------------------------------------------------
# Reliable paper-trading launcher.
# Sources all variables from .env (including CHARLIE_PATH) so the bot can be
# started from any terminal without manually exporting env vars.
#
# Usage:
#   .\scripts\run_paper.ps1
# ---------------------------------------------------------------------------

Set-Location "$PSScriptRoot\.."

# Load .env into the current process environment.
if (Test-Path .env) {
    Get-Content .env | ForEach-Object {
        # Skip blank lines and comments (#...).
        if ($_ -match '^([^#=][^=]*)=(.+)$') {
            $key   = $matches[1].Trim()
            $value = $matches[2].Trim()
            [Environment]::SetEnvironmentVariable($key, $value, 'Process')
        }
    }
    Write-Host "Loaded .env"
} else {
    Write-Warning ".env not found - proceeding with existing environment variables"
}

$env:LOG_FORMAT = "json"
$env:LOG_LEVEL  = "INFO"

Write-Host "CHARLIE_PATH = $env:CHARLIE_PATH"
Write-Host "Starting paper bot..."

.\.venv\Scripts\python.exe main.py --mode paper
