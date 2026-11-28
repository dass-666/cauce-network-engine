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
rem Validacion integral de RC3 en su laboratorio propio. Uso: VALIDAR_RC3.cmd [rapido^|completo^|prolongado^|nocturno]
set "PERFIL=%~1"
if "%PERFIL%"=="" set "PERFIL=completo"
%PY% candidatas.py validar rc3 %PERFIL% %2 %3 %4 %5 %6
set "RC=%ERRORLEVEL%"
echo Informe: %USERPROFILE%\cauce-lab-rc3\informes\integral-...\INFORME_VALIDACION_INTEGRAL.md
if not "%CAUCE_LAB_SIN_PAUSA%"=="1" pause
exit /b %RC%
