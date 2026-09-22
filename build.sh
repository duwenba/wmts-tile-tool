#!/usr/bin/env bash
#
# ============================================================================
#  wmts-tile-tool 一键构建 / 部署脚本
# ----------------------------------------------------------------------------
#  在任意一台新机器上克隆仓库后，只需运行本脚本即可完成全部构建：
#    1. 检查前置依赖（uv / Rust 工具链）
#    2. 安装 Python 依赖（uv sync，含 wxPython 特殊 wheel）
#    3. 编译 Rust 合并引擎（cargo build --release）
#    4. 构建自检（Rust 二进制 --help、Python 核心依赖可导入）
#  脚本可重复执行（幂等），中断后直接重跑即可。
#
#  用法:
#    ./build.sh                 完整构建（默认）
#    ./build.sh --clean         强制全量重建（重装 Python 依赖 + cargo 增量之外）
#    ./build.sh --skip-rust     只构建 Python 环境（跳过 Rust 编译）
#    ./build.sh --skip-uv       只编译 Rust 引擎（跳过 Python 依赖）
#    ./build.sh --no-check      跳过构建后的自检（更快）
#    ./build.sh --help          显示帮助
# ============================================================================

set -euo pipefail

# ---------- 基础路径（脚本可从任意目录调用） ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---------- 参数 ----------
CLEAN=0
SKIP_UV=0
SKIP_RUST=0
NO_CHECK=0

usage() {
    sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

for arg in "$@"; do
    case "$arg" in
        --clean)     CLEAN=1 ;;
        --skip-rust) SKIP_RUST=1 ;;
        --skip-uv)   SKIP_UV=1 ;;
        --no-check)  NO_CHECK=1 ;;
        --help|-h)   usage ;;
        *) echo "未知参数: $arg（运行 $0 --help 查看用法）" >&2; exit 2 ;;
    esac
done

# ---------- 输出工具 ----------
C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
C_CYAN=$'\033[36m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'

info()  { printf "%s[INFO]%s %s\n" "$C_CYAN" "$C_RESET" "$*"; }
ok()    { printf "%s[ OK ]%s %s\n" "$C_GREEN" "$C_RESET" "$*"; }
warn()  { printf "%s[WARN]%s %s\n" "$C_YELLOW" "$C_RESET" "$*"; }
die()   { printf "%s[FAIL]%s %s\n" "$C_RED" "$C_RESET" "$*" >&2; exit 1; }
step()  { printf "\n%s==> %s%s\n" "$C_BOLD" "$*" "$C_RESET"; }

# ---------- 中断处理 ----------
trap 'printf "\n%s[WARN]%s 构建中断，已保存的进度不会丢失，重跑本脚本即可继续。\n" "$C_YELLOW" "$C_RESET"; exit 130' INT TERM

START_TIME=$(date +%s)
TOTAL_STEPS=$(( 3 - SKIP_UV - SKIP_RUST ))
DONE_STEPS=0

# ---------- 1. 前置依赖检查 ----------
DONE_STEPS=$((DONE_STEPS+1))
step "[$DONE_STEPS/$TOTAL_STEPS] 检查前置依赖"

command -v uv >/dev/null 2>&1 || die "未找到 uv（Python 包管理器）。安装方法：curl -LsSf https://astral.sh/uv/install.sh | sh"
command -v cargo >/dev/null 2>&1 || die "未找到 cargo（Rust 工具链）。安装方法：curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
command -v rustc >/dev/null 2>&1 || die "未找到 rustc（Rust 编译器）。请通过 rustup 安装完整工具链。"
command -v git  >/dev/null 2>&1 || warn "未找到 git，不影响构建（仅影响版本信息）。"

info "uv $(uv --version | head -1)"
info "cargo $(cargo --version 2>/dev/null || echo '?')"
info "rustc $(rustc --version 2>/dev/null || echo '?')"
ok "前置依赖检查通过"

