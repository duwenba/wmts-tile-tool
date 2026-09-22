"""
wmts.core.cache — 瓦片缓存管理（统计 / 清理 / 迁移 / 校验 / 范围状态扫描）。

缓存结构 v3（按图层分层）：
    tiles/{layer}/{matrix}/{row}/{col}.png

兼容读取旧结构 v2（tiles/{matrix}/{row}/{col}.png）与 v1 扁平结构；
用 `tile_cache.py migrate --layer <图层>` 把旧结构归入指定图层目录。

由原 tile_cache.py 函数化重构而来，纯逻辑、UI 无关；CLI 入口见 ``cli_main``。
"""

from __future__ import annotations

import argparse
import base64
import os
import shutil
import sys
import time
from collections import defaultdict

from .paths import (
    ensure_tile_dir,
    iter_tiles,
    legacy_tile_path,
    legacy_v2_path,
    safe_layer,
    tile_path,
)

PNG_HEADER = b"\x89PNG\r\n\x1a\n"

# 范围扫描状态码（2 bit/片）
TILE_MISSING = 0
TILE_OK = 1
TILE_INVALID = 2

# 旧结构（未分层）在统计中的展示名
LEGACY_LAYER_LABEL = "(未分层)"


def _fmt_mb(n):
    return f"{n / (1024 * 1024):.1f} MB"


# ---------------- 统计 ----------------

def cache_stats(base="tiles"):
    """统计缓存，按 图层 → 级别 聚合。

    返回 (total_tiles, total_bytes, per_layer)，
    其中 per_layer[layer_label][matrix] = {tiles,size,min_col,max_col,min_row,max_row,rows}
    （旧结构未分层瓦片归入 "(未分层)"）。
    """
    per_layer: dict = defaultdict(lambda: defaultdict(lambda: {
        "tiles": 0, "size": 0, "min_col": None, "max_col": None,
        "min_row": None, "max_row": None, "rows": set(),
    }))
    total_tiles = 0
    total_bytes = 0

    for layer, matrix, col, row, path in iter_tiles(base):
        label = layer or LEGACY_LAYER_LABEL
        st = os.stat(path)
        m = per_layer[label][matrix]
        m["tiles"] += 1
        m["size"] += st.st_size
        m["min_col"] = col if m["min_col"] is None else min(m["min_col"], col)
        m["max_col"] = col if m["max_col"] is None else max(m["max_col"], col)
        m["min_row"] = row if m["min_row"] is None else min(m["min_row"], row)
        m["max_row"] = row if m["max_row"] is None else max(m["max_row"], row)
        m["rows"].add(row)
        total_tiles += 1
        total_bytes += st.st_size

    out: dict = {}
    for label, matrices in per_layer.items():
        out[label] = {}
        for matrix, m in matrices.items():
            m["rows"] = len(m["rows"])
            out[label][matrix] = m
    return total_tiles, total_bytes, out


def print_stats(base="tiles"):
    total_tiles, total_bytes, per_layer = cache_stats(base)
    if not os.path.isdir(base):
        print(f"缓存目录不存在: {base}")
        return
    print(f"缓存目录: {os.path.abspath(base)}")
    print(f"总瓦片数: {total_tiles} | 总大小: {_fmt_mb(total_bytes)}")
    print("-" * 78)
    print(f"{'图层':<26}{'级别':>6}{'瓦片数':>10}{'大小':>12}{'列范围':>18}{'行范围':>18}")
    for label in sorted(per_layer):
        for matrix in sorted(per_layer[label]):
            m = per_layer[label][matrix]
            col_rng = f"{m['min_col']}-{m['max_col']}" if m['min_col'] is not None else "-"
            row_rng = f"{m['min_row']}-{m['max_row']}" if m['min_row'] is not None else "-"
            print(f"{label:<26}{matrix:>6}{m['tiles']:>10}{_fmt_mb(m['size']):>12}"
                  f"{col_rng:>18}{row_rng:>18}")


# ---------------- 范围状态扫描（供 API 状态网格用） ----------------

