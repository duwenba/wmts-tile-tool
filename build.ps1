# ============================================================================
#  wmts-tile-tool 一键构建 / 部署脚本（Windows 版）
# ----------------------------------------------------------------------------
#  在任意一台新 Windows 机器上克隆仓库后，只需运行本脚本即可完成全部构建：
#    1. 检查前置依赖（uv / Rust 工具链）
#    2. 安装 Python 依赖（uv sync，Windows 自动使用 PyPI 官方 wxPython wheel）
#    3. 编译 Rust 合并引擎（cargo build --release，产物 merge_rs.exe）
#    4. 构建自检（Rust 二进制 --help、Python 核心依赖可导入）
#  脚本可重复执行（幂等），中断后直接重跑即可。
#
#  用法（在 PowerShell 中，仓库根目录）：
#    .\build.ps1                完整构建（默认）
#    .\build.ps1 -Clean         强制全量重建（重装依赖 + 清理 cargo 产物）
#    .\build.ps1 -SkipRust      只构建 Python 环境（跳过 Rust 编译）
#    .\build.ps1 -SkipUv        只编译 Rust 引擎（跳过 Python 依赖）
#    .\build.ps1 -NoCheck       跳过构建后的自检（更快）
#    .\build.ps1 -Help          显示帮助
#
#  注意：若 PowerShell 提示“无法加载脚本”，先执行：
#    Set-ExecutionPolicy -Scope Process Bypass
#  或右键脚本文件 → 使用 PowerShell 运行。
# ============================================================================

[CmdletBinding()]
param(
    [switch]$Clean,
    [switch]$SkipRust,
    [switch]$SkipUv,
    [switch]$NoCheck,
    [switch]$Help
)

$ErrorActionPreference = 'Stop'

# ---------- 基础路径（脚本可从任意目录调用） ----------
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

if ($Help) {
    Get-Content $MyInvocation.MyCommand.Path | Where-Object {
        $_ -match '^\s*#\s' -and $_ -notmatch '^#\s*='
    } | ForEach-Object { $_ -replace '^\s*#\s?', '' }
    exit 0
}

# ---------- 输出工具 ----------
function Info  { Write-Host "[INFO] $args" -ForegroundColor Cyan }
function Ok    { Write-Host "[ OK ] $args" -ForegroundColor Green }
function Warn  { Write-Host "[WARN] $args" -ForegroundColor Yellow }
function Die   { Write-Host "[FAIL] $args" -ForegroundColor Red; exit 1 }
function Step  { param($n, $total, $text)
    Write-Host ""
    Write-Host "==> [$n/$total] $text" -ForegroundColor White -BackgroundColor DarkBlue
}

# 运行命令并检查退出码（PowerShell 5.1 原生命令不抛异常）
function Invoke-Checked {
    param([string]$File, [string[]]$ArgumentList)
    & $File @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        Die "命令失败（退出码 $LASTEXITCODE）: $File $($ArgumentList -join ' ')"
    }
}

# ---------- 中断处理 ----------
trap {
    Write-Host ""
    Warn "构建中断，已保存的进度不会丢失，重跑本脚本即可继续。"
    exit 130
}

$StartTime = Get-Date
$TotalSteps = 3 - @($SkipUv, $SkipRust).Where({ $_ }).Count
$DoneSteps = 0

# ---------- 1. 前置依赖检查 ----------
$DoneSteps++
Step $DoneSteps $TotalSteps "检查前置依赖"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Die "未找到 uv（Python 包管理器）。安装方法：powershell -ExecutionPolicy ByPass -c 'irm https://astral.sh/uv/install.ps1 | iex' 或 winget install astral-sh.uv"
}
if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
    Die "未找到 cargo（Rust 工具链）。安装方法：https://rustup.rs 下载 rustup-init.exe 并运行（默认 MSVC 工具链）"
}
if (-not (Get-Command rustc -ErrorAction SilentlyContinue)) {
    Die "未找到 rustc（Rust 编译器）。请通过 rustup 安装完整工具链。"
}

