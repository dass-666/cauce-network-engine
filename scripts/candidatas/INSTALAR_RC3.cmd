@echo off
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
rem RC3 en su PROPIA carpeta (%USERPROFILE%\cauce-lab-rc3, puertos +100). No toca RC2, C:\cauce-msi,
rem %LOCALAPPDATA%\CauceNetworkEngine, respaldos ni rutas globales. Uso: INSTALAR_RC3.cmd COMMIT_ESPERADO
if "%~1"=="" (echo Falta el commit esperado de RC3. Uso: INSTALAR_RC3.cmd COMMIT & pause & exit /b 2)
%PY% candidatas.py instalar --esperado %1
set "RC=%ERRORLEVEL%"
if not "%CAUCE_LAB_SIN_PAUSA%"=="1" pause
exit /b %RC%
