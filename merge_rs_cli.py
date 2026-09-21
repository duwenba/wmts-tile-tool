#!/usr/bin/env python3
"""
WMTS 瓦片合并 - Rust 引擎便捷入口

读取 config.py 的配置，调用 merge_rs/ 里的 Rust 二进制执行合并。
Rust 是唯一的拼接引擎：
  - 内存峰值恒定（分块 BigTIFF 模式），几十 GB 超大图也不怕 OOM
  - 8 核并行压缩，速度提升数倍
  - 默认输出分块 BigTIFF（无损），超大图可被 QGIS/GIMP/ImageMagick 打开

用法：
  uv run python merge_rs_cli.py                    # 默认输出分块 BigTIFF（.tif）
  uv run python merge_rs_cli.py --format tif       # 显式指定 BigTIFF（默认）
  uv run python merge_rs_cli.py --format png       # 单张 PNG
  uv run python merge_rs_cli.py --out big.tif      # 指定输出文件
  uv run python merge_rs_cli.py --threads 8 --level 6

注意：首次使用需先编译：
  cd merge_rs && cargo build --release
"""

import argparse
import subprocess
import sys
from pathlib import Path

from config import (
    OUTPUT_DIR,
    OUTPUT_FILE,
    TILE_COL_END,
    TILE_COL_START,
    TILE_MATRIX,
    TILE_ROW_END,
    TILE_ROW_START,
)

BIN = Path(__file__).resolve().parent / "merge_rs" / "target" / "release" / "merge_rs"


def main() -> None:
    p = argparse.ArgumentParser(
        description="WMTS 瓦片合并（Rust 引擎，超大图低内存）"
    )
    p.add_argument(
        "--format",
        choices=["tif", "png"],
        default="tif",
        help="输出格式：tif=分块BigTIFF（默认，内存恒定）；png=单张PNG",
    )
    p.add_argument(
        "--out", default=None, help="输出文件路径（默认 config.py 的 OUTPUT_FILE）"
    )
    p.add_argument("--threads", type=int, default=None, help="并行线程数（默认 CPU 核数）")
    p.add_argument(
        "--level", type=int, default=None, help="压缩级别 0-12（tif 默认 6；png 默认 1）"
    )
    args = p.parse_args()

    if not BIN.exists():
        print(
            f"错误: 未找到 Rust 二进制: {BIN}\n"
            "请先编译：cd merge_rs && cargo build --release",
            file=sys.stderr,
        )
        sys.exit(1)

    out = args.out or OUTPUT_FILE
    cmd = [
        str(BIN),
        "--tiles-dir",
        OUTPUT_DIR,
        "--matrix",
        str(TILE_MATRIX),
        "--col-start",
        str(TILE_COL_START),
        "--col-end",
        str(TILE_COL_END),
        "--row-start",
        str(TILE_ROW_START),
        "--row-end",
        str(TILE_ROW_END),
        "--out",
        out,
    ]
    cmd += ["--format", args.format]
    if args.threads:
        cmd += ["--threads", str(args.threads)]
    if args.level is not None:
        cmd += ["--level", str(args.level)]

    print("运行:", " ".join(cmd))
    sys.exit(subprocess.call(cmd))


if __name__ == "__main__":
    main()
