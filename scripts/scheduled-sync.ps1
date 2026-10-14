<#
.SYNOPSIS
    One scheduled gitscout run: crawl the profile's repositories incrementally,
    enrich, push to Supabase and refresh the outreach dataset.

.DESCRIPTION
    This is what the Windows scheduled task runs. Everything it does is the single
    command below; the script only adds logging and log rotation around it, so the
    same run can be reproduced by hand at any time.

        gitscout scout -t cloud-security --incremental --with-threads --with-social --push

    Safe to run at any moment:
      * incremental -- it reads only what changed since the last run;
      * idempotent  -- re-running changes nothing that already landed;
      * locked      -- a second concurrent run exits with code 3 instead of racing;
      * atomic      -- data lands in one Supabase transaction or not at all, and a
                       failed refresh keeps the previous outreach dataset serving.

    Credentials come from .env (GITHUB_TOKENS, SUPABASE_DB_URL). Nothing secret is
    written to the log.

.PARAMETER TargetProfile
    Target profile name or path. Default: cloud-security.

.PARAMETER KeepLogs
    How many run logs to keep. Default: 60 (a month at two runs a day).
#>
param(
    # Not "$Profile": that is PowerShell's own automatic variable.
    [string]$TargetProfile = "cloud-security",
    [int]$KeepLogs = 60
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "virtualenv not found at $python" }

$logDir = Join-Path $repo "logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir ("sync-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))

# Without these, a name with a non-Latin character in a profile crashes the run on
# the Windows console's legacy code page.
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$utf8 = New-Object System.Text.UTF8Encoding($false)   # no BOM mid-file
[IO.File]::AppendAllText($log, "gitscout scheduled sync  profile=$TargetProfile  started=$((Get-Date).ToString('o'))`r`n", $utf8)

# The redirection is done by cmd.exe, not PowerShell. Python logs to stderr, and
# Windows PowerShell 5.1 turns every redirected native stderr line into an
# ErrorRecord -- which, under ErrorActionPreference=Stop, would end the run at the
# very first log line.
$run = "`"$python`" -m gitscout.cli scout -t `"$TargetProfile`" --incremental --with-threads --with-social --push >> `"$log`" 2>&1"
cmd.exe /d /c $run
$code = $LASTEXITCODE

[IO.File]::AppendAllText($log, "finished=$((Get-Date).ToString('o'))  exit=$code`r`n", $utf8)

# Rotation: newest $KeepLogs kept.
Get-ChildItem $logDir -Filter "sync-*.log" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip $KeepLogs |
    Remove-Item -Force -ErrorAction SilentlyContinue

# Exit 3 means another run held the lock: not a failure, and not worth a retry.
if ($code -eq 3) { exit 0 }
exit $code
