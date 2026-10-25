param(
    [string]$Executable = "$PSScriptRoot\..\backend\dist\dobot-backend.exe",
    [int]$Port = 18756,
    [int]$TimeoutSeconds = 45
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $Executable)) {
    throw "Backend executable not found: $Executable. Run 'npm run sidecar' first."
}

# Redirect the backend's own output. Without this a startup crash on a bare CI runner reports only
# an exit code, and the log never says why — which is exactly the failure this file should explain.
$outLog = Join-Path ([System.IO.Path]::GetTempPath()) "dobot-smoke-out.log"
$errLog = Join-Path ([System.IO.Path]::GetTempPath()) "dobot-smoke-err.log"
Remove-Item -LiteralPath $outLog, $errLog -Force -ErrorAction SilentlyContinue

function Show-BackendOutput {
    foreach ($log in @($outLog, $errLog)) {
        if ((Test-Path -LiteralPath $log) -and (Get-Item -LiteralPath $log).Length -gt 0) {
            Write-Host "--- backend output: $log ---"
            Get-Content -LiteralPath $log | ForEach-Object { Write-Host "  $_" }
        }
    }
}

$process = Start-Process -FilePath $Executable -ArgumentList "--host", "127.0.0.1", "--port", $Port `
    -PassThru -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog

try {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $health = $null
    while ((Get-Date) -lt $deadline) {
        if ($process.HasExited) {
            Show-BackendOutput
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
        Show-BackendOutput
        throw "Backend did not become healthy within $TimeoutSeconds seconds."
    }
    $runtime = Invoke-RestMethod "http://127.0.0.1:$Port/runtime"
    $capabilities = Invoke-RestMethod "http://127.0.0.1:$Port/capabilities"
    if ($runtime.state -ne "ready") {
        Show-BackendOutput
        throw "Backend reported runtime state '$($runtime.state)' instead of 'ready'."
    }
    if (-not $capabilities.contract_version) {
        Show-BackendOutput
        throw "Capabilities contract version is missing."
    }
    Write-Host "Backend smoke test passed: PID=$($runtime.pid), contract=$($capabilities.contract_version)"
} finally {
    # PyInstaller onefile runs the real server in a child process: killing the bootloader PID alone
    # orphans that child, which then keeps the port bound for the next run. Kill the whole tree.
    if ($process -and -not $process.HasExited) {
        & taskkill /PID $process.Id /T /F 2>$null | Out-Null
    }
}
