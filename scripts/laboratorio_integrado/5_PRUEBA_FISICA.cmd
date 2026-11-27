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
rem Prueba fisica: Network Engine en modo REAL (solo observa) y guion de desconexion y reconexion
rem contra TU servidor de laboratorio. Necesitas dos enlaces (p. ej. Wi-Fi y telefono de otro operador).
set "LABURL=%~1"
if "%LABURL%"=="" set /p LABURL=URL de TU servidor de laboratorio (ej. http://mi-servidor:8770): 
if "%LABURL%"=="" (
  echo Falta la URL del servidor de laboratorio.
  pause
  exit /b 2
)
%PY% lab.py detener >nul
%PY% lab.py todo --modo real --lab-url "%LABURL%"
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (echo Resultado: OK) else (echo Resultado: HAY FALLOS. Revisa el informe en %USERPROFILE%\cauce-lab-integrado\informes)
if not "%CAUCE_LAB_SIN_PAUSA%"=="1" pause
exit /b %RC%
