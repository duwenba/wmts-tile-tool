#!/usr/bin/env python3
"""
WMTS瓦片拼接脚本（极致优化版）

内存和性能优化：
1. 按行分批处理，避免同时加载所有瓦片
2. 使用numpy直接操作，避免中间对象
3. 流式写出 PNG：不再把整张大图放在内存里，内存占用只与单批/单行有关，
   因此超大范围（例如几万瓦片拼出的几 GB 大图）也不会 OOM 崩溃
4. 保存优化：支持快速保存或优化压缩模式
"""

import os
import time
import gc
import struct
import zlib
from pathlib import Path
import numpy as np
from PIL import Image
from config import OUTPUT_DIR as TILES_DIR, TILE_MATRIX, TILE_COL_START, TILE_COL_END, TILE_ROW_START, TILE_ROW_END, OUTPUT_FILE
from tile_path import iter_tiles


def _notify(callback, info):
    """安全地调用进度回调（供 GUI 使用）"""
    if callback:
        try:
            callback(info)
        except Exception:
            pass


class StreamPNGWriter:
    """
    逐行写出 PNG 文件的流式写入器。

    传统做法（np.zeros / Image.new / memmap+np.array）会把整张图片放进内存，
    在瓦片量大、输出图达到数 GB 时会直接 OOM 崩溃。本类把每行像素经过
    Sub 滤波后压入 zlib 流并写成 IDAT 块，内存占用只与单行宽度有关。
    """

    def __init__(self, path, width, height, compress_level=1):
        self.path = path
        self.width = width
        self.height = height
        self.f = open(path, "wb")
        self.f.write(b"\x89PNG\r\n\x1a\n")
        # IHDR: 8bit truecolor(RGB) 无隔行
        ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        self._chunk(b"IHDR", ihdr)
        self.compressor = zlib.compressobj(compress_level)
        self._finished = False

    def _chunk(self, ctype, data):
        self.f.write(struct.pack(">I", len(data)))
        self.f.write(ctype)
        self.f.write(data)
        self.f.write(struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF))

    def write_row(self, row):
        """写入一行像素。row: (width, 3) 的 uint8 数组"""
        arr = np.ascontiguousarray(row, dtype=np.uint8)
        if arr.ndim != 2 or arr.shape[0] != self.width:
            raise ValueError(f"行宽不匹配: 期望 {self.width}, 得到 {arr.shape}")
        # PNG Sub 滤波（bpp=3）：对展平后的字节流逐字节与左侧第 3 个字节做差，
        # uint8 自动按 256 取模
        flat = arr.reshape(-1)
        sub = flat.copy()
        sub[3:] -= sub[:-3]
        raw = b"\x01" + sub.tobytes()
        c = self.compressor.compress(raw)
        if c:
            self._chunk(b"IDAT", c)

    def finish(self):
        c = self.compressor.flush()
        if c:
            self._chunk(b"IDAT", c)
        self._chunk(b"IEND", b"")
        self.f.close()
        self._finished = True

    def abort(self):
        """出错时关闭并删除不完整的输出文件"""
        try:
            self.f.close()
        except Exception:
            pass
        try:
            if os.path.exists(self.path):
                os.remove(self.path)
        except OSError:
            pass


def pre_scan_tiles():
    """预扫描瓦片目录，构建 (col, row) -> path 的字典索引（兼容新旧目录结构）"""
    print("正在扫描瓦片目录...")
    start_time = time.time()

    tiles_dict = {}
    for matrix, col, row, path in iter_tiles(TILES_DIR):
        if matrix == TILE_MATRIX:
            tiles_dict[(col, row)] = path

    scan_time = time.time() - start_time
    print(f"扫描完成！找到 {len(tiles_dict)} 个瓦片文件，耗时 {scan_time:.2f} 秒")
    return tiles_dict


def load_tile_as_array(tile_path):
    """加载单个瓦片为 numpy 数组"""
    try:
        img = Image.open(tile_path)
        if img.mode != 'RGB':
            img = img.convert('RGB')
        arr = np.array(img)
        img.close()
        return arr
    except Exception:
        return None


def load_tiles_by_row(tiles_dict, row, col_start, col_end):
    """加载指定行的所有瓦片"""
    row_tiles = []
    for col in range(col_start, col_end + 1):
        key = (col, row)
        if key in tiles_dict:
            arr = load_tile_as_array(tiles_dict[key])
            if arr is not None:
                row_tiles.append((col, arr))
    return row_tiles


