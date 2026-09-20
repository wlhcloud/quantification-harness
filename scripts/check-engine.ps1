param([string]$BaseURL = 'http://127.0.0.1:9102')

Invoke-RestMethod "$BaseURL/api/v1/health" | ConvertTo-Json -Depth 4
