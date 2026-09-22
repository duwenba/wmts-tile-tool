#!/usr/bin/env python3
"""
瓦片缓存统一路径管理 — 兼容薄壳。

实现已迁移到 wmts.core.paths（单一来源），本文件仅为旧 import 路径保留。
"""

from wmts.core.paths import (  # noqa: F401
    PNG_HEADER,
    ensure_tile_dir,
    find_tile,
    iter_tiles,
    legacy_tile_path,
    tile_path,
)

__all__ = [
    "tile_path",
    "legacy_tile_path",
    "ensure_tile_dir",
    "find_tile",
    "iter_tiles",
    "PNG_HEADER",
]
