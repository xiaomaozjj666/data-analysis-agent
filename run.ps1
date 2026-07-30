$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$FrontendDir = Join-Path $ProjectDir "frontend"
$Vite = Join-Path $FrontendDir "node_modules\vite\bin\vite.js"
$RunsDir = Join-Path $ProjectDir "runs"
$DefaultApiPort = 8000
$DefaultWebPort = 5173

Set-Location $ProjectDir
New-Item -ItemType Directory -Force -Path $RunsDir | Out-Null

# Load local secrets for the child API process without committing them.
$DotEnv = Join-Path $ProjectDir ".env"
if (Test-Path -LiteralPath $DotEnv) {
    foreach ($Line in Get-Content -LiteralPath $DotEnv) {
        if ($Line -match '^\s*([^#=][^=]*)=(.*)$') {
            Set-Item -Path "Env:$($Matches[1].Trim())" -Value $Matches[2].Trim()
        }
    }
}

function Test-PortBindable([int]$Port) {
    # 用 Python 尝试 bind，检测端口是否真正可用（区分 zombie socket 和真正空闲）。
    # Get-NetTCPConnection 对 zombie socket 仍显示 LISTEN，但 bind 会成功或失败
    # 才是判断端口是否可用的 ground truth。
    # 注意：脚本顶层设了 $ErrorActionPreference = "Stop"，Python bind 失败时向
    # stderr 写入 traceback，PowerShell 会把它当成错误记录并抛出异常。必须
    # 临时切换为 SilentlyContinue 并用 try-catch 兜底，否则外层 catch 会
    # 捕获到 "Traceback (most recent call last):" 并误判为启动失败。
    try {
        $ErrorActionPreference = "SilentlyContinue"
        & $Python -c "import socket,sys; s=socket.socket(); s.bind(('127.0.0.1',$Port)); s.close()" 2>$null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Find-UsablePort([int]$Start, [int]$Max = 10) {
    for ($i = 0; $i -lt $Max; $i++) {
        $port = $Start + $i
        if (Test-PortBindable $port) { return $port }
    }
    return 0
}

function Stop-StaleApiProcess([int]$Port) {
    # 端口被占用但不健康：可能是上次 uvicorn 残留进程。清理后才能 bind。
    $StaleConn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $StaleConn) { return }

    foreach ($Pid_ in $StaleConn.OwningProcess) {
        $proc = Get-Process -Id $Pid_ -ErrorAction SilentlyContinue
        if ($proc -and $proc.ProcessName -in @("python", "pythonw", "uvicorn")) {
            Write-Host "[Data Analysis] Cleaning up stale API process (PID $Pid_) on port $Port..." -ForegroundColor Yellow
            # 用 taskkill /T 优雅关闭进程树，避免 Stop-Process -Force 造成
            # zombie socket：强制终止时 socket 句柄未被正常关闭，Windows 内核
            # 会保留 LISTEN 条目数分钟，导致新进程 bind 失败。
            & taskkill /PID $Pid_ /T 2>$null | Out-Null
            Start-Sleep -Seconds 2
            # 仍存活才强制终止
            if (Get-Process -Id $Pid_ -ErrorAction SilentlyContinue) {
                Stop-Process -Id $Pid_ -Force -ErrorAction SilentlyContinue
                Start-Sleep -Seconds 1
            }
        }
    }
}

try {
    if (-not (Test-Path -LiteralPath $Python)) {
        Write-Host "[Data Analysis] Creating Python environment..." -ForegroundColor Cyan
        py -3.13 -m venv .venv
    }

    & $Python -c "import data_agent, fastapi, uvicorn" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[Data Analysis] Installing backend dependencies..." -ForegroundColor Cyan
        & $Python -m pip install -e ".[dev]"
        if ($LASTEXITCODE -ne 0) { throw "Backend dependency installation failed" }
    }

    $NodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
    if (-not $NodeCommand) { throw "Node.js was not found. Install Node.js 20 or newer." }
    $Node = $NodeCommand.Source
    if (-not (Test-Path -LiteralPath $Vite)) {
        Write-Host "[Data Analysis] Installing frontend dependencies..." -ForegroundColor Cyan
        Push-Location $FrontendDir
        try {
            npm install
            if ($LASTEXITCODE -ne 0) { throw "Frontend dependency installation failed" }
        } finally {
            Pop-Location
        }
    }

    # ---- API 端口选择与启动 ----
    # 策略：尝试启动 uvicorn → 验证健康 → 失败则换端口重试。
    # 仅靠 Test-PortBindable 预检测不可靠：Python socket close 后端口
    # 可能瞬间不可用，或 zombie socket 处于过渡态导致假阳性，uvicorn
    # 随后 bind 失败。启动后验证健康才是 ground truth。
    $ApiPort = $DefaultApiPort
    $ApiUrl = "http://127.0.0.1:$ApiPort"
    $ApiReady = $false

    # 1. 先检查默认端口是否已有健康的 API（复用）
    try {
        $health = Invoke-WebRequest -UseBasicParsing "$ApiUrl/api/health" -TimeoutSec 2
        if ($health.StatusCode -eq 200) { $ApiReady = $true }
    } catch {}

    # 2. 没有健康 API 则尝试启动，最多尝试 10 个端口
    if (-not $ApiReady) {
        $StderrLog = Join-Path $RunsDir "api.stderr.log"
        for ($Attempt = 0; $Attempt -lt 10; $Attempt++) {
            $TryPort = $DefaultApiPort + $Attempt
            $TryUrl = "http://127.0.0.1:$TryPort"

            # 清理残留进程
            Stop-StaleApiProcess $TryPort

            # 预检测：快速排除明显被占用的端口
            if (-not (Test-PortBindable $TryPort)) {
                Write-Host "[Data Analysis] Port $TryPort is unavailable, trying next..." -ForegroundColor Yellow
                continue
            }

            Write-Host "[Data Analysis] Starting API on port $TryPort..." -ForegroundColor Cyan
            Start-Process -FilePath $Python `
                -ArgumentList @("-m", "uvicorn", "data_agent.api:app", "--host", "127.0.0.1", "--port", "$TryPort") `
                -WorkingDirectory $ProjectDir `
                -WindowStyle Hidden `
                -RedirectStandardOutput (Join-Path $RunsDir "api.stdout.log") `
                -RedirectStandardError $StderrLog | Out-Null

            # 等待启动并验证健康（最多 8 秒）
            $Started = $false
            for ($Wait = 0; $Wait -lt 16; $Wait++) {
                Start-Sleep -Milliseconds 500
                try {
                    $health = Invoke-WebRequest -UseBasicParsing "$TryUrl/api/health" -TimeoutSec 2
                    if ($health.StatusCode -eq 200) { $Started = $true; break }
                } catch {}
            }

            if ($Started) {
                $ApiPort = $TryPort
                $ApiUrl = $TryUrl
                $ApiReady = $true
                break
            }

            # 健康检查失败：读取日志判断原因
            $StderrContent = ""
            if (Test-Path -LiteralPath $StderrLog) {
                $StderrContent = Get-Content -LiteralPath $StderrLog -Raw -ErrorAction SilentlyContinue
            }
            if ($StderrContent -match "10048|Errno.*bind|address.*already.*in.*use") {
                Write-Host "[Data Analysis] Port $TryPort bind failed (zombie socket or race), trying next port..." -ForegroundColor Yellow
                Stop-StaleApiProcess $TryPort
                continue
            }

            # 非 bind 错误（如依赖缺失、配置错误）：打印日志并退出
            Get-Content -LiteralPath $StderrLog -Tail 30 -ErrorAction SilentlyContinue
            throw "API failed to start on port $TryPort (see log above)"
        }
    }

    if (-not $ApiReady) {
        throw "Failed to start API on any port in range $DefaultApiPort-$($DefaultApiPort + 9)"
    }

    # ---- 前端启动 ----
    # 设置 VITE_API_URL 让前端连接到实际 API 端口（vite 在启动时读取环境变量）
    $Env:VITE_API_URL = $ApiUrl
    $WebUrl = "http://127.0.0.1:$DefaultWebPort"

    $WebHealthy = $false
    try {
        $web = Invoke-WebRequest -UseBasicParsing $WebUrl -TimeoutSec 2
        $WebHealthy = $web.StatusCode -eq 200
    } catch {}

    if (-not $WebHealthy) {
        Write-Host "[Data Analysis] Starting frontend on port $DefaultWebPort..." -ForegroundColor Cyan
        Start-Process -FilePath $Node `
            -ArgumentList @($Vite, "--host", "127.0.0.1", "--port", "$DefaultWebPort", "--strictPort") `
            -WorkingDirectory $FrontendDir `
            -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $RunsDir "frontend.stdout.log") `
            -RedirectStandardError (Join-Path $RunsDir "frontend.stderr.log") | Out-Null
    }

    $Ready = $false
    for ($Attempt = 0; $Attempt -lt 60; $Attempt++) {
        Start-Sleep -Seconds 1
        try {
            $api = Invoke-WebRequest -UseBasicParsing "$ApiUrl/api/health" -TimeoutSec 2
            $web = Invoke-WebRequest -UseBasicParsing $WebUrl -TimeoutSec 2
            if ($api.StatusCode -eq 200 -and $web.StatusCode -eq 200) {
                $Ready = $true
                break
            }
        } catch {}
    }

    if (-not $Ready) {
        Get-Content -LiteralPath (Join-Path $RunsDir "api.stderr.log") -Tail 30 -ErrorAction SilentlyContinue
        Get-Content -LiteralPath (Join-Path $RunsDir "frontend.stderr.log") -Tail 30 -ErrorAction SilentlyContinue
        throw "Frontend or API did not become healthy within 60 seconds"
    }

    Write-Host "[Data Analysis] API ready: $ApiUrl" -ForegroundColor Green
    Write-Host "[Data Analysis] Frontend ready: $WebUrl" -ForegroundColor Green
    Start-Process $WebUrl
} catch {
    Write-Host "`nStartup error: $($_.Exception.Message)" -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}
