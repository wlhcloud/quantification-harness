<#
.SYNOPSIS
  quant 服务看门狗：确保 9101 / 9102 / 9103 三个服务在跑、健康、且鉴权确实打开。

.DESCRIPTION
  背景：服务的计划任务是一次性任务（RestartCount=0），进程一挂就永久缺失；
  9101/9103 甚至没有计划任务。手工/任务启动方式不一致还会导致鉴权静默失效
  （api_key 为空时 require_api_key 直接放行所有写请求）。本脚本每 5 分钟由
  计划任务 quant-services-watchdog 拉起，逐项检查：

    1. /api/v1/health 可达且 status=ok（进程活着但卡死也会被判失败）；
    2. 若 .quant.env 里声明了 API key，则 health.authEnabled 必须为 true；
    3. 端口被非 python 进程占用时只告警不动手（避免误杀）。

  失败则杀掉该端口上的 python 进程，用 data\start-910N.bat（会加载 .quant.env、
  只绑 127.0.0.1）重新拉起，并等待恢复。

.EXAMPLE
  pwsh -File scripts\ensure_services.ps1
  pwsh -File scripts\ensure_services.ps1 -DryRun -Verbose
#>
[CmdletBinding()]
param(
    # 只检查并报告，不重启任何进程
    [switch]$DryRun,
    # 单次健康探测超时（秒）
    [int]$ProbeTimeoutSec = 10,
    # 重启后等待恢复的上限（秒）
    [int]$RecoverTimeoutSec = 90,
    # 端口被占用但健康检查失败时，是否允许杀掉占用进程（仅限 python）
    [switch]$NoKill
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$logPath = Join-Path $root 'data\service-watchdog.log'
$lockPath = Join-Path $root 'data\.service-watchdog.lock'
$envFile = Join-Path $root '.quant.env'
$staleLockMinutes = 20

$services = @(
    # Keys：任一变量非空即视为"该服务应开启鉴权"（QUANT_API_KEY 是收敛后的权威变量，
    # 另一个是 key 收敛前的旧名字，旧部署仍可用）
    [pscustomobject]@{ Name = 'quant-sync';   Port = 9101; Bat = 'data\start-9101.bat'; Keys = @('QUANT_API_KEY','QUANT_SYNC_API_KEY');   Log = 'data\quant-sync-9101.log' },
    [pscustomobject]@{ Name = 'quant-engine'; Port = 9102; Bat = 'data\start-9102.bat'; Keys = @('QUANT_API_KEY','QUANT_ENGINE_API_KEY'); Log = 'data\quant-engine-9102.log' },
    [pscustomobject]@{ Name = 'data-query';   Port = 9103; Bat = 'data\start-9103.bat'; Keys = @();                                    Log = 'data\data-query-9103.log' }
)

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $line = "[{0}] [{1}] {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    Write-Host $line
    try {
        $dir = Split-Path -Parent $logPath
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        Add-Content -Path $logPath -Value $line -Encoding utf8
    } catch {
        Write-Warning "写看门狗日志失败：$_"
    }
}

function Enter-WatchdogLock {
    try {
        $dir = Split-Path -Parent $lockPath
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        $stream = [System.IO.File]::Open($lockPath, 'CreateNew', 'Write', 'None')
        $writer = New-Object System.IO.StreamWriter($stream)
        $writer.Write((Get-Date).ToString('o'))
        $writer.Flush()
        $writer.Dispose()
        return $true
    } catch [System.IO.IOException] {
        $age = $null
        try { $age = (Get-Date) - (Get-Item $lockPath).LastWriteTime } catch { }
        if ($age -and $age.TotalMinutes -gt $staleLockMinutes) {
            Write-Log "发现残留看门狗锁（$([int]$age.TotalMinutes) 分钟前），接管" 'WARN'
            Remove-Item $lockPath -Force -ErrorAction SilentlyContinue
            return (Enter-WatchdogLock)
        }
        Write-Log '已有另一轮看门狗在运行，本次跳过' 'WARN'
        return $false
    }
}

function Exit-WatchdogLock {
    Remove-Item $lockPath -Force -ErrorAction SilentlyContinue
}

function Get-ExpectedKeys {
    # 从 .quant.env 读取声明了哪些 key（服务进程是否真的加载到由 health 断言）
    $keys = @{}
    if (-not (Test-Path $envFile)) { return $keys }
    foreach ($raw in Get-Content $envFile -Encoding utf8) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith('#')) { continue }
        $idx = $line.IndexOf('=')
        if ($idx -lt 1) { continue }
        $keys[$line.Substring(0, $idx).Trim()] = $line.Substring($idx + 1).Trim()
    }
    return $keys
}

