[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot "backend\.venv\Scripts\python.exe"
$backend = Join-Path $repoRoot "backend"
$outputDir = Join-Path $repoRoot "data\discovery"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Backend virtual-environment Python was not found at $python"
}

$environmentNames = @(
    "MARS_DHIS2_DISCOVERY_BASE_URL",
    "MARS_DHIS2_DISCOVERY_USERNAME",
    "MARS_DHIS2_DISCOVERY_PASSWORD",
    "MARS_DHIS2_DISCOVERY_TOKEN",
    "MARS_DHIS2_DISCOVERY_OUTPUT_DIR",
    "MARS_DHIS2_DISCOVERY_VERIFY_TLS"
)
$previous = @{}
foreach ($name in $environmentNames) {
    $previous[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}

$bstr = [IntPtr]::Zero
$plainPassword = $null
try {
    Write-Host "MARS eRegisters metadata-only discovery" -ForegroundColor Cyan
    Write-Host "No tracked entities, enrollments, events, relationships, patient analytics, or data values will be requested."

    $username = Read-Host "eRegisters username"
    if ([string]::IsNullOrWhiteSpace($username)) {
        throw "A username is required."
    }
    $securePassword = Read-Host "eRegisters password" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
    $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    if ([string]::IsNullOrEmpty($plainPassword)) {
        throw "A password is required."
    }

    $env:MARS_DHIS2_DISCOVERY_BASE_URL = "https://eregisters.health.go.ug"
    $env:MARS_DHIS2_DISCOVERY_USERNAME = $username
    $env:MARS_DHIS2_DISCOVERY_PASSWORD = $plainPassword
    [Environment]::SetEnvironmentVariable("MARS_DHIS2_DISCOVERY_TOKEN", $null, "Process")
    $env:MARS_DHIS2_DISCOVERY_OUTPUT_DIR = $outputDir
    $env:MARS_DHIS2_DISCOVERY_VERIFY_TLS = "true"

    Push-Location $backend
    try {
        & $python -m mars.integrations.dhis2.discovery --dry-run-config
        if ($LASTEXITCODE -ne 0) {
            throw "The safe discovery configuration check failed with exit code $LASTEXITCODE."
        }
        & $python -m mars.integrations.dhis2.discovery --output-dir $outputDir
        if ($LASTEXITCODE -ne 0) {
            throw "Metadata discovery failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }

    Write-Host "Discovery complete. Upload the newest JSON and Markdown files from:" -ForegroundColor Green
    Write-Host $outputDir
}
finally {
    $plainPassword = $null
    if ($bstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    foreach ($name in $environmentNames) {
        [Environment]::SetEnvironmentVariable($name, $previous[$name], "Process")
    }
}
