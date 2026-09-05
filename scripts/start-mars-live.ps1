# Start the isolated LIVE MARS stack.
# Frontend 5173, API 8000, database mars_live.
# Never prints credentials. Fails closed if mars_live is missing.

param(
    [switch]$Restart
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$VenvPython = Join-Path $Backend ".venv\Scripts\python.exe"
$LocalSecretDirectory = Join-Path $Root ".local-secrets"
$DbHost = if ($env:MARS_DB_HOST) { $env:MARS_DB_HOST } else { "127.0.0.1" }
$DbPort = if ($env:MARS_DB_PORT) { $env:MARS_DB_PORT } else { "5460" }

if (-not (Test-Path $VenvPython)) {
    Write-Error "backend\.venv is missing. Create it before starting live MARS."
}

function Get-ListenerProcessId {
    param([Parameter(Mandatory = $true)][int]$Port)
    foreach ($Line in (& netstat.exe -ano -p tcp)) {
        $Match = [regex]::Match(
            $Line,
            "^\s*TCP\s+\S+:${Port}\s+\S+\s+LISTENING\s+(\d+)\s*$"
        )
        if ($Match.Success) {
            return [int]$Match.Groups[1].Value
        }
    }
    return $null
}

foreach ($Listener in @(
    @{ Port = 8000; ExpectedProcess = "python" },
    @{ Port = 5173; ExpectedProcess = "node" }
)) {
    $ListenerProcessId = Get-ListenerProcessId -Port $Listener.Port
    if ($null -eq $ListenerProcessId) { continue }
    if (-not $Restart) {
        Write-Error (
            "Port $($Listener.Port) is already in use. Run this launcher with -Restart " +
            "to replace the existing MARS process."
        )
    }
    $ExistingProcess = Get-Process -Id $ListenerProcessId -ErrorAction Stop
    if ($ExistingProcess.ProcessName -ne $Listener.ExpectedProcess) {
        Write-Error (
            "Port $($Listener.Port) belongs to $($ExistingProcess.ProcessName), not the " +
            "expected MARS $($Listener.ExpectedProcess) process. Refusing to stop it."
        )
    }
    Write-Host "Stopping existing MARS listener on port $($Listener.Port)..."
    Stop-Process -Id $ListenerProcessId -Force -ErrorAction Stop
}

function Get-LocalProtectedSecret {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [int]$ByteLength = 32
    )
    New-Item -ItemType Directory -Force -Path $LocalSecretDirectory | Out-Null
    $Path = Join-Path $LocalSecretDirectory "$Name.dpapi"
    if (Test-Path -LiteralPath $Path) {
        try {
            $Secure = Get-Content -LiteralPath $Path -Raw | ConvertTo-SecureString -ErrorAction Stop
        } catch {
            # A launcher running under a different Windows logon identity cannot
            # decrypt another identity's DPAPI blob. Do not overwrite the stable
            # key. Use a process-only key so the local pilot can start, and make
            # the loss of cross-restart pseudonym stability explicit.
            Write-Warning "Could not decrypt $Name.dpapi; using a process-only local key."
            $Bytes = New-Object byte[] $ByteLength
            $Generator = [Security.Cryptography.RandomNumberGenerator]::Create()
            try {
                $Generator.GetBytes($Bytes)
                return [Convert]::ToBase64String($Bytes)
            } finally {
                [Array]::Clear($Bytes, 0, $Bytes.Length)
                $Generator.Dispose()
            }
        }
    } else {
        $Bytes = New-Object byte[] $ByteLength
        $Generator = [Security.Cryptography.RandomNumberGenerator]::Create()
        try {
            $Generator.GetBytes($Bytes)
        } finally {
            $Generator.Dispose()
        }
        $Generated = [Convert]::ToBase64String($Bytes)
        [Array]::Clear($Bytes, 0, $Bytes.Length)
        $Secure = ConvertTo-SecureString -String $Generated -AsPlainText -Force
        $Secure | ConvertFrom-SecureString | Set-Content -LiteralPath $Path -Encoding ascii
        $Generated = $null
    }
    $Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
    }
}

