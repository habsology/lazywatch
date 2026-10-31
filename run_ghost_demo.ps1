# Downtime Ghost demo for Windows PowerShell: one durable haunting pass, with the worker killed on purpose.
#   powershell -ExecutionPolicy Bypass -File run_ghost_demo.ps1
# Needs Python, Ollama (gemma3:4b) and an internet connection. Posts nothing: BLUESKY_APP_PASSWORD is cleared
# for this window, so every page is a dry run that prints the poem it would post. Safe to run again.
# The Temporal server and the workers run hidden; their logs and the workflow history land in ghost-demo-output\,
# zipped at the end as ghost-demo-output.zip.

$ErrorActionPreference = "Continue"
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"
Remove-Item env:BLUESKY_APP_PASSWORD -ErrorAction SilentlyContinue   # the hourly workflow does the posting, never this demo
$env:GHOST_PAUSE = "5"                                                # each page lingers 5 s, so the kill lands mid-haunting
Set-Location $PSScriptRoot
$out = Join-Path $PSScriptRoot "ghost-demo-output"
New-Item -ItemType Directory -Force -Path $out | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
Start-Transcript -Path (Join-Path $out "transcript-$stamp.txt") -Append | Out-Null

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "WARNING: $msg" -ForegroundColor Yellow }

function Stop-GhostWorkers {
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -like "*lazywatch_ghost_temporal.py*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Start-Worker($tag) {
    $p = Start-Process -FilePath "python" -ArgumentList "-u", "lazywatch_ghost_temporal.py", "worker" `
        -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $out "worker-$tag.log") -RedirectStandardError (Join-Path $out "worker-$tag.err")
    Start-Sleep -Seconds 6
    if ($p.HasExited) {
        Warn "worker $tag stopped right away; its error output:"
        Get-Content (Join-Path $out "worker-$tag.err") -Tail 20 | ForEach-Object { Write-Host $_ }
    }
    return $p
}

Step "Installing Python packages and the card browser (a minute or two the first time)"
python -m pip install --quiet temporalio playwright "psycopg[binary]" pgvector
python -m playwright install chromium

Step "Making sure Gemma is here"
ollama pull gemma3:4b

Step "Getting the Temporal CLI (one file, open source)"
if (-not (Test-Path .\temporal.exe)) {
    try {
        Invoke-WebRequest "https://temporal.download/cli/archive/latest?platform=windows&arch=amd64&format=zip" -OutFile temporal.zip
        Expand-Archive temporal.zip -DestinationPath temporal-cli -Force
        Copy-Item (Get-ChildItem temporal-cli -Recurse -Filter temporal.exe | Select-Object -First 1).FullName .\temporal.exe
    } catch {
        Warn "download failed: $_"
        Write-Host "Get temporal_cli_<version>_windows_amd64.zip from https://github.com/temporalio/cli/releases, put temporal.exe in this folder, run this script again."
        Stop-Transcript | Out-Null
        exit 1
    }
}

Step "Starting the Temporal server (hidden)"
Get-Process -Name temporal -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Start-Process -FilePath ".\temporal.exe" -ArgumentList "server", "start-dev", "--db-filename", "ghost.db" `
    -WorkingDirectory $PSScriptRoot -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $out "server.log") -RedirectStandardError (Join-Path $out "server.err") | Out-Null
Start-Sleep -Seconds 8

Step "Worker 1 starts; the haunting pass begins (37 status pages, about 5 s each)"
Stop-GhostWorkers
$worker = Start-Worker "1"
Start-Process "http://localhost:8233"
$job = Start-Job -ScriptBlock { param($d) Set-Location $d; $env:PYTHONIOENCODING = "utf-8"; python -u lazywatch_ghost_temporal.py once } -ArgumentList $PSScriptRoot

Start-Sleep -Seconds 25
Step "Killing worker 1 mid-haunting (on purpose)"
Stop-Process -Id $worker.Id -Force -ErrorAction SilentlyContinue
$killedAt = Get-Date
Write-Host "worker 1 killed at $($killedAt.ToString('HH:mm:ss')); the browser tab shows the run waiting. Starting worker 2 in 20 s."
Start-Sleep -Seconds 20
$worker = Start-Worker "2"
Write-Host "worker 2 started at $((Get-Date).ToString('HH:mm:ss')); the run picks up where it stopped."

if (Wait-Job $job -Timeout 900) { Receive-Job $job | Tee-Object -FilePath (Join-Path $out "haunting-result.txt") }
else { Warn "the pass did not finish in 15 minutes; moving on"; Stop-Job $job }
Remove-Job $job -Force

Step "Saving the workflow history"
try {
    foreach ($w in (.\temporal.exe workflow list --output json | ConvertFrom-Json)) {
        $id = $w.execution.workflowId
        .\temporal.exe workflow show --workflow-id $id --output json | Out-File (Join-Path $out "$id.json") -Encoding utf8
    }
} catch { Warn "could not save the history: $_" }

Step "Asking the Tiger Data graveyard (skipped if TIGER_DATABASE_URL is not set)"
if ($env:TIGER_DATABASE_URL) {
    foreach ($q in "which service keeps going down", "Durable Objects", "voice calls") {
        Write-Host "`n> search `"$q`""
        python -u lazywatch_store.py search $q 2>&1 | Tee-Object -FilePath (Join-Path $out ("search-" + ($q -replace '\W', '-') + ".txt"))
    }
} else { Warn "TIGER_DATABASE_URL is not set in this window" }

Step "Stopping the worker and the server"
Stop-GhostWorkers
Get-Process -Name temporal -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3

Step "Zipping ghost-demo-output"
Stop-Transcript | Out-Null
$zip = Join-Path $PSScriptRoot "ghost-demo-output.zip"
Remove-Item $zip -Force -ErrorAction SilentlyContinue
$stage = Join-Path $env:TEMP "ghost-zip-$stamp"
New-Item -ItemType Directory -Force -Path $stage | Out-Null
foreach ($f in @(Get-ChildItem $out -File) + @(Get-ChildItem "out" -File -ErrorAction SilentlyContinue)) {
    try { Copy-Item $f.FullName $stage -ErrorAction Stop } catch { Write-Host "skipped (still in use): $($f.Name)" }
}
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip -Force
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
if (Test-Path $zip) {
    Write-Host "`nDONE. Send this file: $zip" -ForegroundColor Green
    explorer.exe /select,"$zip"
} else {
    Warn "the zip was not created; send a screenshot of this window instead"
}
