"""georef：地理计算 + BigTIFF 标签写入（最小 BigTIFF 夹具）。"""

import struct

import pytest

from wmts.core.georef import (
    GEO_TAGS,
    attach_geo_for_config,
    compute_geo,
    read_head_ifd,
)


def make_bigtiff(path, width, height):
    """构造最小可用的分块 BigTIFF（含 TileOffsets/TileByteCounts）。"""
    head = b"II" + struct.pack("<H", 43) + struct.pack("<H", 8) \
        + struct.pack("<H", 0) + struct.pack("<Q", 16)
    entries = [
        (256, 16, 1, width),   # ImageWidth
        (257, 16, 1, height),  # ImageLength
        (273, 16, 1, 4096),    # TileOffsets
        (279, 16, 1, 1024),    # TileByteCounts
    ]
    ifd = struct.pack("<Q", len(entries))
    for tag, ty, count, value in entries:
        ifd += struct.pack("<HHQQ", tag, ty, count, value)
    ifd += struct.pack("<Q", 0)
    with open(path, "wb") as f:
        f.write(head)
        f.write(ifd)


def test_compute_geo(config, meta):
    geo = compute_geo(config, meta)
    # 3 列 × 2 行 × 256 px，分辨率 16 级
    assert geo["width"] == 3 * 256 and geo["height"] == 2 * 256
    res = meta.resolution(config.tile_matrix)
    assert geo["resolution"] == pytest.approx(res)
    # 左上角 = 原点 + 列偏移
    assert geo["x0"] == pytest.approx(-180 + 10 * 256 * res)
    assert geo["y0"] == pytest.approx(90 - 20 * 256 * res)


def test_attach_geo_roundtrip(config, meta, tmp_path):
    config.output_file = str(tmp_path / "map.tif")
    make_bigtiff(config.output_file, config.cols * 256, config.rows * 256)
    result = attach_geo_for_config(config, meta=meta)
    assert result["written"] is True
    assert result["epsg"] == config.epsg

    with open(config.output_file, "rb") as f:
        _, entries = read_head_ifd(f)
    tags = {t for t, *_ in entries}
    assert set(GEO_TAGS) <= tags

    # 第二次：已含标签 → 跳过
    result2 = attach_geo_for_config(config, meta=meta)
    assert result2["written"] is False


def test_attach_geo_size_mismatch(config, meta, tmp_path):
    config.output_file = str(tmp_path / "wrong.tif")
    make_bigtiff(config.output_file, 100, 100)  # 与配置范围不符
    with pytest.raises(Exception, match="不一致"):
        attach_geo_for_config(config, meta=meta)
    # force 可绕过
    result = attach_geo_for_config(config, meta=meta, force=True)
    assert result["written"] is True


def test_attach_geo_missing_file(config, meta, tmp_path):
    config.output_file = str(tmp_path / "nope.tif")
    with pytest.raises(Exception, match="找不到"):
        attach_geo_for_config(config, meta=meta)


def test_read_head_ifd_rejects_non_bigtiff(tmp_path):
    p = tmp_path / "classic.tif"
    p.write_bytes(b"II" + struct.pack("<H", 42) + struct.pack("<I", 8))
    with pytest.raises(Exception, match="BigTIFF"):
        with open(p, "rb") as f:
            read_head_ifd(f)
