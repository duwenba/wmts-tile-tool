#!/usr/bin/env python3
"""
瓦片缓存管理工具。

统计 / 清理 / 迁移 tiles 缓存目录（兼容 v1 扁平 与 v2 分级两种结构）。

用法：
  python tile_cache.py stats                      # 统计：总量 + 各级别明细
  python tile_cache.py migrate                    # 把旧扁平结构迁移到分级结构
  python tile_cache.py prune --matrix 16          # 删除指定级别的全部瓦片
  python tile_cache.py prune --matrix 16 --col-start 53400 --col-end 53410 \
                             --row-start 10600 --row-end 10610   # 删除范围内瓦片
  python tile_cache.py prune --older-than 30      # 删除 30 天前下载的瓦片
  python tile_cache.py prune --max-size 500       # 按 mtime 删最旧，直到 ≤500 MB
  python tile_cache.py clear                      # 清空缓存（需 --yes 确认）
  python tile_cache.py verify                     # 校验瓦片文件完整性（PNG 头）
"""

import argparse
import os
import shutil
import sys
import time
from collections import defaultdict

from tile_path import legacy_tile_path, tile_path

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


# ---------------- 统计 ----------------

def cache_stats(base="tiles"):
    """统计缓存：返回 (total_tiles, total_bytes, per_matrix)"""
    per_matrix = defaultdict(lambda: {
        "tiles": 0, "size": 0, "min_col": None, "max_col": None,
        "min_row": None, "max_row": None, "rows": set(),
    })
    total_tiles = 0
    total_bytes = 0

    from tile_path import iter_tiles
    for matrix, col, row, path in iter_tiles(base):
        st = os.stat(path)
        per_matrix[matrix]["tiles"] += 1
        per_matrix[matrix]["size"] += st.st_size
        m = per_matrix[matrix]
        m["min_col"] = col if m["min_col"] is None else min(m["min_col"], col)
        m["max_col"] = col if m["max_col"] is None else max(m["max_col"], col)
        m["min_row"] = row if m["min_row"] is None else min(m["min_row"], row)
        m["max_row"] = row if m["max_row"] is None else max(m["max_row"], row)
        m["rows"].add(row)
        total_tiles += 1
        total_bytes += st.st_size

    for m in per_matrix.values():
        m["rows"] = len(m["rows"])
    return total_tiles, total_bytes, dict(per_matrix)


def _fmt_mb(n):
    return f"{n / (1024 * 1024):.1f} MB"


def print_stats(base="tiles"):
    total_tiles, total_bytes, per = cache_stats(base)
    if not os.path.isdir(base):
        print(f"缓存目录不存在: {base}")
        return
    print(f"缓存目录: {os.path.abspath(base)}")
    print(f"总瓦片数: {total_tiles} | 总大小: {_fmt_mb(total_bytes)}")
    print("-" * 70)
    print(f"{'级别':<6}{'瓦片数':>10}{'大小':>12}{'列范围':>18}{'行范围':>18}{'行目录数':>8}")
    for matrix in sorted(per):
        m = per[matrix]
        col_rng = f"{m['min_col']}-{m['max_col']}" if m['min_col'] is not None else "-"
        row_rng = f"{m['min_row']}-{m['max_row']}" if m['min_row'] is not None else "-"
        print(f"{matrix:<6}{m['tiles']:>10}{_fmt_mb(m['size']):>12}{col_rng:>18}{row_rng:>18}{m['rows']:>8}")


# ---------------- 删除 ----------------

def _remove_with_cleanup(path, base):
    """删除单个瓦片，若其所在行列目录变空则一并清理空目录"""
    os.remove(path)
    d = os.path.dirname(path)
    # 向上清理空目录（最多 2 层：行目录、级别目录）
    for _ in range(2):
        try:
            os.rmdir(d)
        except OSError:
            break
        d = os.path.dirname(d)


def _delete_if(pred, base="tiles"):
    """遍历瓦片，删除 pred(matrix, col, row, path, st) 为 True 的瓦片。

    返回 (removed, freed_bytes)
    """
    from tile_path import iter_tiles
    removed = 0
    freed = 0
    for matrix, col, row, path in iter_tiles(base):
        try:
            st = os.stat(path)
        except OSError:
            continue
        if pred(matrix, col, row, path, st):
            _remove_with_cleanup(path, base)
            removed += 1
            freed += st.st_size
    return removed, freed


def prune_matrix(base, matrix):
    return _delete_if(lambda m, c, r, p, st: m == matrix, base)


def prune_range(base, matrix, col_start, col_end, row_start, row_end):
    return _delete_if(
        lambda m, c, r, p, st: m == matrix
        and col_start <= c <= col_end
        and row_start <= r <= row_end,
        base,
    )


def prune_older_than(base, days):
    cutoff = time.time() - days * 86400
    return _delete_if(lambda m, c, r, p, st: st.st_mtime < cutoff, base)


