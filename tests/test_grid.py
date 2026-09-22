"""grid：经纬度 ⇄ 瓦片换算（核心数学，区域选择与 geo_attach 共用）。"""

import pytest

from wmts.core.grid import (
    bbox_to_range,
    clamp_range,
    estimate_size,
    layer_range,
    lonlat_to_tile,
    range_size,
    range_to_bbox,
    resolution,
    tile_span,
    tile_to_lonlat,
)


def test_resolution_fallback(meta):
    # 16 级在 meta 里显式存在
    assert meta.resolution(16) == pytest.approx(2.145767211880946e-05)
    # 12 级缺失 → 0 级逐级减半推算
    assert meta.resolution(12) == pytest.approx((360.0 / 256) / 2**12)


def test_tile_span(meta):
    assert tile_span(meta, 16) == pytest.approx(256 * meta.resolution(16))


def test_tile_to_lonlat_origin(meta):
    lon, lat = tile_to_lonlat(meta, 8, 0, 0)
    assert lon == pytest.approx(-180.0)
    assert lat == pytest.approx(90.0)


def test_lonlat_to_tile_roundtrip(meta):
    level = 12
    lon, lat = 113.0, 31.5
    col, row = lonlat_to_tile(meta, level, lon, lat)
    # 瓦片左上角应落在该点的左上方
    tlon, tlat = tile_to_lonlat(meta, level, col, row)
    assert tlon <= lon < tlon + tile_span(meta, level)
    assert tlat >= lat > tlat - tile_span(meta, level)


def test_bbox_to_range_roundtrip(meta):
    level = 16
    bbox = (112.9, 31.2, 113.0, 31.3)
    cs, ce, rs, re = bbox_to_range(meta, level, bbox)
    back = range_to_bbox(meta, level, cs, ce, rs, re)
    # 换算回的瓦片范围应完整覆盖原 bbox
    assert back[0] <= bbox[0] and back[1] <= bbox[1]
    assert back[2] >= bbox[2] and back[3] >= bbox[3]


def test_bbox_to_range_clamps_to_layer(meta):
    # 远超图层范围 → 裁剪到 fullExtent
    cs, ce, rs, re = bbox_to_range(meta, 10, (0.0, 0.0, 179.0, 89.0))
    lcs, lce, lrs, lre = layer_range(meta, 10)
    assert (cs, ce, rs, re) == (lcs, lce, lrs, lre)


def test_bbox_outside_layer_raises(meta):
    with pytest.raises(ValueError):
        bbox_to_range(meta, 10, (0.0, 0.0, 1.0, 1.0))


def test_clamp_range(meta):
    cs, ce, rs, re = layer_range(meta, 16)
    out = clamp_range(meta, 16, cs - 100, ce + 100, rs - 100, re + 100)
    assert out == (cs, ce, rs, re)


def test_range_size():
    assert range_size(0, 9, 0, 4) == (10, 5, 50)


def test_estimate_size():
    assert estimate_size(1000, 1024) == 1024 * 1000


def test_resolution_zero_division_free(meta):
    # 每个已定义级别都能算出正数分辨率
    for lv in range(0, 17):
        assert resolution(meta, lv) > 0
