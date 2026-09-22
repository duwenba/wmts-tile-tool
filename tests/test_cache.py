"""cache：分层统计 / 清理 / 迁移 / 校验 / 范围状态位图。"""

import base64
import os

import pytest

from wmts.core.cache import (
    LEGACY_LAYER_LABEL,
    cache_stats,
    clear_cache,
    migrate,
    prune,
    range_status,
    verify,
)
from wmts.core.paths import legacy_tile_path, legacy_v2_path, tile_path

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
LAYER = "L1"


def _mk_ok(base, m, c, r, layer=LAYER):
    p = tile_path(m, c, r, base, layer=layer)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(PNG)
    return p


def _mk_bad(base, m, c, r, layer=LAYER):
    p = tile_path(m, c, r, base, layer=layer)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"not png")
    return p


def test_cache_stats_by_layer(tmp_path):
    base = str(tmp_path)
    _mk_ok(base, 16, 1, 1, "L1")
    _mk_ok(base, 16, 2, 1, "L1")
    _mk_ok(base, 15, 3, 4, "L2")
    total, size, per = cache_stats(base)
    assert total == 3
    assert size == 3 * len(PNG)
    assert per["L1"][16]["tiles"] == 2
    assert per["L2"][15]["tiles"] == 1


def test_cache_stats_legacy_label(tmp_path):
    base = str(tmp_path)
    open(legacy_tile_path(16, 1, 2, base), "wb").close()
    _total, _size, per = cache_stats(base)
    assert LEGACY_LAYER_LABEL in per


def test_layers_do_not_mix_in_stats(tmp_path):
    base = str(tmp_path)
    _mk_ok(base, 16, 1, 1, "L1")
    _mk_ok(base, 16, 1, 1, "L2")  # 同坐标不同图层
    total, _size, per = cache_stats(base)
    assert total == 2
    assert per["L1"][16]["tiles"] == 1 and per["L2"][16]["tiles"] == 1


def test_prune_range_scoped_to_layer(tmp_path):
    base = str(tmp_path)
    for c in range(3):
        for r in range(3):
            _mk_ok(base, 16, c, r, "L1")
            _mk_ok(base, 16, c, r, "L2")
    removed, _freed = prune(base, layer="L1", matrix=16,
                            col_start=0, col_end=1, row_start=0, row_end=1)
    assert removed == 4
    total, _size, per = cache_stats(base)
    assert total == 18 - 4
    assert per["L2"][16]["tiles"] == 9  # L2 未受影响
    assert per["L1"][16]["tiles"] == 5


def test_prune_all_layers(tmp_path):
    base = str(tmp_path)
    _mk_ok(base, 16, 1, 1, "L1")
    _mk_ok(base, 16, 1, 1, "L2")
    removed, _freed = prune(base, layer=None, matrix=16)
    assert removed == 2


def test_prune_requires_condition(tmp_path):
    with pytest.raises(ValueError):
        prune(str(tmp_path))


def test_migrate_v1_and_v2_into_layer(tmp_path):
    base = str(tmp_path)
    open(legacy_tile_path(16, 7, 8, base), "wb").close()          # v1
    p2 = legacy_v2_path(16, 9, 10, base)                          # v2
    os.makedirs(os.path.dirname(p2), exist_ok=True)
    open(p2, "wb").close()

    moved, skipped, left = migrate(base, "L1")
    assert moved == 2 and skipped == 0 and left == 0
    assert os.path.exists(tile_path(16, 7, 8, base, layer="L1"))
    assert os.path.exists(tile_path(16, 9, 10, base, layer="L1"))
    assert not os.path.exists(legacy_tile_path(16, 7, 8, base))

    # 再迁一次：目标已存在 → skipped；旧位置文件保留，故 legacy_left=1
    open(legacy_tile_path(16, 7, 8, base), "wb").close()
    moved, skipped, left = migrate(base, "L1")
    assert moved == 0 and skipped == 1 and left == 1


def test_migrate_requires_layer(tmp_path):
    with pytest.raises(ValueError):
        migrate(str(tmp_path), layer=None)


def test_migrate_dry_run(tmp_path):
    base = str(tmp_path)
    open(legacy_tile_path(16, 7, 8, base), "wb").close()
    moved, _skipped, _left = migrate(base, "L1", dry_run=True)
    assert moved == 1
    assert os.path.exists(legacy_tile_path(16, 7, 8, base))  # 未真正移动


def test_verify_scoped_to_layer(tmp_path):
    base = str(tmp_path)
    _mk_ok(base, 1, 1, 1, "L1")
    _mk_bad(base, 1, 2, 2, "L1")
    _mk_bad(base, 1, 3, 3, "L2")
    total, bad = verify(base, layer="L1")
    assert total == 2 and len(bad) == 1
    total_all, bad_all = verify(base, layer=None)
    assert total_all == 3 and len(bad_all) == 2


def test_clear_cache(tmp_path):
    base = str(tmp_path)
    _mk_ok(base, 1, 1, 1, "L1")
    _mk_ok(base, 1, 1, 1, "L2")
    open(os.path.join(base, "loose.png"), "wb").close()
    removed, _freed = clear_cache(base)
    assert removed == 3  # 两个图层目录 + 散落文件
    assert cache_stats(base)[0] == 0


def test_range_status_bitmap_with_layer(tmp_path):
    base = str(tmp_path)
    _mk_ok(base, 5, 0, 0, "L1")
    _mk_ok(base, 5, 1, 0, "L1")
    _mk_bad(base, 5, 2, 0, "L1")
    # L2 同坐标有瓦片，但不影响 L1 的统计
    _mk_ok(base, 5, 2, 0, "L2")
    counts, b64 = range_status(base, 5, 0, 2, 0, 1, layer="L1")
    assert counts == {"missing": 3, "ok": 2, "invalid": 1, "total": 6}
    raw = base64.b64decode(b64)
    assert len(raw) == 2
    st = [(raw[i >> 2] >> ((i & 3) * 2)) & 3 for i in range(6)]
    assert st == [1, 1, 2, 0, 0, 0]  # 行优先


def test_range_status_falls_back_to_legacy(tmp_path):
    base = str(tmp_path)
    p = legacy_v2_path(5, 0, 0, base)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(PNG)
    counts, _b64 = range_status(base, 5, 0, 0, 0, 0, layer="L1")
    assert counts["ok"] == 1
