# Start the isolated DEMO MARS stack.
# Frontend 5174, API 8001, database mars_local.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$VenvPython = Join-Path $Backend ".venv\Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Error "backend\.venv is missing. Create it before starting demo MARS."
}

if (-not $env:MARS_DATABASE_URL) {
    $env:MARS_DATABASE_URL = "postgresql+psycopg://mars@127.0.0.1:5460/mars_local"
}

$env:MARS_ENVIRONMENT = "local"
$env:MARS_AUTH_MODE = "demo"
$env:MARS_DEV_AUTH_ENABLED = "true"
$env:MARS_DEMO_MODE_ENABLED = "true"
$env:MARS_HOST = "127.0.0.1"
$env:MARS_PORT = "8001"
$env:MARS_API_PROXY_TARGET = "http://127.0.0.1:8001"
$env:MARS_CORS_ALLOW_ORIGINS = '["http://127.0.0.1:5174","http://localhost:5174"]'

Write-Host "Starting demo API on 127.0.0.1:8001 (mars_local)..."
$Api = Start-Process -FilePath $VenvPython -ArgumentList @(
    "-m", "uvicorn", "mars.main:app", "--app-dir", "src",
    "--host", "127.0.0.1", "--port", "8001"
) -WorkingDirectory $Backend -PassThru

Write-Host "Starting demo UI on 127.0.0.1:5174..."
$Ui = Start-Process -FilePath "npm" -ArgumentList @("run", "dev", "--", "--host", "127.0.0.1", "--port", "5174", "--strictPort") -WorkingDirectory $Frontend -PassThru

Write-Host "Demo UI: http://127.0.0.1:5174"
Write-Host "Demo API: http://127.0.0.1:8001"
Write-Host "Press Ctrl+C in this window to stop."
try {
    Wait-Process -Id $Api.Id
} finally {
    if (-not $Api.HasExited) { Stop-Process -Id $Api.Id -Force -ErrorAction SilentlyContinue }
    if (-not $Ui.HasExited) { Stop-Process -Id $Ui.Id -Force -ErrorAction SilentlyContinue }
}
