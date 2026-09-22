"""
wmts.core.paths — 瓦片缓存统一路径管理（原 tile_path.py，单一来源）。

目录结构（v3，按图层分层）：
    tiles/{layer}/{matrix}/{row}/{col}.png

旧结构（保留兼容读取，可用 `tile_cache.py migrate --layer X` 迁移到 v3）：
    v2  tiles/{matrix}/{row}/{col}.png        （无图层维度，多图层会互相覆盖）
    v1  tiles/{matrix}_{col}_{row}.png        （扁平）

为什么按图层分层：不同图层在同一 matrix/col/row 的瓦片内容不同，
不隔离会互相覆盖（例如图层 007006 与 007018 在 16 级存在重叠区）。

约定：
    - 写入一律用 v3（tile_path + ensure_tile_dir，需传 layer）
    - 读取用 find_tile / iter_tiles，v3/v2/v1 都能识别
    - Rust 合并引擎按 {tiles-dir}/{matrix}/{row}/{col}.png 扫描，
      故合并时把 --tiles-dir 指向 tiles/{layer}（见 wmts/core/merger.py）
"""

from __future__ import annotations

import os
import re

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

PNG_HEADER = b"\x89PNG\r\n\x1a\n"

_UNSAFE = re.compile(r"[^0-9A-Za-z._-]")


def safe_layer(layer: str | None) -> str | None:
    """把图层名规整为可安全用作目录名的字符串（None/空 → None）。"""
    if not layer:
        return None
    return _UNSAFE.sub("_", str(layer))


def _root(base, layer) -> str:
    """带图层命名空间的缓存根目录。"""
    lay = safe_layer(layer)
    return os.path.join(base, lay) if lay else base


def tile_path(matrix, col, row, base="tiles", layer=None):
    """v3 分层路径：{base}/{layer}/{matrix}/{row}/{col}.png（layer 为空则退回 v2 形式）"""
    return os.path.join(_root(base, layer), str(matrix), str(row), f"{col}.png")


def legacy_v2_path(matrix, col, row, base="tiles"):
    """v2 旧分级路径（无图层维度）：{base}/{matrix}/{row}/{col}.png"""
    return os.path.join(base, str(matrix), str(row), f"{col}.png")


def legacy_tile_path(matrix, col, row, base="tiles"):
    """v1 扁平路径：{base}/{matrix}_{col}_{row}.png"""
    return os.path.join(base, f"{matrix}_{col}_{row}.png")


def ensure_tile_dir(matrix, row, base="tiles", layer=None):
    """确保 (layer, matrix, row) 目录存在，返回目录路径"""
    d = os.path.join(_root(base, layer), str(matrix), str(row))
    os.makedirs(d, exist_ok=True)
    return d


def find_tile(matrix, col, row, base="tiles", layer=None):
    """查找瓦片：v3（当前图层）→ v2（无图层旧结构）→ v1（扁平）。

    存在返回路径，否则 None。
    """
    candidates = []
    if safe_layer(layer):
        candidates.append(tile_path(matrix, col, row, base, layer))
    candidates.append(legacy_v2_path(matrix, col, row, base))
    candidates.append(legacy_tile_path(matrix, col, row, base))
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def iter_tiles(base="tiles"):
    """遍历缓存全部瓦片，产出 (layer, matrix, col, row, path)。

    兼容三种结构，layer 为 None 表示旧结构（未分层）。os.walk 惰性，超大缓存不占内存。
    """
    base = str(base)
    if not os.path.isdir(base):
        return
    for root, _dirs, files in os.walk(base):
        for fn in files:
            if not fn.endswith(".png"):
                continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, base)
            parts = rel.split(os.sep)
            if len(parts) == 1:
                # v1 扁平：{matrix}_{col}_{row}.png
                try:
                    m, c, r = (int(x) for x in parts[0][:-4].split("_"))
                except ValueError:
                    continue
                yield None, m, c, r, path
            elif len(parts) == 3:
                # v2 旧分级：{matrix}/{row}/{col}.png（无图层）
                try:
                    m, row, col = int(parts[0]), int(parts[1]), int(parts[2][:-4])
                except ValueError:
                    continue
                yield None, m, col, row, path
            elif len(parts) == 4:
                # v3 分层：{layer}/{matrix}/{row}/{col}.png
                try:
                    m, row, col = int(parts[1]), int(parts[2]), int(parts[3][:-4])
                except ValueError:
                    continue
                yield parts[0], m, col, row, path
