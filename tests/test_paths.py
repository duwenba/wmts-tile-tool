"""paths：v1/v2 路径兼容。"""

from wmts.core.paths import (
    ensure_tile_dir,
    find_tile,
    iter_tiles,
    legacy_tile_path,
    tile_path,
)


def test_v2_path_format():
    assert tile_path(16, 53248, 10558, "tiles") == "tiles/16/10558/53248.png".replace("/", "/")


def test_find_tile_v2(tmp_path):
    base = str(tmp_path)
    d = ensure_tile_dir(16, 10558, base)
    p = tile_path(16, 100, 10558, base)
    (p and open(p, "wb").close())
    assert find_tile(16, 100, 10558, base) == p


def test_find_tile_v1_fallback(tmp_path):
    base = str(tmp_path)
    p = legacy_tile_path(16, 7, 8, base)
    open(p, "wb").close()
    assert find_tile(16, 7, 8, base) == p


def test_find_tile_missing(tmp_path):
    assert find_tile(1, 2, 3, str(tmp_path)) is None


def test_iter_tiles_both_structures(tmp_path):
    base = str(tmp_path)
    open(legacy_tile_path(1, 2, 3, base), "wb").close()
    ensure_tile_dir(4, 6, base)
    open(tile_path(4, 5, 6, base), "wb").close()
    got = {(m, c, r) for m, c, r, _ in iter_tiles(base)}
    assert got == {(1, 2, 3), (4, 5, 6)}


def test_iter_tiles_ignores_garbage(tmp_path):
    base = str(tmp_path)
    open(f"{base}/not_a_tile.png", "wb").close()
    open(f"{base}/readme.txt", "wb").close()
    assert list(iter_tiles(base)) == []
