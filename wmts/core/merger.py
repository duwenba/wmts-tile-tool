"""
wmts.core.merger — Rust 合并引擎（merge_rs）封装，UI 无关。

调用 ``merge_rs/target/release/merge_rs`` 子进程，把其 ``--progress-json``
stderr 输出解析为统一 ProgressEvent。

    - 内存峰值恒定（分块 BigTIFF 模式），几十 GB 超大图也不 OOM；
    - 8 核并行压缩；
    - 默认输出分块 BigTIFF（.tif），可被 QGIS / GIMP / ImageMagick / GDAL 打开。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import ROOT, Config
from .events import (
    EV_LOG,
    EV_PROGRESS,
    STAGE_MERGE,
    make_event,
)


class MergeError(RuntimeError):
    """合并引擎不可用或执行失败。"""


def binary_path() -> Path:
    """Rust 二进制路径（自动适配 Windows .exe）。"""
    name = "merge_rs.exe" if sys.platform == "win32" else "merge_rs"
    return ROOT / "merge_rs" / "target" / "release" / name


@dataclass
class MergeResult:
    ok: bool
    output: str = ""
    returncode: int | None = None
    tiles: int = 0
    peak_mb: float = 0.0
    elapsed: float = 0.0
    error: str | None = None
    raw_lines: list[str] = field(default_factory=list)


def merge_tiles(
    config: Config,
    on_event=None,
    cancel_event=None,
    out: str | None = None,
    fmt: str = "tif",
    threads: int | None = None,
    level: int | None = None,
    layer: str | None = None,
) -> MergeResult:
    """执行合并并解析进度。阻塞直至子进程退出。

    fmt: "tif"（分块 BigTIFF，默认）| "png"（单张 PNG，中小成图）
    layer: 合并哪个图层的瓦片（默认 config.layer）；瓦片按图层分层存放，
           Rust 引擎按 {tiles-dir}/{matrix}/{row}/{col}.png 扫描。
    """
    start = time.time()
    bin_path = binary_path()
    if not bin_path.exists():
        return MergeResult(
            ok=False, elapsed=0.0, error=(
                f"未找到 Rust 引擎: {bin_path}\n请先执行: cd merge_rs && cargo build --release"))

    out_path = Path(out) if out else config.resolve_path(config.output_file)
    if fmt == "tif":
        out_path = out_path.with_suffix(".tif")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 图层目录优先；若该图层尚无缓存则回退到缓存根（兼容旧结构）
    base_dir = config.resolve_path(config.output_dir)
    use_layer = layer if layer is not None else config.layer
    layer_dir = base_dir / use_layer if use_layer else base_dir
    tiles_dir = layer_dir if layer_dir.is_dir() else base_dir

    cmd = [
        str(bin_path),
        "--tiles-dir", str(tiles_dir),
        "--matrix", str(config.tile_matrix),
        "--col-start", str(config.col_start), "--col-end", str(config.col_end),
        "--row-start", str(config.row_start), "--row-end", str(config.row_end),
        "--out", str(out_path), "--format", fmt, "--progress-json",
    ]
    if threads:
        cmd += ["--threads", str(threads)]
    if level is not None:
        cmd += ["--level", str(level)]

    if on_event:
        try:
            on_event(make_event(EV_LOG, stage=STAGE_MERGE,
                                message="运行 Rust 引擎: " + " ".join(cmd)))
        except Exception:
            pass

    result = MergeResult(ok=False, output=str(out_path))
    proc = None
    try:
        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True, bufsize=1)
        assert proc.stderr is not None
        for line in proc.stderr:
            if cancel_event is not None and cancel_event.is_set():
                proc.terminate()
                result.error = "已取消"
                break
            line = line.strip()
            if not line.startswith("{"):
                result.raw_lines.append(line)
                continue
            try:
                info = json.loads(line)
            except ValueError:
                continue
            if info.get("done"):
                result.peak_mb = float(info.get("peak_mb", 0.0))
                if on_event:
                    try:
                        on_event(make_event(
                            EV_PROGRESS, stage=STAGE_MERGE, percent=100.0,
                            message=f"写出完成 | 峰值内存 {result.peak_mb:.0f} MB"))
                    except Exception:
                        pass
                continue
            result.tiles = int(info.get("tiles", result.tiles or 0))
            percent = float(info.get("percent", 0.0))
            if on_event:
                try:
                    on_event(make_event(
                        EV_PROGRESS, stage=STAGE_MERGE, percent=percent,
                        done=int(info.get("tiles", 0)),
                        message=f"拼接中 {percent:.1f}% | 已拼接 {info.get('tiles', 0)} 片"
                                f" | 缺失 {info.get('missing', 0)} 片"))
                except Exception:
                    pass
        rc = proc.wait()
        proc = None
        result.returncode = rc
        if cancel_event is not None and cancel_event.is_set():
            result.ok = False
        else:
            result.ok = (rc == 0) and out_path.exists()
            if not result.ok:
                result.error = result.error or f"Rust 引擎退出码 {rc}"
    except Exception as e:
        result.error = f"Rust 引擎运行异常: {e}"
    finally:
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        result.elapsed = time.time() - start

    if result.ok:
        result.tiles = result.tiles or 0
    return result
