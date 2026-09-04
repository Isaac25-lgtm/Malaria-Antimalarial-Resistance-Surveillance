# Metadata-only login probe against the local live API.
# Username and password are collected with hidden prompts. They are never
# written to a file, a command argument, or this script's output.

$ErrorActionPreference = "Stop"

$Api = if ($env:MARS_LIVE_API) { $env:MARS_LIVE_API } else { "http://127.0.0.1:8000" }
$Origin = if ($env:MARS_LIVE_ORIGIN) { $env:MARS_LIVE_ORIGIN } else { "http://127.0.0.1:5173" }

$Username = Read-Host "eRegisters username"
$Secure = Read-Host -AsSecureString "eRegisters password"
$BSTR = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
try {
    $Password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($BSTR)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($BSTR)
}

$BodyObj = @{ username = $Username; password = $Password }
$Json = $BodyObj | ConvertTo-Json -Compress
$Password = $null
$BodyObj = $null

try {
    $Response = Invoke-WebRequest -Uri "$Api/api/v1/auth/login" -Method POST -ContentType "application/json" -Headers @{ Origin = $Origin } -Body $Json -UseBasicParsing
    $Json = $null
    Write-Host "HTTP $($Response.StatusCode)"
    $Parsed = $Response.Content | ConvertFrom-Json
    if ($Parsed.authenticated) {
        Write-Host "authenticated=true"
        Write-Host ("scope_type=" + $Parsed.scope.scope_type)
        Write-Host ("mapping=" + $Parsed.source_status.mapping)
        Write-Host ("landing=" + $Parsed.profile.landing_path)
    } else {
        Write-Host "authenticated=false"
    }
} catch {
    $Json = $null
    $Status = $_.Exception.Response.StatusCode.value__
    if ($Status) {
        Write-Host "HTTP $Status"
    } else {
        Write-Host "Request failed: $($_.Exception.Message)"
    }
}
