#!/usr/bin/env python3
"""
geo_attach.py — 为合并后的 BigTIFF 附加地理参考 — 实现已迁移到 wmts/core/georef.py。

与旧版差异：格网定义不再读取临时的 info.json，改为从数据源在线拉取图层元数据
（首次获取后缓存到 layer_meta/{layer}.json，可用 --refresh 强制刷新）。

用法:
  uv run python geo_attach.py                # 修补 config 的 output_file
  uv run python geo_attach.py --dry-run      # 只计算并打印，不写文件
  uv run python geo_attach.py --file x.tif --epsg 4326
  uv run python geo_attach.py --force        # 尺寸不一致 / 已含标签时仍继续
"""

from wmts.core.georef import cli_main  # noqa: F401

if __name__ == "__main__":
    cli_main()
