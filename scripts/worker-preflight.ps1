$ErrorActionPreference = 'Stop'
$root = 'C:\PlaudWhisperWorker'
$backup = Join-Path $root ('backups\before-audioscraper-' + (Get-Date -Format yyyyMMdd-HHmmss))
New-Item -ItemType Directory -Path $backup -Force | Out-Null
Copy-Item "$root\plaud_worker\api.py" $backup
if (Test-Path "$root\plaud_worker\phone_context.py") {
    Copy-Item "$root\plaud_worker\phone_context.py" $backup
}
Copy-Item "$root\README.md" $backup
Write-Output "BACKUP=$backup"
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | ForEach-Object { Write-Output $_.CommandLine }
& "$root\.venv\Scripts\python.exe" -c "import os,sqlite3; p=os.path.join(os.environ['LOCALAPPDATA'],'PlaudWhisperWorker','jobs.sqlite3'); c=sqlite3.connect(p); print('QUEUE',c.execute('select status,count(*) from jobs group by status').fetchall())"
