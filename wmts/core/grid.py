"""
wmts.core.grid — 经纬度 ⇄ 瓦片行列换算（基于在线 LayerMeta）。

格网约定（与 IGServer / WMTS 一致）：
    - 原点 origin 在左上角（默认 -180°, 90°）；
    - 级别 level 的分辨率 res（度/像素），瓦片 T×T 像素（默认 256）；
    - 瓦片 (col, row) 左上角经纬度::

        lon = origin_x + col * T * res
        lat = origin_y - row * T * res

本模块是 bbox 换算的唯一实现：区域选择下载（bbox→范围）与
geo_attach（范围→地理坐标）共用同一套数学，避免漂移。
"""

from __future__ import annotations

import math

from .layer_meta import LayerMeta

BBox = tuple[float, float, float, float]  # xmin, ymin, xmax, ymax
TileRange = tuple[int, int, int, int]     # col_start, col_end, row_start, row_end


def resolution(meta: LayerMeta, level: int) -> float:
    """级别分辨率（度/像素）。"""
    return meta.resolution(level)


def tile_span(meta: LayerMeta, level: int) -> float:
    """单瓦片的经纬度跨度（度）。"""
    return meta.tile_size * resolution(meta, level)


def lonlat_to_tile(meta: LayerMeta, level: int, lon: float, lat: float) -> tuple[int, int]:
    """经纬度 → 所在瓦片 (col, row)。"""
    span = tile_span(meta, level)
    col = math.floor((lon - meta.origin_x) / span)
    row = math.floor((meta.origin_y - lat) / span)
    return col, row


def tile_to_lonlat(meta: LayerMeta, level: int, col: int, row: int) -> tuple[float, float]:
    """瓦片左上角经纬度 (lon, lat)。"""
    span = tile_span(meta, level)
    return meta.origin_x + col * span, meta.origin_y - row * span


def range_to_bbox(meta: LayerMeta, level: int, col_start: int, col_end: int,
                  row_start: int, row_end: int) -> BBox:
    """瓦片范围 → 地理范围 (xmin, ymin, xmax, ymax)。"""
    span = tile_span(meta, level)
    xmin = meta.origin_x + col_start * span
    xmax = meta.origin_x + (col_end + 1) * span
    ymax = meta.origin_y - row_start * span
    ymin = meta.origin_y - (row_end + 1) * span
    return xmin, ymin, xmax, ymax


def bbox_to_range(meta: LayerMeta, level: int, bbox: BBox,
                  clamp_to_layer: bool = True) -> TileRange:
    """地理范围 → 覆盖它的瓦片范围（边界落在线上时取"恰好覆盖"的最小范围）。

    clamp_to_layer=True 时把结果裁剪到图层有效范围（fullExtent）。
    """
    xmin, ymin, xmax, ymax = bbox
    if xmin > xmax:
        xmin, xmax = xmax, xmin
    if ymin > ymax:
        ymin, ymax = ymax, ymin

    if clamp_to_layer:
        lxmin, lymin, lxmax, lymax = meta.full_extent
        xmin, xmax = max(xmin, lxmin), min(xmax, lxmax)
        ymin, ymax = max(ymin, lymin), min(ymax, lymax)
        if xmin > xmax or ymin > ymax:
            raise ValueError("所选范围与图层有效范围无交集")

    span = tile_span(meta, level)
    col_start = math.floor((xmin - meta.origin_x) / span)
    col_end = math.ceil((xmax - meta.origin_x) / span) - 1
    row_start = math.floor((meta.origin_y - ymax) / span)
    row_end = math.ceil((meta.origin_y - ymin) / span) - 1
    return col_start, col_end, row_start, row_end


def layer_range(meta: LayerMeta, level: int) -> TileRange:
    """图层有效范围（fullExtent）在该级别覆盖的完整瓦片范围。"""
    return bbox_to_range(meta, level, meta.full_extent, clamp_to_layer=False)


def clamp_range(meta: LayerMeta, level: int, col_start: int, col_end: int,
                row_start: int, row_end: int) -> TileRange:
    """把瓦片范围裁剪到图层有效范围（fullExtent 对应的瓦片范围）内。"""
    cs, ce, rs, re = layer_range(meta, level)
    return (
        max(col_start, cs), min(col_end, ce),
        max(row_start, rs), min(row_end, re),
    )


def range_size(col_start: int, col_end: int, row_start: int, row_end: int) -> tuple[int, int, int]:
    """返回 (cols, rows, total)。"""
    cols = col_end - col_start + 1
    rows = row_end - row_start + 1
    return cols, rows, cols * rows


def estimate_size(total_tiles: int, bytes_per_tile: float = 18 * 1024) -> int:
    """按平均瓦片体积估算下载总量（字节）。默认 18KB/片（PNG 256×256 地质图经验值）。"""
    return int(total_tiles * bytes_per_tile)
