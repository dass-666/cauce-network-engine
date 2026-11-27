@echo off
rem Servidor de validacion PROPIO y privado (E5). Se ejecuta en un SEGUNDO equipo tuyo, NO en la MSI.
rem Uso: SERVIDOR_VALIDACION.cmd 192.168.1.50   (la IPv4 privada de ESTE equipo; ver "ipconfig")
rem Parar: Ctrl+C o cerrar la ventana. No abre puertos en el router ni publica nada.
setlocal
cd /d "%~dp0\..\.."
set "IP=%~1"
if "%IP%"=="" (
  echo Direcciones IPv4 de este equipo:
  ipconfig | findstr /R /C:"IPv4"
  set /p IP=">>> Escribe la IPv4 privada de este equipo (192.168.x.x, 10.x.x.x o 100.x.x.x de Tailscale): "
)
set "DATOS=%USERPROFILE%\cauce-servidor-validacion"
if not exist "%DATOS%" mkdir "%DATOS%"
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
echo.
echo Firewall de Windows: si pregunta, permite SOLO "Redes privadas". No marques "Redes publicas".
echo La URL para la MSI es http://%IP%:8770/^<clave^>; la clave esta en %DATOS%\clave.txt
echo.
%PY% -m cauce labserver --host %IP% --port 8770 --clave-archivo "%DATOS%\clave.txt"
endlocal
