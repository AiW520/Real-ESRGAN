param(
    [string]$EnvFile = '',
    [string]$BindAddress = '127.0.0.1'
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if ((Split-Path -Qualifier $PSScriptRoot) -ne 'D:') { throw 'Run this deployment from drive D.' }
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $python)) { throw 'Create the D-drive Python environment first.' }
$env:TEMP = Join-Path $PSScriptRoot '.cache\tmp'
$env:TMP = $env:TEMP
$env:PYTHONUTF8 = '1'
$env:PYTHONNOUSERSITE = '1'
$env:UPSCALER_PRODUCTION = '1'
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
$configArgs = @()
if ($EnvFile) {
    $resolved = (Resolve-Path -LiteralPath $EnvFile).Path
    if ((Split-Path -Qualifier $resolved) -ne 'D:') { throw 'Keep the service configuration on drive D.' }
    $configArgs = @('--env-file', $resolved)
}
& $python scripts/preflight.py @configArgs --output "$PSScriptRoot\data\preflight.json"
if ($LASTEXITCODE -ne 0) { throw 'Preflight failed. Correct the reported checks before starting.' }
& $python -m backend.serve @configArgs --host $BindAddress
exit $LASTEXITCODE
