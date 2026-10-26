# Step 1 · a Windows PC as a delega node.
# Run in PowerShell AS ADMINISTRATOR:
#   Set-ExecutionPolicy -Scope Process Bypass; .\1-windows.ps1
# Idempotent. Leaves:
#   - WSL2 with Ubuntu (the worker runs there: delega expects Linux)
#   - WSL that does not shut down when idle
#   - lid closed = do nothing WHILE PLUGGED IN (on battery it still suspends)
#   - a scheduled task that starts WSL at boot, without signing in
# No machine-specific values live in this file.
$ErrorActionPreference = "Stop"
$TaskName = "delega WSL node"
# Use the Ubuntu you already have in WSL; if there is none, install Ubuntu 24.04.
$Distro = (wsl.exe -l -q 2>$null) -replace "`0", "" | Where-Object { $_ -match '^Ubuntu' } | Select-Object -First 1
if (-not $Distro) {
  Write-Host ">> Installing WSL + Ubuntu 24.04. When it finishes, REBOOT, open Ubuntu, create your user and run this again."
  wsl.exe --install -d Ubuntu-24.04
  return
}
$Distro = $Distro.Trim()
Write-Host ">> WSL distro: $Distro"
wsl.exe --update

# No idle shutdown (otherwise WSL stops after ~60 s without an open terminal).
$cfg = "$env:USERPROFILE\.wslconfig"
# Keep whatever is already there (e.g. networkingMode=mirrored); only add what is missing.
$txt = if (Test-Path $cfg) { Get-Content -Raw $cfg } else { "" }
if ($txt -notmatch '(?m)^\[wsl2\]')   { $txt += "`r`n[wsl2]`r`n" }
if ($txt -notmatch '(?m)^vmIdleTimeout') { $txt = $txt -replace '(?m)^\[wsl2\]\s*$', "[wsl2]`r`nvmIdleTimeout=-1" }
if ($txt -notmatch '(?m)^\[general\]') { $txt += "`r`n[general]`r`n" }
if ($txt -notmatch '(?m)^instanceIdleTimeout') { $txt = $txt -replace '(?m)^\[general\]\s*$', "[general]`r`ninstanceIdleTimeout=-1" }
Copy-Item $cfg "$cfg.before-delega" -ErrorAction SilentlyContinue
Set-Content -Encoding ascii $cfg $txt.Trim()
Write-Host ">> $cfg written"

# --- Power: lid closed without suspending, when plugged in ---------------------
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0   # 0 = do nothing
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /setactive SCHEME_CURRENT
Write-Host ">> Power: plugged in, it neither suspends nor sleeps when the lid closes"

# --- Start WSL when the machine boots -------------------------------------------
$user    = "$env:USERDOMAIN\$env:USERNAME"
$action  = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-d $Distro --exec /bin/sleep infinity"
$trigger = @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn -User $user))
$who     = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
$set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Principal $who -Settings $set -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host ">> Task '$TaskName' registered and started"

Write-Host ""
Write-Host "Next: open Ubuntu, copy .env.example to .env, fill it in and run:  bash /mnt/c/<path>/2-wsl.sh"
