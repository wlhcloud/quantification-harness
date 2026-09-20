param(
  [int]$Port = 3081,
  [string]$BindHost = '127.0.0.1'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:DSH_HOME = Join-Path $projectRoot 'dsh-home'
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

Set-Location $projectRoot
npm run dsh -- web --host $BindHost --port $Port --no-open