$PlainDatabasePassword = $null
if (
    -not $env:MARS_MIGRATION_DATABASE_URL -or
    -not $env:MARS_DATABASE_URL -or
    -not $env:MARS_IDENTITY_DATABASE_URL
) {
    $DbPassword = Read-Host -AsSecureString "Password for the local mars_live roles (blank if trust auth)"
    $BSTR = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($DbPassword)
    try {
        $PlainDatabasePassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($BSTR)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($BSTR)
    }
}

$Credential = if ([string]::IsNullOrWhiteSpace($PlainDatabasePassword)) {
    ""
} else {
    ":$([Uri]::EscapeDataString($PlainDatabasePassword))"
}
if (-not $env:MARS_MIGRATION_DATABASE_URL) {
    $env:MARS_MIGRATION_DATABASE_URL = "postgresql+psycopg://mars${Credential}@${DbHost}:${DbPort}/mars_live"
}
if (-not $env:MARS_DATABASE_URL) {
    $env:MARS_DATABASE_URL = "postgresql+psycopg://mars_app_login${Credential}@${DbHost}:${DbPort}/mars_live"
}
if (-not $env:MARS_IDENTITY_DATABASE_URL) {
    $env:MARS_IDENTITY_DATABASE_URL = "postgresql+psycopg://mars_identity_login${Credential}@${DbHost}:${DbPort}/mars_live"
}

foreach ($Url in @(
    $env:MARS_MIGRATION_DATABASE_URL,
    $env:MARS_DATABASE_URL,
    $env:MARS_IDENTITY_DATABASE_URL
)) {
    if ($Url -notmatch "/mars_live(\?|$)" -or $Url -match "/mars_local(\?|$)") {
        Write-Error "Live mode requires mars_live for every database role. Refusing to start."
    }
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

# Local-pilot secrets are stable across restarts, encrypted by Windows DPAPI
# for this OS user, and excluded from git. They are never printed. Production
# must supply independently managed secrets and a dedicated identity DB role.
if (-not $env:MARS_PATIENT_DISPLAY_KEY) {
    $env:MARS_PATIENT_DISPLAY_KEY = Get-LocalProtectedSecret -Name "patient-display-key"
}
if (-not $env:MARS_IDENTITY_LINKAGE_KEY) {
    $env:MARS_IDENTITY_LINKAGE_KEY = Get-LocalProtectedSecret -Name "identity-linkage-key"
}
if (-not $env:MARS_IDENTITY_ENCRYPTION_KEY) {
    $env:MARS_IDENTITY_ENCRYPTION_KEY = Get-LocalProtectedSecret -Name "identity-encryption-key"
}
$PlainDatabasePassword = $null
$Credential = $null

Write-Host "Applying database migrations to mars_live on ${DbHost}:${DbPort}..."
$RuntimeDatabaseUrl = $env:MARS_DATABASE_URL
$env:MARS_DATABASE_URL = $env:MARS_MIGRATION_DATABASE_URL
Push-Location $Backend
try {
    & $VenvPython -m alembic -c "alembic.ini" upgrade head
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Database migration failed. Live MARS was not started."
    }
} finally {
    Pop-Location
    $env:MARS_DATABASE_URL = $RuntimeDatabaseUrl
}

Write-Host "Starting live API on 127.0.0.1:8000 (mars_live)..."
$Api = Start-Process -FilePath $VenvPython -ArgumentList @(
    "-m", "uvicorn", "mars.main:app", "--app-dir", "src",
    "--host", "127.0.0.1", "--port", "8000"
) -WorkingDirectory $Backend -WindowStyle Hidden -PassThru

Write-Host "Starting live UI on 127.0.0.1:5173..."
$NpmCommand = (Get-Command "npm.cmd" -ErrorAction Stop).Source
$Ui = Start-Process -FilePath $NpmCommand -ArgumentList @("run", "dev", "--", "--host", "127.0.0.1", "--port", "5173", "--strictPort") -WorkingDirectory $Frontend -WindowStyle Hidden -PassThru

Write-Host "Live UI: http://127.0.0.1:5173"
Write-Host "Live API: http://127.0.0.1:8000"
Write-Host "Press Ctrl+C in this window to stop."
try {
    Wait-Process -Id $Api.Id
} finally {
    if (-not $Api.HasExited) { Stop-Process -Id $Api.Id -Force -ErrorAction SilentlyContinue }
    if (-not $Ui.HasExited) { Stop-Process -Id $Ui.Id -Force -ErrorAction SilentlyContinue }
}
