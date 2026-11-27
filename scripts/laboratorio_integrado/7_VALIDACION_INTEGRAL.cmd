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
rem VALIDACION INTEGRAL: un solo lanzador. Instala si hace falta, arranca los tres motores en el laboratorio
rem aislado, ejecuta todas las pruebas posibles y solo pide ayuda para lo fisico (telefono, Wi-Fi).
rem Uso: 7_VALIDACION_INTEGRAL.cmd [rapido^|completo^|prolongado^|nocturno] [mas opciones de integral.py]
set "PERFIL=%~1"
if not "%PERFIL%"=="" shift
if "%PERFIL%"=="" (
  echo.
  echo  Validacion integral de Cauce ^(laboratorio aislado en %USERPROFILE%\cauce-lab-integrado^)
  echo    1. Rapido      ~5 min   comprobacion basica, sin cortes fisicos
  echo    2. Completo    ~25 min  bateria integral con telefono y corte del Wi-Fi guiados
  echo    3. Prolongado  1 hora   supervision con red simulada
  echo    4. Nocturno    8 horas  supervision con red simulada
  echo.
  set /p OPC=Elige 1-4 [2]: 
)
if "%PERFIL%"=="" (
  if "%OPC%"=="1" set "PERFIL=rapido"
  if "%OPC%"=="3" set "PERFIL=prolongado"
  if "%OPC%"=="4" set "PERFIL=nocturno"
  if not defined PERFIL set "PERFIL=completo"
)
%PY% lab.py al-dia
if errorlevel 3 (
  echo Se instala o actualiza el laboratorio ^(10-20 min la primera vez, necesita internet^).
  %PY% lab.py detener >nul 2>nul
  %PY% lab.py instalar --navegador-forms
  if errorlevel 1 (
    echo La instalacion fallo. Revisa la salida de arriba.
    if not "%CAUCE_LAB_SIN_PAUSA%"=="1" pause
    exit /b 2
  )
)
%PY% integral.py --perfil %PERFIL% %1 %2 %3 %4 %5 %6
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (echo Resultado: SIN FALLOS ^(lo PENDIENTE no cuenta como aprobado^)) else (echo Resultado: HAY FALLOS)
echo Informe: %USERPROFILE%\cauce-lab-integrado\informes\integral-...\INFORME_VALIDACION_INTEGRAL.md
if not "%CAUCE_LAB_SIN_PAUSA%"=="1" pause
exit /b %RC%
