# AGENTS.md

WMTS 瓦片下载与拼接工具：Python（uv）承载核心逻辑 + HTTP API + Web 前端（vanilla JS）+
wx 薄客户端；Rust（`merge_rs/`）是唯一拼接引擎。文档与注释一律用中文；设计细节见
`README.md`、`wmts/` 包内模块 docstring 与 `docs/performance.md`，与本文冲突时以代码为准。

## 构建与验证

- 包管理只用 **uv**（不是 pip/poetry）。Python 版本钉在 `.python-version`（3.14），`requires-python >= 3.11`。
- 一键构建：`./build.sh`（Windows：`.\build.ps1`）= 前置检查 → `uv sync` → `cargo build --release` → 自检。
  可重复执行；参数 `--clean` / `--skip-rust` / `--skip-uv` / `--no-check`。
- 运行任何脚本都走 `uv run`：
  - Web 服务：`uv run python -m wmts.server`（推荐；`--port` / `--host` / `--no-browser`）
  - GUI：`uv run python gui_main.py`（需要显示环境，纯 CLI/无头机器跑不了）
  - 下载：`uv run python download_tiles_async.py`
  - 拼接：`uv run python merge_rs_cli.py`
  - 缓存：`uv run python tile_cache.py stats|prune|migrate|verify|clear`
  - 地理标签：`uv run python geo_attach.py`
- **有测试**：`uv run pytest`（核心层单元测试，不联网；`tests/` 目录，含真实元数据 fixture）。
  没有 linter、formatter、typecheck、CI。构建自检 = `merge_rs --help` + 依赖导入 + pytest + API 可导入。
- `merge_rs/` 是**独立 cargo 项目**（不是 workspace）：仓库根目录直接 `cargo build` 会失败，
  必须 `cargo build --release --manifest-path merge_rs/Cargo.toml`（或 `cd merge_rs`）。
  产物：`merge_rs/target/release/merge_rs`。首次拼接前必须先编译，否则拼接任务报错。
- 改了 `pyproject.toml` 的依赖或 wheel URL 后要 `uv lock && uv sync`。

## 架构要点（文件名看不出来的）

### 分层（自底向上）

- `wmts/core/`：**纯业务逻辑，UI 无关**。不 import wx / fastapi，不依赖 CWD。
  输入全走 `Config` 对象，输出全走 `ProgressEvent`（见 `core/events.py`，统一 schema）。
  - `core/config.py`：`Config` dataclass。**默认值读根目录 `config.py`**（保留手改习惯），
    **运行期覆盖在 `config.json`**（Web/GUI「保存配置」写入；兼容迁移旧 `gui_config.json`）。
    修改任何模块全局变量的做法已废弃，禁止 `from config import *` 式耦合。
  - `core/layer_meta.py`：瓦片格网/图层范围**一律在线拉取**
    （`/api/igs/rest/mrcs/tiles/{layer}?f=json&v=2.0`，Cookie 鉴权），
    首次获取后快照到 `layer_meta/{layer}.json`（TTL `meta_ttl`，`refresh=True` 强制刷新）。
    **不再依赖 info.json**（那是旧图层 007021 的临时文件，已弃用）。
  - `core/grid.py`：经纬度⇄瓦片换算唯一实现，区域选择（bbox→范围）与 geo_attach 共用。
  - `core/downloader.py`：`AsyncTileDownloader(config, on_event, cancel_event)`。
    鉴权失效（401/403/405 或 JSON 错误体）会置 `auth_error` 并中止整批抛 `AuthError`，
    不会把全部瓦片打成"无效 PNG"。
  - `core/merger.py`：封装 Rust 二进制，stderr `--progress-json` 行解析为事件。
  - `core/georef.py` / `core/cache.py` / `core/preview.py` / `core/paths.py`：地理标签、缓存、
    单瓦片预览（Cookie 只在服务端用）、**按图层分层的缓存路径**。
    缓存结构 v3：`tiles/{layer}/{matrix}/{row}/{col}.png`（不同图层同坐标互不覆盖；
    兼容读取旧 v2 `tiles/{matrix}/{row}/{col}.png` 与 v1 扁平，用
    `tile_cache.py migrate --layer X` 归入图层）。Rust 合并引擎按
    `{tiles-dir}/{matrix}/{row}/{col}.png` 扫描，故 `merger.py` 把 `--tiles-dir`
    指向 `tiles/{layer}`（该图层目录不存在时回退到缓存根）。`iter_tiles` 产出
    5 元组 `(layer, matrix, col, row, path)`，`layer=None` 表示旧结构未分层。
