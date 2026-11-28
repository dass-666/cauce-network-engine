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
rem Restaura un respaldo E10 (parada): no retrocede las operaciones de Forms y el motor pide revision de todo. Uso: RESTAURAR_E10.cmd ARCHIVO.zip
if "%~1"=="" (echo Falta el archivo de respaldo. Uso: RESTAURAR_E10.cmd ARCHIVO.zip & pause & exit /b 2)
%PY% e10.py restaurar %1
set "RC=%ERRORLEVEL%"
if not "%CAUCE_LAB_SIN_PAUSA%"=="1" pause
exit /b %RC%
