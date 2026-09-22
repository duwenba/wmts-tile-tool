"""pytest 公共夹具。"""

import json
from pathlib import Path

import pytest

from wmts.core.config import Config
from wmts.core.layer_meta import LayerMeta

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def config(tmp_path) -> Config:
    """指向临时目录的测试配置（不碰真实 tiles / 输出文件）。"""
    cfg = Config()
    cfg.output_dir = str(tmp_path / "tiles")
    cfg.output_file = str(tmp_path / "out.tif")
    cfg.progress_file = str(tmp_path / "progress.json")
    cfg.failed_file = str(tmp_path / "failed.txt")
    cfg.col_start, cfg.col_end = 10, 12
    cfg.row_start, cfg.row_end = 20, 21
    cfg.max_workers = 4
    return cfg


@pytest.fixture
def meta() -> LayerMeta:
    """与真实图层（EPSG:4326，原点 -180/90）一致的测试元数据。"""
    return LayerMeta(
        layer="TEST",
        origin_x=-180.0,
        origin_y=90.0,
        tile_size=256,
        start_level=0,
        end_level=16,
        resolutions={"0": 360.0 / 256, "1": 360.0 / 512, "16": 2.145767211880946e-05},
        full_extent=(112.498, 30.991, 114.041, 32.008),
    )


@pytest.fixture
def layer_meta_json() -> dict:
    return json.loads((FIXTURES / "layer_meta_007006.json").read_text(encoding="utf-8"))
