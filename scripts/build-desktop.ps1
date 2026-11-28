<#
.SYNOPSIS
    编译 Data Analysis Agent 原生 Windows 桌面程序（基于系统内置 WebView2，无黑框、无浏览器标签页）。
.DESCRIPTION
    使用 Windows 内置的 .NET Framework csc.exe 与微软官方 Microsoft.Web.WebView2 程序集编译
    desktop-app\Data Analysis Agent.exe。关闭窗口时自动通过 Windows Job Object 清理后台本地服务进程。
.PARAMETER DesktopShortcut
    同时在当前用户桌面创建快捷方式。
.PARAMETER RebuildFrontend
    强制重新执行 frontend 的 npm run build。
#>
param(
    [switch]$RebuildFrontend,
    [switch]$DesktopShortcut
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$OutDir = Join-Path $RepoRoot "desktop-app"
$ExeName = "Data Analysis Agent.exe"
$ExePath = Join-Path $OutDir $ExeName
$SourceFile = Join-Path $PSScriptRoot "desktop\DesktopHost.cs"
$IconFile = Join-Path $PSScriptRoot "desktop\app.ico"
$WebView2Version = "1.0.4370.26"

# 确保前端静态产物存在（桌面版由 FastAPI 单端口直接托管，无需 Vite 开发服务器）
$FrontendDist = Join-Path $RepoRoot "frontend\dist\index.html"
if ($RebuildFrontend -or -not (Test-Path -LiteralPath $FrontendDist)) {
    Write-Host "[Desktop] 构建前端生产产物 (frontend/dist)..." -ForegroundColor Cyan
    Push-Location (Join-Path $RepoRoot "frontend")
    try {
        if (-not (Test-Path -LiteralPath "node_modules")) {
            & npm install
            if ($LASTEXITCODE -ne 0) { throw "前端 npm install 失败" }
        }
        & npm run build
        if ($LASTEXITCODE -ne 0) { throw "前端 npm run build 失败" }
    } finally {
        Pop-Location
    }
}

# 下载并缓存微软官方 Microsoft.Web.WebView2 程序集（仅首次联网，约 2 MB）
$CacheDir = Join-Path $env:LOCALAPPDATA "xiaomao-desktop-cache\webview2\$WebView2Version"
$CoreDll = Join-Path $CacheDir "Microsoft.Web.WebView2.Core.dll"
$WinFormsDll = Join-Path $CacheDir "Microsoft.Web.WebView2.WinForms.dll"
$LoaderDll = Join-Path $CacheDir "WebView2Loader.dll"

if (-not ((Test-Path -LiteralPath $CoreDll) -and (Test-Path -LiteralPath $WinFormsDll) -and (Test-Path -LiteralPath $LoaderDll))) {
    Write-Host "[Desktop] 下载 Microsoft.Web.WebView2 $WebView2Version 程序集..." -ForegroundColor Cyan
    New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
    $NupkgUrl = "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/$WebView2Version/microsoft.web.webview2.$WebView2Version.nupkg"
    $ZipPath = Join-Path $CacheDir "webview2.zip"
    $ExtractDir = Join-Path $CacheDir "pkg"
    Invoke-WebRequest -UseBasicParsing -Uri $NupkgUrl -OutFile $ZipPath
    if (Test-Path -LiteralPath $ExtractDir) { Remove-Item -Recurse -Force $ExtractDir }
    Expand-Archive -Path $ZipPath -DestinationPath $ExtractDir -Force

    $NetLib = Get-ChildItem -Path (Join-Path $ExtractDir "lib") -Directory |
        Where-Object { $_.Name -match '^net4[5-8]' } |
        Sort-Object Name -Descending |
        Select-Object -First 1
    if (-not $NetLib) { throw "在 WebView2 包中未找到 net4x 程序集目录" }

    Copy-Item -Force (Join-Path $NetLib.FullName "Microsoft.Web.WebView2.Core.dll") $CoreDll
    Copy-Item -Force (Join-Path $NetLib.FullName "Microsoft.Web.WebView2.WinForms.dll") $WinFormsDll
    Copy-Item -Force (Join-Path $ExtractDir "runtimes\win-x64\native\WebView2Loader.dll") $LoaderDll
    Remove-Item -Force $ZipPath
    Remove-Item -Recurse -Force $ExtractDir
}

# 使用 Windows 内置 .NET Framework csc.exe 编译原生桌面程序
$Csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path -LiteralPath $Csc)) {
    throw "未找到 Windows .NET Framework 编译器：$Csc"
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
Copy-Item -Force $CoreDll $OutDir
Copy-Item -Force $WinFormsDll $OutDir
Copy-Item -Force $LoaderDll $OutDir

$CscArgs = @(
    "/nologo",
    "/target:winexe",
    "/platform:x64",
    "/optimize+",
    "/utf8output",
    "/out:$ExePath",
    "/r:System.dll",
    "/r:System.Drawing.dll",
    "/r:System.Windows.Forms.dll",
    "/r:System.Net.Http.dll",
    "/r:$CoreDll",
    "/r:$WinFormsDll"
)
if (Test-Path -LiteralPath $IconFile) {
    $CscArgs += "/win32icon:$IconFile"
}
$CscArgs += $SourceFile

Write-Host "[Desktop] 编译 $ExeName ..." -ForegroundColor Cyan
& $Csc @CscArgs
if ($LASTEXITCODE -ne 0) {
    throw "编译失败（csc 退出码 $LASTEXITCODE）"
}

Write-Host "[Desktop] 构建完成：$ExePath" -ForegroundColor Green

if ($DesktopShortcut) {
    $Desktop = [Environment]::GetFolderPath("Desktop")
    $LnkPath = Join-Path $Desktop "Data Analysis Agent.lnk"
    $Wsh = New-Object -ComObject WScript.Shell
    $Shortcut = $Wsh.CreateShortcut($LnkPath)
    $Shortcut.TargetPath = $ExePath
    $Shortcut.WorkingDirectory = $OutDir
    if (Test-Path -LiteralPath $IconFile) { $Shortcut.IconLocation = $IconFile }
    $Shortcut.Description = "Data Analysis Agent — LLM 数据分析工作台"
    $Shortcut.Save()
    Write-Host "[Desktop] 已创建桌面快捷方式：$LnkPath" -ForegroundColor Green
}
