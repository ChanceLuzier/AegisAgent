# Aegis AI — one-command setup
# Run from the project root: .\setup.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "==> Creating virtual environment..." -ForegroundColor Cyan
python -m venv "$root\.venv"

Write-Host "==> Installing dependencies (this may take a few minutes for PyTorch/Kokoro)..." -ForegroundColor Cyan
& "$root\.venv\Scripts\python.exe" -m pip install --upgrade pip
& "$root\.venv\Scripts\python.exe" -m pip install -r "$root\requirements.txt"

# Create required runtime directories
foreach ($dir in @("tmp", "sessions")) {
    $path = Join-Path $root $dir
    if (-not (Test-Path $path)) {
        New-Item -ItemType Directory -Path $path | Out-Null
        Write-Host "==> Created $dir\" -ForegroundColor Gray
    }
}

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "  1. Copy .env.example to .env and adjust any settings (optional)"
Write-Host "  2. Start Ollama:  ollama serve"
Write-Host "  3. Run Aegis:     .\run.ps1"
Write-Host "  4. Open browser:  http://127.0.0.1:8000/ui"
Write-Host ""
Write-Host "Voice model (Kokoro) downloads automatically on first use." -ForegroundColor Gray
