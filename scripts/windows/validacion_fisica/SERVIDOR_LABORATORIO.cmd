@echo off
rem Servidor de laboratorio PROPIO para la validacion fisica. Ejecutalo en OTRO equipo tuyo (no en el de pruebas),
rem alcanzable desde los dos enlaces del equipo de pruebas. Windows preguntara si permites la conexion: acepta
rem solo para redes privadas o abre el puerto 8770 solo hacia ese equipo. Cierra esta ventana al terminar.
setlocal
cd /d "%~dp0..\..\.."
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo No se encontro Python 3.10 o superior.
  pause
  exit /b 2
)
set "PYTHONPATH=%CD%"
%PY% -m cauce labserver --host 0.0.0.0 --port 8770
