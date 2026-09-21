"""
瓦片缓存统一路径管理（单一来源，所有脚本共用）。

目录结构 v2（分级缓存）：
    tiles/{matrix}/{row}/{col}.png      按 级别 → 行 → 列 分目录，合并时按行读取最快

旧结构 v1（扁平，已废弃但保留兼容读取）：
    tiles/{matrix}_{col}_{row}.png

约定：
    - 写入一律用 v2（tile_path + ensure_tile_dir）
    - 读取用 find_tile / iter_tiles，新旧结构都能识别
    - 用 `python tile_cache.py migrate` 把旧结构迁移到新结构
"""

import os

__all__ = [
    "tile_path",
    "legacy_tile_path",
    "ensure_tile_dir",
    "find_tile",
    "iter_tiles",
]


def tile_path(matrix, col, row, base="tiles"):
    """v2 分级路径：{base}/{matrix}/{row}/{col}.png"""
    return os.path.join(base, str(matrix), str(row), f"{col}.png")


def legacy_tile_path(matrix, col, row, base="tiles"):
    """v1 扁平路径：{base}/{matrix}_{col}_{row}.png"""
    return os.path.join(base, f"{matrix}_{col}_{row}.png")


def ensure_tile_dir(matrix, row, base="tiles"):
    """确保 (matrix, row) 目录存在，返回目录路径"""
    d = os.path.join(base, str(matrix), str(row))
    os.makedirs(d, exist_ok=True)
    return d


def find_tile(matrix, col, row, base="tiles"):
    """查找瓦片：优先 v2，回退 v1。存在返回路径，否则 None"""
    p = tile_path(matrix, col, row, base)
    if os.path.isfile(p):
        return p
    lp = legacy_tile_path(matrix, col, row, base)
    if os.path.isfile(lp):
        return lp
    return None


def iter_tiles(base="tiles"):
    """遍历缓存全部瓦片，产出 (matrix, col, row, path)，兼容 v1/v2 两种结构。

    注意：产出的是生成器，os.walk 是惰性的，超大缓存也不会占内存。
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
                yield m, c, r, path
            elif len(parts) == 3:
                # v2 分级：{matrix}/{row}/{col}.png
                try:
                    m, row, col = int(parts[0]), int(parts[1]), int(parts[2][:-4])
                except ValueError:
                    continue
                yield m, col, row, path
