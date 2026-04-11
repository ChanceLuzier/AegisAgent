# Aegis AI — start the server
# Run from the project root: .\run.ps1

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = "$root\.venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "ERROR: No venv found. Run .\setup.ps1 first." -ForegroundColor Red
    exit 1
}

Write-Host "Starting Aegis AI..." -ForegroundColor Cyan
Write-Host "Open: http://127.0.0.1:8000/ui" -ForegroundColor Green
& $python -m uvicorn app:app --host 127.0.0.1 --port 8000
