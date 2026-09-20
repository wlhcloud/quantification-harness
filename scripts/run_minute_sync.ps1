param(
  [string]$HostAddress = '127.0.0.1',
  [int]$Port = 8091
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$privateEnv = Join-Path $projectRoot '.quant.env'
if (Test-Path $privateEnv) {
  foreach ($line in Get-Content $privateEnv) {
    if (-not $line -or $line.TrimStart().StartsWith('#')) { continue }
    $parts = $line.Split('=', 2)
    if ($parts.Count -eq 2) {
      [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), 'Process')
    }
  }
}
$env:MINUTE_SYNC_HOST = $HostAddress
$env:MINUTE_SYNC_PORT = [string]$Port
$env:MINUTE_SYNC_CONCURRENCY = '10'
$env:MINUTE_SYNC_5M_SEGMENT_DAYS = '120'
Set-Location $projectRoot
$node22 = 'D:\ProdApp\NVM\nvm\v22.19.0\node.exe'
if (-not (Test-Path $node22)) { $node22 = 'node' }
& $node22 services/minute-sync-service/server.mjs
