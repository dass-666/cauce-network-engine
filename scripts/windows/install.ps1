<#
.SYNOPSIS
  Instalación aislada de Cauce Network Engine para el usuario actual (sin administrador).

.DESCRIPTION
  - Copia el código a %LOCALAPPDATA%\CauceNetworkEngine\app
  - Crea un entorno virtual de Python propio en %LOCALAPPDATA%\CauceNetworkEngine\venv
  - Crea el lanzador cauce.cmd en %LOCALAPPDATA%\CauceNetworkEngine\bin
  - Ejecuta el diagnóstico (cauce doctor)
  NO modifica rutas, adaptadores, firewall, PATH del sistema ni servicios.
  NO necesita privilegios de administrador (y se recomienda no usarlos).

.PARAMETER Python
  Ruta a python.exe (3.10 o superior). Por defecto usa el lanzador 'py -3'.
.PARAMETER AddToUserPath
  Añade la carpeta bin al PATH del USUARIO (no del sistema). Opcional.
#>
param(
  [string]$Python = "",
  [switch]$AddToUserPath
)
$ErrorActionPreference = "Stop"
$Root = Join-Path $env:LOCALAPPDATA "CauceNetworkEngine"
$App = Join-Path $Root "app"
$Venv = Join-Path $Root "venv"
$Bin = Join-Path $Root "bin"
$Src = Resolve-Path (Join-Path $PSScriptRoot "..\..")

function Find-Python {
  if ($Python) { return $Python }
  $py = Get-Command py.exe -ErrorAction SilentlyContinue
  if ($py) { return "py.exe" }
  $p = Get-Command python.exe -ErrorAction SilentlyContinue
  if ($p -and $p.Source -notlike "*WindowsApps*") { return $p.Source }
  throw "No se encontró Python 3.10+. Instálalo desde https://www.python.org/downloads/windows/ (marca 'py launcher')."
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) { Write-Warning "Estás en una consola de administrador. No es necesario; se recomienda instalar y ejecutar como usuario normal." }

$PyExe = Find-Python
$pyArgs = @()
if ($PyExe -eq "py.exe") { $pyArgs = @("-3") }
$ver = & $PyExe @pyArgs -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$ver -lt [version]"3.10") { throw "Se requiere Python 3.10 o superior (encontrado $ver)." }
Write-Host "Python $ver encontrado."

New-Item -ItemType Directory -Force -Path $Root, $Bin | Out-Null
if (Test-Path $App) { Remove-Item -Recurse -Force $App }
New-Item -ItemType Directory -Force -Path $App | Out-Null
Copy-Item -Recurse -Force (Join-Path $Src "cauce") $App
Copy-Item -Force (Join-Path $Src "config.example.json") $App -ErrorAction SilentlyContinue

if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
  Write-Host "Creando entorno virtual aislado en $Venv ..."
  & $PyExe @pyArgs -m venv $Venv
}
$VPy = Join-Path $Venv "Scripts\python.exe"

$cmd = @"
@echo off
rem Lanzador de Cauce Network Engine (instalación aislada del usuario)
set "PYTHONPATH=$App"
"$VPy" -m cauce %*
"@
Set-Content -Path (Join-Path $Bin "cauce.cmd") -Value $cmd -Encoding ASCII

$start = @"
@echo off
rem Inicia el motor real y abre el panel. Cierra esta ventana o usa cauce-stop.cmd para detenerlo.
call "%~dp0cauce.cmd" run --open
"@
Set-Content -Path (Join-Path $Bin "cauce-start.cmd") -Value $start -Encoding ASCII
$sim = @"
@echo off
rem Inicia el SIMULADOR (3 conexiones ficticias) y abre el panel.
call "%~dp0cauce.cmd" sim --open
"@
Set-Content -Path (Join-Path $Bin "cauce-sim.cmd") -Value $sim -Encoding ASCII
$stop = @"
@echo off
call "%~dp0cauce.cmd" stop
"@
Set-Content -Path (Join-Path $Bin "cauce-stop.cmd") -Value $stop -Encoding ASCII

if ($AddToUserPath) {
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  if ($userPath -notlike "*$Bin*") {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$Bin", "User")
    Write-Host "Añadido $Bin al PATH del usuario (abre una consola nueva)."
  }
}

Write-Host "`nDiagnóstico:"
& (Join-Path $Bin "cauce.cmd") doctor
Write-Host "`nInstalado en $Root"
Write-Host "  Motor real:   $Bin\cauce-start.cmd"
Write-Host "  Simulador:    $Bin\cauce-sim.cmd"
Write-Host "  Detener:      $Bin\cauce-stop.cmd"
Write-Host "  Interfaces:   $Bin\cauce.cmd discover"
Write-Host "  Registros:    $Root\logs\cauce.log  y  $Root\audit.jsonl"
