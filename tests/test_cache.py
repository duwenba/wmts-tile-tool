"""cache：统计 / 清理 / 迁移 / 校验 / 范围状态位图。"""

import os

from wmts.core.cache import (
    cache_stats,
    clear_cache,
    migrate,
    prune,
    range_status,
    verify,
)
from wmts.core.paths import legacy_tile_path, tile_path

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _mk_ok(base, m, c, r):
    p = tile_path(m, c, r, base)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(PNG)


def _mk_bad(base, m, c, r):
    p = tile_path(m, c, r, base)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"not png")


def test_cache_stats(tmp_path):
    base = str(tmp_path)
    _mk_ok(base, 16, 1, 1)
    _mk_ok(base, 16, 2, 1)
    _mk_ok(base, 15, 3, 4)
    total, size, per = cache_stats(base)
    assert total == 3
    assert size == 3 * len(PNG)
    assert per[16]["tiles"] == 2 and per[15]["tiles"] == 1


def test_prune_range(tmp_path):
    base = str(tmp_path)
    for c in range(3):
        for r in range(3):
            _mk_ok(base, 16, c, r)
    removed, freed = prune(base, matrix=16, col_start=0, col_end=1, row_start=0, row_end=1)
    assert removed == 4
    assert cache_stats(base)[0] == 5


def test_prune_requires_condition(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        prune(str(tmp_path))


def test_migrate_v1_to_v2(tmp_path):
    base = str(tmp_path)
    open(legacy_tile_path(16, 7, 8, base), "wb").close()
    moved, left = migrate(base)
    assert moved == 1 and left == 0
    assert os.path.exists(tile_path(16, 7, 8, base))
    assert not os.path.exists(legacy_tile_path(16, 7, 8, base))
    # 再迁一次：目标已存在 → skipped
    open(legacy_tile_path(16, 7, 8, base), "wb").close()
    moved, left = migrate(base)
    assert moved == 0 and left == 1


def test_verify_detects_bad_png(tmp_path):
    base = str(tmp_path)
    _mk_ok(base, 1, 1, 1)
    _mk_bad(base, 1, 2, 2)
    total, bad = verify(base)
    assert total == 2 and len(bad) == 1


def test_clear_cache(tmp_path):
    base = str(tmp_path)
    _mk_ok(base, 1, 1, 1)
    open(os.path.join(base, "loose.png"), "wb").close()
    removed, freed = clear_cache(base)
    assert removed == 2  # 目录 + 散落文件
    assert cache_stats(base)[0] == 0


def test_range_status_bitmap(tmp_path):
    base = str(tmp_path)
    # 3x2 范围：(0,0)=ok (1,0)=ok (2,0)=bad (0,1)=missing ...
    _mk_ok(base, 5, 0, 0)
    _mk_ok(base, 5, 1, 0)
    _mk_bad(base, 5, 2, 0)
    counts, b64 = range_status(base, 5, 0, 2, 0, 1)
    assert counts == {"missing": 3, "ok": 2, "invalid": 1, "total": 6}
    import base64
    raw = base64.b64decode(b64)
    assert len(raw) == 2  # 6 片 × 2bit = 12 bit → 2 字节
    st = [(raw[i >> 2] >> ((i & 3) * 2)) & 3 for i in range(6)]
    assert st == [1, 1, 2, 0, 0, 0]  # 行优先