function Test-ServiceHealth {
    param([int]$Port)
    try {
        $resp = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/v1/health" -TimeoutSec $ProbeTimeoutSec
        if ($resp.status -ne 'ok') { return @{ Ok = $false; Reason = "health.status=$($resp.status)"; Body = $resp } }
        return @{ Ok = $true; Reason = ''; Body = $resp }
    } catch {
        return @{ Ok = $false; Reason = $_.Exception.Message; Body = $null }
    }
}

function Get-PortOwner {
    param([int]$Port)
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if (-not $conn) { return $null }
        return Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
    } catch {
        return $null
    }
}

function Start-ServiceProcess {
    param([pscustomobject]$Service)
    $bat = Join-Path $root $Service.Bat
    if (-not (Test-Path $bat)) {
        Write-Log "启动脚本缺失：$bat" 'ERROR'
        return $false
    }
    Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', "`"$bat`"" -WindowStyle Hidden -WorkingDirectory $root | Out-Null
    Write-Log "已拉起 $($Service.Name)（$($Service.Bat)）"
    return $true
}

function Wait-ServiceHealth {
    param([pscustomobject]$Service, [int]$TimeoutSec)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 3
        if ((Test-ServiceHealth -Port $Service.Port).Ok) { return $true }
    }
    return $false
}

if (-not (Enter-WatchdogLock)) { exit 0 }

$expected = Get-ExpectedKeys
$problems = 0
try {
    foreach ($svc in $services) {
        $probe = Test-ServiceHealth -Port $svc.Port
        $authExpected = $false
        foreach ($keyName in $svc.Keys) {
            if ($expected.ContainsKey($keyName) -and $expected[$keyName]) { $authExpected = $true }
        }
        $authOk = $true
        if ($probe.Ok -and $authExpected) {
            $authOk = [bool]$probe.Body.authEnabled
        }

        if ($probe.Ok -and $authOk) {
            Write-Verbose "$($svc.Name) 健康（authEnabled=$($probe.Body.authEnabled) authSource=$($probe.Body.authSource)）"
            continue
        }

        $problems++
        if ($probe.Ok) {
            if ($null -eq $probe.Body.authEnabled) {
                Write-Log "$($svc.Name):$($svc.Port) health 未返回 authEnabled（版本 v$($probe.Body.version) 早于鉴权自检），无法断言鉴权，按需重启" 'WARN'
            } else {
                Write-Log "$($svc.Name):$($svc.Port) 鉴权未生效（.quant.env 声明了 $($svc.Keys -join ' / ') 但 health.authEnabled=$($probe.Body.authEnabled)），需用 start-910N.bat 重启" 'WARN'
            }
        } else {
            Write-Log "$($svc.Name):$($svc.Port) 健康检查失败：$($probe.Reason)" 'WARN'
        }

        if ($DryRun) {
            Write-Log "$($svc.Name): DryRun，跳过重启" 'WARN'
            continue
        }

        $owner = Get-PortOwner -Port $svc.Port
        if ($owner) {
            if ($owner.ProcessName -like 'python*' -and -not $NoKill) {
                Write-Log "$($svc.Name): 终止僵死进程 pid=$($owner.Id) ($($owner.ProcessName))" 'WARN'
                Stop-Process -Id $owner.Id -Force -ErrorAction SilentlyContinue
                Start-Sleep -Seconds 2
            } else {
                Write-Log "$($svc.Name): 端口被 $($owner.ProcessName)(pid=$($owner.Id)) 占用，非 python，跳过杀戮" 'ERROR'
                continue
            }
        }

        if (-not (Start-ServiceProcess -Service $svc)) { continue }
        if (Wait-ServiceHealth -Service $svc -TimeoutSec $RecoverTimeoutSec) {
            $after = Test-ServiceHealth -Port $svc.Port
            if ($authExpected -and -not $after.Body.authEnabled) {
                # 重启后鉴权仍未生效：说明 .quant.env 里没有这个 key 或 .bat 没能加载它
                Write-Log "$($svc.Name): 进程已恢复但鉴权仍未生效（authEnabled=$($after.Body.authEnabled)），请检查 .quant.env 与 $($svc.Bat)" 'ERROR'
            } else {
                Write-Log "$($svc.Name): 已恢复（authEnabled=$($after.Body.authEnabled) authSource=$($after.Body.authSource)）"
            }
        } else {
            Write-Log "$($svc.Name): 重启后 $RecoverTimeoutSec 秒内仍未恢复，见 $($svc.Log)" 'ERROR'
        }
    }
} finally {
    Exit-WatchdogLock
}

if ($problems -gt 0) { exit 1 }
exit 0
