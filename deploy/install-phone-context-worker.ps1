param(
    [string]$PayloadDir = $PSScriptRoot,
    [string]$InstallDir = 'C:\PlaudWhisperWorker'
)

$ErrorActionPreference = 'Stop'
$Python = Join-Path $InstallDir '.venv\Scripts\python.exe'
$ApiSource = Join-Path $PayloadDir 'plaud_worker\api.py'
$ContextSource = Join-Path $PayloadDir 'plaud_worker\phone_context.py'
$TestSource = Join-Path $PayloadDir 'tests\test_phone_context.py'

foreach ($Required in @($Python, $ApiSource, $ContextSource, $TestSource)) {
    if (-not (Test-Path $Required)) {
        throw "Vereist bestand ontbreekt: $Required"
    }
}

$Health = Invoke-RestMethod -Uri 'http://192.168.1.28:8765/health' -TimeoutSec 10
if ($Health.inference_phase) {
    throw "Worker is nog bezig met $($Health.inference_phase). Probeer opnieuw nadat de huidige inference klaar is."
}

$Backup = Join-Path $InstallDir ('backups\before-phone-context-' + (Get-Date -Format yyyyMMdd-HHmmss))
New-Item -ItemType Directory -Path (Join-Path $Backup 'plaud_worker') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $Backup 'tests') -Force | Out-Null
Copy-Item (Join-Path $InstallDir 'plaud_worker\api.py') (Join-Path $Backup 'plaud_worker\api.py')
if (Test-Path (Join-Path $InstallDir 'plaud_worker\phone_context.py')) {
    Copy-Item (Join-Path $InstallDir 'plaud_worker\phone_context.py') (Join-Path $Backup 'plaud_worker\phone_context.py')
}
if (Test-Path (Join-Path $InstallDir 'tests\test_phone_context.py')) {
    Copy-Item (Join-Path $InstallDir 'tests\test_phone_context.py') (Join-Path $Backup 'tests\test_phone_context.py')
}

Copy-Item $ApiSource (Join-Path $InstallDir 'plaud_worker\api.py') -Force
Copy-Item $ContextSource (Join-Path $InstallDir 'plaud_worker\phone_context.py') -Force
Copy-Item $TestSource (Join-Path $InstallDir 'tests\test_phone_context.py') -Force
if (Test-Path (Join-Path $PayloadDir 'README.md')) {
    Copy-Item (Join-Path $PayloadDir 'README.md') (Join-Path $InstallDir 'README.md') -Force
}

$env:PYTHONPATH = $InstallDir
& $Python -m pytest (Join-Path $InstallDir 'tests') -q
if ($LASTEXITCODE -ne 0) {
    Copy-Item (Join-Path $Backup 'plaud_worker\api.py') (Join-Path $InstallDir 'plaud_worker\api.py') -Force
    if (Test-Path (Join-Path $Backup 'plaud_worker\phone_context.py')) {
        Copy-Item (Join-Path $Backup 'plaud_worker\phone_context.py') (Join-Path $InstallDir 'plaud_worker\phone_context.py') -Force
    } else {
        Remove-Item (Join-Path $InstallDir 'plaud_worker\phone_context.py') -ErrorAction SilentlyContinue
    }
    throw "Worker-tests zijn mislukt. De broncode is hersteld uit $Backup en de actieve worker is niet herstart."
}

& $Python -m pip install --no-deps --force-reinstall $InstallDir
if ($LASTEXITCODE -ne 0) {
    throw 'Het bijgewerkte worker-package kon niet worden geïnstalleerd.'
}

Stop-ScheduledTask -TaskName 'Plaud Whisper Worker'
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object CommandLine -Like '* -m plaud_worker*' |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1
Start-ScheduledTask -TaskName 'Plaud Whisper Worker'
Start-Sleep -Seconds 5

$NewHealth = Invoke-RestMethod -Uri 'http://192.168.1.28:8765/health' -TimeoutSec 15
$OpenApi = Invoke-RestMethod -Uri 'http://192.168.1.28:8765/openapi.json' -TimeoutSec 15
if (-not $OpenApi.paths.'/v1/phone-context') {
    throw 'Worker is herstart, maar /v1/phone-context ontbreekt.'
}

Write-Output "BACKUP=$Backup"
Write-Output 'PHONE_CONTEXT_ROUTE=ok'
$NewHealth | ConvertTo-Json -Compress
