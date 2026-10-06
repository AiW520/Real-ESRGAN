$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if ((Split-Path -Qualifier $PSScriptRoot) -ne 'D:') { throw 'Install this project on drive D.' }
$env:TEMP = "$PSScriptRoot\.cache\tmp"
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = "$PSScriptRoot\.cache\pip"
$env:npm_config_cache = "$PSScriptRoot\.cache\npm"
$env:PLAYWRIGHT_BROWSERS_PATH = "$PSScriptRoot\.cache\playwright"
New-Item -ItemType Directory -Force -Path $env:TEMP,$env:PIP_CACHE_DIR,$env:npm_config_cache | Out-Null
if (!(Test-Path "$PSScriptRoot\.venv\Scripts\python.exe")) { py -3.11 -m venv "$PSScriptRoot\.venv" }
& "$PSScriptRoot\.venv\Scripts\python.exe" -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
Push-Location frontend
try {
    npm.cmd ci
    if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
    npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
} finally { Pop-Location }