def _open_sample(tiles_dict):
    """获取一块瓦片用于读取尺寸"""
    first_key = (TILE_COL_START, TILE_ROW_START)
    sample_path = tiles_dict.get(first_key)
    if not sample_path:
        sample_key = list(tiles_dict.keys())[0]
        sample_path = tiles_dict[sample_key]
    sample_img = Image.open(sample_path)
    tile_width, tile_height = sample_img.size
    sample_img.close()
    return tile_width, tile_height


def _compress_level(use_fast_save):
    """快速保存 -> 低压缩级别；优化压缩 -> 较高级别"""
    return 1 if use_fast_save else 6


def _finish_stats(writer, start_time, loaded, missing, progress_callback):
    """保存收尾：写 IEND、统计文件信息、回调完成事件"""
    writer.finish()
    save_time = time.time() - start_time
    file_size = os.path.getsize(OUTPUT_FILE) / (1024 * 1024)

    print("-" * 60)
    print(f"拼接成功！")
    print(f"  输出文件: {os.path.abspath(OUTPUT_FILE)}")
    print(f"  文件大小: {file_size:.2f} MB")
    print(f"  加载瓦片: {loaded}")
    print(f"  缺失瓦片: {missing}")
    print(f"  总耗时: {save_time:.2f} 秒")
    print("=" * 60)

    _notify(progress_callback, {
        "stage": "done",
        "percent": 100,
        "loaded": loaded,
        "missing": missing,
        "output": os.path.abspath(OUTPUT_FILE),
        "size_mb": file_size,
        "elapsed": save_time,
    })


def merge_tiles_ultra(use_fast_save=True, batch_rows=10, progress_callback=None):
    """
    批量模式：按批粘贴到内存块，再逐行流式写出 PNG。

    参数:
        use_fast_save: True=快速保存(低压缩级别)，False=优化压缩
        batch_rows: 每批处理的行数，用于平衡速度和内存
        progress_callback: 进度回调，接收 dict（stage/percent/loaded/missing 等），供 GUI 使用
    """
    print("=" * 60)
    print("瓦片拼接（批量模式 - 流式写出）")
    print("=" * 60)

    if not os.path.exists(TILES_DIR):
        print(f"错误: 瓦片目录 '{TILES_DIR}' 不存在")
        return False

    tiles_dict = pre_scan_tiles()
    if not tiles_dict:
        print(f"错误: 没有找到任何瓦片文件")
        return False

    _notify(progress_callback, {"stage": "scan", "percent": 2, "message": f"扫描完成，找到 {len(tiles_dict)} 个瓦片"})

    tile_width, tile_height = _open_sample(tiles_dict)
    print(f"瓦片尺寸: {tile_width}x{tile_height}")

    cols = TILE_COL_END - TILE_COL_START + 1
    rows = TILE_ROW_END - TILE_ROW_START + 1
    total_width = cols * tile_width
    total_height = rows * tile_height
    total_tiles = cols * rows

    print(f"拼接尺寸: {total_width}x{total_height} ({cols}列 x {rows}行 = {total_tiles} 瓦片)")

    writer = StreamPNGWriter(OUTPUT_FILE, total_width, total_height,
                             compress_level=_compress_level(use_fast_save))
    start_time = time.time()
    loaded = 0

    try:
        for batch_start in range(TILE_ROW_START, TILE_ROW_END + 1, batch_rows):
            batch_end = min(batch_start + batch_rows - 1, TILE_ROW_END)
            batch_h = batch_end - batch_start + 1

            # 当前批次的整块像素（内存只占 batch_rows 行瓦片）
            block = np.zeros((batch_h * tile_height, total_width, 3), dtype=np.uint8)
            for row in range(batch_start, batch_end + 1):
                row_tiles = load_tiles_by_row(tiles_dict, row, TILE_COL_START, TILE_COL_END)
                for col, arr in row_tiles:
                    y0 = (row - batch_start) * tile_height
                    x0 = (col - TILE_COL_START) * tile_width
                    block[y0:y0 + tile_height, x0:x0 + tile_width, :] = arr
                    loaded += 1

            for i in range(batch_h * tile_height):
                writer.write_row(block[i])
            del block
            gc.collect()

            completed_rows = batch_end - TILE_ROW_START + 1
            progress = (completed_rows / rows) * 100
            elapsed = time.time() - start_time
            eta = elapsed / completed_rows * (rows - completed_rows) if completed_rows > 0 else 0
            print(f"\r进度: {progress:.1f}% ({completed_rows}/{rows} 行) | 已用: {elapsed:.1f}s | 预计剩余: {eta:.1f}s", end='', flush=True)
            _notify(progress_callback, {
                "stage": "merge",
                "percent": progress,
                "done_rows": completed_rows,
                "total_rows": rows,
                "loaded": loaded,
                "missing": total_tiles - loaded,
                "elapsed": elapsed,
                "eta": eta,
            })
    except Exception as e:
        writer.abort()
        print(f"\n错误: 拼接失败 - {e}")
        _notify(progress_callback, {"stage": "failed", "percent": 100, "message": f"拼接失败: {e}"})
        return False

    print()
    missing = total_tiles - loaded
    _finish_stats(writer, start_time, loaded, missing, progress_callback)
    return True


