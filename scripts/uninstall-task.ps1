param(
  [string]$TaskName = "CanvasTelegramBot"
)

$ErrorActionPreference = "Stop"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
  Write-Host "Removed Windows Scheduled Task '$TaskName'."
} else {
  Write-Host "Scheduled Task '$TaskName' was not found."
}
