# Start the isolated LIVE MARS stack.
# Frontend 5173, API 8000, database mars_live.
# Never prints credentials. Fails closed if mars_live is missing.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$VenvPython = Join-Path $Backend ".venv\Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Error "backend\.venv is missing. Create it before starting live MARS."
}

if (-not $env:MARS_DATABASE_URL) {
    $DbPassword = Read-Host -AsSecureString "Password for the mars_live database role (blank if trust auth)"
    $BSTR = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($DbPassword)
    try {
        $Plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($BSTR)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($BSTR)
    }
    if ([string]::IsNullOrWhiteSpace($Plain)) {
        $env:MARS_DATABASE_URL = "postgresql+psycopg://mars@127.0.0.1:5460/mars_live"
    } else {
        $env:MARS_DATABASE_URL = "postgresql+psycopg://mars:${Plain}@127.0.0.1:5460/mars_live"
    }
    $Plain = $null
}

if ($env:MARS_DATABASE_URL -notmatch "/mars_live(\?|$)") {
    Write-Error "Live mode requires database mars_live. Refusing to start."
}
if ($env:MARS_DATABASE_URL -match "/mars_local(\?|$)") {
    Write-Error "Live mode refuses mars_local."
}

$env:MARS_ENVIRONMENT = "local"
$env:MARS_AUTH_MODE = "live"
$env:MARS_DEV_AUTH_ENABLED = "false"
$env:MARS_DEMO_MODE_ENABLED = "false"
$env:MARS_DHIS2_LOGIN_BASE_URL = "https://eregisters.health.go.ug"
$env:MARS_DHIS2_LOGIN_VERIFY_TLS = "true"
$env:MARS_CORS_ALLOW_ORIGINS = '["http://127.0.0.1:5173","http://localhost:5173"]'
$env:MARS_HOST = "127.0.0.1"
$env:MARS_PORT = "8000"
$env:MARS_API_PROXY_TARGET = "http://127.0.0.1:8000"

Write-Host "Starting live API on 127.0.0.1:8000 (mars_live)..."
$Api = Start-Process -FilePath $VenvPython -ArgumentList @(
    "-m", "uvicorn", "mars.main:app", "--app-dir", "src",
    "--host", "127.0.0.1", "--port", "8000"
) -WorkingDirectory $Backend -PassThru

Write-Host "Starting live UI on 127.0.0.1:5173..."
$Ui = Start-Process -FilePath "npm" -ArgumentList @("run", "dev", "--", "--host", "127.0.0.1", "--port", "5173", "--strictPort") -WorkingDirectory $Frontend -PassThru

Write-Host "Live UI: http://127.0.0.1:5173"
Write-Host "Live API: http://127.0.0.1:8000"
Write-Host "Press Ctrl+C in this window to stop."
try {
    Wait-Process -Id $Api.Id
} finally {
    if (-not $Api.HasExited) { Stop-Process -Id $Api.Id -Force -ErrorAction SilentlyContinue }
    if (-not $Ui.HasExited) { Stop-Process -Id $Ui.Id -Force -ErrorAction SilentlyContinue }
}
