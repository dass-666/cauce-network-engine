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
rem Lanza el PROPIO 7_VALIDACION_INTEGRAL.cmd de RC2 desde su clon (6e2cceb), tal cual. No modifica RC2.
rem Antes comprueba que RC3 este parada. Uso: VALIDAR_RC2.cmd [rapido^|completo^|prolongado^|nocturno]
set "PERFIL=%~1"
if "%PERFIL%"=="" set "PERFIL=completo"
%PY% candidatas.py validar rc2 %PERFIL% %2 %3 %4 %5 %6
set "RC=%ERRORLEVEL%"
if not "%CAUCE_LAB_SIN_PAUSA%"=="1" pause
exit /b %RC%
