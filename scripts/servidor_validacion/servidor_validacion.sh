#!/bin/sh
# Servidor de validación PROPIO y privado (E5) en un segundo equipo Linux o macOS. Uso: ./servidor_validacion.sh 192.168.1.50
# Parar: Ctrl+C. No abre puertos en el router ni publica nada.
set -e
cd "$(dirname "$0")/../.."
IP="${1:?Indica la IPv4 privada de este equipo}"
DATOS="${HOME}/.cauce-servidor-validacion"
mkdir -p "$DATOS" && chmod 700 "$DATOS"
echo "La URL para la MSI es http://$IP:8770/<clave>; la clave está en $DATOS/clave.txt"
exec python3 -m cauce labserver --host "$IP" --port 8770 --clave-archivo "$DATOS/clave.txt"
