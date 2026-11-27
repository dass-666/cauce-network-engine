@echo off
rem Laboratorio integrado de Cauce (Monitor + Forms + Network Engine) en %USERPROFILE%\cauce-lab-integrado.
rem No toca C:\cauce-msi, %LOCALAPPDATA%\CauceNetworkEngine ni la configuracion de red de Windows.
setlocal
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3.13"
if defined PY %PY% -c "1" >nul 2>nul || set "PY="
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo No se encontro Python. Instala Python 3.13 de python.org y vuelve a intentarlo.
  pause
  exit /b 2
)
set "PYTHONIOENCODING=utf-8"
rem Paso 2: comprueba puertos y versiones, inicia los tres motores en modo SIMULADO y ejecuta las pruebas.
rem Deja los servicios en marcha y abre Monitor en la pantalla Red. Para pararlos: 4_DETENER.cmd
%PY% lab.py todo --modo simulado %*
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (echo Resultado: OK) else (echo Resultado: HAY FALLOS. Revisa el informe en %USERPROFILE%\cauce-lab-integrado\informes)
if not "%CAUCE_LAB_SIN_PAUSA%"=="1" pause
exit /b %RC%
