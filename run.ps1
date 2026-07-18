$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$FrontendDir = Join-Path $ProjectDir "frontend"
$Vite = Join-Path $FrontendDir "node_modules\vite\bin\vite.js"
$RunsDir = Join-Path $ProjectDir "runs"
$ApiUrl = "http://127.0.0.1:8000"
$WebUrl = "http://127.0.0.1:5173"

Set-Location $ProjectDir
New-Item -ItemType Directory -Force -Path $RunsDir | Out-Null

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

    $ApiHealthy = $false
    try {
        $health = Invoke-WebRequest -UseBasicParsing "$ApiUrl/api/health" -TimeoutSec 2
        $ApiHealthy = $health.StatusCode -eq 200
    } catch {}

    if (-not $ApiHealthy) {
        Write-Host "[Data Analysis] Starting API on port 8000..." -ForegroundColor Cyan
        Start-Process -FilePath $Python `
            -ArgumentList @("-m", "uvicorn", "data_agent.api:app", "--host", "127.0.0.1", "--port", "8000") `
            -WorkingDirectory $ProjectDir `
            -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $RunsDir "api.stdout.log") `
            -RedirectStandardError (Join-Path $RunsDir "api.stderr.log") | Out-Null
    }

    $WebHealthy = $false
    try {
        $web = Invoke-WebRequest -UseBasicParsing $WebUrl -TimeoutSec 2
        $WebHealthy = $web.StatusCode -eq 200
    } catch {}

    if (-not $WebHealthy) {
        Write-Host "[Data Analysis] Starting frontend on port 5173..." -ForegroundColor Cyan
        Start-Process -FilePath $Node `
            -ArgumentList @($Vite, "--host", "127.0.0.1", "--port", "5173", "--strictPort") `
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
