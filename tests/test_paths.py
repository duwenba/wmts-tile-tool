"""paths：v3 分层 / v2 旧分级 / v1 扁平 三种结构的读写兼容。"""

from wmts.core.paths import (
    ensure_tile_dir,
    find_tile,
    iter_tiles,
    legacy_tile_path,
    legacy_v2_path,
    safe_layer,
    tile_path,
)


def test_v3_path_format():
    assert tile_path(16, 53248, 10558, "tiles", layer="L1") == "tiles/L1/16/10558/53248.png"


def test_path_without_layer_falls_back_to_v2_form():
    assert tile_path(16, 1, 2, "tiles") == "tiles/16/2/1.png"


def test_safe_layer():
    assert safe_layer("WMTS020101010007018") == "WMTS020101010007018"
    assert safe_layer("a/b c") == "a_b_c"
    assert safe_layer("") is None and safe_layer(None) is None


def test_find_tile_v3(tmp_path):
    base = str(tmp_path)
    ensure_tile_dir(16, 10558, base, layer="L1")
    p = tile_path(16, 100, 10558, base, layer="L1")
    open(p, "wb").close()
    assert find_tile(16, 100, 10558, base, layer="L1") == p


def test_find_tile_v2_fallback(tmp_path):
    base = str(tmp_path)
    p = legacy_v2_path(16, 7, 8, base)
    import os
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").close()
    assert find_tile(16, 7, 8, base, layer="L1") == p


def test_find_tile_v1_fallback(tmp_path):
    base = str(tmp_path)
    p = legacy_tile_path(16, 7, 8, base)
    open(p, "wb").close()
    assert find_tile(16, 7, 8, base, layer="L1") == p


def test_find_tile_missing(tmp_path):
    assert find_tile(1, 2, 3, str(tmp_path), layer="L1") is None


def test_layers_are_isolated(tmp_path):
    """同一 matrix/col/row，不同图层必须是不同文件（曾会互相覆盖）。"""
    base = str(tmp_path)
    for layer in ("L1", "L2"):
        ensure_tile_dir(16, 5, base, layer=layer)
        open(tile_path(16, 1, 5, base, layer=layer), "wb").close()
    p1 = find_tile(16, 1, 5, base, layer="L1")
    p2 = find_tile(16, 1, 5, base, layer="L2")
    assert p1 != p2
    assert "/L1/" in p1 and "/L2/" in p2
    assert find_tile(16, 1, 5, base, layer="L3") is None  # 不串层


def test_iter_tiles_all_structures(tmp_path):
    import os
    base = str(tmp_path)
    open(legacy_tile_path(1, 2, 3, base), "wb").close()          # v1
    p2 = legacy_v2_path(4, 5, 6, base)                            # v2
    os.makedirs(os.path.dirname(p2), exist_ok=True)
    open(p2, "wb").close()
    ensure_tile_dir(7, 8, base, layer="L1")                       # v3
    open(tile_path(7, 9, 8, base, layer="L1"), "wb").close()

    got = {(lay, m, c, r) for lay, m, c, r, _ in iter_tiles(base)}
    assert got == {(None, 1, 2, 3), (None, 4, 5, 6), ("L1", 7, 9, 8)}


def test_iter_tiles_ignores_garbage(tmp_path):
    base = str(tmp_path)
    open(f"{base}/not_a_tile.png", "wb").close()
    open(f"{base}/readme.txt", "wb").close()
    assert list(iter_tiles(base)) == []
