/* grid.js — 瓦片状态位图工具（2 bit/片），供地图叠加层使用。
 *
 * 后端 /api/tiles/status 返回 base64 打包的位图（行优先，每片 2 bit）：
 *     0 = 缺失（待处理）  1 = 已下载（有效 PNG）  2 = 损坏（PNG 头非法）
 * 本文件只负责解码与查询，不再承担画布渲染与鼠标交互（已并入 map.js）。
 */
"use strict";

const TILE_MISSING = 0;
const TILE_OK = 1;
const TILE_INVALID = 2;

class StatusBitmap {
  constructor(b64, colStart, rowStart, cols, rows, counts) {
    const bin = atob(b64 || "");
    this.data = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) this.data[i] = bin.charCodeAt(i);
    this.colStart = colStart;
    this.rowStart = rowStart;
    this.cols = cols;
    this.rows = rows;
    this.counts = counts || null;
  }

  /** 查询某瓦片状态；超出范围返回缺失。 */
  at(col, row) {
    const ci = col - this.colStart, ri = row - this.rowStart;
    if (ci < 0 || ri < 0 || ci >= this.cols || ri >= this.rows) return TILE_MISSING;
    const idx = ri * this.cols + ci;
    return (this.data[idx >> 2] >> ((idx & 3) * 2)) & 3;
  }

  /** 运行中增量更新某瓦片状态（下载进度实时着色）。 */
  mark(col, row, result) {
    const ci = col - this.colStart, ri = row - this.rowStart;
    if (ci < 0 || ri < 0 || ci >= this.cols || ri >= this.rows) return false;
    const status = result === "failed" ? TILE_INVALID
      : (result === "skipped" || result === "success") ? TILE_OK : TILE_MISSING;
    const idx = ri * this.cols + ci;
    const shift = (idx & 3) * 2;
    const byte = idx >> 2;
    this.data[byte] = (this.data[byte] & ~(3 << shift)) | (status << shift);
    return true;
  }
}
