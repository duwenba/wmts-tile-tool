#!/usr/bin/env python3
"""
瓦片缓存管理工具 — 实现已迁移到 wmts/core/cache.py，本文件为 CLI 兼容薄壳。

用法：
  uv run python tile_cache.py stats
  uv run python tile_cache.py migrate [--dry-run]
  uv run python tile_cache.py prune --matrix 16 [--col-start .. --col-end .. --row-start .. --row-end ..]
  uv run python tile_cache.py prune --older-than 30 | --max-size 500
  uv run python tile_cache.py clear [--yes]
  uv run python tile_cache.py verify
"""

from wmts.core.cache import cli_main  # noqa: F401

if __name__ == "__main__":
    cli_main()