# ---------- 2. Python 依赖 ----------
if [ "$SKIP_UV" -eq 0 ]; then
    DONE_STEPS=$((DONE_STEPS+1))
    step "[$DONE_STEPS/$TOTAL_STEPS] 安装 Python 依赖（uv sync）"

    UV_ARGS=()
    if [ "$CLEAN" -eq 1 ]; then
        UV_ARGS+=(--reinstall)
        info "clean 模式：强制重装全部依赖"
    fi
    uv sync "${UV_ARGS[@]}"
    ok "Python 依赖就绪（.venv）"
else
    warn "已跳过 Python 依赖安装（--skip-uv）"
fi

# ---------- 3. Rust 合并引擎 ----------
if [ "$SKIP_RUST" -eq 0 ]; then
    DONE_STEPS=$((DONE_STEPS+1))
    step "[$DONE_STEPS/$TOTAL_STEPS] 编译 Rust 合并引擎（merge_rs）"

    RUST_BIN="merge_rs/target/release/merge_rs"
    if [ "$CLEAN" -eq 1 ] && [ -d merge_rs/target ]; then
        info "clean 模式：清理旧的 cargo 构建产物"
        cargo clean --manifest-path merge_rs/Cargo.toml
    fi

    info "cargo build --release（首次编译需数分钟，请耐心等待）"
    cargo build --release --manifest-path merge_rs/Cargo.toml

    [ -x "$RUST_BIN" ] || die "Rust 引擎编译产物缺失: $RUST_BIN"
    ok "Rust 引擎编译完成: $RUST_BIN"
else
    warn "已跳过 Rust 编译（--skip-rust）"
    RUST_BIN="merge_rs/target/release/merge_rs"
fi

# ---------- 4. 构建自检 ----------
if [ "$NO_CHECK" -eq 0 ]; then
    step "构建自检"
    FAILED=0

    if [ "$SKIP_RUST" -eq 0 ]; then
        if [ -x "$RUST_BIN" ] && "$RUST_BIN" --help >/dev/null 2>&1; then
            ok "Rust 二进制可执行（$RUST_BIN）"
        else
            warn "Rust 二进制自检未通过（请检查构建日志）"
            FAILED=1
        fi
    fi

    if [ "$SKIP_UV" -eq 0 ]; then
        if uv run python -c "import wx, numpy, httpx, tqdm, aiofiles, fastapi, uvicorn; print('  Python 核心依赖 OK（wxPython', wx.__version__ + '）')" 2>/dev/null; then
            :
        else
            warn "Python 依赖导入自检未通过"
            FAILED=1
        fi
        # 核心层单元测试（不联网）
        if uv run pytest -q >/dev/null 2>&1; then
            ok "核心层单元测试通过（pytest）"
        else
            warn "单元测试未通过（可运行: uv run pytest 查看详情）"
            FAILED=1
        fi
        # Web 服务可导入
        if uv run python -c "from wmts.api import app; print('  Web 服务 OK（FastAPI 路由', len(app.routes), '条）')" 2>/dev/null; then
            :
        else
            warn "Web 服务导入自检未通过"
            FAILED=1
        fi
    fi

    [ "$FAILED" -eq 0 ] || die "自检存在告警，请根据上方输出排查。可用 --no-check 跳过自检。"
fi

# ---------- 完成 ----------
ELAPSED=$(( $(date +%s) - START_TIME ))
printf "\n%s"
step "构建完成 ✔（耗时 ${ELAPSED}s）"
printf "%s\n" "$C_GREEN"
echo "  ▸ 启动 Web 服务:       uv run python -m wmts.server   （推荐，浏览器访问 http://127.0.0.1:8760）"
echo "  ▸ 启动图形界面:        uv run python gui_main.py"
echo "  ▸ 下载瓦片(HTTP/2):    uv run python download_tiles_async.py"
echo "  ▸ 拼接大图(Rust 引擎): uv run python merge_rs_cli.py"
echo "  ▸ 缓存管理:            uv run python tile_cache.py stats"
echo "  ▸ API 文档:            服务启动后访问 http://127.0.0.1:8760/docs"
echo "  ▸ 参数配置:            编辑 config.py（默认值）；运行期配置在 config.json"
printf "%s\n" "$C_RESET"
