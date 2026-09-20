# 运行量化平台全部 Python 单测（基于标准库 unittest，无需 pytest）
#
# 用法：
#   pwsh -File scripts/run_tests.ps1                     # 跑全部三个服务
#   pwsh -File scripts/run_tests.ps1 -Service quant-engine   # 只跑一个服务
#
# 说明：仓库里没有安装 pytest（`python -m pytest` 会报 No module named pytest），
# 但 tests/ 下全部是 unittest 用例，因此直接用 unittest discover 即可。
# 测试数据都建在临时目录里，不会碰 data/ 下的真实库。
param([string]$Service = "")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$services = if ($Service) { @($Service) } else { @("quant-engine", "quant-sync", "data-query-service") }
$failed = @()

foreach ($s in $services) {
    $dir = Join-Path $root "services\$s"
    if (-not (Test-Path $dir)) { Write-Host "[SKIP] 未找到 $dir" -ForegroundColor Yellow; continue }
    Write-Host "`n===== $s =====" -ForegroundColor Cyan
    Push-Location $dir
    try {
        # 服务包在 src/ 下，未安装为 site-packages，因此显式加入 PYTHONPATH
        $env:PYTHONPATH = (Join-Path $dir "src")
        python -m unittest discover -s tests -t tests -p "test_*.py"
        if ($LASTEXITCODE -ne 0) { $failed += $s }
    } finally {
        Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
        Pop-Location
    }
}

if ($failed.Count) {
    Write-Host "`n[FAIL] 未通过：$($failed -join ', ')" -ForegroundColor Red
    exit 1
}
Write-Host "`n[OK] 全部服务测试通过" -ForegroundColor Green
