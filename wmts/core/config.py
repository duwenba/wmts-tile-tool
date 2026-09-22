"""
wmts.core.config — 统一配置对象（单一来源，UI 无关）

设计：
    - 默认值取自仓库根目录 ``config.py``（保持"改 config.py 即改默认"的习惯）；
    - 运行期覆盖保存在 ``config.json``（gitignored），由 Web 前端 / GUI 写入；
    - 兼容旧的 ``gui_config.json``：不存在 ``config.json`` 时自动迁移读取；
    - 所有业务模块只依赖本对象，不再使用模块级全局变量。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

# 仓库根目录（wmts/core/config.py → 上两级）
ROOT = Path(__file__).resolve().parents[2]

CONFIG_FILE = "config.json"          # 运行期配置（gitignored）
LEGACY_GUI_CONFIG = "gui_config.json"  # 旧 GUI 配置，首次读取后兼容迁移

# config.py 里的常量名 → Config 字段名
_PY_DEFAULT_MAP = {
    "BASE_URL": "base_url",
    "LAYER": "layer",
    "STYLE": "style",
    "TILEMATRIXSET": "tilematrixset",
    "SERVICE": "service",
    "REQUEST": "request",
    "VERSION": "version",
    "FORMAT": "image_format",
    "TILE_MATRIX": "tile_matrix",
    "TILE_COL_START": "col_start",
    "TILE_COL_END": "col_end",
    "TILE_ROW_START": "row_start",
    "TILE_ROW_END": "row_end",
    "OUTPUT_DIR": "output_dir",
    "OUTPUT_FILE": "output_file",
    "MAX_WORKERS": "max_workers",
    "TIMEOUT": "timeout",
    "HEADERS": "headers",
    "META_URL": "meta_url",
    "EPSG": "epsg",
}


def _load_py_defaults() -> dict[str, Any]:
    """从仓库根目录 config.py 读取默认值（不改变其原有用途）。"""
    path = ROOT / "config.py"
    if not path.exists():
        return {}
    spec = importlib.util.spec_from_file_location("wmts_root_config_defaults", path)
    if spec is None or spec.loader is None:
        return {}
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # pragma: no cover - 用户配置语法错误时给出可读提示
        print(f"警告: 读取 config.py 默认配置失败: {e}", file=sys.stderr)
        return {}
    out: dict[str, Any] = {}
    for py_name, field_name in _PY_DEFAULT_MAP.items():
        if hasattr(mod, py_name):
            out[field_name] = getattr(mod, py_name)
    return out


def _derive_meta_url(base_url: str) -> str:
    """从 WMTS 取图地址推导图层元数据（mrcs）地址。

    https://host/api/igs/rest/ogc/WMTSServer
        → https://host/api/igs/rest/mrcs/tiles/{layer}?f=json&v=2.0
    """
    marker = "/api/igs/rest/ogc/"
    idx = base_url.find(marker)
    if idx >= 0:
        host = base_url[: idx + len("/api/igs")]
    else:
        # 兜底：取 scheme://host
        try:
            from urllib.parse import urlparse

            u = urlparse(base_url)
            host = f"{u.scheme}://{u.netloc}/api/igs"
        except Exception:
            host = base_url.rstrip("/")
    return f"{host}/rest/mrcs/tiles/{{layer}}?f=json&v=2.0"


@dataclass
class Config:
    """WMTS 下载 / 拼接 / 地理标签的全部配置。"""

    # ---- 数据源 ----
    base_url: str = "https://geocloud.hubgs.com/api/igs/rest/ogc/WMTSServer"
    layer: str = "WMTS020101010007006"
    style: str = "default"
    tilematrixset: str = "EPSG:4326"
    service: str = "WMTS"
    request: str = "GetTile"
    version: str = "1.0.0"
    image_format: str = "image/png"
    # 图层元数据地址模板，{layer} 占位；None = 从 base_url 推导
    meta_url: str | None = None

    # ---- 下载范围 ----
    tile_matrix: int = 16
    col_start: int = 0
    col_end: int = 0
    row_start: int = 0
    row_end: int = 0

    # ---- 输出 ----
    output_dir: str = "tiles"
    output_file: str = "merged_map.tif"

    # ---- 下载设置 ----
    max_workers: int = 16
    timeout: float = 10.0
    headers: dict[str, str] = field(default_factory=dict)

    # ---- 断点续传文件（相对仓库根目录或绝对路径） ----
    progress_file: str = "download_progress.json"
    failed_file: str = "download_failed.txt"

    # ---- 拼接 / 地理标签 ----
    epsg: int = 4326          # GeoTIFF 坐标系编码
    auto_geo: bool = True     # 流水线拼接后自动附加地理标签

    # ---- 图层元数据缓存 ----
    meta_ttl: float = 86400.0  # 磁盘快照有效期（秒），0 = 每次都在线拉取

    # ================= 基础方法 =================

    @classmethod
    def defaults(cls) -> "Config":
        """以 config.py 为默认值构造（缺省兜底为内置值）。"""
        d = _load_py_defaults()
        cfg = cls()
        for f in fields(cls):
            if f.name in d and d[f.name] is not None:
                setattr(cfg, f.name, d[f.name])
        return cfg

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        """加载配置：config.py 默认值 → config.json 覆盖 → 兼容 gui_config.json。"""
        cfg = cls.defaults()
        target = Path(path) if path else ROOT / CONFIG_FILE
        overlay_file = target
        if not overlay_file.exists():
            # 兼容旧 GUI 配置
            legacy = ROOT / LEGACY_GUI_CONFIG
            if legacy.exists():
                overlay_file = legacy
        if overlay_file.exists():
            try:
                data = json.loads(overlay_file.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"警告: 读取 {overlay_file} 失败: {e}", file=sys.stderr)
                data = {}
            known = {f.name for f in fields(cls)}
            for k, v in data.items():
                if k in known:
                    setattr(cfg, k, v)
        cfg.sanitize()
        return cfg

    def save(self, path: str | Path | None = None) -> Path:
        """保存运行期配置到 config.json（返回写入路径）。"""
        target = Path(path) if path else ROOT / CONFIG_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def sanitize(self) -> None:
        """类型归一化（JSON 里可能是字符串数字等）。"""
        int_fields = ("tile_matrix", "col_start", "col_end", "row_start", "row_end",
                      "max_workers", "epsg")
        float_fields = ("timeout", "meta_ttl")
        for name in int_fields:
            try:
                setattr(self, name, int(getattr(self, name)))
            except (TypeError, ValueError):
                pass
        for name in float_fields:
            try:
                setattr(self, name, float(getattr(self, name)))
            except (TypeError, ValueError):
                pass
        if not isinstance(self.headers, dict):
            self.headers = dict(self.headers) if self.headers else {}

    # ================= 派生信息 =================

    @property
    def cols(self) -> int:
        return self.col_end - self.col_start + 1

    @property
    def rows(self) -> int:
        return self.row_end - self.row_start + 1

    @property
    def total(self) -> int:
        return self.cols * self.rows

    def meta_url_for(self, layer: str | None = None) -> str:
        """解析元数据地址模板（{layer} 占位）。"""
        template = self.meta_url or _derive_meta_url(self.base_url)
        return template.replace("{layer}", layer or self.layer)

    def tile_url(self, tile_matrix: int, tile_col: int, tile_row: int) -> str:
        """构建单瓦片下载 URL。"""
        params = [
            ("layer", self.layer),
            ("style", self.style),
            ("tilematrixset", self.tilematrixset),
            ("Service", self.service),
            ("Request", self.request),
            ("Version", self.version),
            ("Format", self.image_format),
            ("TileMatrix", tile_matrix),
            ("TileCol", tile_col),
            ("TileRow", tile_row),
        ]
        qs = "&".join(f"{k}={v}" for k, v in params)
        return f"{self.base_url}?{qs}"

    def resolve_path(self, p: str | Path) -> Path:
        """相对路径 → 相对仓库根目录（服务可从任意 CWD 启动）。"""
        path = Path(p)
        return path if path.is_absolute() else ROOT / path

    # ================= 校验 =================

    MAX_SIDE = 5000  # 单边瓦片数上限（与原 GUI 校验一致）

    def validate_download_range(self) -> list[str]:
        """校验下载范围，返回错误列表（空列表 = 合法）。"""
        errs: list[str] = []
        if self.col_end < self.col_start or self.row_end < self.row_start:
            errs.append("列/行范围不合法（结束值不能小于起始值）")
        if self.cols > self.MAX_SIDE or self.rows > self.MAX_SIDE:
            errs.append(f"下载范围过大（单边超过 {self.MAX_SIDE}）")
        if self.tile_matrix < 0 or self.tile_matrix > 30:
            errs.append("缩放级别需在 0-30 之间")
        if not (1 <= self.max_workers <= 128):
            errs.append("并发数需在 1-128 之间")
        return errs

    def to_public_dict(self) -> dict[str, Any]:
        """对外输出（API 用）：headers 里的 Cookie 打码。"""
        d = asdict(self)
        hdrs = dict(d.get("headers") or {})
        cookie = hdrs.get("Cookie", "")
        if cookie:
            shown = cookie[:12] + "…" + cookie[-8:] if len(cookie) > 24 else "…"
            hdrs["Cookie"] = shown
            hdrs["_cookie_set"] = True
        d["headers"] = hdrs
        d["meta_url_effective"] = self.meta_url_for()
        return d
