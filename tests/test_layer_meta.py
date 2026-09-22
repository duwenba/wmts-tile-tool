"""layer_meta：解析（真实响应 fixture）/ 缓存快照。"""

import pytest

from wmts.core import layer_meta as lm
from wmts.core.layer_meta import LayerMeta, MetaError, parse_layer_meta


def test_parse_real_fixture(layer_meta_json):
    meta = parse_layer_meta("WMTS020101010007006", layer_meta_json)
    assert meta.layer == "WMTS020101010007006"
    assert meta.origin_x == -180.0 and meta.origin_y == 90.0
    assert meta.tile_size == 256
    assert meta.start_level == 0 and meta.end_level == 16
    assert meta.resolution(16) == pytest.approx(2.145767211880946e-05)
    xmin, ymin, xmax, ymax = meta.full_extent
    assert 112.4 < xmin < 112.6 and 30.9 < ymin < 31.1
    assert 113.9 < xmax < 114.1 and 31.9 < ymax < 32.1


def test_parse_missing_tile_info():
    with pytest.raises(MetaError):
        parse_layer_meta("L", {"foo": "bar"})


def test_parse_missing_extent():
    with pytest.raises(MetaError):
        parse_layer_meta("L", {"tileInfo": {"origin": {"x": 0, "y": 0},
                                            "lods": [{"level": 0, "resolution": 1.0}]}})


def test_parse_auth_error_json():
    """鉴权错误 JSON 不应被误解析成功。"""
    data = {"error": "操作权限不足", "status": 405}
    with pytest.raises(MetaError):
        parse_layer_meta("L", data)


def test_snapshot_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(lm, "ROOT", tmp_path)
    meta = LayerMeta(layer="TEST", origin_x=-180, origin_y=90, tile_size=256,
                     start_level=0, end_level=16,
                     resolutions={"0": 1.4, "1": 0.7},
                     full_extent=(1.0, 2.0, 3.0, 4.0), fetched_at=123.0)
    meta.to_dict()
    # 通过内部快照函数验证落盘/读取
    lm._write_snapshot(meta)
    loaded = lm._read_snapshot("TEST", ttl=0)
    assert loaded == meta
    # TTL 过期 → 返回 None
    assert lm._read_snapshot("TEST", ttl=10) is None


def test_fetch_layer_meta_with_mock_client(config, layer_meta_json, tmp_path, monkeypatch):
    """注入 mock client 验证在线拉取 + 解析 + 快照写入 + 快照复用。"""
    monkeypatch.setattr(lm, "ROOT", tmp_path)

    class FakeResponse:
        status_code = 200
        text = __import__("json").dumps(layer_meta_json)

        def json(self):
            return layer_meta_json

    class FakeClient:
        def __init__(self):
            self.calls = 0

        def get(self, url, headers=None):
            self.calls += 1
            assert "{layer}" not in url
            assert url.endswith("f=json&v=2.0")
            return FakeResponse()

    client = FakeClient()
    meta = lm.fetch_layer_meta(config, refresh=True, client=client)
    assert meta.layer == config.layer
    assert meta.resolution(16) > 0
    assert client.calls == 1
    # 二次调用（不刷新）应命中磁盘快照，不再发起请求
    meta2 = lm.fetch_layer_meta(config, client=client)
    assert meta2.fetched_at == meta.fetched_at
    assert client.calls == 1


def test_fetch_layer_meta_auth_error(config, tmp_path, monkeypatch):
    """鉴权错误且无快照 → 报可读错误。"""
    monkeypatch.setattr(lm, "ROOT", tmp_path)

    class AuthErrorClient:
        def get(self, url, headers=None):
            class R:
                status_code = 405
                text = '{"error":"操作权限不足","status":405}'

                def json(self):
                    raise ValueError()

            return R()

    with pytest.raises(MetaError, match="鉴权"):
        lm.fetch_layer_meta(config, refresh=True, client=AuthErrorClient())