def merge_tiles_streaming_v2(use_fast_save=True, progress_callback=None):
    """
    流式模式：逐行拼接并写出 PNG（内存占用只有一行）
    """
    print("=" * 60)
    print("瓦片拼接（流式模式 - 低内存）")
    print("=" * 60)

    if not os.path.exists(TILES_DIR):
        print(f"错误: 瓦片目录 '{TILES_DIR}' 不存在")
        return False

    tiles_dict = pre_scan_tiles()
    if not tiles_dict:
        print(f"错误: 没有找到任何瓦片文件")
        return False

    _notify(progress_callback, {"stage": "scan", "percent": 2, "message": f"扫描完成，找到 {len(tiles_dict)} 个瓦片"})

    tile_width, tile_height = _open_sample(tiles_dict)
    cols = TILE_COL_END - TILE_COL_START + 1
    rows = TILE_ROW_END - TILE_ROW_START + 1
    total_width = cols * tile_width
    total_height = rows * tile_height
    total_tiles = cols * rows

    print(f"拼接尺寸: {total_width}x{total_height} ({cols}列 x {rows}行 = {total_tiles} 瓦片)")

    writer = StreamPNGWriter(OUTPUT_FILE, total_width, total_height,
                             compress_level=_compress_level(use_fast_save))
    start_time = time.time()
    loaded = 0

    try:
        for row_offset, row in enumerate(range(TILE_ROW_START, TILE_ROW_END + 1)):
            row_array = np.zeros((total_width, 3), dtype=np.uint8)
            row_tiles = load_tiles_by_row(tiles_dict, row, TILE_COL_START, TILE_COL_END)
            for j in range(tile_height):
                row_array[:] = 0
                for col, arr in row_tiles:
                    x0 = (col - TILE_COL_START) * tile_width
                    row_array[x0:x0 + tile_width, :] = arr[j]
                writer.write_row(row_array)
            loaded += len(row_tiles)
            del row_tiles

            progress = ((row_offset + 1) / rows) * 100
            elapsed = time.time() - start_time
            eta = elapsed / (row_offset + 1) * (rows - row_offset - 1) if row_offset >= 0 else 0
            print(f"\r进度: {progress:.1f}% ({row_offset + 1}/{rows} 行) | 已用: {elapsed:.1f}s | 预计剩余: {eta:.1f}s", end='', flush=True)
            _notify(progress_callback, {
                "stage": "merge",
                "percent": progress,
                "done_rows": row_offset + 1,
                "total_rows": rows,
                "loaded": loaded,
                "missing": total_tiles - loaded,
                "elapsed": elapsed,
                "eta": eta,
            })
    except Exception as e:
        writer.abort()
        print(f"\n错误: 拼接失败 - {e}")
        _notify(progress_callback, {"stage": "failed", "percent": 100, "message": f"拼接失败: {e}"})
        return False

    print()
    missing = total_tiles - loaded
    _finish_stats(writer, start_time, loaded, missing, progress_callback)
    return True