- `wmts/tasks.py`：`TaskManager`——**同一时刻只允许一个任务**（download / merge / geo /
  pipeline / retry_failed），占用时 `start()` 抛 `TaskBusyError`。事件带全局自增 `seq` 并写入
  每任务的回放缓冲（SSE 订阅前发出的不丢失）。流水线权重：下载 50% / 拼接 40% / 地理标签 10%。
- `wmts/api.py`：FastAPI。REST + SSE（`/api/tasks/{id}/events`、`/api/logs/stream`）+
  静态托管 `wmts/web/`（mount 在最后，`/api` 优先）。输出下载支持 Range。
- `wmts/web/`：vanilla JS 单页（无构建步骤）。`app.js` 主控、`grid.js` 状态网格（2bit 位图）、
  `map.js` 图层概览地图 + 区域框选（瓦片经 `/api/tiles/{z}/{col}/{row}` 代理，Cookie 不下发浏览器）。

### 根目录脚本全是薄壳

`download_tiles.py`、`download_tiles_async.py`、`merge_rs_cli.py`、`tile_cache.py`、
`geo_attach.py`、`tile_path.py` 只是 CLI/兼容壳，**改行为要改 `wmts/core/` 对应模块**。

### GUI 是薄客户端

`gui_main.py` 只做 wx 渲染：业务走进程内 `TaskManager` + EventBus（后台线程读事件队列 →
`wx.CallAfter` 派发）。不要在 GUI 里写业务逻辑；`_sync_core_modules()` 式的全局变量同步已删除。

## 运行时注意事项

- 下载鉴权靠 `config.py` `HEADERS` 里的 Cookie（或 Web「配置」页更新，存入 `config.json`），
  过期后接口返回 `{"error":"操作权限不足","status":405}`；核心层会识别并中止任务、给出明确提示，
  需从浏览器重新拷贝 Cookie。
- 断点续传：直接重跑下载（或 `POST /api/tasks {type:"retry_failed"}`）；进度在
  `download_progress.json`（JSONL），失败列表在 `download_failed.txt`，两者均 gitignored、
  下载成功后自动清理。
- Linux 上 wxPython 无 PyPI wheel，`pyproject.toml` `[tool.uv.sources]` 按 Python 版本钉死了
  extras.wxpython.org 的 Ubuntu 24.04 wheel URL（glibc 2.39+）。换发行版/架构或改 Python 版本时
  `uv sync` 可能失败，需改这些 URL（见 README FAQ）后 `uv lock && uv sync`。
  Web 前端不依赖 wxPython——无头机器可用 `uv run python -m wmts.server`。
- `.gitignore` 覆盖 `tiles/`、`merged_*.tif`、`merged_*.png`、`config.json`、`layer_meta/`、
  `gui_config*`、下载进度文件；但 `merged_map.tif.aux.xml` 已被跟踪。
- 缓存按图层分层：切换图层后状态网格/统计只反映当前图层；清理（prune/verify）默认只作用于
  当前图层，`--all-layers` 才跨图层。旧结构（未分层）瓦片对所有图层都可见（v2/v1 回退），
  需尽快 `migrate --layer` 或清空。
- Rust 输出的 TIFF **不含地理参考**；流水线会自动跑 geo 标签（`auto_geo` 配置），
  手动拼接后需重跑 `uv run python geo_attach.py`（尺寸与配置不一致会拒绝，需 `--force`）。

## 版本控制

- 本仓库是 **jj 与 git 共存（colocated）**，日常提交/回退用 jj（`.jj/`），git 仅作远端桥接。
  处理版本控制操作时加载 `jjit` skill。
