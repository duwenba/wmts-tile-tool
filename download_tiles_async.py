#!/usr/bin/env python3
"""
WMTS瓦片下载脚本（异步IO版，CLI 快速入口）— 实现已迁移到 wmts/core/downloader.py。

本文件仅保持命令行入口兼容（默认 64 并发，比 GUI 默认高，适合纯下载场景）。
"""

from download_tiles import ASYNC_MAX_WORKERS, main  # noqa: F401

if __name__ == "__main__":
    main()
