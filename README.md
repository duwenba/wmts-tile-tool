# 🗺️ WMTS 瓦片下载与拼接工具

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![Rust](https://img.shields.io/badge/Rust-2021%20Edition-dea584?logo=rust&logoColor=white)
![uv](https://img.shields.io/badge/uv-%E5%8C%85%E7%AE%A1%E7%90%86%E5%99%A8-8E7DFF?logo=astral&logoColor=white)
![GitHub Stars](https://img.shields.io/github/stars/duwenba/wmts-tile-tool?style=social)

高性能的 **WMTS 瓦片批量下载与拼接** 工具：HTTP/2 异步高并发下载、断点续传与失败重试；默认 **Rust 拼接引擎**（内存恒定、实测 <22 MB）快速合并超大图（数十亿像素）并输出分块 BigTIFF（.tif）；内置 wxPython 图形界面。

## ✨ 功能特性

- ⚡ **异步高并发下载**：HTTP/2、有界队列 + 固定 worker 池、流式写盘，内存恒定在 O(并发数)
- 🔁 **断点续传与失败重试**：智能指数退避（尊重服务端限流），`Ctrl+C` 安全中断
- 🧩 **Rust 拼接引擎**：分块 BigTIFF / 流式 PNG、内存恒定、8 核并行压缩，实测 32.9 亿像素 8 秒完成、峰值内存 21.6 MB
- 🖥️ **wxPython 图形界面**：任务流水线可视化、单个瓦片预览、范围总览网格、实时进度
- 🗃️ **瓦片缓存管理**：分级目录结构、统计 / 清理 / 迁移 / 校验（CLI 与 GUI）

## 🚀 快速开始

```bash
# 1. 克隆仓库
git clone https://github.com/duwenba/wmts-tile-tool.git
cd wmts-tile-tool

# 2. 安装依赖（uv + wxPython）
uv sync

# 3. 启动图形界面（推荐）
uv run python gui_main.py
```

> ⚠️ 首次使用前，请先在 `config.py` 中配置目标 WMTS 服务的 URL 与下载范围（见下文「配置参数」）。

## 🗂️ 项目结构

| 文件 | 说明 |
|------|------|
| `config.py` | **统一配置文件**（下载与拼接参数都在此修改） |
| `download_tiles_async.py` | 异步下载脚本（HTTP/2，推荐） |
| `download_tiles.py` | 同步下载脚本 |
| `merge_rs_cli.py` | **Rust 合并引擎便捷入口（拼接入口）** |
| `merge_rs/` | Rust 合并引擎源码（cargo 项目） |
| `gui_main.py` | **wxPython 图形界面**（推荐使用） |
| `tile_path.py` | 缓存路径统一管理（新旧目录结构兼容） |
| `tile_cache.py` | 缓存管理 CLI（统计 / 清理 / 迁移 / 校验） |
| `tiles/` | 瓦片缓存目录（自动创建，分级结构） |

## ⚙️ 配置参数

所有下载与拼接参数集中在 `config.py`，各脚本自动读取同一份配置：

```python
# 下载范围配置
TILE_MATRIX = 16          # 缩放级别（数字越大放大倍数越高）
TILE_COL_START = 53248    # 列起始
TILE_COL_END = 53521      # 列结束
TILE_ROW_START = 10558    # 行起始
TILE_ROW_END = 10740      # 行结束

# 其他设置
MAX_WORKERS = 16          # 并发下载数
OUTPUT_FILE = "merged_map.tif"   # 输出文件名（默认 Rust 引擎分块 BigTIFF）
```

> 如需更换数据源，请更新 `BASE_URL`、`LAYER` 与 `HEADERS`（部分服务需要 Cookie 鉴权）。

## 📥 下载瓦片

```bash
# 异步版（HTTP/2，速度更快，推荐）
uv run python download_tiles_async.py

# 同步版
uv run python download_tiles.py
```

下载支持断点续传：中断后直接重新运行即可，自动跳过已下载瓦片、重试失败瓦片。详见「断点续传」。

## ⚡ Rust 高速合并引擎

> 🚀 **Rust 是唯一的拼接引擎**，CLI 与 GUI 均通过它拼接，默认输出分块 BigTIFF（.tif）。
> 内存峰值恒定（实测 <22 MB），超大图（数十亿像素）也不会 OOM。

先编译一次：

```bash
cd merge_rs && cargo build --release
```

使用（自动读取 `config.py` 的下载范围）：

```bash
# 默认：输出分块 BigTIFF（.tif，无损、内存恒定、8 核并行）
uv run python merge_rs_cli.py

# 指定输出文件 / 压缩级别 / 线程数
uv run python merge_rs_cli.py --out merged_map.tif --threads 8 --level 6

# 单张 PNG（中等成图 / 普通看图软件）
uv run python merge_rs_cli.py --format png --out merged_map.png
```

也可直接用二进制，自定义瓦片目录和范围：

```bash
merge_rs/target/release/merge_rs --tiles-dir tiles \
  --matrix 16 --col-start 53248 --col-end 53521 --row-start 10558 --row-end 10740 \
  --format tif --out merged_map.tif
```

**设计要点**（完整设计与实测数据见 [docs/performance.md](docs/performance.md)）：

1. **内存恒定**：分块 BigTIFF 模式下内存峰值只与单批瓦片有关（实测几十 MB），与整图大小无关。实测 50,142 瓦片（32.9 亿像素、未压缩 RGB 9.86 GB）：**耗时 8 秒，峰值内存 21.6 MB**。
2. **并行压缩**：每张 256×256 瓦片独立 zlib(deflate) 压缩，rayon 8 核并行；写入串行保证偏移连续。
3. **输出格式**：
   - `tif`（默认）：分块 BigTIFF，无损 deflate，可被 QGIS / GIMP / ImageMagick / GDAL 直接打开；
     超大图建议用此格式（普通看图软件打不开超大 PNG）。
   - `png`：单张 PNG 流式写出（Sub 滤波 + zlib），内存 ≈ 一条瓦片行，适合中等成图。
4. **行为说明**：源瓦片展开为 RGB 并丢弃 alpha（等价 `convert('RGB')`），缺失瓦片填黑。

## 🖥️ 图形界面（wxPython）

```bash
uv run python gui_main.py
```

界面提供以下能力：

- **任务流程可视化**：顶部流水线展示 `配置参数 → 下载瓦片 → 拼接大图 → 任务完成` 四步状态（待命/进行中/完成/失败）与整体进度条。
- **单个瓦片预览（自动加载）**：右侧面板输入级别/列/行（或点击总览网格格子）自动加载预览——本地已有则直接显示，缺失时自动从网络获取（仅预览、不落盘），可手动「保存瓦片」。
- **范围总览网格**：下载页以颜色网格展示每个瓦片状态（绿=已下载、红=失败、灰=待处理），点击格子跳转预览。
- **一键执行全部**：自动依次完成 下载 → 拼接，也可单独执行下载或拼接。
- **实时进度**：下载（成功/失败/跳过/速度/剩余时间）与拼接进度、统计信息、日志面板。
- **缓存管理**：工具栏「缓存管理」按钮，查看统计、按级别/时间/容量清理、迁移旧结构、清空缓存。

> 配置保存在 `gui_config.json`（启动时自动加载），也可在界面上恢复默认或写回配置。

### wxPython 安装说明（Linux）

wxPython 在 PyPI 上没有 Linux 预编译包。本项目已在 `pyproject.toml` 中通过 `[tool.uv.sources]`
固定指向官方 extras 仓库的 Ubuntu 24.04 构建（glibc 2.39+，兼容 Arch/CachyOS 等发行版），`uv sync` 即可。
若在其他发行版/架构使用，见 [FAQ](#其他-linux-发行版架构装不上-wxpython-怎么办)。

## 🗃️ 缓存管理

**缓存结构**（v2 分级，下载与迁移后默认）：

```
tiles/
└── 16/                 ← 缩放级别
    └── 10558/          ← 行
        └── 53248.png   ← 列
```

旧 v1 扁平结构 `tiles/{matrix}_{col}_{row}.png` 仍兼容读取，可用 `migrate` 命令迁移。

**命令行**（`tile_cache.py`）：

```bash
uv run python tile_cache.py stats                          # 统计：总量 + 各级别明细
uv run python tile_cache.py migrate                        # 迁移旧扁平结构到分级结构
uv run python tile_cache.py prune --matrix 16              # 删除指定级别的全部瓦片
uv run python tile_cache.py prune --matrix 16 --col-start 53400 --col-end 53410 \
                                 --row-start 10600 --row-end 10610    # 删除范围内瓦片
uv run python tile_cache.py prune --older-than 30          # 删除 30 天前下载的瓦片
uv run python tile_cache.py prune --max-size 500           # 删最旧直到缓存 ≤500MB
uv run python tile_cache.py clear --yes                    # 清空缓存
uv run python tile_cache.py verify                         # 校验全部瓦片 PNG 完整性
```

## ⏯️ 断点续传

下载脚本支持断点续传与安全中断：

- **自动保存进度**：每 10 秒自动保存到 `download_progress.json`
- **安全中断**：按 `Ctrl+C` 安全中断，进度自动保存
- **断点续传**：重新运行自动跳过已下载瓦片
- **失败重试**：失败瓦片记录到 `download_failed.txt`，下次运行自动重试
- **自动清理**：下载成功后自动清理进度文件

```bash
uv run python download_tiles_async.py    # 中断后直接重新运行即可继续
cat download_failed.txt                  # 查看失败列表（如有）
```

| 文件 | 说明 |
|------|------|
| `download_progress.json` | 下载进度文件，记录已完成的瓦片 |
| `download_failed.txt` | 失败瓦片列表 |

## 📈 性能与内存优化

本项目在下载与拼接环节做了针对性优化，核心手段：

- **下载**：JSONL 追加写进度（避免进度常驻内存）、有界队列 + 固定 worker 池、`client.stream` 流式写盘、指数退避重试（429/503 退避更久）、取消响应及时。
- **拼接（Rust）**：分块 BigTIFF 内存恒定、rayon 8 核并行压缩、写入串行保证偏移连续（详见 [docs/performance.md](docs/performance.md)）。

> 📄 完整设计思路、演进过程与实测数据见 [docs/performance.md](docs/performance.md)。

## ❓ 常见问题（FAQ）

### 其他 Linux 发行版/架构装不上 wxPython 怎么办？

wxPython 在 PyPI 上没有 Linux 预编译包。请到
https://extras.wxpython.org/wxPython4/extras/linux/gtk3/ 选择对应发行版/架构的 wheel，
修改 `pyproject.toml` 中 `[tool.uv.sources]` 的 `wxpython` URL 后重新 `uv sync`。

### 没有 Rust 工具链还能用吗？

拼接依赖 Rust 引擎，首次使用前需先 `cd merge_rs && cargo build --release`（只需一次）。
若无法安装 Rust 工具链，则无法完成拼接（下载瓦片与 GUI 仍可用）。

### 超大图用什么格式、什么软件打开？

Rust 引擎输出 `tif` 分块 BigTIFF，可用 QGIS / GIMP / ImageMagick / GDAL 打开；
超大 PNG 普通看图软件打不开，建议超大图一律用 `tif`。

### 下载中断后如何续传？

直接重新运行下载脚本即可——自动跳过已下载瓦片、重试失败瓦片，无需任何额外操作。

### WMS/WMTS 服务地址怎么换？

修改 `config.py` 中的 `BASE_URL`、`LAYER`、`STYLE`、`TILEMATRIXSET` 等参数；需要鉴权的服务请更新 `HEADERS`。
