param(
  [string]$HostAddress = '127.0.0.1',
  [int]$Port = 9102,
  [string]$ApiKey = ''
)

$projectRoot = Split-Path -Parent $PSScriptRoot
$engineRoot = Join-Path $projectRoot 'services\quant-engine'
$env:QUANT_ENGINE_PROJECT_ROOT = $projectRoot
if ($ApiKey) { $env:QUANT_ENGINE_API_KEY = $ApiKey }
if ($HostAddress -notin @('127.0.0.1','localhost') -and -not $env:QUANT_ENGINE_API_KEY) {
  throw 'A non-loopback engine requires -ApiKey or QUANT_ENGINE_API_KEY.'
}

python -m uvicorn quant_engine.main:app --app-dir (Join-Path $engineRoot 'src') --host $HostAddress --port $Port
