"""Config：默认值 / 加载保存 / 兼容迁移 / 校验 / URL 构造。"""

import json

import pytest

from wmts.core.config import ROOT, Config, _derive_meta_url


def test_defaults_from_root_config_py():
    """默认值应来自仓库根目录 config.py。"""
    cfg = Config.defaults()
    assert cfg.base_url.startswith("https://")
    assert cfg.layer
    assert cfg.max_workers >= 1


def test_meta_url_derivation():
    url = _derive_meta_url("https://geocloud.hubgs.com/api/igs/rest/ogc/WMTSServer")
    assert url == "https://geocloud.hubgs.com/api/igs/rest/mrcs/tiles/{layer}?f=json&v=2.0"
    cfg = Config(base_url="https://example.com/api/igs/rest/ogc/WMTSServer", layer="L1")
    assert cfg.meta_url_for() == "https://example.com/api/igs/rest/mrcs/tiles/L1?f=json&v=2.0"


def test_meta_url_template_override(tmp_path):
    cfg = Config(meta_url="https://x.com/meta?layer={layer}&f=json")
    assert cfg.meta_url_for("ABC") == "https://x.com/meta?layer=ABC&f=json"


def test_save_and_load_roundtrip(tmp_path):
    cfg = Config(layer="AAA", tile_matrix=13, col_start=1, col_end=5,
                 row_start=6, row_end=9, max_workers=7)
    path = tmp_path / "config.json"
    cfg.save(path)
    loaded = Config.load(path)
    assert loaded.layer == "AAA"
    assert loaded.tile_matrix == 13
    assert loaded.max_workers == 7


def test_load_legacy_gui_config(tmp_path):
    """无 config.json 时应兼容读取 gui_config.json。"""
    legacy = tmp_path / "gui_config.json"
    legacy.write_text(json.dumps({
        "layer": "LEGACY", "tile_matrix": "14",  # 字符串数字应被归一化
        "col_start": 3, "col_end": 4, "row_start": 5, "row_end": 6,
    }), encoding="utf-8")
    cfg = Config.load(legacy)
    assert cfg.layer == "LEGACY"
    assert cfg.tile_matrix == 14
    assert cfg.col_end == 4


def test_validate_download_range():
    cfg = Config()
    cfg.col_start, cfg.col_end = 10, 5
    errs = cfg.validate_download_range()
    assert errs and "范围不合法" in errs[0]

    cfg.col_start, cfg.col_end = 0, Config.MAX_SIDE
    cfg.row_start, cfg.row_end = 0, 1
    errs = cfg.validate_download_range()
    assert errs and "过大" in errs[0]

    cfg.col_start, cfg.col_end = 0, 9
    errs = cfg.validate_download_range()
    assert errs == []


def test_tile_url():
    cfg = Config(base_url="https://x.com/wmts", layer="L", style="default",
                 tilematrixset="EPSG:4326")
    url = cfg.tile_url(5, 3, 4)
    assert url.startswith("https://x.com/wmts?")
    assert "TileMatrix=5" in url and "TileCol=3" in url and "TileRow=4" in url
    assert "layer=L" in url


def test_to_public_dict_masks_cookie():
    cfg = Config(headers={"Cookie": "a" * 40, "User-Agent": "UA"})
    d = cfg.to_public_dict()
    assert d["headers"]["Cookie"] != "a" * 40
    assert d["headers"]["_cookie_set"] is True
    assert d["headers"]["User-Agent"] == "UA"


def test_derived_properties():
    cfg = Config(col_start=10, col_end=12, row_start=20, row_end=21)
    assert (cfg.cols, cfg.rows, cfg.total) == (3, 2, 6)
