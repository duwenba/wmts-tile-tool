# WMTS 瓦片下载与拼接工具

用于下载和拼接 WMTS 地图服务的瓦片数据。

## 文件说明

- `config.py` - **统一配置文件**（在此修改下载和拼接参数）
- `download_tiles.py` - 瓦片下载脚本（异步 IO，HTTP/2 支持）
- `merge_tiles.py` - 瓦片拼接脚本（Python 版）
- `merge_rs_cli.py` - **Rust 合并引擎便捷入口**（推荐超大图，见下）
- `merge_rs/` - **Rust 合并引擎源码**（超大图内存恒定、并行压缩）
- `tile_path.py` - **缓存路径统一管理**（新旧目录结构兼容）
- `tile_cache.py` - **缓存管理工具**（统计/清理/迁移，见下）
- `gui_main.py` - **图形界面（GUI，wxPython）**（推荐使用）
- `tiles/` - 瓦片缓存目录（自动创建，分级结构）

## 瓦片缓存结构

v2 分级结构（推荐，下载与迁移后默认）：

```
tiles/
└── 16/                 ← 缩放级别
    └── 10558/          ← 行
        └── 53248.png   ← 列
```

旧 v1 扁平结构 `tiles/{matrix}_{col}_{row}.png` 仍兼容读取，可用缓存工具迁移。

## 缓存管理

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

**图形界面**：点顶部工具栏「缓存管理」按钮，可查看统计、按级别/时间/容量清理、迁移旧结构、清空缓存。

## 图形界面（wxPython GUI）

```bash
uv run python gui_main.py
```

界面提供以下能力：

- **任务流程可视化**：顶部流水线展示 `配置参数 → 下载瓦片 → 拼接大图 → 任务完成` 四个步骤的状态（待命/进行中/完成/失败）与整体进度条。
- **单个瓦片预览（自动加载）**：右侧面板输入级别/列/行（或点击总览网格格子）后自动加载预览——本地已有则直接显示，缺失时自动从网络获取（仅预览、不落盘）。网络预览后可点「保存瓦片」手动保存到输出目录。
- **范围总览网格**：下载页以颜色网格展示每个瓦片状态（绿=已下载、红=失败、灰=待处理），点击任意格子可跳转预览该瓦片。
- **一键执行全部**：自动依次完成 下载 → 拼接 的完整流程，也可单独执行下载或拼接。
- **实时进度**：下载（成功/失败/跳过/速度/剩余时间）与拼接进度、统计信息、日志面板。

> 配置保存在 `gui_config.json`（启动时自动加载），也可在界面上恢复默认或写回配置。

### wxPython 安装说明（Linux）

wxPython 在 PyPI 上没有 Linux 预编译包。本项目已在 `pyproject.toml` 中通过 `[tool.uv.sources]`
固定指向官方 extras 仓库的 Ubuntu 24.04 构建（glibc 2.39+，兼容 Arch/CachyOS 等发行版）：

```bash
uv sync
```

若在其他 Linux 发行版/架构使用，请到 https://extras.wxpython.org/wxPython4/extras/linux/gtk3/ 选择对应 wheel 并修改 `pyproject.toml` 中的 URL。

## 使用 uv 运行项目

