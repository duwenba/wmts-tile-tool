#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
geo_attach.py — 为合并后的分块 BigTIFF 附加地理参考信息（GeoTIFF 标签）

读取 config.py 的下载范围 + info.json 的瓦片格网定义，计算合并大图左上角
地理坐标 (x0, y0) 与像素分辨率（度/像素），然后把三个 GeoTIFF 标签写入目标文件：

  - ModelPixelScaleTag (33550)  像素大小（度/像素）
  - ModelTiepointTag   (33922)  左上角地理坐标（PixelIsArea）
  - GeoKeyDirectoryTag (34735)  坐标系定义（默认 EPSG:4326 / WGS84）

原理（安全、可逆）：
  BigTIFF 文件头 8-15 字节指向"第一个 IFD"。本脚本不改动原有任何瓦片数据，
  只在文件末尾追加【地理标签数据 + 新 IFD】，再把文件头改指向新 IFD。
  若需还原：把文件头 8-15 字节改回旧 IFD 偏移即可（脚本会打印旧偏移）。

用法:
  uv run python geo_attach.py                # 修补 config.py 指定的输出文件（默认 merged_map.tif）
  uv run python geo_attach.py --dry-run      # 只计算并打印，不写文件
  uv run python geo_attach.py --file x.tif   # 指定要修补的 TIFF
  uv run python geo_attach.py --epsg 4490    # 自定义坐标系（默认 4326）
  uv run python geo_attach.py --force        # 文件尺寸与配置不一致时仍继续
