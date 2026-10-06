$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$projectDrive = (Split-Path -Qualifier $PSScriptRoot)
if ($projectDrive -ne 'D:') { throw "The upscaler must run from drive D (current: $projectDrive)." }

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $python)) {
    throw "The D-drive virtual environment is missing: $python. Run setup.ps1 first."
}

$port = 8000
if ($env:UPSCALER_PORT) {
    if (![int]::TryParse($env:UPSCALER_PORT, [ref]$port) -or $port -notin 1..65535) {
        throw "UPSCALER_PORT must be a valid TCP port."
    }
}
$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($listener) {
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)" -ErrorAction SilentlyContinue
    $command = if ($owner) { $owner.CommandLine } else { 'unknown process' }
    throw "Port $port is already in use by PID $($listener.OwningProcess): $command"
}

$env:TEMP = "$PSScriptRoot\.cache\tmp"
$env:TMP = $env:TEMP
$env:TORCH_HOME = "$PSScriptRoot\.cache\torch"
$env:HF_HOME = "$PSScriptRoot\.cache\huggingface"
$env:XDG_CACHE_HOME = "$PSScriptRoot\.cache"
New-Item -ItemType Directory -Force -Path $env:TEMP,$env:TORCH_HOME,$env:HF_HOME | Out-Null
$env:PYTHONUTF8 = '1'
$env:PYTHONNOUSERSITE = '1'
& $python -m backend.serve --host 127.0.0.1 --port $port
