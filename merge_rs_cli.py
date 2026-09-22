#!/usr/bin/env python3
"""
WMTS 瓦片合并 — Rust 引擎便捷入口 — 实现已迁移到 wmts/core/merger.py。

用法：
  uv run python merge_rs_cli.py                    # 默认输出分块 BigTIFF（.tif）
  uv run python merge_rs_cli.py --format png       # 单张 PNG
  uv run python merge_rs_cli.py --out big.tif      # 指定输出文件
  uv run python merge_rs_cli.py --threads 8 --level 6

注意：首次使用需先编译：
  cd merge_rs && cargo build --release
"""

from __future__ import annotations

import argparse
import sys

from wmts.core.config import Config
from wmts.core.merger import MergeError, binary_path, merge_tiles


def main() -> None:
    p = argparse.ArgumentParser(description="WMTS 瓦片合并（Rust 引擎，超大图低内存）")
    p.add_argument("--format", choices=["tif", "png"], default="tif",
                   help="输出格式：tif=分块BigTIFF（默认，内存恒定）；png=单张PNG")
    p.add_argument("--out", default=None, help="输出文件路径（默认 config 的 output_file）")
    p.add_argument("--threads", type=int, default=None, help="并行线程数（默认 CPU 核数）")
    p.add_argument("--level", type=int, default=None,
                   help="压缩级别 0-12（tif 默认 6；png 默认 1）")
    args = p.parse_args()

    bin_path = binary_path()
    if not bin_path.exists():
        print(f"错误: 未找到 Rust 二进制: {bin_path}\n"
              "请先编译：cd merge_rs && cargo build --release",
              file=sys.stderr)
        sys.exit(1)

    cfg = Config.load()
    result = merge_tiles(
        cfg,
        on_event=lambda ev: print(ev.get("message") or "") if ev.get("message") else None,
        out=args.out,
        fmt=args.format,
        threads=args.threads,
        level=args.level,
    )
    if not result.ok:
        print(f"错误: {result.error}", file=sys.stderr)
        sys.exit(1)
    print(f"完成: {result.output}")


if __name__ == "__main__":
    main()
