#!/usr/bin/env python3
"""
瓦片缓存统一路径管理 — 兼容薄壳。

实现已迁移到 wmts.core.paths（单一来源），本文件仅为旧 import 路径保留。
缓存结构现为 v3（按图层分层）：tiles/{layer}/{matrix}/{row}/{col}.png
"""

from wmts.core.paths import (  # noqa: F401
    PNG_HEADER,
    ensure_tile_dir,
    find_tile,
    iter_tiles,
    legacy_tile_path,
    legacy_v2_path,
    safe_layer,
    tile_path,
)

__all__ = [
    "tile_path",
    "legacy_v2_path",
    "legacy_tile_path",
    "ensure_tile_dir",
    "find_tile",
    "iter_tiles",
    "safe_layer",
    "PNG_HEADER",
]