def prune_to_max_size(base, max_mb):
    total_tiles, total_bytes, _ = cache_stats(base)
    if total_bytes <= max_mb * 1024 * 1024:
        return 0, 0
    # 按 mtime 排序，从最旧开始删
    from tile_path import iter_tiles
    entries = [(st.st_mtime, path) for _, _, _, path, st in
               ((m, c, r, p, os.stat(p)) for m, c, r, p in iter_tiles(base))]
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
    """清空缓存（删除 tiles 目录下所有内容，保留目录本身）"""
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

def migrate(base="tiles", dry_run=False):
    """把 v1 扁平结构（{matrix}_{col}_{row}.png）迁移到 v2 分级结构。

    返回 (moved, legacy_left)；仅移动目标位置不存在的瓦片（避免覆盖）。
    """
    moved = 0
    legacy_left = 0
    if not os.path.isdir(base):
        return 0, 0
    for name in sorted(os.listdir(base)):
        if not name.endswith(".png"):
            continue
        try:
            matrix, col, row = (int(x) for x in name[:-4].split("_"))
        except ValueError:
            continue
        src = os.path.join(base, name)
        dst = tile_path(matrix, col, row, base)
        if os.path.exists(dst):
            legacy_left += 1
            continue
        if not dry_run:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(src, dst)
        moved += 1
    return moved, legacy_left


# ---------------- 校验 ----------------

def verify(base="tiles"):
    """检查所有瓦片是否为有效 PNG（只读文件头），返回坏文件列表"""
    from tile_path import iter_tiles
    bad = []
    total = 0
    for _m, _c, _r, path in iter_tiles(base):
        total += 1
        try:
            with open(path, "rb") as f:
                if f.read(8) != PNG_HEADER:
                    bad.append(path)
        except OSError as e:
            bad.append(f"{path} ({e})")
    return total, bad


# ---------------- CLI ----------------

def main():
    ap = argparse.ArgumentParser(description="WMTS 瓦片缓存管理")
    ap.add_argument("--dir", default="tiles", help="缓存目录（默认 tiles）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stats", help="统计缓存")

    p_mig = sub.add_parser("migrate", help="迁移旧扁平结构到分级结构")
    p_mig.add_argument("--dry-run", action="store_true", help="只预览不动文件")

    p_prune = sub.add_parser("prune", help="清理瓦片（可按级别/范围/时间/容量）")
    p_prune.add_argument("--matrix", type=int, help="只清理该级别")
    p_prune.add_argument("--col-start", type=int)
    p_prune.add_argument("--col-end", type=int)
    p_prune.add_argument("--row-start", type=int)
    p_prune.add_argument("--row-end", type=int)
    p_prune.add_argument("--older-than", type=int, help="删除 N 天前的瓦片")
    p_prune.add_argument("--max-size", type=int, help="删除最旧瓦片直到总大小 ≤ N MB")

    p_clear = sub.add_parser("clear", help="清空缓存")
    p_clear.add_argument("--yes", action="store_true", help="跳过确认")

    sub.add_parser("verify", help="校验瓦片 PNG 完整性")

    args = ap.parse_args()
    base = args.dir

    if args.cmd == "stats":
        print_stats(base)
        return

    if args.cmd == "migrate":
        moved, left = migrate(base, dry_run=args.dry_run)
        print(f"迁移完成：移动 {moved} 个文件" + (f"，目标已存在跳过 {left} 个" if left else ""))
        return

    if args.cmd == "verify":
        total, bad = verify(base)
        print(f"共 {total} 个瓦片，损坏 {len(bad)} 个")
        for b in bad[:20]:
            print("  坏文件:", b)
        return

    if args.cmd == "clear":
        if not args.yes:
            total, size, _ = cache_stats(base)
            ans = input(
                f"将删除全部 {total} 个瓦片（{_fmt_mb(size)}），确认？[y/N] "
            )
            if ans.strip().lower() not in ("y", "yes"):
                print("已取消")
                return
        removed, freed = clear_cache(base)
        print(f"已清空：删除 {removed} 项，释放 {_fmt_mb(freed)}")
        return

    if args.cmd == "prune":
        removed = freed = 0
        # 范围清理（允许只给部分参数，缺省用极值）
        if args.matrix is not None and any(v is not None for v in
                                           (args.col_start, args.col_end, args.row_start, args.row_end)):
            cs = args.col_start if args.col_start is not None else 0
            ce = args.col_end if args.col_end is not None else 2_000_000
            rs = args.row_start if args.row_start is not None else 0
            re = args.row_end if args.row_end is not None else 2_000_000
            removed, freed = prune_range(base, args.matrix, cs, ce, rs, re)
        elif args.matrix is not None:
            removed, freed = prune_matrix(base, args.matrix)
        elif args.older_than is not None:
            removed, freed = prune_older_than(base, args.older_than)
        elif args.max_size is not None:
            removed, freed = prune_to_max_size(base, args.max_size)
        else:
            print("prune 需要至少一个条件：--matrix / --older-than / --max-size")
            sys.exit(1)
        print(f"清理完成：删除 {removed} 个瓦片，释放 {_fmt_mb(freed)}")


if __name__ == "__main__":
    main()
