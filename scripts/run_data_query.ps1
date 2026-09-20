param([string]$HostAddress='127.0.0.1',[int]$Port=9103)
$ErrorActionPreference='Stop'
$projectRoot=Split-Path -Parent $PSScriptRoot
$serviceRoot=Join-Path $projectRoot 'services\data-query-service'
$env:DATA_QUERY_PROJECT_ROOT=$projectRoot
python -m uvicorn data_query.main:app --app-dir (Join-Path $serviceRoot 'src') --host $HostAddress --port $Port
