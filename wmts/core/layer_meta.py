"""
wmts.core.layer_meta — 图层元数据（在线获取，替代临时 info.json）。

数据源（MapGIS IGServer 风格）::

    GET {META_URL}/{layer}?f=json&v=2.0   （默认从 base_url 推导，需 Cookie 鉴权）

返回结构里用到::

    TileInfo2.tileInfo.origin            格网原点 {x, y}
    TileInfo2.tileInfo.cols / rows       瓦片尺寸（256）
    TileInfo2.tileInfo.lods[].level      级别
    TileInfo2.tileInfo.lods[].resolution 该级别分辨率（度/像素）
    TileInfo2.tileInfo.startLevel/endLevel
    TileInfo2.fullExtent                 图层有效范围（xmin/ymin/xmax/ymax）

缓存策略：
    进程内缓存 + 磁盘快照 layer_meta/{layer}.json（TTL 由 config.meta_ttl 控制，
    0 = 每次在线拉取）；refresh=True 强制刷新。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from .config import ROOT, Config


class MetaError(RuntimeError):
    """图层元数据获取/解析失败。"""


@dataclass
class LayerMeta:
    """归一化后的图层元数据。"""

    layer: str
    origin_x: float
    origin_y: float
    tile_size: int
    start_level: int
    end_level: int
    resolutions: dict[str, float]          # level(字符串键，JSON 友好) -> 度/像素
    full_extent: tuple[float, float, float, float]  # xmin, ymin, xmax, ymax
    fetched_at: float = 0.0

    # ---- 便捷方法 ----
    def resolution(self, level: int) -> float:
        """指定级别分辨率；级别缺失时按 0 级逐级减半推算。"""
        r = self.resolutions.get(str(level))
        if r is not None:
            return r
        r0 = self.resolutions.get(str(self.start_level), 360.0 / 256)
        steps = level - self.start_level
        return r0 / (2**steps) if steps >= 0 else r0 * (2 ** (-steps))

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return self.full_extent

    def to_dict(self) -> dict:
        d = asdict(self)
        d["full_extent"] = list(self.full_extent)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "LayerMeta":
        return cls(
            layer=d["layer"],
            origin_x=float(d["origin_x"]),
            origin_y=float(d["origin_y"]),
            tile_size=int(d["tile_size"]),
            start_level=int(d["start_level"]),
            end_level=int(d["end_level"]),
            resolutions={str(k): float(v) for k, v in d["resolutions"].items()},
            full_extent=tuple(float(x) for x in d["full_extent"]),  # type: ignore[arg-type]
            fetched_at=float(d.get("fetched_at", 0.0)),
        )


# ---------------- 解析 ----------------

def _find_tile_info(d) -> dict | None:
    """递归查找同时含 origin(字典) 与 lods(列表) 的节点。"""
    if isinstance(d, dict):
        if isinstance(d.get("origin"), dict) and isinstance(d.get("lods"), list):
            return d
        for v in d.values():
            r = _find_tile_info(v)
            if r is not None:
                return r
    elif isinstance(d, list):
        for v in d:
            r = _find_tile_info(v)
            if r is not None:
                return r
    return None


def _find_full_extent(d) -> tuple | None:
    """递归查找 fullExtent（含 xmin/ymin/xmax/ymax 的字典）。"""
    if isinstance(d, dict):
        fe = d.get("fullExtent")
        if isinstance(fe, dict) and all(k in fe for k in ("xmin", "ymin", "xmax", "ymax")):
            return (float(fe["xmin"]), float(fe["ymin"]),
                    float(fe["xmax"]), float(fe["ymax"]))
        for v in d.values():
            r = _find_full_extent(v)
            if r is not None:
                return r
    elif isinstance(d, list):
        for v in d:
            r = _find_full_extent(v)
            if r is not None:
                return r
    return None


def parse_layer_meta(layer: str, data: dict) -> LayerMeta:
    """把 IGServer 元数据 JSON 解析为 LayerMeta（纯函数，可单测）。"""
    tile_info = _find_tile_info(data)
    if not tile_info:
        raise MetaError("元数据中找不到瓦片格网定义（origin/lods）")

    origin = tile_info["origin"]
    size = tile_info.get("cols") or tile_info.get("rows") or 256
    lods = tile_info["lods"]
    if not lods:
        raise MetaError("元数据 lods 为空")

    resolutions = {}
    for lod in lods:
        try:
            resolutions[str(int(lod["level"]))] = float(lod["resolution"])
        except (KeyError, TypeError, ValueError):
            continue
    if not resolutions:
        raise MetaError("元数据 lods 中没有有效的 level/resolution")

    levels = sorted(int(k) for k in resolutions)
    start_level = tile_info.get("startLevel")
    end_level = tile_info.get("endLevel")
    try:
        start_level = int(start_level) if start_level is not None else levels[0]
    except (TypeError, ValueError):
        start_level = levels[0]
    try:
        end_level = int(end_level) if end_level is not None else levels[-1]
    except (TypeError, ValueError):
        end_level = levels[-1]

    extent = _find_full_extent(data)
    if extent is None:
        raise MetaError("元数据中找不到 fullExtent（图层有效范围）")

    return LayerMeta(
        layer=layer,
        origin_x=float(origin["x"]),
        origin_y=float(origin["y"]),
        tile_size=int(size),
        start_level=start_level,
        end_level=end_level,
        resolutions=resolutions,
        full_extent=extent,
        fetched_at=time.time(),
    )


# ---------------- 获取 ----------------

def _snapshot_path(layer: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in layer)
    return ROOT / "layer_meta" / f"{safe}.json"


def _read_snapshot(layer: str, ttl: float) -> LayerMeta | None:
    p = _snapshot_path(layer)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        meta = LayerMeta.from_dict(d)
        if ttl > 0 and (time.time() - meta.fetched_at) > ttl:
            return None
        return meta
    except Exception:
        return None


def _write_snapshot(meta: LayerMeta) -> Path:
    p = _snapshot_path(meta.layer)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(meta.to_dict(), ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return p


def _looks_like_auth_error(text: str) -> bool:
    return '"error"' in text or "权限不足" in text


def fetch_layer_meta(
    config: Config,
    refresh: bool = False,
    client: httpx.Client | None = None,
) -> LayerMeta:
    """获取图层元数据：内存缓存 → 磁盘快照（TTL 内）→ 在线拉取。

    refresh=True 跳过缓存强制在线。
    client 参数供测试注入 mock。
    """
    layer = config.layer
    if not refresh:
        cached = _read_snapshot(layer, config.meta_ttl)
        if cached is not None:
            return cached

    url = config.meta_url_for(layer)
    headers = dict(config.headers)
    timeout = max(config.timeout, 15.0)
    try:
        if client is not None:
            resp = client.get(url, headers=headers)
        else:
            resp = httpx.get(url, headers=headers, timeout=timeout)
    except httpx.HTTPError as e:
        # 网络失败时回退到过期快照（聊胜于无，并提示时间）
        stale = _read_snapshot(layer, ttl=0) if not refresh else None
        if stale is not None:
            return stale
        raise MetaError(f"请求图层元数据失败: {e}") from e

    text = resp.text
    if _looks_like_auth_error(text[:512]):
        stale = _read_snapshot(layer, ttl=0) if not refresh else None
        if stale is not None:
            return stale
        raise MetaError("鉴权失效：Cookie 已过期，请更新 config 中的 Cookie")
    if resp.status_code >= 400:
        stale = _read_snapshot(layer, ttl=0) if not refresh else None
        if stale is not None:
            return stale
        raise MetaError(f"图层元数据请求失败: HTTP {resp.status_code}")

    try:
        data = resp.json()
    except ValueError as e:
        stale = _read_snapshot(layer, ttl=0) if not refresh else None
        if stale is not None:
            return stale
        raise MetaError(f"图层元数据不是有效 JSON: {e}") from e

    meta = parse_layer_meta(layer, data)
    try:
        _write_snapshot(meta)
    except OSError:
        pass  # 快照写失败不影响使用
    return meta