Info "uv $(uv --version)"
Info "cargo $(cargo --version)"
Info "rustc $(rustc --version)"
Ok "前置依赖检查通过"

# ---------- 2. Python 依赖 ----------
if (-not $SkipUv) {
    $DoneSteps++
    Step $DoneSteps $TotalSteps "安装 Python 依赖（uv sync）"

    $UvArgs = @('sync')
    if ($Clean) {
        $UvArgs += '--reinstall'
        Info "clean 模式：强制重装全部依赖"
    }
    Invoke-Checked 'uv' $UvArgs
    Ok "Python 依赖就绪（.venv）"
}
else {
    Warn "已跳过 Python 依赖安装（-SkipUv）"
}

# ---------- 3. Rust 合并引擎 ----------
if (-not $SkipRust) {
    $DoneSteps++
    Step $DoneSteps $TotalSteps "编译 Rust 合并引擎（merge_rs）"

    $RustBin = Join-Path $ScriptDir 'merge_rs\target\release\merge_rs.exe'
    if ($Clean -and (Test-Path (Join-Path $ScriptDir 'merge_rs\target'))) {
        Info "clean 模式：清理旧的 cargo 构建产物"
        Invoke-Checked 'cargo' @('clean', '--manifest-path', 'merge_rs/Cargo.toml')
    }

    Info "cargo build --release（首次编译需数分钟，请耐心等待）"
    Invoke-Checked 'cargo' @('build', '--release', '--manifest-path', 'merge_rs/Cargo.toml')

    if (-not (Test-Path $RustBin)) {
        Die "Rust 引擎编译产物缺失: $RustBin"
    }
    Ok "Rust 引擎编译完成: $RustBin"
}
else {
    Warn "已跳过 Rust 编译（-SkipRust）"
    $RustBin = Join-Path $ScriptDir 'merge_rs\target\release\merge_rs.exe'
}

# ---------- 4. 构建自检 ----------
if (-not $NoCheck) {
    Write-Host ""
    Write-Host "==> 构建自检" -ForegroundColor White -BackgroundColor DarkBlue
    $Failed = $false

    if (-not $SkipRust) {
        $binOk = $false
        if (Test-Path $RustBin) {
            & $RustBin '--help' 2>$null | Out-Null
            $binOk = ($LASTEXITCODE -eq 0)
        }
        if ($binOk) {
            Ok "Rust 二进制可执行（$RustBin）"
        }
        else {
            Warn "Rust 二进制自检未通过（请检查构建日志）"
            $Failed = $true
        }
    }

    if (-not $SkipUv) {
        $wxVer = & uv run python -c "import wx; print(wx.__version__)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $wxVer) {
            Ok "Python 核心依赖 OK（wxPython $wxVer）"
        }
        else {
            Warn "Python 依赖导入自检未通过"
            $Failed = $true
        }
    }

    if ($Failed) { Die "自检存在告警，请根据上方输出排查。可用 -NoCheck 跳过自检。" }
}

# ---------- 完成 ----------
$Elapsed = [int]((Get-Date) - $StartTime).TotalSeconds
Write-Host ""
Write-Host "==> 构建完成 ✔（耗时 ${Elapsed}s）" -ForegroundColor White -BackgroundColor DarkBlue
Write-Host ""
Write-Host "  ▸ 启动图形界面:        uv run python gui_main.py"
Write-Host "  ▸ 下载瓦片(HTTP/2):    uv run python download_tiles_async.py"
Write-Host "  ▸ 拼接大图(Rust 引擎): uv run python merge_rs_cli.py"
Write-Host "  ▸ 缓存管理:            uv run python tile_cache.py stats"
Write-Host "  ▸ 参数配置:            编辑 config.py（下载范围/数据源）"
Write-Host "  ▸ 首次使用前记得在 config.py 中配置目标 WMTS 服务参数"
Write-Host ""
