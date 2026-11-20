<#
.SYNOPSIS
  Captura (solo lectura) la información de red que usa el motor, para convertirla
  en fixture de pruebas real. No modifica nada.
  REVISA el archivo antes de compartirlo: contiene direcciones MAC e IP locales.
#>
param([string]$Out = "windows_capture.json")
$adapters = @(Get-NetAdapter | Select-Object Name, InterfaceDescription, ifIndex, Status, MacAddress, LinkSpeed, MediaType, PhysicalMediaType, NdisPhysicalMedium, Virtual, HardwareInterface)
$addresses = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object InterfaceIndex, IPAddress, PrefixLength, PrefixOrigin, AddressState)
$routes = @(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Select-Object ifIndex, NextHop, RouteMetric)
$ipif = @(Get-NetIPInterface -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object ifIndex, InterfaceMetric, ConnectionState)
ConvertTo-Json -Depth 4 -InputObject @{ adapters = $adapters; addresses = $addresses; routes = $routes; ipinterfaces = $ipif } | Set-Content -Encoding UTF8 $Out
Write-Host "Guardado en $Out"
