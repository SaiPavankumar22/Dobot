param(
    [string]$Executable = "$PSScriptRoot\..\backend\dist\dobot-backend.exe",
    [int]$Port = 18756,
    [int]$TimeoutSeconds = 45
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $Executable)) {
    throw "Backend executable not found: $Executable. Run `npm run sidecar` first."
}

$process = Start-Process -FilePath $Executable -ArgumentList "--host", "127.0.0.1", "--port", $Port `
    -PassThru -WindowStyle Hidden

try {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $health = $null
    while ((Get-Date) -lt $deadline) {
        if ($process.HasExited) {
            throw "Backend exited during startup with code $($process.ExitCode)."
        }
        try {
            $health = Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 2
            break
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $health) {
        throw "Backend did not become healthy within $TimeoutSeconds seconds."
    }
    $runtime = Invoke-RestMethod "http://127.0.0.1:$Port/runtime"
    $capabilities = Invoke-RestMethod "http://127.0.0.1:$Port/capabilities"
    if ($runtime.state -ne "ready") {
        throw "Backend reported runtime state '$($runtime.state)' instead of 'ready'."
    }
    if (-not $capabilities.contract_version) {
        throw "Capabilities contract version is missing."
    }
    Write-Host "Backend smoke test passed: PID=$($runtime.pid), contract=$($capabilities.contract_version)"
} finally {
    if ($process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force
    }
}
