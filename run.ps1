$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$FrontendDir = Join-Path $ProjectDir "frontend"
$Vite = Join-Path $FrontendDir "node_modules\vite\bin\vite.js"
$RunsDir = Join-Path $ProjectDir "runs"
$DefaultApiPort = 8000
$DefaultWebPort = 5173
# 与 pyproject.toml 的 requires-python = ">=3.11,<3.14" 对齐，按新到旧依次尝试。
$SupportedPythons = @("3.13", "3.12", "3.11")
# vite 8.3.8 要求 ^20.19.0 || >=22.12.0，frontend/package.json 声明 >=22，取交集 22.12。
$MinNodeMajor = 22
$MinNodeMinor = 12
# 以 -NonInteractive 运行（CI、计划任务）时失败直接退出，不等回车。
$Interactive = [Environment]::UserInteractive -and
    -not ([Environment]::GetCommandLineArgs() -match '^-NonI')

Set-Location $ProjectDir
New-Item -ItemType Directory -Force -Path $RunsDir | Out-Null

# Load local secrets for the child API process without committing them.
$DotEnv = Join-Path $ProjectDir ".env"
if (-not (Test-Path -LiteralPath $DotEnv)) {
    # 不自动复制：.env 里放的是密钥，由用户自己决定写什么。没有 .env 也能启动——
    # 「简报核验」内置样例不调用模型，API Key 也可以在页面侧栏保存。
    Write-Host "[Data Analysis] 未找到 .env。需要调用模型时，可复制模板后填入自己的 API Key：" -ForegroundColor Yellow
    Write-Host "    Copy-Item .env.example .env   （再编辑 .env 里的 DEEPSEEK_API_KEY）" -ForegroundColor Yellow
    Write-Host "    也可以启动后在页面左侧「分析引擎」里填写 API Key。" -ForegroundColor Yellow
}
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

function Invoke-Quiet {
    # 调原生程序，只取退出码与标准输出。PS 5.1 在 $ErrorActionPreference = "Stop"
    # 下会把原生程序写到 stderr 的一行当作终止错误抛出（实测 py -3.12 未安装时
    # 直接抛出英文 "No suitable Python runtime found"），因此这里临时放宽并兜底。
    param([string]$FilePath, [string[]]$Arguments = @())
    $Previous = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    try {
        $Output = & $FilePath @Arguments 2>$null
        return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = (@($Output) -join "`n") }
    } catch {
        return [pscustomobject]@{ ExitCode = -1; Output = "" }
    } finally {
        $ErrorActionPreference = $Previous
    }
}

function Find-SupportedPython {
    # 返回用于创建 .venv 的解释器：先用 py 启动器按 3.13/3.12/3.11 依次尝试，
    # 再看 PATH 上的 python.exe 是否在支持区间（跳过应用商店占位程序）。都没有返回 $null。
    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        foreach ($Version in $SupportedPythons) {
            $Probe = Invoke-Quiet "py.exe" @("-$Version", "-c", "import sys")
            if ($Probe.ExitCode -eq 0) {
                return [pscustomobject]@{ Version = $Version; Launcher = "py.exe"; Prefix = @("-$Version") }
            }
        }
    }
    $PathPython = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($PathPython -and $PathPython.Source -notmatch "WindowsApps") {
        $Probe = Invoke-Quiet $PathPython.Source @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")
        $Found = $Probe.Output.Trim()
        if ($Probe.ExitCode -eq 0 -and $SupportedPythons -contains $Found) {
            return [pscustomobject]@{ Version = $Found; Launcher = $PathPython.Source; Prefix = @() }
        }
    }
    return $null
}

function Test-NodeVersion([string]$VersionText) {
    # 解析 node --version 的 "v24.22.1"，判断是否 >= 22.12。
    if ($VersionText -notmatch '^v?(\d+)\.(\d+)') { return $false }
    $Major = [int]$Matches[1]
    $Minor = [int]$Matches[2]
    return ($Major -gt $MinNodeMajor) -or ($Major -eq $MinNodeMajor -and $Minor -ge $MinNodeMinor)
}

