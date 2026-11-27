# Comprueba (solo lectura) que Monitor, Forms y Network Engine responden a la vez en este equipo.
# No arranca ni detiene nada: abrelos tu antes con sus propios lanzadores.
param([int]$MonitorPort = 8000, [int]$FormsPort = 8765, [int]$NetworkPort = 8780)
$targets = @(
  @{ Name = "Monitor";        Port = $MonitorPort; Path = "/api/info" },
  @{ Name = "Forms";          Port = $FormsPort;   Path = "/" },
  @{ Name = "Network Engine"; Port = $NetworkPort; Path = "/api/v1/ping" }
)
$ok = 0
foreach ($t in $targets) {
  $listening = (Test-NetConnection -ComputerName 127.0.0.1 -Port $t.Port -WarningAction SilentlyContinue).TcpTestSucceeded
  $code = "-"
  if ($listening) {
    try { $code = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri ("http://127.0.0.1:{0}{1}" -f $t.Port, $t.Path)).StatusCode }
    catch { if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode } else { $code = "sin respuesta HTTP" } }
    $ok++
  }
  "{0,-15} puerto {1,-5} escuchando={2,-5} http={3}" -f $t.Name, $t.Port, $listening, $code
}
# Que dos motores no compartan puerto
$dup = $targets | Group-Object Port | Where-Object Count -gt 1
if ($dup) { Write-Warning ("Puertos repetidos: " + ($dup.Name -join ", ")) }
if ($ok -eq 3) { "Los tres motores responden a la vez." } else { "Responden $ok de 3." }
