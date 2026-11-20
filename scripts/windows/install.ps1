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
  Ruta o nombre de un intérprete concreto (3.10 o superior). Si se indica, solo se prueba ese.
.PARAMETER PythonCandidates
  Comandos a probar, en orden, si no se indica -Python. Por defecto: py -3, python, python3.
  El lanzador 'py' es OPCIONAL: si no existe se usa 'python'.
.PARAMETER InstallRoot
  Carpeta de instalación. Por defecto %LOCALAPPDATA%\CauceNetworkEngine.
.PARAMETER SkipDoctor
  No ejecutar el diagnóstico al final (lo usan las pruebas automáticas).
.PARAMETER AddToUserPath
  Añade la carpeta bin al PATH del USUARIO (no del sistema). Opcional.
#>
param(
  [string]$Python = "",
  [string[]]$PythonCandidates = @("py", "python", "python3"),
  [string]$InstallRoot = "",
  [switch]$SkipDoctor,
  [switch]$AddToUserPath
)
$ErrorActionPreference = "Stop"
# Con "powershell -File" la lista llega como un solo texto "a,b": se separa aquí.
$PythonCandidates = @($PythonCandidates | ForEach-Object { $_ -split "," } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if (-not $InstallRoot) { $InstallRoot = Join-Path $env:LOCALAPPDATA "CauceNetworkEngine" }
$Root = $InstallRoot
$App = Join-Path $Root "app"
$Venv = Join-Path $Root "venv"
$Bin = Join-Path $Root "bin"
$Src = Resolve-Path (Join-Path $PSScriptRoot "..\..")

function Test-PythonCandidate([string]$Exe, [string[]]$PreArgs) {
  # Ejecuta el intérprete y le pregunta su versión real y si tiene venv.
  # Así se descartan el alias de Microsoft Store sin Python instalado, versiones
  # antiguas o un 'py' inexistente. Devuelve $null si no sirve.
  # Sin '%' ni '|' para que también funcione con envoltorios .cmd (pyenv-win, etc.)
  $probe = "import sys, venv; print('.'.join(map(str, sys.version_info[:3])) + '#' + sys.executable)"
  $old = $ErrorActionPreference
  $ErrorActionPreference = "Continue"   # en PowerShell 5.1, stderr de un nativo con Stop lanza excepción
  try {
    $out = & $Exe @PreArgs -c $probe 2>$null
    $code = $LASTEXITCODE
  } catch {
    return $null
  } finally {
    $ErrorActionPreference = $old
  }
  if ($code -ne 0 -or -not $out) { return $null }
  $line = ($out | Select-Object -Last 1).ToString().Trim()
  if ($line -notmatch '^(\d+)\.(\d+)\.(\d+)#(.+)$') { return $null }
  $v = [version]("{0}.{1}.{2}" -f $Matches[1], $Matches[2], $Matches[3])
  return [pscustomobject]@{ Exe = $Exe; Args = $PreArgs; Version = $v; Path = $Matches[4] }
}

function Find-Python {
  $tried = @()
  if ($Python) { $list = @($Python) } else { $list = $PythonCandidates }
  foreach ($name in $list) {
    $preArgs = @()
    if ([IO.Path]::GetFileNameWithoutExtension($name) -eq "py") { $preArgs = @("-3") }
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) { $tried += "$name (no existe)"; continue }
    $c = Test-PythonCandidate $name $preArgs
    if (-not $c) { $tried += "$name (no responde como Python)"; continue }
    if ($c.Version -lt [version]"3.10") { $tried += "$name ($($c.Version), demasiado antiguo)"; continue }
    return $c
  }
  throw ("No se encontró Python 3.10 o superior. Probados: " + ($tried -join "; ") +
         ". Instala Python desde https://www.python.org/downloads/windows/ o indica la ruta con -Python C:\ruta\python.exe")
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) { Write-Warning "Estás en una consola de administrador. No es necesario; se recomienda instalar y ejecutar como usuario normal." }

$Found = Find-Python
$PyExe = $Found.Exe
$pyArgs = $Found.Args
Write-Host "Python $($Found.Version) encontrado mediante '$PyExe' ($($Found.Path))."

New-Item -ItemType Directory -Force -Path $Root, $Bin | Out-Null
if (Test-Path $App) { Remove-Item -Recurse -Force $App }
New-Item -ItemType Directory -Force -Path $App | Out-Null
Copy-Item -Recurse -Force (Join-Path $Src "cauce") $App
Copy-Item -Force (Join-Path $Src "config.example.json") $App -ErrorAction SilentlyContinue

$VPy = Join-Path $Venv "Scripts\python.exe"
if (Test-Path $VPy) {
  $existing = Test-PythonCandidate $VPy @()
  if (-not $existing -or $existing.Version -lt [version]"3.10") {
    Write-Host "El entorno virtual existente no funciona; se vuelve a crear."
    Remove-Item -Recurse -Force $Venv
  }
}
if (-not (Test-Path $VPy)) {
  Write-Host "Creando entorno virtual aislado en $Venv con '$PyExe' ..."
  & $PyExe @pyArgs -m venv $Venv
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VPy)) { throw "No se pudo crear el entorno virtual con '$PyExe'." }
}
$check = Test-PythonCandidate $VPy @()
if (-not $check) { throw "El Python del entorno virtual ($VPy) no responde." }
Write-Host "Entorno virtual listo: Python $($check.Version)."

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

if (-not $SkipDoctor) {
  Write-Host "`nDiagnóstico:"
  & (Join-Path $Bin "cauce.cmd") doctor
}
Write-Host "`nInstalado en $Root"
Write-Host "  Motor real:   $Bin\cauce-start.cmd"
Write-Host "  Simulador:    $Bin\cauce-sim.cmd"
Write-Host "  Detener:      $Bin\cauce-stop.cmd"
Write-Host "  Interfaces:   $Bin\cauce.cmd discover"
Write-Host "  Registros:    $Root\logs\cauce.log  y  $Root\audit.jsonl"
