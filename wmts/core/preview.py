"""
wmts.core.preview — 单瓦片预览（本地缓存 / 上游实时拉取），UI 无关。

鉴权失效（Cookie 过期）时抛 ``AuthError``，调用方据此提示用户更新 Cookie。
"""

from __future__ import annotations

from pathlib import Path

import httpx

from .config import Config
from .downloader import AuthError
from .paths import ensure_tile_dir, find_tile, tile_path

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def load_tile_local(config: Config, matrix: int, col: int, row: int) -> bytes | None:
    """读取本地缓存瓦片（校验 PNG 头）；不存在或损坏返回 None。"""
    path = find_tile(matrix, col, row, config.output_dir)
    if path is None:
        return None
    try:
        with open(path, "rb") as f:
            content = f.read()
    except OSError:
        return None
    return content if content.startswith(PNG_HEADER) else None


def fetch_tile_remote(config: Config, matrix: int, col: int, row: int,
                      timeout: float | None = None) -> bytes:
    """从数据源拉取单瓦片（仅预览用，不落盘）。校验 PNG 头与鉴权状态。"""
    url = config.tile_url(matrix, col, row)
    try:
        resp = httpx.get(url, headers=config.headers,
                         timeout=timeout or max(config.timeout, 15.0))
    except httpx.HTTPError as e:
        raise RuntimeError(f"网络请求失败: {e}") from e
    if resp.status_code in (401, 403, 405):
        raise AuthError(f"鉴权失效（HTTP {resp.status_code}），Cookie 可能已过期")
    resp.raise_for_status()
    content = resp.content
    if not content.startswith(PNG_HEADER):
        if content.lstrip()[:1] == b"{":
            raise AuthError("数据源返回错误信息（Cookie 可能已过期）: "
                            + content[:120].decode("utf-8", "replace"))
        raise ValueError("响应内容不是有效 PNG 图片")
    return content


def get_tile(config: Config, matrix: int, col: int, row: int,
             source: str = "auto") -> tuple[bytes, str]:
    """获取瓦片内容。source: auto（本地优先，缺失联网）| local | remote。

    返回 (content, 实际来源)。source=local 且本地缺失时抛 FileNotFoundError。
    """
    if source == "local":
        content = load_tile_local(config, matrix, col, row)
        if content is None:
            raise FileNotFoundError(
                str(config.resolve_path(tile_path(matrix, col, row, config.output_dir))))
        return content, "local"
    if source == "remote":
        return fetch_tile_remote(config, matrix, col, row), "remote"
    # auto
    content = load_tile_local(config, matrix, col, row)
    if content is not None:
        return content, "local"
    return fetch_tile_remote(config, matrix, col, row), "remote"


def save_tile(config: Config, matrix: int, col: int, row: int, content: bytes) -> Path:
    """把瓦片内容写入缓存（v2 分级路径）。"""
    if not content.startswith(PNG_HEADER):
        raise ValueError("内容不是有效 PNG，拒绝保存")
    base = config.resolve_path(config.output_dir)
    path = Path(tile_path(matrix, col, row, str(base)))
    ensure_tile_dir(matrix, row, str(base))
    path.write_bytes(content)
    return path
