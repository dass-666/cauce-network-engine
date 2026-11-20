<#
.SYNOPSIS
  Desinstala Cauce Network Engine del usuario actual.
  Por defecto conserva los registros (audit.jsonl, logs). Usa -RemoveData para borrarlos.
#>
param([switch]$RemoveData)
$ErrorActionPreference = "Stop"
$Root = Join-Path $env:LOCALAPPDATA "CauceNetworkEngine"
$Bin = Join-Path $Root "bin"
if (Test-Path (Join-Path $Bin "cauce.cmd")) { & (Join-Path $Bin "cauce.cmd") stop 2>$null }
Start-Sleep -Seconds 2
foreach ($d in @("app", "venv", "bin")) {
  $p = Join-Path $Root $d
  if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -like "*$Bin*") {
  [Environment]::SetEnvironmentVariable("Path", (($userPath -split ";") | Where-Object { $_ -ne $Bin }) -join ";", "User")
}
if ($RemoveData -and (Test-Path $Root)) { Remove-Item -Recurse -Force $Root }
if ($RemoveData) { $msg = "Desinstalado. Datos eliminados." } else { $msg = "Desinstalado. Registros conservados en $Root." }
Write-Host $msg