本项目使用 [uv](https://github.com/astral-sh/uv) 作为包管理器。

### 安装依赖

```bash
uv sync
```

### 运行脚本

**图形界面（推荐）：**
```bash
uv run python gui_main.py
```

**下载瓦片：**
```bash
uv run python download_tiles.py
```

**拼接大图：**
```bash
# 默认模式（streaming + fast）
uv run python merge_tiles.py

# 批量模式（平衡速度和内存）
uv run python merge_tiles.py batch fast

# 超低内存模式（超大图）
uv run python merge_tiles.py lowmem fast

# 优化压缩保存（文件更小但较慢）
uv run python merge_tiles.py batch optimize

# 查看帮助
uv run python merge_tiles.py --help
```

## 使用方法

### 1. 配置参数

只需在 `config.py` 中修改配置参数，两个脚本会自动使用相同配置：

```python
# 下载范围配置
TILE_MATRIX = 10          # 缩放级别
TILE_COL_START = 800      # 列起始
TILE_COL_END = 850        # 列结束
TILE_ROW_START = 150      # 行起始
TILE_ROW_END = 200        # 行结束

# 其他设置
MAX_WORKERS = 10          # 并发下载数
OUTPUT_FILE = "merged_map.png"    # 输出文件名
```

### 2. 下载瓦片

推荐使用异步版下载脚本（速度更快）：

```bash
uv run python download_tiles_async.py
```

或使用同步版：
```bash
uv run python download_tiles.py
```

### 3. 拼接大图

**拼接大图：**
```bash
# 默认模式（streaming + fast）
uv run python merge_tiles.py
```

其他模式：
```bash
# 批量模式（平衡速度和内存）
uv run python merge_tiles.py batch fast

# 超低内存模式（超大图）
uv run python merge_tiles.py lowmem fast

# 优化压缩保存
uv run python merge_tiles.py batch optimize
```

拼接后的大图将保存为 `merged_map.png`

## Rust 高速合并引擎（推荐超大图）

> 当拼接范围很大（合并后未压缩超过 2~3 GB）时，**强烈建议使用 Rust 引擎**。
> Python 版在 7.5GB 内存的机器上，batch 模式会直接 OOM，streaming 模式单线程压缩需要数小时。

先编译一次：

```bash
cd merge_rs && cargo build --release
```

然后使用（自动读取 `config.py` 的下载范围）：

```bash
# 默认：按 config.py 输出文件名自动判断格式（merged_map.png → PNG）
uv run python merge_rs_cli.py

# 推荐超大图：输出分块 BigTIFF（无损、内存恒定、8 核并行）
uv run python merge_rs_cli.py --format tif --out merged_map.tif

# 指定压缩级别 / 线程数
uv run python merge_rs_cli.py --format tif --threads 8 --level 6
```

也可以直接用二进制，自定义瓦片目录和范围：

```bash
merge_rs/target/release/merge_rs --tiles-dir tiles \
  --matrix 16 --col-start 53248 --col-end 53521 --row-start 10558 --row-end 10740 \
  --format tif --out merged_map.tif
```

### Rust 引擎设计要点

1. **内存恒定**：分块 BigTIFF 模式下，内存峰值只与“单批瓦片”有关（实测几十 MB），与整图大小无关。
   实测本仓库 50,142 瓦片（合并后 **70,144×46,848 = 32.9 亿像素，未压缩 RGB 9.86GB**）：
   **耗时 8 秒，峰值内存 21.6 MB**（Python 版必 OOM）。
2. **并行压缩**：每个 256×256 瓦片独立 zlib(deflate) 压缩，rayon 8 核并行；写入串行保证偏移连续。
3. **输出格式**：
   - `tif`（默认）：分块 BigTIFF，无损 deflate。可被 QGIS/GIMP/ImageMagick/GDAL 直接打开；
     超大图建议用此格式（普通看图软件打不开超大 PNG）。
   - `png`：单张 PNG 流式写出（Sub 滤波 + zlib），内存≈一条瓦片行，适合中等成图。
4. **行为与 Python 版一致**：源瓦片展开为 RGB 并丢弃 alpha（等价 `convert('RGB')`），缺失瓦片填黑。

> 已验证：Rust 输出与 Python 版逐像素一致（`compare -metric AE` = 0），大文件通过
> `tiffinfo`/`tiffcrop`/PIL 采样校验，四角与中心瓦片全部一致。

## 拼接脚本模式对比

| 模式 | 保存模式 | 内存占用 | 速度 | 最佳场景 |
|------|----------|----------|------|----------|
| batch | fast | 中 | 最快 | 快速开发/测试 |
| batch | optimize | 中 | 快 | 最终输出/压缩 |
| streaming | fast | 低 | 中快 | 内存受限（默认） |
| streaming | optimize | 低 | 中 | 需要压缩+低内存 |
| lowmem | fast | 最低 | 中 | 超大图/低内存 |

### 核心优化点

**download_tiles.py（下载脚本）的内存优化：**
1. **进度记录改为 JSONL 追加写**：不再把所有瓦片记录常驻内存（旧版 3.5 万条 = 4.6MB，且只在结束时整体写盘）；断点续传依据是「瓦片文件是否已存在」，进度文件仅作日志，旧 dict 格式会自动归档为 `.bak`
2. **有界队列 + 固定 worker 池**：不再一次性创建全部下载协程，任务再多内存占用也恒定在 O(并发数)
3. **流式写盘**：`client.stream` 分块写入，不再把整片数据缓冲进内存
4. **智能重试**：指数退避（429/503 退避更久，尊重服务端限流），404 等不可恢复错误直接失败，不浪费时间
5. **取消响应及时**：生产者每 0.5s 检查取消信号，不会被满队列阻塞

**merge_tiles.py 的优化：**
1. **内存优化**：按行分批处理，避免同时加载所有瓦片
2. **保存优化**：
   - `fast` 模式：跳过 `optimize=True`，保存速度提升 5-10 倍
   - `optimize` 模式：仅在需要时才进行 PNG 优化
3. **显式释放**：使用 `gc.collect()` 及时释放内存
4. **超低内存模式**：使用内存映射文件，仅在保存时才加载完整数据

**建议：**
- 快速开发/测试：`merge_tiles.py batch fast`
- 最终输出需压缩：`merge_tiles.py batch optimize`
- 默认/内存受限：`merge_tiles.py`（默认 streaming fast）
- 超大图（>50,000 瓦片）：`merge_tiles.py lowmem fast`

## 配置说明

### 下载范围

根据你的需求调整 TileMatrix、TileCol 和 TileRow 的范围：

- **TileMatrix**: 缩放级别，数字越大放大倍数越高
- **TileCol**: 列号，从左到右增加
- **TileRow**: 行号，从上到下增加

### 并发设置

在 `config.py` 中调整并发数：

```python
MAX_WORKERS = 10  # 并发下载数，可根据网络情况调整
```

## 断点续传

下载脚本支持断点续传和安全中断：

### 功能特性

- **自动保存进度**：每10秒自动保存下载进度到 `download_progress.json`
- **安全中断**：按 `Ctrl+C` 可安全中断，进度自动保存
- **断点续传**：重新运行脚本会自动跳过已下载的瓦片
- **失败重试**：失败的瓦片会记录到 `download_failed.txt`，下次运行会自动重试
- **自动清理**：下载成功后会自动清理进度文件

### 使用方式

```bash
# 首次下载
uv run python download_tiles.py

# 如果下载中断，直接重新运行即可继续
uv run python download_tiles.py

# 查看失败列表（如果有的话）
cat download_failed.txt
```

### 进度文件说明

| 文件 | 说明 |
|------|------|
| `download_progress.json` | 下载进度文件，记录已完成的瓦片 |
| `download_failed.txt` | 失败瓦片列表 |

## 注意事项

1. 确保网络连接稳定
2. 大量下载可能需要较长时间
3. 拼接大图可能占用较多内存
4. 如需修改服务URL，请更新 `BASE_URL` 和 `LAYER` 参数

## 内存优化说明

### 原始版本（批量加载模式）的瓶颈

1. **批量加载模式**：所有瓦片同时加载到内存
   - 10,000 个瓦片（256×256 RGB）≈ 1.9GB
   - 加上最终大图数组 ≈ 2.4GB+

2. **保存大图瓶颈**：
   - `Image.fromarray()` 创建新对象，完整复制数组
   - `optimize=True` 增加额外 10-60 秒压缩时间

### 当前实现（按行分批 + 流式）的改进

1. **按行分批处理**：一次只处理 10 行（可配置），峰值内存降低 80%+
2. **快速保存模式**：跳过 PNG 优化，保存速度提升 5-10 倍
3. **显式内存释放**：每批次处理完后立即释放内存
4. **超低内存模式**：使用内存映射，仅在最后才加载数据
