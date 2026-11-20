"""Ejemplo de aplicación externa que usa Cauce SOLO a través de la API local.

Requisitos: el motor en marcha (``cauce run`` o ``cauce sim``) y el servidor de
laboratorio (``cauce labserver``) si se usa el modo real.

    python examples/integracion_externa.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cauce.client import CauceAPIError, CauceClient  # noqa: E402

c = CauceClient.from_local_install()
print("Motor:", c.ping())
for x in c.connections():
    print(f"- {x['id']:<10} {x['name']:<28} salud={x['health']['state']:<9} disponible={x['available']} salida={x['independence']}")

try:
    route = c.request_route(owner="ejemplo-externo", purpose="demostración")
except CauceAPIError as e:
    sys.exit(f"No hay ruta disponible: {e}")
print("Ruta asignada:", route["connection_id"], "IP de origen:", route["bind"]["source_ip"])
# Aquí la aplicación abriría sus sockets con c.connect_via(route, (host, puerto))
c.release_route(route["id"], owner="ejemplo-externo")
print("Ruta liberada.")
