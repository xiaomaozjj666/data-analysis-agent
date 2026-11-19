#Requires -Version 7.0
<#
.SYNOPSIS
    定时 ping 健康检查地址，降低 Render Free 实例被休眠的概率。

.DESCRIPTION
    Render Free 档约 15 分钟无请求后休眠，冷启动通常需要 35–90 秒。
    本脚本按固定间隔请求 -Url，打印每次的状态码与耗时。
    单次失败不会退出，下一轮继续；按 Ctrl+C 结束循环。

    -Once 只执行一轮，便于手动验证或交给计划任务：
    返回 2xx 时退出码 0，其余情况退出码 1。

.PARAMETER Url
    要探测的地址，默认指向本项目在 Render 上的健康检查端点。

.PARAMETER IntervalSeconds
    两轮探测之间的间隔秒数，默认 240 秒（4 分钟）。

.PARAMETER TimeoutSeconds
    单次请求的超时秒数，默认 90 秒，需覆盖冷启动时间。

.PARAMETER Once
    只探测一次后退出，不进入循环。

.EXAMPLE
    .\scripts\keepalive.ps1 -Once

.EXAMPLE
    .\scripts\keepalive.ps1 -IntervalSeconds 300 -TimeoutSeconds 120
#>
[CmdletBinding()]
param(
    [string]$Url = "https://data-analysis-agent-wikz.onrender.com/api/health",

    [ValidateRange(1, 86400)]
    [int]$IntervalSeconds = 240,

    [ValidateRange(1, 600)]
    [int]$TimeoutSeconds = 90,

    [switch]$Once
)

$ErrorActionPreference = "Stop"
# 进度条会让 Invoke-WebRequest 在大响应上明显变慢，也会污染终端输出。
$ProgressPreference = "SilentlyContinue"

if (-not [uri]::IsWellFormedUriString($Url, [UriKind]::Absolute)) {
    Write-Host "[错误] -Url 不是合法的绝对地址：$Url"
    exit 2
}

function Invoke-HealthPing {
    # 发一次 GET，返回统一结构；任何异常都转成失败结果，不向上抛。
    param(
        [string]$Target,
        [int]$Timeout
    )

    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        # -SkipHttpErrorCheck：4xx/5xx 也返回响应对象，便于打印真实状态码。
        $response = Invoke-WebRequest -Uri $Target -Method Get `
            -TimeoutSec $Timeout -SkipHttpErrorCheck -ErrorAction Stop
        $watch.Stop()
        $status = [int]$response.StatusCode
        return [pscustomobject]@{
            Ok         = ($status -ge 200 -and $status -lt 300)
            StatusCode = $status
            Seconds    = [math]::Round($watch.Elapsed.TotalSeconds, 2)
            Error      = $null
        }
    }
    catch {
        $watch.Stop()
        return [pscustomobject]@{
            Ok         = $false
            StatusCode = $null
            Seconds    = [math]::Round($watch.Elapsed.TotalSeconds, 2)
            Error      = $_.Exception.Message
        }
    }
}

function Write-PingResult {
    # 每轮输出：本地时间、判定、状态码、耗时；失败时补一行原因。
    param(
        [pscustomobject]$Result,
        [string]$Target
    )

    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    if ($null -ne $Result.StatusCode) {
        $state = if ($Result.Ok) { "正常" } else { "异常" }
        Write-Host ("[{0}] {1}  状态码 {2}  耗时 {3:N2} 秒  {4}" -f `
            $stamp, $state, $Result.StatusCode, $Result.Seconds, $Target)
    }
    else {
        Write-Host ("[{0}] [失败]  未取得状态码  耗时 {1:N2} 秒  {2}" -f `
            $stamp, $Result.Seconds, $Target)
    }
    if ($Result.Error) {
        Write-Host ("        原因：{0}" -f $Result.Error)
    }
}

Write-Host ("保活开始：{0}" -f $Url)
Write-Host ("间隔 {0} 秒，单次超时 {1} 秒；失败会继续下一轮，按 Ctrl+C 结束。" -f `
    $IntervalSeconds, $TimeoutSeconds)

while ($true) {
    $result = Invoke-HealthPing -Target $Url -Timeout $TimeoutSeconds
    Write-PingResult -Result $result -Target $Url

    if ($Once) {
        # 单轮模式用退出码表达结果，交给调用方判断。
        if ($result.Ok) { exit 0 } else { exit 1 }
    }

    # 冷启动可能超过一轮的时间窗，失败不退出，下一轮继续探测。
    Start-Sleep -Seconds $IntervalSeconds
}
