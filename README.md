# 🗺️ WMTS 瓦片下载与拼接工具

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![Rust](https://img.shields.io/badge/Rust-2021%20Edition-dea584?logo=rust&logoColor=white)
![uv](https://img.shields.io/badge/uv-%E5%8C%85%E7%AE%A1%E7%90%86%E5%99%A8-8E7DFF?logo=astral&logoColor=white)
![GitHub Stars](https://img.shields.io/github/stars/duwenba/wmts-tile-tool?style=social)

高性能的 **WMTS 瓦片批量下载与拼接** 工具：HTTP/2 异步高并发下载、断点续传与失败重试；默认 **Rust 拼接引擎**（内存恒定、实测 <22 MB）快速合并超大图（数十亿像素）并输出分块 BigTIFF（.tif）；提供 **Web 前端**（内置 HTTP API + SSE 实时进度）与 wxPython 图形界面（薄客户端）。

## ✨ 功能特性

- 🌐 **Web 前端 + HTTP API**：浏览器完成全流程；`/api/*` REST + SSE 可编程接口（Swagger 文档自动生成），下载/拼接/地理标签与 UI 彻底解耦
- 🗺️ **地图区域选择下载**：在线拉取图层元数据（无需 info.json），地图框选或经纬度输入 → 自动换算瓦片范围
- ⚡ **异步高并发下载**：HTTP/2、有界队列 + 固定 worker 池、流式写盘，内存恒定在 O(并发数)
- 🔁 **断点续传与失败重试**：智能指数退避（尊重服务端限流），`Ctrl+C` 安全中断；鉴权失效（Cookie 过期）自动中止并明确提示
- 🧩 **Rust 拼接引擎**：分块 BigTIFF / 流式 PNG、内存恒定、8 核并行压缩，实测 32.9 亿像素 8 秒完成、峰值内存 21.6 MB
- 🖥️ **wxPython 图形界面**：任务流水线可视化、单个瓦片预览、范围总览网格、实时进度（薄客户端，业务与 Web 共用同一核心）
- 🗃️ **瓦片缓存管理**：分级目录结构、统计 / 清理 / 迁移 / 校验（CLI、Web 与 GUI）

## 🚀 快速开始

### 一键构建（推荐）

```bash
# 1. 克隆仓库
git clone https://github.com/duwenba/wmts-tile-tool.git
cd wmts-tile-tool

# 2. 一键构建（安装 Python 依赖 + 编译 Rust 引擎 + 自检）
./build.sh

# 3. 启动 Web 前端（推荐，浏览器自动打开 http://127.0.0.1:8760）
uv run python -m wmts.server

# 或启动图形界面
uv run python gui_main.py
```

`build.sh` 自动完成全部构建步骤：检查前置依赖（uv / Rust 工具链）→ `uv sync` 安装依赖 →
`cargo build --release` 编译 Rust 引擎 → 构建自检，并输出常用命令提示。可重复执行（幂等），
中断后重跑即可继续；也支持分步与自定义参数：

```bash
./build.sh --clean        # 强制全量重建（重装依赖 + 清理 cargo 产物）
./build.sh --skip-rust    # 只装 Python 依赖
./build.sh --skip-uv      # 只编译 Rust 引擎
./build.sh --no-check     # 跳过构建后自检（更快）
./build.sh --help         # 查看全部用法
```

**Windows 版**（PowerShell 5.1 / 7 均可，用法与 Linux 版一一对应）：

```powershell
# 在仓库根目录打开 PowerShell，执行：
.\build.ps1                      # 一键构建
.\build.ps1 -Clean               # 强制全量重建
.\build.ps1 -SkipRust            # 只装 Python 依赖
.\build.ps1 -SkipUv              # 只编译 Rust 引擎
.\build.ps1 -NoCheck             # 跳过构建后自检
.\build.ps1 -Help                # 查看全部用法
```

> 若提示“无法加载脚本”，先执行 `Set-ExecutionPolicy -Scope Process Bypass` 或右键脚本 →
> “使用 PowerShell 运行”。Windows 上 wxPython 自动使用 PyPI 官方 wheel（4.3.x），
> 无需额外配置（见下文「wxPython 跨平台安装」）。

> ⚠️ 首次使用前，请先在 `config.py` 中配置目标 WMTS 服务的 URL 与下载范围（见下文「配置参数」）。

## 🗂️ 项目结构

| 文件 | 说明 |
|------|------|
| `config.py` | **默认配置文件**（下载与拼接参数的默认值在此修改） |
| `config.json` | 运行期配置（Web/GUI「保存配置」写入，gitignored，优先级高于 config.py） |
| `build.sh` | **一键构建脚本（Linux/macOS）**（装依赖 + 编译 Rust 引擎 + 自检） |
| `build.ps1` | **一键构建脚本（Windows，PowerShell）** |
| `wmts/` | **核心包**（业务逻辑单一来源，UI 无关） |
| `wmts/core/` | 配置 / 下载 / 合并 / 地理标签 / 缓存 / 预览 / 图层元数据 / 事件 |
| `wmts/tasks.py` | 任务编排（TaskManager：流水线、取消、事件广播） |
| `wmts/api.py` | **HTTP API**（FastAPI：REST + SSE + 静态托管） |
| `wmts/server.py` | Web 服务启动入口 |
| `wmts/web/` | Web 前端（vanilla JS 单页，无构建步骤） |
| `download_tiles_async.py` | 异步下载 CLI 薄壳（HTTP/2，默认 64 并发） |
| `download_tiles.py` | 下载 CLI 薄壳（兼容旧 import 路径；实现在 `wmts/core/downloader.py`） |
| `merge_rs_cli.py` | Rust 合并引擎便捷薄壳（拼接入口） |
| `merge_rs/` | Rust 合并引擎源码（cargo 项目） |
| `gui_main.py` | **wxPython 图形界面**（薄客户端） |
| `tile_path.py` | 缓存路径薄壳（实现在 `wmts/core/paths.py`） |
| `tile_cache.py` | 缓存管理 CLI（薄壳；实现在 `wmts/core/cache.py`） |
| `geo_attach.py` | 附加地理信息 CLI（薄壳；实现在 `wmts/core/georef.py`） |
| `tests/` | 核心层单元测试（pytest） |
| `tiles/` | 瓦片缓存目录（自动创建，分级结构） |

## ⚙️ 配置参数

**默认值**在 `config.py`（与旧版一致），**运行期配置**保存在 `config.json`（Web/GUI 的
「保存配置」写入；存在时优先于 `config.py`）：

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
> 图层元数据地址默认从 `BASE_URL` 自动推导（`/api/igs/rest/mrcs/tiles/{layer}?f=json&v=2.0`），
> 特殊服务可显式配置 `META_URL` 模板。**不再依赖临时的 `info.json`**——瓦片格网定义
> 一律从数据源在线拉取（首次获取后缓存到 `layer_meta/{layer}.json`，TTL 由 `META_TTL` 控制）。

## 🗺️ 数据源：湖北省地质大数据平台（geocloud.hubgs.com）

本配置默认的数据源为**湖北省地质局「地质大数据平台」**（[geocloud.hubgs.com](http://geocloud.hubgs.com)），
其核心应用「湖北省地质一张图」对外提供多专业、多比例尺地质空间数据 Web 地图服务。
后端为**中地数码 MapGIS IGServer**（国产 GIS 平台）。

### 瓦片接口

- **元数据**（瓦片格网定义）：`/api/igs/rest/mrcs/tiles/{layer}?f=json&v=2.0`，
  返回 `tileInfo`（切片原点、分辨率、级别、坐标系等）与 `fullExtent`（图层有效范围），
  由工具在线拉取（见 `wmts/core/layer_meta.py`）。
- **取图**：OGC 标准 WMTS `GetTile`，`tilematrixset=EPSG:4326`：

```
https://geocloud.hubgs.com/api/igs/rest/ogc/WMTSServer?layer={layer}&style=default&tilematrixset=EPSG:4326&Service=WMTS&Request=GetTile&Version=1.0.0&Format=image/png&TileMatrix={z}&TileCol={col}&TileRow={row}
```

### 瓦片格网规则（在线图层元数据）

| 项 | 值 |
|---|---|
| 原点 Origin | (-180°, 90°)（左上角） |
| 瓦片尺寸 | 256 × 256 |
| 级别 | 0–18，共 19 级 |
| 0 级分辨率 | 1.40625°（= 360°/256），逐级减半 |
| 坐标系 | 对外 `EPSG:4326`；底层标注为西安80（地理坐标，度），两者网格一致 |

### 图层

| 图层编号 | 覆盖范围 | 备注 |
|---|---|---|
| `WMTS020101010007021` | 109.49–111.02°E, 31.99–33.01°N（**十堰一带**，含丹江口水库西缘） | 旧版临时 `info.json` 记录的即为此图层的元数据 |
| `WMTS020101010007006` | 112.5–114.0°E, 31.0–32.0°N（**随州—孝感一带**） | `config.py` 当前配置，matrix 16 共 274×183 瓦片 |

### 内容判定（地质专题图）

对已合并的 `merged_map.tif`（图层 006，matrix 16 ≈ 2.4 m/px）做像素特征分析：

- **矢量图斑风格**：整图仅约 95 种颜色（8× 采样），非遥感影像；图斑边界清晰、文字线划多。
- **配色符合地质图惯例**：浅黄 42.9%（第四系松散沉积）、黄绿 27.8%、紫红（红层，如广水北部大块图斑）、
  蓝 = 水系 4.2%、黑 = 线划/文字 3.5%。
- 部分下载区域为纯色背景（超出图层有效范围，属正常）。

结合平台性质，两个图层为**湖北地质一张图发布的地质专题图**（区域地质/基础地质类），
图层编号为平台内部目录编码，精确图名需登录平台目录查看。

### 鉴权说明

瓦片与 GetCapabilities 接口均校验登录 Cookie，Cookie 过期后返回 `{"error":"操作权限不足","status":405}`。
如需重新下载，请先在浏览器登录平台并更新 `config.py` 中 `HEADERS` 的 `Cookie`。

> 📌 补充：`merged_map.tif` 为分块 BigTIFF，但偏移表用 `StripOffsets`(tag 273, LONG8) 而非 `TileOffsets`，
> ffmpeg / ImageMagick 解析失败，可按 256×256 分块逐块 zlib 解压后用 numpy 直接读取（本次分析即用此法）。

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

## 🗺️ 附加地理信息（GeoTIFF）

Rust 引擎输出的 TIFF 默认不含地理参考。合并完成后运行 `geo_attach.py`：
它读取配置的下载范围与**在线图层元数据**（自动从数据源拉取瓦片格网定义，不再依赖
info.json），自动计算左上角地理坐标与像素分辨率，把 GeoTIFF 标签（ModelPixelScale /
ModelTiepoint / GeoKeyDirectory）写入 TIFF，使 QGIS / GDAL / ArcGIS 能按真实经纬度配准显示：

```bash
uv run python geo_attach.py            # 修补 merged_map.tif（默认 config.py 的 OUTPUT_FILE）
uv run python geo_attach.py --dry-run  # 先预览计算出的地理范围，不写文件
uv run python geo_attach.py --file x.tif --epsg 4610   # 自定义文件 / 坐标系
```

- **坐标系**：默认 EPSG:4326（WGS84，服务对外坐标系）；更换数据源时可 `--epsg` 指定其它编码。
- **安全性**：只在文件末尾追加【地理标签数据 + 新 IFD】并回填文件头 8 字节，原瓦片数据字节不变；
  脚本会打印旧 IFD 偏移，必要时可改回文件头还原。
- 重新拼接生成新 TIFF 后需再次运行本脚本（文件尺寸与 config.py 不一致时会拒绝并提示 `--force`）。


## 🌐 Web 前端与 HTTP API

```bash
uv run python -m wmts.server                 # 默认 127.0.0.1:8760，自动打开浏览器
uv run python -m wmts.server --port 9000     # 自定义端口
uv run python -m wmts.server --host 0.0.0.0 --no-browser   # 局域网访问（注意 Cookie 安全）
```

Web 前端（`wmts/web/`，vanilla JS 单页、无构建步骤）是**地图优先工作台**：左边大地图，
右边「数据源与范围 → 任务与进度 → 结果与导出」流程面板，缓存与日志收在底部抽屉。

- **地图工作台**：瓦片影像 + 状态着色（绿=已下载/红=损坏/灰=待处理）叠加在同一张图上；
  左键拖拽平移、滚轮以光标为锚缩放、双击放大，瓦片网格带 `级别/列/行` 编号，左下实时经纬度/Tile/分辨率读数
- **框选即下载范围**：按 `S` 切到框选工具（或 `Shift+拖拽`）画框，选区条即时显示「N 列 × M 行 = X 片 · 约 Y MB」，
  一键应用为下载范围；也支持「填入整个图层」「用当前视图」或手填列/行
- **点击瓦片预览**：点地图任意瓦片弹出气泡（缩略图 + 来源 + 经纬度范围），可「加入缓存」或「放大到此瓦片」
- **任务与流水线**：`配置 → 下载 → 拼接 → 地理标签 → 完成` 五步状态 + 总进度条；下载中地图实时着色，
  失败列表可就地「重试失败瓦片」；鉴权失效弹出顶部红色横幅
- **结果与缓存**：产物列表直接下载（GeoTIFF 支持断点续传）；缓存统计 / 清理 / 迁移 / 校验收在底部抽屉；
  SSE 实时日志
- **快捷键**：`+` / `-` 缩放，`S` 框选，`V` 平移，`Esc` 取消框选，`Enter` 应用范围

> 📊 端到端流程与典型场景（只下一个区块 / 断点续传 / 换图层 / 单瓦片预览）见
> [docs/usage.md](docs/usage.md)（Mermaid 使用实例图）。

### API 一览（启动后访问 `/docs` 查看 Swagger）

```
配置    GET/PUT /api/config              POST /api/config/reset
图层    GET  /api/layer/meta?refresh=1   （在线元数据 + 图层覆盖范围）
网格    GET  /api/grid                   POST /api/grid/from-bbox（bbox→瓦片范围）
状态    GET  /api/tiles/status           （范围内状态位图，2bit/片）
预览    GET  /api/tiles/{z}/{col}/{row}?source=auto|local|remote
        POST /api/tiles/{z}/{col}/{row}/save
续传    GET  /api/download/progress      （进度日志 + 失败列表）
任务    POST /api/tasks                  {type: download|merge|geo|pipeline|retry_failed, params}
        GET  /api/tasks  /api/tasks/{id}      POST /api/tasks/{id}/cancel
        GET  /api/tasks/{id}/events      → SSE 进度流
缓存    GET  /api/cache/stats            POST /api/cache/{prune|migrate|verify|clear}
输出    GET  /api/outputs                GET /api/outputs/{name}（支持 Range 断点续传）
日志    GET  /api/logs                   GET /api/logs/stream (SSE)
```

约定：

- 同一时刻只允许一个任务运行，占用时 `POST /api/tasks` 返回 `409`；
- 进度为单向 SSE，事件带自增 `seq`，先回放缓冲再接实时流（不丢不重）；
- 上游 Cookie 只保存在服务端（`config.json`），`GET /api/config` 返回打码值；
- 任务事件统一为 `ProgressEvent` 结构，下载 / 拼接 / 地理标签共用同一 schema。

## 🖥️ 图形界面（wxPython 薄客户端）

```bash
uv run python gui_main.py
```

界面提供以下能力（业务逻辑与 Web 共用 `wmts` 包，GUI 只做渲染）：

- **任务流程可视化**：流水线展示 `配置参数 → 下载瓦片 → 拼接大图 → 地理标签 → 任务完成`
  五步状态（待命/进行中/完成/失败）与整体进度条，支持一键执行全部（下载→拼接→地理标签）。
- **单个瓦片预览（自动加载）**：右侧面板输入级别/列/行（或点击总览网格格子）自动加载预览——本地已有则直接显示，缺失时自动从网络获取（仅预览、不落盘），可手动「保存瓦片」。
- **范围总览网格**：下载页以颜色网格展示每个瓦片状态（绿=已下载、红=失败、灰=待处理），点击格子跳转预览。
- **一键执行全部**：自动依次完成 下载 → 拼接，也可单独执行下载或拼接。
- **实时进度**：下载（成功/失败/跳过/速度/剩余时间）与拼接进度、统计信息、日志面板。
- **缓存管理**：工具栏「缓存管理」按钮，查看统计、按级别/时间/容量清理、迁移旧结构、清空缓存。

> 配置保存在 `config.json`（启动时自动加载，与 Web 前端共用；兼容读取旧 `gui_config.json`）。

### wxPython 跨平台安装

wxPython 在 PyPI 上没有 Linux 预编译包，但 Windows/macOS 有官方 wheel。
`pyproject.toml` 已按平台自动分流，`uv sync` 无需任何手动配置：

- **Linux**：自动使用官方 extras 仓库的 Ubuntu 24.04 构建（glibc 2.39+，兼容 Arch/CachyOS 等发行版），
  并按 Python 版本（3.11–3.14）匹配对应 wheel；
- **Windows / macOS**：自动回退到 PyPI 官方 wheel（wxPython 4.3.x）。

若在其他 Linux 发行版/架构使用，见 [FAQ](#其他-linux-发行版架构装不上-wxpython-怎么办)。

## 🗃️ 缓存管理

**缓存结构**（v3，按图层分层；不同图层同一行列的瓦片互不覆盖）：

```
tiles/
└── WMTS020101010007018/    ← 图层
    └── 16/                 ← 缩放级别
        └── 10558/          ← 行
            └── 53248.png   ← 列
```

旧结构仍兼容读取，可用 `migrate` 归入指定图层目录：

- v2（无图层维度）：`tiles/{matrix}/{row}/{col}.png` —— 多图层会互相覆盖，建议迁移
- v1（扁平）：`tiles/{matrix}_{col}_{row}.png`

> Rust 合并引擎按 `{tiles-dir}/{matrix}/{row}/{col}.png` 扫描，合并时自动把
> `--tiles-dir` 指向 `tiles/{当前图层}`（见 `wmts/core/merger.py`）。

**命令行**（`tile_cache.py`，`--layer` 可放在子命令前后；默认取当前配置图层）：

```bash
uv run python tile_cache.py stats                          # 统计：按图层/级别明细
uv run python tile_cache.py migrate --layer WMTS020101010007018   # 旧结构迁入该图层
uv run python tile_cache.py prune --matrix 16              # 删除当前图层该级别的全部瓦片
uv run python tile_cache.py prune --matrix 16 --col-start 53400 --col-end 53410 \
                                 --row-start 10600 --row-end 10610    # 删除范围内瓦片
uv run python tile_cache.py prune --older-than 30          # 删除 30 天前下载的瓦片
uv run python tile_cache.py prune --max-size 500           # 删最旧直到缓存 ≤500MB
uv run python tile_cache.py prune --matrix 16 --all-layers # 不限图层
uv run python tile_cache.py clear --yes                    # 清空缓存（全部图层）
uv run python tile_cache.py verify                         # 校验当前图层瓦片 PNG 完整性
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
修改 `pyproject.toml` 中 `[tool.uv.sources]` 的 `wxpython` 各 URL（保持 marker 分版本写法）后重新 `uv lock && uv sync`。

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
