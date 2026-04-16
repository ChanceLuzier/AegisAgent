# Aegis AI — Native Desktop Launcher (Tauri)
# Starts the FastAPI backend, waits for it, then opens the Tauri window.
# Run from the project root: .\launch.ps1

$root   = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = "$root\.venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "ERROR: No venv found. Run .\setup.ps1 first." -ForegroundColor Red
    exit 1
}

if (-not (Get-Command "npm" -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: npm not found. Install Node.js first." -ForegroundColor Red
    exit 1
}

# Install npm deps if needed
if (-not (Test-Path "$root\node_modules")) {
    Write-Host "Installing npm dependencies..." -ForegroundColor Cyan
    Push-Location $root
    npm install
    Pop-Location
}

# Start FastAPI backend in background
Write-Host "Starting Aegis AI backend..." -ForegroundColor Cyan
$backendJob = Start-Process -FilePath $python `
    -ArgumentList "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8000" `
    -WorkingDirectory $root `
    -PassThru `
    -WindowStyle Hidden

# Wait for port 8000 to be ready (up to 60s)
Write-Host "Waiting for backend..." -ForegroundColor Cyan
$ready = $false
for ($i = 0; $i -lt 120; $i++) {
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $tcp.Connect("127.0.0.1", 8000)
        $tcp.Close()
        $ready = $true
        break
    } catch {}
    Start-Sleep -Milliseconds 500
}

if (-not $ready) {
    Write-Host "ERROR: Backend did not start within 60s." -ForegroundColor Red
    $backendJob | Stop-Process -Force -ErrorAction SilentlyContinue
    exit 1
}

Write-Host "Backend ready. Launching native window..." -ForegroundColor Green

# Run Tauri (blocks until window is closed)
Push-Location $root
try {
    npm run tauri dev
} finally {
    # Kill backend when window closes
    $backendJob | Stop-Process -Force -ErrorAction SilentlyContinue
    Write-Host "Aegis AI stopped." -ForegroundColor Cyan
}
Pop-Location
