$ErrorActionPreference = 'Stop'
$root = 'C:\PlaudWhisperWorker'
Set-Location $root
$env:PYTHONPATH = $root
& "$root\.venv\Scripts\python.exe" -m pytest tests -q
if ($LASTEXITCODE -ne 0) { throw 'Worker tests failed; running worker was not restarted.' }
& "$root\.venv\Scripts\python.exe" -m pip install --no-deps --force-reinstall $root
if ($LASTEXITCODE -ne 0) { throw 'Installation failed.' }
Stop-ScheduledTask -TaskName 'Plaud Whisper Worker'
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object CommandLine -Like '* -m plaud_worker*' | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1
Start-ScheduledTask -TaskName 'Plaud Whisper Worker'
Start-Sleep -Seconds 4
Invoke-RestMethod -Uri 'http://192.168.1.28:8765/health' -TimeoutSec 15 | ConvertTo-Json -Compress
$OpenApi = Invoke-RestMethod -Uri 'http://192.168.1.28:8765/openapi.json' -TimeoutSec 15
if (-not $OpenApi.paths.'/v1/phone-context') {
    throw 'De nieuwe route /v1/phone-context ontbreekt na de worker-herstart.'
}
Write-Output 'PHONE_CONTEXT_ROUTE=ok'
