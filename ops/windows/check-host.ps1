<#
.SYNOPSIS
  Report whether this Windows host is fit to run Earn unattended.

.DESCRIPTION
  Read-only. Prints one line per check and, with -Json, a machine-readable object the
  console's Operations page (GET /api/ops/host) parses. Nothing here changes the machine;
  ops\windows\install-autostart.ps1 is what fixes things.

  Checks:
    wsl_running      the distro is up
    keepalive_task   the EarnWSLKeepAlive scheduled task exists and is Ready/Running
    sleep_ac         standby-timeout-ac is 0 (the live preflight blocks otherwise)
    hibernate_ac     hibernate-timeout-ac is 0
    wslconfig        %USERPROFILE%\.wslconfig exists with vmIdleTimeout=-1
    docker           a docker CLI is on PATH
    ollama           something answers on 127.0.0.1:11434
    disk             free space on the system drive

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File ops\windows\check-host.ps1 -Json
#>

[CmdletBinding()]
param(
    [string]$Distro = 'Ubuntu-24.04',
    [string]$TaskName = 'EarnWSLKeepAlive',
    [switch]$Json
)

$ErrorActionPreference = 'Continue'
$results = [System.Collections.ArrayList]::new()

function Add-Check {
    param([string]$Name, [string]$Status, [string]$Detail, [string]$Fix = '')
    [void]$results.Add([pscustomobject]@{
            name   = $Name
            status = $Status      # ok | warn | fail
            detail = $Detail
            fix    = $Fix
        })
}

# ---------------------------------------------------------------- WSL

try {
    $running = & wsl.exe --list --running 2>$null
    # wsl.exe emits UTF-16; PowerShell may show NULs between characters.
    $clean = ($running -join ' ') -replace "`0", ''
    if ($clean -match [regex]::Escape($Distro)) {
        Add-Check 'wsl_running' 'ok' "$Distro is running"
    }
    else {
        Add-Check 'wsl_running' 'fail' "$Distro is not running" `
            "wsl -d $Distro --exec /bin/true, then install the keep-alive task"
    }
}
catch {
    Add-Check 'wsl_running' 'fail' "wsl.exe failed: $($_.Exception.Message)"
}

# ---------------------------------------------------------------- keep-alive task

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    Add-Check 'keepalive_task' 'fail' "scheduled task $TaskName is missing" `
        'powershell -ExecutionPolicy Bypass -File ops\windows\install-autostart.ps1'
}
elseif ($task.State -in @('Ready', 'Running')) {
    Add-Check 'keepalive_task' 'ok' "$TaskName is $($task.State)"
}
else {
    Add-Check 'keepalive_task' 'warn' "$TaskName is $($task.State)" `
        "Enable-ScheduledTask -TaskName $TaskName"
}

# ---------------------------------------------------------------- power

function Get-PowerSetting {
    param([string]$SubGroup, [string]$Setting)
    $out = & powercfg.exe /query SCHEME_CURRENT $SubGroup $Setting 2>$null
    $line = ($out | Select-String -Pattern 'Current AC Power Setting Index' | Select-Object -First 1)
    if ($null -eq $line) { return $null }
    $hex = ($line.ToString() -split ':')[-1].Trim()
    try { return [Convert]::ToInt32($hex, 16) } catch { return $null }
}

$standby = Get-PowerSetting 'SUB_SLEEP' 'STANDBYIDLE'
if ($null -eq $standby) {
    Add-Check 'sleep_ac' 'warn' 'could not read the AC standby timeout'
}
elseif ($standby -eq 0) {
    Add-Check 'sleep_ac' 'ok' 'the machine never sleeps on AC'
}
else {
    Add-Check 'sleep_ac' 'fail' "the machine sleeps after $standby s on AC" `
        'powercfg /change standby-timeout-ac 0'
}

$hib = Get-PowerSetting 'SUB_SLEEP' 'HIBERNATEIDLE'
if ($null -ne $hib -and $hib -ne 0) {
    Add-Check 'hibernate_ac' 'warn' "the machine hibernates after $hib s on AC" `
        'powercfg /change hibernate-timeout-ac 0'
}
else {
    Add-Check 'hibernate_ac' 'ok' 'no hibernation on AC'
}

# ---------------------------------------------------------------- .wslconfig

$wslConfigPath = Join-Path $env:USERPROFILE '.wslconfig'
if (Test-Path $wslConfigPath) {
    $text = Get-Content $wslConfigPath -Raw
    if ($text -match 'vmIdleTimeout\s*=\s*-1') {
        Add-Check 'wslconfig' 'ok' 'vmIdleTimeout=-1'
    }
    else {
        Add-Check 'wslconfig' 'warn' '.wslconfig has no vmIdleTimeout=-1' `
            'ops\windows\install-autostart.ps1 -WriteWslConfig'
    }
}
else {
    Add-Check 'wslconfig' 'warn' 'no .wslconfig' `
        'ops\windows\install-autostart.ps1 -WriteWslConfig'
}

# ---------------------------------------------------------------- docker / ollama

if (Get-Command docker -ErrorAction SilentlyContinue) {
    Add-Check 'docker' 'ok' 'docker CLI found on PATH'
}
else {
    Add-Check 'docker' 'warn' 'no docker CLI on the Windows PATH (fine if you run docker engine inside WSL)'
}

try {
    $r = Invoke-WebRequest -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3 -UseBasicParsing
    if ($r.StatusCode -eq 200) { Add-Check 'ollama' 'ok' 'Ollama answers on 127.0.0.1:11434' }
    else { Add-Check 'ollama' 'warn' "Ollama returned HTTP $($r.StatusCode)" }
}
catch {
    Add-Check 'ollama' 'warn' 'nothing answers on 127.0.0.1:11434' 'start Ollama, or disable the ollama provider'
}

# ---------------------------------------------------------------- disk

try {
    $drive = Get-PSDrive -Name ($env:SystemDrive.TrimEnd(':')) -ErrorAction Stop
    $freeGb = [math]::Round($drive.Free / 1GB, 1)
    if ($freeGb -lt 10) { Add-Check 'disk' 'fail' "$freeGb GB free on $env:SystemDrive" 'free up space' }
    elseif ($freeGb -lt 25) { Add-Check 'disk' 'warn' "$freeGb GB free on $env:SystemDrive" }
    else { Add-Check 'disk' 'ok' "$freeGb GB free on $env:SystemDrive" }
}
catch {
    Add-Check 'disk' 'warn' 'could not read free space'
}

# ---------------------------------------------------------------- output

if ($Json) {
    $results | ConvertTo-Json -Depth 4 -Compress
}
else {
    foreach ($r in $results) {
        $tag = switch ($r.status) { 'ok' { '  ok  ' } 'warn' { ' warn ' } default { ' FAIL ' } }
        Write-Host ("{0} {1,-16} {2}" -f $tag, $r.name, $r.detail)
        if ($r.fix -and $r.status -ne 'ok') { Write-Host ("        fix: {0}" -f $r.fix) }
    }
    if ($results | Where-Object { $_.status -eq 'fail' }) { exit 1 }
}
