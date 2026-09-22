"""
wmts.core.georef — 为合并后的分块 BigTIFF 附加地理参考（GeoTIFF 标签）。

由 geo_attach.py 函数化重构而来；格网数据来自 ``core.layer_meta``（在线图层
元数据），不再依赖临时的 info.json。

写入三个 GeoTIFF 标签：
    - ModelPixelScaleTag (33550)  像素大小（度/像素）
    - ModelTiepointTag   (33922)  左上角地理坐标（PixelIsArea）
    - GeoKeyDirectoryTag (34735)  坐标系定义（默认 EPSG:4326 / WGS84）

原理（安全、可逆）：不改动原有瓦片数据，只在文件末尾追加
【地理标签数据 + 新 IFD】，再把文件头 8-15 字节改指向新 IFD。
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import time
from pathlib import Path

from .config import Config
from .events import EV_LOG, EV_PROGRESS, STAGE_GEO, make_event
from .grid import resolution as grid_resolution
from .layer_meta import LayerMeta, MetaError, fetch_layer_meta

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


class GeoRefError(RuntimeError):
    """地理标签写入失败。"""


# ---------------- 计算 ----------------

def compute_geo(config: Config, meta: LayerMeta) -> dict:
    """按瓦片格网计算合并大图左上角坐标、像素尺寸与预期宽高。

    返回 {x0, y0, width, height, resolution, origin_x, origin_y, tile_size}。
    """
    res = grid_resolution(meta, config.tile_matrix)
    x0 = meta.origin_x + config.col_start * meta.tile_size * res
    y0 = meta.origin_y - config.row_start * meta.tile_size * res
    w = config.cols * meta.tile_size
    h = config.rows * meta.tile_size
    return {
        "x0": x0, "y0": y0, "width": w, "height": h,
        "resolution": res,
        "origin_x": meta.origin_x, "origin_y": meta.origin_y,
        "tile_size": meta.tile_size,
        "level": config.tile_matrix,
    }


# ---------------- BigTIFF 读写 ----------------

def read_head_ifd(f):
    """读取 BigTIFF 文件头指向的 IFD，返回 (ifd_off, [(tag, type, count, value), ...])。"""
    f.seek(0)
    head = f.read(16)
    if head[:2] != b"II":
        raise GeoRefError("不是小端序 TIFF")
    if struct.unpack("<H", head[2:4])[0] != 43:
        raise GeoRefError("不是 BigTIFF（本模块仅支持 Rust 引擎输出的分块 BigTIFF）")
    ifd_off = struct.unpack("<Q", head[8:16])[0]
    f.seek(ifd_off)
    n = struct.unpack("<Q", f.read(8))[0]
    entries = []
    for _ in range(n):
        tag, ty, count, value = struct.unpack("<HHQQ", f.read(20))
        entries.append((tag, ty, count, value))
    return ifd_off, entries


def attach_geo(path: Path, x0: float, y0: float, res: float, epsg: int,
               force: bool = False, on_event=None) -> bool:
    """向 BigTIFF 追加地理标签。返回是否实际写入。"""
    with open(path, "rb+") as f:
        old_ifd_off, entries = read_head_ifd(f)

        if any(t in GEO_TAGS for t, *_ in entries) and not force:
            _log(on_event, f"{path} 已包含地理信息标签，跳过（--force 可强制重写）")
            return False

        tags = {t for t, *_ in entries}
        if not {TAG_TILE_OFFSETS, TAG_TILE_BYTE_COUNTS} <= tags:
            raise GeoRefError("头 IFD 缺少 TileOffsets/TileByteCounts，文件结构异常，已中止")

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
            1, 1, 0, 3,                                    # 版本 + NumKeys
            1024, 0, 1, GT_MODEL_TYPE_GEOGRAPHIC,          # GTModelTypeGeoKey = 2
            1025, 0, 1, GT_RASTER_TYPE_PIXEL_IS_AREA,      # GTRasterTypeGeoKey = 1
            2048, 0, 1, epsg,                              # GeographicTypeGeoKey
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

        f.seek(8)
        f.write(struct.pack("<Q", new_ifd_off))
        f.flush()
        os.fsync(f.fileno())

    _log(on_event,
         f"旧 IFD @ {old_ifd_off}（还原：把文件头 8-15 字节改回该值）| "
         f"新 IFD @ {new_ifd_off}，原瓦片数据未改动")
    return True


def verify_geo(path: Path) -> str | None:
    """若有 gdalinfo 则验证地理信息，返回输出文本（无 gdalinfo 返回 None）。"""
    import shutil
    import subprocess

    gdalinfo = shutil.which("gdalinfo")
    if not gdalinfo:
        return None
    out = subprocess.run([gdalinfo, str(path)], capture_output=True, text=True).stdout
    lines = out.splitlines()
    keep = []
    for i, ln in enumerate(lines):
        if ln.startswith("Coordinate System is:"):
            keep.append(ln)
            j = i + 1
            while j < len(lines) and lines[j].strip():
                keep.append("  " + lines[j].strip())
                j += 1
        elif ln.startswith(("Origin", "Pixel Size", "Upper Left", "Lower Right")):
            keep.append(ln)
    return "\n".join(keep) if keep else None


# ---------------- 高层入口 ----------------

def attach_geo_for_config(
    config: Config,
    meta: LayerMeta | None = None,
    file: str | Path | None = None,
    epsg: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    on_event=None,
) -> dict:
    """按 config 的范围 + 在线图层元数据为输出文件附加地理标签。

    返回 {written, path, x0, y0, width, height, resolution, epsg, message}。
    """
    if meta is None:
        meta = fetch_layer_meta(config)
    geo = compute_geo(config, meta)
    use_epsg = epsg or config.epsg

    if on_event:
        on_event(make_event(
            EV_PROGRESS, stage=STAGE_GEO, state="running", percent=10.0,
            message=f"瓦片格网: 原点 ({geo['origin_x']:g}, {geo['origin_y']:g}), "
                    f"瓦片 {geo['tile_size']}×{geo['tile_size']}, 级别 {geo['level']}, "
                    f"分辨率 {geo['resolution']:.6g} °/px"))

    path = config.resolve_path(file) if file else config.resolve_path(config.output_file)
    if not path.exists():
        raise GeoRefError(f"找不到文件 {path}")

    with open(path, "rb") as f:
        _, entries = read_head_ifd(f)
    fw = next(value for tag, *_rest, value in entries if tag == TAG_IMAGE_WIDTH)
    fh = next(value for tag, *_rest, value in entries if tag == TAG_IMAGE_LENGTH)

    written = False
    message = ""
    if dry_run:
        message = "(dry-run) 未写入任何内容"
    else:
        if (fw, fh) != (geo["width"], geo["height"]) and not force:
            raise GeoRefError(
                f"文件尺寸 {fw}×{fh} 与配置范围计算值 {geo['width']}×{geo['height']} 不一致；"
                "该文件可能不是按当前配置范围合并的，如确需继续请加 force")
        written = attach_geo(path, geo["x0"], geo["y0"], geo["resolution"],
                             use_epsg, force=force, on_event=on_event)
        message = f"已为 {path} 附加地理信息 (EPSG:{use_epsg})"

    if on_event:
        on_event(make_event(
            EV_PROGRESS, stage=STAGE_GEO,
            state="done" if (written or dry_run) else "running",
            percent=100.0, message=message))

    return {
        "written": written,
        "path": str(path),
        "x0": geo["x0"], "y0": geo["y0"],
        "width": geo["width"], "height": geo["height"],
        "resolution": geo["resolution"],
        "epsg": use_epsg,
        "message": message,
    }


def _log(on_event, message: str) -> None:
    if on_event:
        try:
            on_event(make_event(EV_LOG, stage=STAGE_GEO, message=message))
        except Exception:
            pass


# ---------------- CLI ----------------

def cli_main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="为合并后的分块 BigTIFF 附加地理参考信息（GeoTIFF 标签）")
    ap.add_argument("--file", default=None, help="要修补的 TIFF（默认 config 的 output_file）")
    ap.add_argument("--epsg", type=int, default=None, help="坐标系 EPSG 编码（默认 4326）")
    ap.add_argument("--dry-run", action="store_true", help="只计算并打印，不写文件")
    ap.add_argument("--force", action="store_true", help="尺寸不一致 / 已含地理标签时仍继续")
    ap.add_argument("--refresh", action="store_true", help="强制重新拉取图层元数据")
    args = ap.parse_args(argv)

    config = Config.load()
    try:
        meta = fetch_layer_meta(config, refresh=args.refresh)
    except MetaError as e:
        sys.exit(f"错误: {e}")

    geo = compute_geo(config, meta)
    res = geo["resolution"]
    xmax = geo["x0"] + geo["width"] * res
    ymin = geo["y0"] - geo["height"] * res
    print(f"图层: {config.layer}（元数据获取于 "
          f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(meta.fetched_at))}）")
    print("地理范围 (EPSG:%d, 度):" % (args.epsg or config.epsg))
    print("  左上 (%.10f, %.10f)   右上 (%.10f, %.10f)"
          % (geo["x0"], geo["y0"], xmax, geo["y0"]))
    print("  左下 (%.10f, %.10f)   右下 (%.10f, %.10f)"
          % (geo["x0"], ymin, xmax, ymin))
    print("GeoTransform: (%.10f, %.17g, 0, %.10f, 0, %.17g)"
          % (geo["x0"], res, geo["y0"], -res))

    try:
        result = attach_geo_for_config(
            config, meta=meta, file=args.file, epsg=args.epsg,
            force=args.force, dry_run=args.dry_run,
            on_event=lambda ev: print(ev["message"]) if ev.get("message") else None,
        )
    except (GeoRefError, MetaError) as e:
        sys.exit(f"错误: {e}")

    if not args.dry_run:
        info = verify_geo(Path(result["path"]))
        if info:
            print("验证 (gdalinfo):")
            print(info)
        else:
            print("提示: 未找到 gdalinfo，可手动执行 gdalinfo 验证")


if __name__ == "__main__":
    cli_main()