function Write-NetworkHint([string]$What) {
    # 只给建议，不替用户改 pip / npm 的全局配置。
    Write-Host ""
    Write-Host "[Data Analysis] $($What)失败，常见原因是网络无法访问软件源。可以尝试：" -ForegroundColor Yellow
    Write-Host "  1. 检查网络；如需代理，先在本窗口设置 HTTP_PROXY / HTTPS_PROXY。" -ForegroundColor Yellow
    Write-Host "  2. 临时换国内镜像（只对这一次命令生效，不改全局配置）：" -ForegroundColor Yellow
    Write-Host '     .\.venv\Scripts\python.exe -m pip install -e ".[dev]" -i https://mirrors.aliyun.com/pypi/simple/' -ForegroundColor Yellow
    Write-Host "     cd frontend; npm install --registry=https://registry.npmmirror.com" -ForegroundColor Yellow
    Write-Host "  3. 比赛或演示现场请提前在有网时启动一次；依赖装好后再启动不需要联网。" -ForegroundColor Yellow
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
        $BasePython = Find-SupportedPython
        if (-not $BasePython) {
            throw ("未找到受支持的 Python（需要 3.11、3.12 或 3.13）。请从 " +
                "https://www.python.org/downloads/ 安装 Python 3.13，安装时勾选 " +
                "「Add python.exe to PATH」和「py launcher」，然后重新双击 run.bat。")
        }
        Write-Host "[Data Analysis] 使用 Python $($BasePython.Version) 创建虚拟环境 .venv ..." -ForegroundColor Cyan
        $Created = Invoke-Quiet $BasePython.Launcher (@($BasePython.Prefix) + @("-m", "venv", ".venv"))
        if ($Created.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $Python)) {
            throw ("创建虚拟环境失败。请确认项目目录可写，删除残留的 .venv 文件夹后重试；" +
                "也可以手动执行：py -$($BasePython.Version) -m venv .venv")
        }
    }

    $Imports = Invoke-Quiet $Python @("-c", "import data_agent, fastapi, uvicorn")
    if ($Imports.ExitCode -ne 0) {
        Write-Host "[Data Analysis] 正在安装后端依赖（首次需要联网，约几分钟）..." -ForegroundColor Cyan
        $PipExit = 0
        try {
            & $Python -m pip install -e ".[dev]"
            $PipExit = $LASTEXITCODE
        } catch {
            # PS 5.1 下 pip 写 stderr 也会被当成终止错误，统一按失败处理。
            $PipExit = -1
        }
        if ($PipExit -ne 0) {
            Write-NetworkHint "安装后端依赖"
            throw "后端依赖安装失败（pip 退出码 $PipExit），按上面的提示处理后重新双击 run.bat。"
        }
    }

    $NodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
    if (-not $NodeCommand) {
        throw ("未找到 Node.js。请从 https://nodejs.org/ 安装 22 LTS 或更高版本" +
            "（至少 $MinNodeMajor.$MinNodeMinor），装好后重新打开窗口再双击 run.bat。")
    }
    $Node = $NodeCommand.Source
    $NodeVersion = (Invoke-Quiet $Node @("--version")).Output.Trim()
    if (-not (Test-NodeVersion $NodeVersion)) {
        throw ("当前 Node.js 版本为 $NodeVersion，前端需要 $MinNodeMajor.$MinNodeMinor 或更高。" +
            "请从 https://nodejs.org/ 升级到 22 LTS 后重新双击 run.bat。")
    }
    if (-not (Test-Path -LiteralPath $Vite)) {
        Write-Host "[Data Analysis] 正在安装前端依赖（首次需要联网）..." -ForegroundColor Cyan
        Push-Location $FrontendDir
        try {
            $NpmExit = 0
            try {
                & npm install
                $NpmExit = $LASTEXITCODE
            } catch {
                $NpmExit = -1
            }
            if ($NpmExit -ne 0) {
                Write-NetworkHint "安装前端依赖"
                throw "前端依赖安装失败（npm 退出码 $NpmExit），按上面的提示处理后重新双击 run.bat。"
            }
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
    Write-Host "`n[Data Analysis] 启动失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "[Data Analysis] 服务日志在 runs\api.stderr.log 与 runs\frontend.stderr.log。" -ForegroundColor Red
    # 双击运行时窗口会立刻关闭，交互模式下停一下让人看清原因；非交互直接退出。
    if ($Interactive) { Read-Host "按回车键关闭窗口" | Out-Null }
    exit 1
}
