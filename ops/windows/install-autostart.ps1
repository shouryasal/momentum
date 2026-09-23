<#
.SYNOPSIS
  Make the Windows host a dependable place for Earn to run unattended.

.DESCRIPTION
  WSL2 is not a server. Left alone, Windows suspends the VM when the last process in it
  exits, sleeps the machine on AC power, and does not start WSL at boot. Any of the three
  stops the bots, the cron jobs and the console — while the exchange keeps trading.

  This script fixes all three, idempotently:

    1. a Scheduled Task ("EarnWSLKeepAlive") that starts at boot, runs whether or not
       anyone is logged in, and holds a process open inside the distro so the WSL VM
       never idles out;
    2. `powercfg /change standby-timeout-ac 0` — no sleep on AC (the live preflight
       blocks going live while this is non-zero);
    3. a suggested %USERPROFILE%\.wslconfig with vmIdleTimeout=-1 and mirrored
       networking (written only with -WriteWslConfig, and backed up first).

  Everything it does is reversible: -Remove undoes the task.

.PARAMETER Distro
  WSL distribution name. Default: Ubuntu-24.04

.PARAMETER TaskName
  Scheduled Task name. Default: EarnWSLKeepAlive

.PARAMETER WriteWslConfig
  Also write %USERPROFILE%\.wslconfig (backs up any existing file first).

.PARAMETER Remove
  Remove the scheduled task and exit.

.EXAMPLE
  # Run from an ELEVATED PowerShell:
  powershell -ExecutionPolicy Bypass -File ops\windows\install-autostart.ps1
#>

[CmdletBinding()]
param(
    [string]$Distro = 'Ubuntu-24.04',
    [string]$TaskName = 'EarnWSLKeepAlive',
    [switch]$WriteWslConfig,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

function Test-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Elevated)) {
    Write-Error 'This script changes machine power settings and a boot-time task: run it from an elevated PowerShell.'
    exit 1
}

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "removed scheduled task $TaskName"
    }
    else {
        Write-Host "scheduled task $TaskName was not present"
    }
    exit 0
}

# ---------------------------------------------------------------- 1. keep-alive task

# `sleep infinity` inside the distro is the whole trick: WSL2 shuts the VM down a few
# seconds after its last process exits, and that takes the bots with it.
$wsl = Join-Path $env:WINDIR 'System32\wsl.exe'
$action = New-ScheduledTaskAction -Execute $wsl `
    -Argument "-d $Distro --exec /bin/sh -c `"exec sleep infinity`""

$triggers = @(
    New-ScheduledTaskTrigger -AtStartup
    New-ScheduledTaskTrigger -AtLogOn
)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Set-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
        -Settings $settings -Principal $principal | Out-Null
    Write-Host "updated scheduled task $TaskName"
}
else {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
        -Settings $settings -Principal $principal `
        -Description 'Keeps the WSL2 VM alive so Earn cron jobs, bots and console keep running.' | Out-Null
    Write-Host "registered scheduled task $TaskName"
}

Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Write-Host 'keep-alive task started'

# ---------------------------------------------------------------- 2. power policy

& powercfg.exe /change standby-timeout-ac 0
& powercfg.exe /change hibernate-timeout-ac 0
& powercfg.exe /change disk-timeout-ac 0
Write-Host 'sleep/hibernate/disk timeouts on AC set to 0 (never)'

# ---------------------------------------------------------------- 3. .wslconfig

$wslConfigPath = Join-Path $env:USERPROFILE '.wslconfig'
$suggested = @'
# Suggested by ops\windows\install-autostart.ps1 for Earn.
[wsl2]
# Never suspend the VM for being idle: cron, the bots and the console must keep running.
vmIdleTimeout=-1
# Mirrored networking makes 127.0.0.1 mean the same thing in Windows and in WSL, which is
# what lets the browser reach the console and WSL reach Ollama without gateway guessing.
networkingMode=mirrored
# Let Windows reclaim memory the distro no longer needs.
autoMemoryReclaim=gradual
'@

if ($WriteWslConfig) {
    if (Test-Path $wslConfigPath) {
        $backup = "$wslConfigPath.earn-backup"
        Copy-Item $wslConfigPath $backup -Force
        Write-Host "backed up existing .wslconfig to $backup"
    }
    Set-Content -Path $wslConfigPath -Value $suggested -Encoding utf8
    Write-Host "wrote $wslConfigPath — run 'wsl --shutdown' to apply"
}
else {
    Write-Host ''
    Write-Host "Suggested $wslConfigPath (re-run with -WriteWslConfig to write it):"
    Write-Host $suggested
}

Write-Host ''
Write-Host 'Done. Verify with:  powershell -ExecutionPolicy Bypass -File ops\windows\check-host.ps1'
