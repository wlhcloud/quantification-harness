<#
.SYNOPSIS
  幂等注册 quant 相关 Windows 计划任务（服务自愈 + 每日链）。

.DESCRIPTION
  背景：编排分散在三处（计划任务 / 进程内 MarketScheduler / 豆包任务），任务定义只存在于
  注册表里，容易出现"有的服务有任务、有的没有""任务用内联命令没加载 .quant.env 导致鉴权
  静默失效"这类漂移。这里把任务定义收敛成一份可复读、可重放的脚本。

  注册/修正的任务：
    quant-quant-sync-9101    一次任务，跑 data\start-9101.bat（加载 .quant.env，只绑 127.0.0.1）
    quant-engine-9102        同上，跑 data\start-9102.bat
    quant-data-query-9103    同上，跑 data\start-9103.bat（此前只有手工启动，重启后就会缺服务）
    quant-services-watchdog  每 5 分钟 + 每次登录：体检 9101/9102/9103，失败则重启（scripts\ensure_services.ps1）
    stock-factor-daily-poll  每 30 分钟跑 scripts\run-stock-chain.bat（链内自带单实例锁）

.EXAMPLE
  pwsh -File scripts\register_tasks.ps1 -DryRun   # 只打印将要注册的内容
  pwsh -File scripts\register_tasks.ps1           # 实际注册（需要当前用户权限，不需要管理员）
#>
[CmdletBinding()]
param([switch]$DryRun)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$pwsh = (Get-Command pwsh -ErrorAction SilentlyContinue).Source
if (-not $pwsh) { $pwsh = (Get-Command powershell.exe).Source }

$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 72) -RestartCount 0
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

function New-ServiceTask {
    param([string]$Name, [string]$Bat)
    $action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$(Join-Path $root $Bat)`"" -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
        -Description "quant 服务：$Bat（只绑 127.0.0.1，启动前加载 .quant.env）"
    return @{ Name = $Name; Task = $task }
}

function New-WatchdogTask {
    $action = New-ScheduledTaskAction -Execute $pwsh `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $root 'scripts\ensure_services.ps1')`"" `
        -WorkingDirectory $root
    $every5 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
    $atLogOn = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    # 重复任务：单次执行上限 10 分钟，避免卡死后拖住下一轮（IgnoreNew 会跳过重叠轮次）
    $watchdogSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -RestartCount 0 `
        -StartWhenAvailable
    $task = New-ScheduledTask -Action $action -Trigger @($every5, $atLogOn) -Settings $watchdogSettings -Principal $principal `
        -Description 'quant 服务看门狗：每 5 分钟检查 9101/9102/9103 健康与鉴权，异常则用 start-910N.bat 重启'
    return @{ Name = 'quant-services-watchdog'; Task = $task }
}

function New-ChainTask {
    $action = New-ScheduledTaskAction -Execute 'cmd.exe' `
        -Argument "/c `"$(Join-Path $root 'scripts\run-stock-chain.bat')`"" -WorkingDirectory $root
    # 起点取"现在+5 分钟"，之后每 30 分钟一轮；不用固定钟点，避免注册当天首轮被推迟数小时
    $every30 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Minutes 30)
    $task = New-ScheduledTask -Action $action -Trigger $every30 -Settings $settings -Principal $principal `
        -Description '股票/ETF 每日链：每 30 分钟补齐行情→因子→模拟盘（链内单实例锁 + JSONL 运行台账）'
    return @{ Name = 'stock-factor-daily-poll'; Task = $task }
}

$definitions = @(
    (New-ServiceTask -Name 'quant-quant-sync-9101' -Bat 'data\start-9101.bat'),
    (New-ServiceTask -Name 'quant-engine-9102' -Bat 'data\start-9102.bat'),
    (New-ServiceTask -Name 'quant-data-query-9103' -Bat 'data\start-9103.bat'),
    (New-WatchdogTask),
    (New-ChainTask)
)

$failed = 0
foreach ($def in $definitions) {
    if ($DryRun) {
        Write-Host "[DryRun] 将注册 $($def.Name)：$($def.Task.Actions[0].Execute) $($def.Task.Actions[0].Arguments)"
        continue
    }
    try {
        Register-ScheduledTask -TaskName $def.Name -InputObject $def.Task -Force | Out-Null
        $state = (Get-ScheduledTask -TaskName $def.Name).State
        Write-Host "[OK] $($def.Name) 已注册（state=$state）"
    } catch {
        Write-Host "[ERROR] $($def.Name) 注册失败：$_"
        $failed++
    }
}

if ($failed -gt 0) { exit 1 }
exit 0