def merge_tiles_low_memory(use_fast_save=True, progress_callback=None):
    """
    超低内存模式：与流式模式相同——逐行拼接并流式写出 PNG，
    全程不创建整张大图，几 GB 的输出也不会占用几 GB 内存。
    """
    print("=" * 60)
    print("瓦片拼接（超低内存模式 - 流式写出）")
    print("=" * 60)

    if not os.path.exists(TILES_DIR):
        print(f"错误: 瓦片目录 '{TILES_DIR}' 不存在")
        return False

    tiles_dict = pre_scan_tiles()
    if not tiles_dict:
        print(f"错误: 没有找到任何瓦片文件")
        return False

    _notify(progress_callback, {"stage": "scan", "percent": 2, "message": f"扫描完成，找到 {len(tiles_dict)} 个瓦片"})

    tile_width, tile_height = _open_sample(tiles_dict)
    cols = TILE_COL_END - TILE_COL_START + 1
    rows = TILE_ROW_END - TILE_ROW_START + 1
    total_width = cols * tile_width
    total_height = rows * tile_height
    total_tiles = cols * rows

    print(f"拼接尺寸: {total_width}x{total_height} ({cols}列 x {rows}行 = {total_tiles} 瓦片)")

    writer = StreamPNGWriter(OUTPUT_FILE, total_width, total_height,
                             compress_level=_compress_level(use_fast_save))
    start_time = time.time()
    loaded = 0

    try:
        for row_offset, row in enumerate(range(TILE_ROW_START, TILE_ROW_END + 1)):
            row_array = np.zeros((total_width, 3), dtype=np.uint8)
            row_tiles = load_tiles_by_row(tiles_dict, row, TILE_COL_START, TILE_COL_END)
            for j in range(tile_height):
                row_array[:] = 0
                for col, arr in row_tiles:
                    x0 = (col - TILE_COL_START) * tile_width
                    row_array[x0:x0 + tile_width, :] = arr[j]
                writer.write_row(row_array)
            loaded += len(row_tiles)
            del row_tiles

            progress = ((row_offset + 1) / rows) * 100
            elapsed = time.time() - start_time
            eta = elapsed / (row_offset + 1) * (rows - row_offset - 1) if row_offset >= 0 else 0
            print(f"\r进度: {progress:.1f}% ({row_offset + 1}/{rows} 行) | 已用: {elapsed:.1f}s | 预计剩余: {eta:.1f}s", end='', flush=True)
            _notify(progress_callback, {
                "stage": "merge",
                "percent": progress,
                "done_rows": row_offset + 1,
                "total_rows": rows,
                "loaded": loaded,
                "missing": total_tiles - loaded,
                "elapsed": elapsed,
                "eta": eta,
            })
    except Exception as e:
        writer.abort()
        print(f"\n错误: 拼接失败 - {e}")
        _notify(progress_callback, {"stage": "failed", "percent": 100, "message": f"拼接失败: {e}"})
        return False

    print()
    missing = total_tiles - loaded
    _finish_stats(writer, start_time, loaded, missing, progress_callback)
    return True


if __name__ == "__main__":
    import sys

    # 默认使用 streaming fast 模式
    if len(sys.argv) < 2:
        mode = "streaming"
        save_mode = "fast"
    else:
        mode = sys.argv[1]
        save_mode = sys.argv[2] if len(sys.argv) > 2 else "fast"

    # 显示帮助
    if mode in ["-h", "--help", "help"]:
        print("使用方法:")
        print("  python merge_tiles_ultra.py [mode] [save_mode]  # 默认: streaming fast")
        print()
        print("模式:")
        print("  batch     - 批量模式（平衡速度和内存）")
        print("  streaming - 流式模式（低内存，默认）")
        print("  lowmem    - 超低内存模式（超大图）")
        print()
        print("保存模式:")
        print("  fast     - 快速保存，低压缩级别（默认）")
        print("  optimize - 优化压缩，文件更小但速度较慢")
        print()
        print("示例:")
        print("  python merge_tiles_ultra.py                # 默认: streaming fast")
        print("  python merge_tiles_ultra.py batch fast      # 批量模式，快速保存")
        print("  python merge_tiles_ultra.py batch optimize  # 批量模式，优化压缩")
        sys.exit(0)

    use_fast_save = (save_mode == "fast")

    if mode == "batch":
        # 批量模式：平衡速度和内存
        batch_size = int(sys.argv[3]) if len(sys.argv) > 3 else 10
        merge_tiles_ultra(use_fast_save=use_fast_save, batch_rows=batch_size)
    elif mode == "streaming":
        # 流式模式：最低内存
        merge_tiles_streaming_v2(use_fast_save=use_fast_save)
    elif mode == "lowmem":
        # 超低内存：流式写出
        merge_tiles_low_memory(use_fast_save=use_fast_save)
    else:
        print(f"错误: 未知模式 '{mode}'")
        print("使用 'python merge_tiles_ultra.py --help' 查看帮助")
        sys.exit(1)
