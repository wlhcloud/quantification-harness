param([string]$HostAddress='127.0.0.1',[int]$Port=9101,[string]$ApiKey='')
$ErrorActionPreference='Stop'
$projectRoot=Split-Path -Parent $PSScriptRoot
$serviceRoot=Join-Path $projectRoot 'services\quant-sync'
$env:QUANT_SYNC_PROJECT_ROOT=$projectRoot
if ($ApiKey) { $env:QUANT_SYNC_API_KEY=$ApiKey }
if ($HostAddress -notin @('127.0.0.1','localhost') -and -not $env:QUANT_SYNC_API_KEY) {
  throw 'A non-loopback sync service requires -ApiKey or QUANT_SYNC_API_KEY.'
}
python -m uvicorn quant_sync.main:app --app-dir (Join-Path $serviceRoot 'src') --host $HostAddress --port $Port