def range_status(base, matrix, col_start, col_end, row_start, row_end, layer=None):
    """扫描范围内瓦片状态（优先当前图层目录，回退旧结构）。

    返回 (counts, packed_b64)：packed 为 2bit/片的位图，按行优先。
    """
    cols = col_end - col_start + 1
    rows = row_end - row_start + 1
    total = cols * rows
    arr = bytearray((total + 3) // 4)

    def set_status(idx, status):
        byte_i, shift = divmod(idx * 2, 8)
        arr[byte_i] |= status << shift

    lay = safe_layer(layer)
    index = 0
    for row in range(row_start, row_end + 1):
        for col in range(col_start, col_end + 1):
            candidates = []
            if lay:
                candidates.append(tile_path(matrix, col, row, base, lay))
            candidates.append(legacy_v2_path(matrix, col, row, base))
            candidates.append(legacy_tile_path(matrix, col, row, base))
            path = next((p for p in candidates if os.path.isfile(p)), None)
            if path is None:
                status = TILE_MISSING
            else:
                try:
                    with open(path, "rb") as f:
                        status = TILE_OK if f.read(8) == PNG_HEADER else TILE_INVALID
                except OSError:
                    status = TILE_INVALID
            set_status(index, status)
            index += 1

    counts = {"missing": 0, "ok": 0, "invalid": 0, "total": total}
    for idx in range(total):
        byte_i, shift = divmod(idx * 2, 8)
        s = (arr[byte_i] >> shift) & 0x3
        if s == TILE_OK:
            counts["ok"] += 1
        elif s == TILE_INVALID:
            counts["invalid"] += 1
        else:
            counts["missing"] += 1
    return counts, base64.b64encode(bytes(arr)).decode("ascii")


# ---------------- 删除 ----------------

def _remove_with_cleanup(path, base):
    """删除单个瓦片，若其所在行列目录变空则一并清理空目录（最多向上 3 层）"""
    os.remove(path)
    d = os.path.dirname(path)
    for _ in range(3):
        try:
            os.rmdir(d)
        except OSError:
            break
        d = os.path.dirname(d)


def _match_layer(layer, want):
    """层过滤：want 为空表示不限图层；None 图层按 "(未分层)" 处理。"""
    if not want:
        return True
    return (layer or LEGACY_LAYER_LABEL) == want


def _delete_if(pred, base="tiles"):
    """遍历瓦片，删除 pred(layer, matrix, col, row, path, st) 为 True 的瓦片。

    返回 (removed, freed_bytes)
    """
    removed = 0
    freed = 0
    for layer, matrix, col, row, path in iter_tiles(base):
        try:
            st = os.stat(path)
        except OSError:
            continue
        if pred(layer, matrix, col, row, path, st):
            _remove_with_cleanup(path, base)
            removed += 1
            freed += st.st_size
    return removed, freed


def prune_matrix(base, matrix, layer=None):
    return _delete_if(
        lambda l, m, c, r, p, st: m == matrix and _match_layer(l, layer), base)


def prune_range(base, matrix, col_start, col_end, row_start, row_end, layer=None):
    return _delete_if(
        lambda l, m, c, r, p, st: m == matrix
        and col_start <= c <= col_end and row_start <= r <= row_end
        and _match_layer(l, layer),
        base,
    )


def prune_older_than(base, days, layer=None):
    cutoff = time.time() - days * 86400
    return _delete_if(
        lambda l, m, c, r, p, st: st.st_mtime < cutoff and _match_layer(l, layer),
        base)


def prune_to_max_size(base, max_mb, layer=None):
    total_tiles, total_bytes, _ = cache_stats(base)
    if total_bytes <= max_mb * 1024 * 1024:
        return 0, 0
    entries = [(st.st_mtime, path) for layer, m, c, r, path, st in
               ((l, m, c, r, p, os.stat(p))
                for l, m, c, r, p in iter_tiles(base)
                if _match_layer(l, layer))]
    entries.sort()
    removed = 0
    freed = 0
    for _mtime, path in entries:
        if total_bytes - freed <= max_mb * 1024 * 1024:
            break
        try:
            sz = os.path.getsize(path)
            _remove_with_cleanup(path, base)
            removed += 1
            freed += sz
        except OSError:
            continue
    return removed, freed


def clear_cache(base):
    """清空缓存（删除缓存目录下所有内容，保留目录本身）"""
    removed = 0
    freed = 0
    if not os.path.isdir(base):
        return 0, 0
    for name in os.listdir(base):
        p = os.path.join(base, name)
        if os.path.isfile(p) or os.path.islink(p):
            sz = os.path.getsize(p)
            os.remove(p)
            removed += 1
            freed += sz
        elif os.path.isdir(p):
            for root, _dirs, files in os.walk(p):
                freed += sum(os.path.getsize(os.path.join(root, f)) for f in files)
            removed += 1
            shutil.rmtree(p)
    return removed, freed


# ---------------- 迁移 ----------------

def migrate(base="tiles", layer=None, dry_run=False):
    """把旧结构（v1 扁平 / v2 无图层分级）迁移到 v3 分层结构。

    layer：目标图层名（旧结构不含图层信息，必须由调用方指定）。
    返回 (moved, skipped, legacy_left)：
        moved      实际移动的文件数
        skipped    目标已存在而跳过的文件数
        legacy_left 迁移后仍在旧位置的文件数（dry_run 时为待迁移数）
    """
    if not layer:
        raise ValueError("迁移需要指定目标图层（旧结构不含图层信息）")
    moved = 0
    skipped = 0
    legacy_left = 0

    def move_one(matrix, col, row, src):
        nonlocal moved, skipped
        dst = tile_path(matrix, col, row, base, layer)
        if os.path.exists(dst):
            skipped += 1
            return
        if not dry_run:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(src, dst)
        moved += 1

    if not os.path.isdir(base):
        return 0, 0, 0

    # v1 扁平：{matrix}_{col}_{row}.png
    for name in sorted(os.listdir(base)):
        if not name.endswith(".png"):
            continue
        try:
            matrix, col, row = (int(x) for x in name[:-4].split("_"))
        except ValueError:
            continue
        move_one(matrix, col, row, os.path.join(base, name))

    # v2 无图层分级：{matrix}/{row}/{col}.png（目录名为纯数字）
    for entry in sorted(os.listdir(base)):
        d = os.path.join(base, entry)
        if not (os.path.isdir(d) and entry.isdigit()):
            continue
        for row_name in sorted(os.listdir(d)):
            row_dir = os.path.join(d, row_name)
            if not (os.path.isdir(row_dir) and row_name.isdigit()):
                continue
            for fn in sorted(os.listdir(row_dir)):
                if not fn.endswith(".png") or not fn[:-4].isdigit():
                    continue
                move_one(int(entry), int(fn[:-4]), int(row_name),
                         os.path.join(row_dir, fn))

    # 统计旧位置剩余
    for lay, m, c, r, path in iter_tiles(base):
        if lay is None:
            legacy_left += 1
    return moved, skipped, legacy_left


# ---------------- 校验 ----------------

def verify(base="tiles", layer=None):
    """检查瓦片是否为有效 PNG（只读文件头），返回 (total, bad_list)。"""
    bad = []
    total = 0
    for lay, _m, _c, _r, path in iter_tiles(base):
        if not _match_layer(lay, layer):
            continue
        total += 1
        try:
            with open(path, "rb") as f:
                if f.read(8) != PNG_HEADER:
                    bad.append(path)
        except OSError as e:
            bad.append(f"{path} ({e})")
    return total, bad


# ---------------- 便捷封装（供 API 使用） ----------------

def prune(base, layer=None, matrix=None, col_start=None, col_end=None,
          row_start=None, row_end=None, older_than=None, max_size=None):
    """统一清理入口：按 图层 / 范围 / 级别 / 时间 / 容量 清理。返回 (removed, freed)。"""
    if matrix is not None and any(v is not None for v in
                                  (col_start, col_end, row_start, row_end)):
        cs = col_start if col_start is not None else 0
        ce = col_end if col_end is not None else 2_000_000
        rs = row_start if row_start is not None else 0
        re = row_end if row_end is not None else 2_000_000
        return prune_range(base, matrix, cs, ce, rs, re, layer)
    if matrix is not None:
        return prune_matrix(base, matrix, layer)
    if older_than is not None:
        return prune_older_than(base, older_than, layer)
    if max_size is not None:
        return prune_to_max_size(base, max_size, layer)
    raise ValueError("prune 需要至少一个条件：matrix / older_than / max_size")


# ---------------- CLI ----------------

def cli_main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="WMTS 瓦片缓存管理（按图层分层）")
    ap.add_argument("--dir", default="tiles", help="缓存目录（默认 tiles）")
    ap.add_argument("--layer", default=None,
                    help="图层（默认取 config.json/config.py 的当前图层）")

    # 子命令也接受 --dir/--layer（SUPPRESS 保证不覆盖全局值），
    # 这样 `--layer X migrate` 与 `migrate --layer X` 两种写法都可用。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", default=argparse.SUPPRESS)
    common.add_argument("--layer", default=argparse.SUPPRESS)

    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stats", help="统计缓存（按图层/级别）", parents=[common])

    p_mig = sub.add_parser("migrate", help="把旧结构迁移到按图层分层结构",
                           parents=[common])
    p_mig.add_argument("--dry-run", action="store_true", help="只预览不动文件")

    p_prune = sub.add_parser("prune", help="清理瓦片（可按图层/级别/范围/时间/容量）",
                             parents=[common])
    p_prune.add_argument("--matrix", type=int, help="只清理该级别")
    p_prune.add_argument("--col-start", type=int)
    p_prune.add_argument("--col-end", type=int)
    p_prune.add_argument("--row-start", type=int)
    p_prune.add_argument("--row-end", type=int)
    p_prune.add_argument("--older-than", type=int, help="删除 N 天前的瓦片")
    p_prune.add_argument("--max-size", type=int, help="删除最旧瓦片直到总大小 ≤ N MB")
    p_prune.add_argument("--all-layers", action="store_true",
                         help="不限图层（默认只清理 --layer / 当前配置图层）")

    p_clear = sub.add_parser("clear", help="清空缓存（全部图层）", parents=[common])
    p_clear.add_argument("--yes", action="store_true", help="跳过确认")

    p_verify = sub.add_parser("verify", help="校验瓦片 PNG 完整性", parents=[common])
    p_verify.add_argument("--all-layers", action="store_true", help="不限图层")

    args = ap.parse_args(argv)
    base = args.dir

    layer = args.layer
    if layer is None:
        try:
            from .config import Config
            layer = Config.load().layer
        except Exception:
            layer = None

    if args.cmd == "stats":
        print_stats(base)
        return

    if args.cmd == "migrate":
        if not layer:
            sys.exit("错误: migrate 需要 --layer 指定目标图层")
        moved, skipped, left = migrate(base, layer, dry_run=args.dry_run)
        prefix = "（dry-run）" if args.dry_run else ""
        print(f"{prefix}迁移到图层 {layer}：移动 {moved} 个文件"
              + (f"，目标已存在跳过 {skipped} 个" if skipped else "")
              + (f"，旧位置仍剩 {left} 个" if left else ""))
        return

    if args.cmd == "verify":
        total, bad = verify(base, layer=None if args.all_layers else layer)
        scope = "全部图层" if args.all_layers else f"图层 {layer}"
        print(f"{scope}: 共 {total} 个瓦片，损坏 {len(bad)} 个")
        for b in bad[:20]:
            print("  坏文件:", b)
        return

    if args.cmd == "clear":
        if not args.yes:
            total, size, _ = cache_stats(base)
            ans = input(f"将删除全部 {total} 个瓦片（{_fmt_mb(size)}，含所有图层），确认？[y/N] ")
            if ans.strip().lower() not in ("y", "yes"):
                print("已取消")
                return
        removed, freed = clear_cache(base)
        print(f"已清空：删除 {removed} 项，释放 {_fmt_mb(freed)}")
        return

    if args.cmd == "prune":
        use_layer = None if args.all_layers else layer
        removed, freed = prune(base, layer=use_layer, matrix=args.matrix,
                               col_start=args.col_start, col_end=args.col_end,
                               row_start=args.row_start, row_end=args.row_end,
                               older_than=args.older_than, max_size=args.max_size)
        scope = "全部图层" if args.all_layers else f"图层 {use_layer}"
        print(f"清理完成（{scope}）：删除 {removed} 个瓦片，释放 {_fmt_mb(freed)}")


if __name__ == "__main__":
    cli_main()
