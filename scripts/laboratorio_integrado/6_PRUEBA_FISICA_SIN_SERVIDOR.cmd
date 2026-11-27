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
rem Prueba fisica SIN servidor propio: Network Engine en modo REAL (solo observa) y solo abre y cierra
rem conexiones TCP al destino que indiques, por cada enlace. Cubre F1, F2, F3, F6, F7 y F10-F12.
rem Necesitas dos enlaces (p. ej. Wi-Fi y telefono de otro operador por USB). Usa un destino que estes
rem autorizado a usar, p. ej. un servicio del que ya eres cliente.
set "OBJETIVO=%~1"
if "%OBJETIVO%"=="" set /p OBJETIVO=Destino host:puerto (ej. github.com:443): 
if "%OBJETIVO%"=="" (
  echo Falta el destino.
  pause
  exit /b 2
)
%PY% lab.py detener >nul
%PY% lab.py todo --modo real --objetivo "%OBJETIVO%"
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (echo Resultado: OK) else (echo Resultado: HAY FALLOS. Revisa el informe en %USERPROFILE%\cauce-lab-integrado\informes)
if not "%CAUCE_LAB_SIN_PAUSA%"=="1" pause
exit /b %RC%
