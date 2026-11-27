@echo off
rem Validacion fisica guiada de Cauce Network Engine (dos enlaces reales).
rem Se ejecuta DESDE ESTA CARPETA del repositorio: no instala nada, no toca la instalacion
rem existente de %LOCALAPPDATA%\CauceNetworkEngine ni la configuracion de red de Windows.
rem Requisito: TU servidor de laboratorio en marcha (SERVIDOR_LABORATORIO.cmd en otro equipo propio).
setlocal
cd /d "%~dp0..\..\.."
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo No se encontro Python 3.10 o superior. Instala Python de python.org y vuelve a intentarlo.
  pause
  exit /b 2
)
set "LABURL=%~1"
if "%LABURL%"=="" set /p LABURL=URL de TU servidor de laboratorio (ej. http://mi-servidor:8770): 
if "%LABURL%"=="" (
  echo Falta la URL del servidor de laboratorio.
  pause
  exit /b 2
)
set "PYTHONPATH=%CD%"
set "PYTHONIOENCODING=utf-8"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "TS=%%i"
set "OUT=%CD%\reports\validacion-fisica-%TS%"
echo.
echo Informe en: %OUT%
echo Sigue las instrucciones: te pedira desconectar y reconectar el enlace principal.
echo.
%PY% -m cauce fieldcheck --lab-url "%LABURL%" --out "%OUT%"
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (echo Resultado: SIN FALLOS) else (echo Resultado: HAY FALLOS, revisa %OUT%\fieldcheck.md)
pause
exit /b %RC%
