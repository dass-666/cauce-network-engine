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
rem E10 en su PROPIA carpeta (%USERPROFILE%\cauce-lab-e10, puertos +200). No toca RC2, RC3, C:\cauce-msi ni rutas globales. Uso: INSTALAR_E10.cmd COMMIT_ESPERADO
if "%~1"=="" (echo Falta el commit esperado de E10. Uso: INSTALAR_E10.cmd COMMIT & pause & exit /b 2)
%PY% e10.py instalar --esperado %1
set "RC=%ERRORLEVEL%"
if not "%CAUCE_LAB_SIN_PAUSA%"=="1" pause
exit /b %RC%
