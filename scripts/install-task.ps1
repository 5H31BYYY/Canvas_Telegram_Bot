param(
  [string]$TaskName = "CanvasTelegramBot",
  [int]$IntervalMinutes = 10
)

$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonCmd = (
  Get-Command python -All -ErrorAction SilentlyContinue |
  Where-Object { $_.Source -and $_.Source -notlike "*\WindowsApps\*" } |
  Select-Object -First 1 -ExpandProperty Source
)
if (-not $PythonCmd) {
  $PythonCmd = (
    Get-ChildItem "$env:LOCALAPPDATA\Python\pythoncore-*" -Filter "python.exe" -ErrorAction SilentlyContinue |
    Select-Object -First 1 -ExpandProperty FullName
  )
}
if (-not $PythonCmd) {
  $PythonCmd = (
    Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\*" -Filter "python.exe" -ErrorAction SilentlyContinue |
    Select-Object -First 1 -ExpandProperty FullName
  )
}
if (-not $PythonCmd) {
  throw "Could not locate python.exe. Ensure Python 3.10+ is installed."
}

Write-Host "Registering Windows Scheduled Task '$TaskName'..."
Write-Host "  Project  : $ProjectDir"
Write-Host "  Python   : $PythonCmd"
Write-Host "  Schedule : Every $IntervalMinutes minutes"

$Action = New-ScheduledTaskAction `
  -Execute $PythonCmd `
  -Argument "bot.py --once" `
  -WorkingDirectory $ProjectDir

$Trigger = New-ScheduledTaskTrigger `
  -Once `
  -At (Get-Date).AddMinutes(1) `
  -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
  -RepetitionDuration (New-TimeSpan -Days 3650)

$Settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
  -MultipleInstances IgnoreNew

Register-ScheduledTask `
  -TaskName $TaskName `
  -Action $Action `
  -Trigger $Trigger `
  -Settings $Settings `
  -Description "Polls Canvas LMS every $IntervalMinutes minutes and sends a grouped Telegram digest alert." `
  -Force | Out-Null

Write-Host ""
Write-Host "Done! Task '$TaskName' is registered and will run every $IntervalMinutes minutes."
Write-Host "To test it immediately, run: Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To remove it later, run:    .\scripts\uninstall-task.ps1"
