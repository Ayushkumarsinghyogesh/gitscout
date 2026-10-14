<#
.SYNOPSIS
    Register (or re-register) the gitscout scheduled sync as a Windows task.

.DESCRIPTION
    Idempotent: running it again replaces the task with the current settings.

        powershell -ExecutionPolicy Bypass -File scripts\install-schedule.ps1
        powershell -ExecutionPolicy Bypass -File scripts\install-schedule.ps1 -EveryHours 24
        powershell -ExecutionPolicy Bypass -File scripts\install-schedule.ps1 -Remove

    Task settings, and why:
      * StartWhenAvailable  -- a run missed while the PC was off starts at the next
                               opportunity. Runs are incremental, so nothing is lost.
      * MultipleInstances IgnoreNew -- never two runs at once (the run lock is the
                               second guard, for manual runs).
      * 3 h time limit      -- a normal run takes minutes; a hung one is stopped.
      * 2 retries, 15 min apart -- rides out a network blip without hammering.
      * runs as you, only while you are logged on -- no stored password, no elevation.

.PARAMETER EveryHours
    12 (default) or 24.

.PARAMETER FirstRun
    Local time of the first run each day. Default 09:07: off the hour, when every
    other scheduler fires, and inside a working day so the PC is likely on.
#>
param(
    [ValidateSet(12, 24)][int]$EveryHours = 12,
    [string]$FirstRun = "09:07",
    [string]$TargetProfile = "cloud-security",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$name = "gitscout-scheduled-sync"
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo "scripts\scheduled-sync.ps1"

if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Write-Host "removed existing task $name"
}
if ($Remove) { return }

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$script`" -TargetProfile $TargetProfile" `
    -WorkingDirectory $repo

$start = [datetime]::ParseExact($FirstRun, "HH:mm", $null)
$triggers = @(New-ScheduledTaskTrigger -Daily -At $start)
if ($EveryHours -eq 12) {
    $triggers += New-ScheduledTaskTrigger -Daily -At $start.AddHours(12)
}

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 15) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $name -Action $action -Trigger $triggers `
    -Settings $settings -Principal $principal `
    -Description "gitscout: incremental crawl of the $TargetProfile profile, push to Supabase, refresh the outreach dataset. Logs in $repo\logs." | Out-Null

$task = Get-ScheduledTask -TaskName $name
$info = Get-ScheduledTaskInfo -TaskName $name
Write-Host ("registered {0}: every {1} h from {2}, next run {3}" -f $name, $EveryHours, $FirstRun, $info.NextRunTime)