"""

import argparse
import json
import os
import struct
import sys
from pathlib import Path

from config import (
    OUTPUT_FILE,
    TILE_COL_END,
    TILE_COL_START,
    TILE_MATRIX,
    TILE_ROW_END,
    TILE_ROW_START,
)

ROOT = Path(__file__).resolve().parent

# ---- TIFF 标签 ----
TAG_IMAGE_WIDTH = 256
TAG_IMAGE_LENGTH = 257
TAG_TILE_OFFSETS = 273
TAG_TILE_BYTE_COUNTS = 279

# ---- GeoTIFF 标签 ----
TAG_MODEL_PIXEL_SCALE = 33550
TAG_MODEL_TIEPOINT = 33922
TAG_GEO_KEY_DIRECTORY = 34735
GEO_TAGS = (TAG_MODEL_PIXEL_SCALE, TAG_MODEL_TIEPOINT, TAG_GEO_KEY_DIRECTORY)

# ---- TIFF 类型 ----
TYPE_SHORT = 3
TYPE_DOUBLE = 12

# GeoKey 常量
GT_MODEL_TYPE_GEOGRAPHIC = 2
GT_RASTER_TYPE_PIXEL_IS_AREA = 1


def load_tile_grid(matrix: int) -> dict:
    """从 info.json 递归解析瓦片格网：原点、瓦片尺寸、指定级别的分辨率。"""
    info = json.loads((ROOT / "info.json").read_text(encoding="utf-8"))

    def find(d):
        if isinstance(d, dict):
            if isinstance(d.get("origin"), dict) and isinstance(d.get("lods"), list):
                return d
            for v in d.values():
                r = find(v)
                if r is not None:
                    return r
        elif isinstance(d, list):
            for v in d:
                r = find(v)
                if r is not None:
                    return r
        return None

    tile_info = find(info)
    if not tile_info:
        sys.exit("错误: info.json 中找不到瓦片格网定义（origin/lods）")

    origin = tile_info["origin"]
    size = tile_info.get("cols") or tile_info.get("rows") or 256
    lods = tile_info["lods"]
    lod = next((l for l in lods if l.get("level") == matrix), None)
    if lod:
        res = lod["resolution"]
    else:
        lod0 = next((l for l in lods if l.get("level") == 0), None)
        res = lod0["resolution"] / (2**matrix) if lod0 else 360.0 / 256 / (2**matrix)

    return {
        "origin_x": float(origin["x"]),
        "origin_y": float(origin["y"]),
        "tile_size": int(size),
        "resolution": float(res),
    }


def compute_geo():
    """按瓦片格网计算合并大图左上角坐标与像素尺寸。"""
    grid = load_tile_grid(TILE_MATRIX)
    size = grid["tile_size"]
    x0 = grid["origin_x"] + TILE_COL_START * size * grid["resolution"]
    y0 = grid["origin_y"] - TILE_ROW_START * size * grid["resolution"]
    w = (TILE_COL_END - TILE_COL_START + 1) * size
    h = (TILE_ROW_END - TILE_ROW_START + 1) * size
    return grid, x0, y0, w, h


def read_head_ifd(f):
    """读取 BigTIFF 文件头指向的 IFD，返回 (ifd_off, [(tag, type, count, value), ...])。"""
    f.seek(0)
    head = f.read(16)
    if head[:2] != b"II":
        sys.exit("错误: 不是小端序 TIFF")
    if struct.unpack("<H", head[2:4])[0] != 43:
        sys.exit("错误: 不是 BigTIFF（本脚本仅支持 Rust 引擎输出的分块 BigTIFF）")
    ifd_off = struct.unpack("<Q", head[8:16])[0]
    f.seek(ifd_off)
    n = struct.unpack("<Q", f.read(8))[0]
    entries = []
    for _ in range(n):
        tag, ty, count, value = struct.unpack("<HHQQ", f.read(20))
        entries.append((tag, ty, count, value))
    return ifd_off, entries


def attach_geo(path: Path, x0: float, y0: float, res: float, epsg: int, force: bool) -> bool:
    """向 BigTIFF 追加地理标签。返回是否实际写入。"""
    with open(path, "rb+") as f:
        old_ifd_off, entries = read_head_ifd(f)

        if any(t in GEO_TAGS for t, *_ in entries) and not force:
            print(f"提示: {path} 已包含地理信息标签，跳过（--force 可强制重写）")
            return False

        tags = {t for t, *_ in entries}
        if not {TAG_TILE_OFFSETS, TAG_TILE_BYTE_COUNTS} <= tags:
            sys.exit("错误: 头 IFD 缺少 TileOffsets/TileByteCounts，文件结构异常，已中止")

        # ---- 在文件末尾追加：地理标签数据 → 新 IFD → 回填文件头 ----
        f.seek(0, os.SEEK_END)
        eof = f.tell()
        pad = (-eof) % 8  # 新 IFD 对齐到 8 字节
        if pad:
            f.write(b"\x00" * pad)
        data_off = eof + pad

        pix_off = data_off                # ModelPixelScale  (3 × DOUBLE = 24B)
        tie_off = data_off + 24           # ModelTiepoint    (6 × DOUBLE = 48B)
        geo_off = data_off + 24 + 48      # GeoKeyDirectory  (16 × SHORT = 32B)
        f.write(struct.pack("<3d", res, res, 0.0))
        f.write(struct.pack("<6d", 0.0, 0.0, 0.0, x0, y0, 0.0))
        keys = [
            1, 1, 0, 3,                  # KeyDirectoryVersion, KeyRevision, Minor, NumKeys
            1024, 0, 1, GT_MODEL_TYPE_GEOGRAPHIC,        # GTModelTypeGeoKey = 2 (地理坐标)
            1025, 0, 1, GT_RASTER_TYPE_PIXEL_IS_AREA,    # GTRasterTypeGeoKey = 1 (PixelIsArea)
            2048, 0, 1, epsg,                             # GeographicTypeGeoKey = epsg
        ]
        f.write(struct.pack("<16H", *keys))

        new_ifd_off = f.tell()
        new_entries = entries + [
            (TAG_MODEL_PIXEL_SCALE, TYPE_DOUBLE, 3, pix_off),
            (TAG_MODEL_TIEPOINT, TYPE_DOUBLE, 6, tie_off),
            (TAG_GEO_KEY_DIRECTORY, TYPE_SHORT, 16, geo_off),
        ]
        new_entries.sort()  # TIFF 规范要求标签升序
        f.write(struct.pack("<Q", len(new_entries)))
        for tag, ty, count, value in new_entries:
            f.write(struct.pack("<HHQQ", tag, ty, count, value))
        f.write(struct.pack("<Q", 0))  # next IFD = 0

        # ---- 回填文件头：第一个 IFD 指向新 IFD ----
        f.seek(8)
        f.write(struct.pack("<Q", new_ifd_off))
        f.flush()
        os.fsync(f.fileno())

    print(f"  旧 IFD @ {old_ifd_off}（如需还原：把文件头 8-15 字节改回该值）")
    print(f"  新 IFD @ {new_ifd_off}，追加 {pad + 104 + 8 + len(new_entries) * 20 + 8} 字节，原瓦片数据未改动")
    return True


def verify(path: Path):
    """若有 gdalinfo 则自动验证地理信息。"""
    import shutil
    import subprocess

    gdalinfo = shutil.which("gdalinfo")
    if not gdalinfo:
        print("提示: 未找到 gdalinfo，可用以下命令自行验证:")
        print(f"  gdalinfo {path}")
        return
    out = subprocess.run([gdalinfo, str(path)], capture_output=True, text=True).stdout
    print("验证 (gdalinfo):")
    lines = out.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("Coordinate System is:"):
            print("  " + ln)
            j = i + 1
            while j < len(lines) and lines[j].strip():
                print("  " + lines[j])
                j += 1
        elif ln.startswith(("Origin", "Pixel Size", "Upper Left", "Lower Right")):
            print("  " + ln)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="为合并后的分块 BigTIFF 附加地理参考信息（GeoTIFF 标签）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法:", 1)[-1],
    )
    ap.add_argument("--file", default=None, help=f"要修补的 TIFF（默认 config.py 的 {OUTPUT_FILE}）")
    ap.add_argument("--epsg", type=int, default=4326, help="坐标系 EPSG 编码（默认 4326 = WGS84）")
    ap.add_argument("--dry-run", action="store_true", help="只计算并打印，不写文件")
    ap.add_argument("--force", action="store_true", help="文件尺寸与配置不一致 / 已含地理标签时仍继续")
    args = ap.parse_args()

    grid, x0, y0, w, h = compute_geo()
    res = grid["resolution"]
    xmax = x0 + w * res
    ymin = y0 - h * res

    print("瓦片格网: 原点 (%g, %g), 瓦片 %d×%d, 级别 %d, 分辨率 %.17g °/px"
          % (grid["origin_x"], grid["origin_y"], grid["tile_size"], grid["tile_size"], TILE_MATRIX, res))
    print("地理范围 (EPSG:%d, 度):" % args.epsg)
    print("  左上 (%.10f, %.10f)   右上 (%.10f, %.10f)" % (x0, y0, xmax, y0))
    print("  左下 (%.10f, %.10f)   右下 (%.10f, %.10f)" % (x0, ymin, xmax, ymin))
    print("GeoTransform: (%.10f, %.17g, 0, %.10f, 0, %.17g)" % (x0, res, y0, -res))
    print("注: info.json 底层标注为 西安80 地理坐标（度），与服务对外 EPSG:4326 网格一致")

    path = Path(args.file) if args.file else ROOT / OUTPUT_FILE
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        sys.exit(f"错误: 找不到文件 {path}")

    with open(path, "rb") as f:
        _, entries = read_head_ifd(f)
    fw = next(value for tag, *_rest, value in entries if tag == TAG_IMAGE_WIDTH)
    fh = next(value for tag, *_rest, value in entries if tag == TAG_IMAGE_LENGTH)
    print(f"目标文件: {path} ({fw}×{fh})")

    if (fw, fh) != (w, h):
        msg = (f"警告: 文件尺寸 {fw}×{fh} 与 config.py 范围计算值 {w}×{h} 不一致\n"
               "      该文件可能不是按当前 config.py 的范围合并的，地理坐标将不准确")
        if args.dry_run:
            print(msg)
        elif not args.force:
            sys.exit(msg + "\n如确需继续请加 --force")
        else:
            print(msg)

    if args.dry_run:
        print("(--dry-run) 未写入任何内容。")
        return

    if attach_geo(path, x0, y0, res, args.epsg, args.force):
        print(f"完成: 已为 {path} 附加地理信息 (EPSG:{args.epsg})")
        verify(path)


if __name__ == "__main__":
    main()
