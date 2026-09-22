/* map.js — 地图工作台（Canvas）
 *
 * 瓦片经服务端 /api/tiles/{z}/{col}/{row} 代理获取（Cookie 不下发浏览器）。
 * 能力：瓦片影像 + 状态着色 + 网格/编号 + 框选区域 + 实时读数 + 点击瓦片。
 * 交互照搬单文件版：左键拖拽平移、滚轮以光标为锚缩放、双击放大；
 * 框选需切到「框选」工具（S）或按住 Shift 拖拽。
 */
"use strict";

class MapWorkbench {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.onSelect = opts.onSelect || (() => {});
    this.onCursor = opts.onCursor || (() => {});
    this.onTileClick = opts.onTileClick || (() => {});
    this.onLevelChange = opts.onLevelChange || (() => {});

    this.meta = null;
    this.level = null;
    this.lon = 0;
    this.lat = 0;
    this.tool = "pan";          // pan | select
    this.showGrid = true;
    this.showStatus = true;

    this.tiles = new Map();     // "level/col/row" -> Image | null(加载中) | false(失败)
    this.status = null;         // StatusBitmap
    this.statusLevel = null;
    this._statusKey = null;
    this._statusTimer = 0;
    this._statusReq = 0;

    this.selection = null;      // 画布像素矩形 {x0,y0,x1,y1}
    this.downloadRange = null;  // {level, colStart, colEnd, rowStart, rowEnd}
    this._drag = null;
    this._raf = 0;

    this._bind();
    this._resizeObserver();
  }

  /* ================= 尺寸 ================= */

  _resizeObserver() {
    const ro = new ResizeObserver(() => { this._syncSize(); this.draw(); });
    ro.observe(this.canvas);
    this._syncSize();
  }

  _syncSize() {
    const r = this.canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    this.canvas.width = Math.max(1, Math.round(r.width * dpr));
    this.canvas.height = Math.max(1, Math.round(r.height * dpr));
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.vw = r.width;
    this.vh = r.height;
  }

  /* ================= 元数据 / 坐标 ================= */

  async loadMeta(force = false) {
    const meta = await api("/api/layer/meta" + (force ? "?refresh=1" : ""));
    // 后端 to_dict() 字段名为 full_extent，这里统一成 bbox 供前端使用。
    if (!meta.bbox) meta.bbox = meta.full_extent;
    this.meta = meta;
    return this.meta;
  }

  resolution(level) {
    const r = this.meta.resolutions[String(level)];
    if (r != null) return r;
    const r0 = this.meta.resolutions[String(this.meta.start_level)] ?? 360 / 256;
    const steps = level - this.meta.start_level;
    return steps >= 0 ? r0 / 2 ** steps : r0 * 2 ** -steps;
  }

  tileSpan(level) { return this.meta.tile_size * this.resolution(level); }

  tileAt(lon, lat, level) {
    const s = this.tileSpan(level);
    return {
      col: Math.floor((lon - this.meta.origin_x) / s),
      row: Math.floor((this.meta.origin_y - lat) / s),
    };
  }

  tileBounds(level, col, row) {
    const s = this.tileSpan(level);
    const west = this.meta.origin_x + col * s;
    const north = this.meta.origin_y - row * s;
    return { west, east: west + s, north, south: north - s };
  }

  /** 瓦片范围 → 地理范围 [xmin, ymin, xmax, ymax]。 */
  rangeBBox(level, colStart, colEnd, rowStart, rowEnd) {
    const s = this.tileSpan(level);
    return [
      this.meta.origin_x + colStart * s,
      this.meta.origin_y - (rowEnd + 1) * s,
      this.meta.origin_x + (colEnd + 1) * s,
      this.meta.origin_y - rowStart * s,
    ];
  }

  /** 缩放到能容纳给定地理范围的最大级别（不超过 maxLevel）。 */
  fitBBox(bbox, maxLevel) {
    if (!this.meta) return;
    const [xmin, ymin, xmax, ymax] = bbox;
    this.lon = (xmin + xmax) / 2;
    this.lat = (ymin + ymax) / 2;
    const cap = maxLevel == null ? this.meta.end_level : maxLevel;
    let best = this.meta.start_level;
    for (let lv = this.meta.start_level; lv <= cap; lv++) {
      const res = this.resolution(lv);
      if ((xmax - xmin) / res <= this.vw * 0.9 && (ymax - ymin) / res <= this.vh * 0.9) {
        best = lv;
      }
    }
    this.level = best;
    this._afterViewChange();
  }

  layerTileRange(level) {
    const s = this.tileSpan(level);
    const [xmin, ymin, xmax, ymax] = this.meta.bbox;
    return {
      c0: Math.floor((xmin - this.meta.origin_x) / s),
      c1: Math.floor((xmax - this.meta.origin_x) / s),
      r0: Math.floor((this.meta.origin_y - ymax) / s),
      r1: Math.floor((this.meta.origin_y - ymin) / s),
    };
  }

  viewTileRange() {
    const ts = this.meta.tile_size;
    const res = this.resolution(this.level);
    const cx = (this.lon - this.meta.origin_x) / res;
    const cy = (this.meta.origin_y - this.lat) / res;
    const left = cx - this.vw / 2, top = cy - this.vh / 2;
    return {
      c0: Math.floor(left / ts) - 1,
      c1: Math.floor((left + this.vw) / ts) + 1,
      r0: Math.floor(top / ts) - 1,
      r1: Math.floor((top + this.vh) / ts) + 1,
    };
  }

  screenToLonLat(px, py) {
    const res = this.resolution(this.level);
    const cx = (this.lon - this.meta.origin_x) / res;
    const cy = (this.meta.origin_y - this.lat) / res;
    const x = (cx - this.vw / 2) + px;
    const y = (cy - this.vh / 2) + py;
    return [this.meta.origin_x + x * res, this.meta.origin_y - y * res];
  }

  _clamp() {
    this.lon = Math.max(-180, Math.min(180, this.lon));
    this.lat = Math.max(-90, Math.min(90, this.lat));
  }

  /* ================= 视图控制 ================= */

  fitLayer() {
    if (!this.meta) return;
    const [xmin, ymin, xmax, ymax] = this.meta.bbox;
    this.lon = (xmin + xmax) / 2;
    this.lat = (ymin + ymax) / 2;
    let best = this.meta.start_level;
    for (let lv = this.meta.start_level; lv <= this.meta.end_level; lv++) {
      const res = this.resolution(lv);
      if ((xmax - xmin) / res <= this.vw * 0.92 && (ymax - ymin) / res <= this.vh * 0.92) {
        best = lv;
      }
    }
    this.level = best;
    this._afterViewChange();
  }

  centerOn(level, lon, lat) {
    if (level != null) this.level = level;
    this.lon = lon;
    this.lat = lat;
    this._clamp();
    this._afterViewChange();
  }

  zoomBy(delta, px, py) {
    if (!this.meta || this.level == null) return;
    if (px == null) { px = this.vw / 2; py = this.vh / 2; }
    const old = this.level;
    const next = Math.max(this.meta.start_level,
      Math.min(this.meta.end_level, old + delta));
    if (next === old) return;
    const [lonP, latP] = this.screenToLonLat(px, py);
    this.level = next;
    const res = this.resolution(next);
    const tx = (lonP - this.meta.origin_x) / res;
    const ty = (this.meta.origin_y - latP) / res;
    this.lon = this.meta.origin_x + (tx - (px - this.vw / 2)) * res;
    this.lat = this.meta.origin_y - (ty - (py - this.vh / 2)) * res;
    this._clamp();
    this._afterViewChange();
  }

  setLevel(level) {
    if (!this.meta) return;
    this.level = Math.max(this.meta.start_level, Math.min(this.meta.end_level, level));
    this._afterViewChange();
  }

  setTool(tool) {
    this.tool = tool === "select" ? "select" : "pan";
    this.canvas.classList.toggle("selecting", this.tool === "select");
    if (this.tool !== "select") { this.selection = null; this.draw(); }
  }

  setGrid(on) { this.showGrid = !!on; this.draw(); }
  setStatus(on) {
    this.showStatus = !!on;
    this._statusKey = null;
    this.draw();
  }

  setDownloadRange(level, colStart, colEnd, rowStart, rowEnd) {
    this.downloadRange = (level == null) ? null
      : { level, colStart, colEnd, rowStart, rowEnd };
    this.draw();
  }

  clearSelection() { this.selection = null; this.draw(); }

  /** 刷新：清空瓦片与状态缓存后重绘。 */
  refresh() {
    this.tiles.clear();
    this.status = null;
    this.statusLevel = null;
    this._statusKey = null;
    this.draw();
  }

  /** 只重新拉取状态位图（保留已加载瓦片，用于下载完成后着色）。 */
  reloadStatus() {
    this.status = null;
    this.statusLevel = null;
    this._statusKey = null;
    this.draw();
  }

  /** 下载进度实时着色（仅当视图级别与状态位图级别一致时生效）。 */
  markTile(level, col, row, result) {
    if (!this.status || this.statusLevel !== level) return false;
    const ok = this.status.mark(col, row, result);
    if (ok) this.draw();
    return ok;
  }

  _afterViewChange() {
    this._statusKey = null;
    this.onLevelChange(this.level);
    this.draw();
  }

  /* ================= 状态位图 ================= */

  _maybeLoadStatus(c0, c1, r0, r1) {
    if (!this.showStatus) return;
    const lr = this.layerTileRange(this.level);
    const a = Math.max(c0, lr.c0), b = Math.min(c1, lr.c1);
    const c = Math.max(r0, lr.r0), d = Math.min(r1, lr.r1);
    if (b < a || d < c) {
      this.status = null; this.statusLevel = null; this._statusKey = null;
      return;
    }
    const key = `${this.level}:${a}:${b}:${c}:${d}`;
    if (key === this._statusKey) return;
    this._statusKey = key;
    clearTimeout(this._statusTimer);
    this._statusTimer = setTimeout(() => this._loadStatus(a, b, c, d), 220);
  }

  async _loadStatus(c0, c1, r0, r1) {
    const reqId = ++this._statusReq;
    try {
      const r = await api(`/api/tiles/status?matrix=${this.level}`
        + `&col_start=${c0}&col_end=${c1}&row_start=${r0}&row_end=${r1}`);
      if (reqId !== this._statusReq) return;      // 过期响应丢弃
      this.status = new StatusBitmap(r.status_b64, c0, r0,
        c1 - c0 + 1, r1 - r0 + 1, r.counts);
      this.statusLevel = this.level;
    } catch (e) {
      if (reqId !== this._statusReq) return;
      this.status = null;
      this.statusLevel = null;
    }
    this.draw();
  }

  /* ================= 瓦片加载 ================= */

  _fetchTile(level, col, row, key) {
    if (this.tiles.size > 2000) {
      const it = this.tiles.keys();
      for (let i = 0; i < 500; i++) {
        const k = it.next();
        if (k.done) break;
        this.tiles.delete(k.value);
      }
    }
    const img = new Image();
    img.onload = () => {
      if (this.tiles.get(key) === null) { this.tiles.set(key, img); this.draw(); }
    };
    img.onerror = () => {
      if (this.tiles.get(key) === null) this.tiles.set(key, false);
    };
    img.src = `/api/tiles/${level}/${col}/${row}?source=auto`;
  }

  /* ================= 绘制 ================= */

  draw() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => { this._raf = 0; this._draw(); });
  }

  _draw() {
    const { ctx, vw, vh } = this;
    ctx.save();
    ctx.clearRect(0, 0, vw, vh);
    ctx.fillStyle = "#dfe3e8";
    ctx.fillRect(0, 0, vw, vh);
    if (!this.meta || this.level == null) { ctx.restore(); return; }

    const ts = this.meta.tile_size;
    const res = this.resolution(this.level);
    const cx = (this.lon - this.meta.origin_x) / res;
    const cy = (this.meta.origin_y - this.lat) / res;
    const left = cx - vw / 2, top = cy - vh / 2;

    const c0 = Math.floor(left / ts) - 1, c1 = Math.floor((left + vw) / ts) + 1;
    const r0 = Math.floor(top / ts) - 1, r1 = Math.floor((top + vh) / ts) + 1;
    const lr = this.layerTileRange(this.level);

    for (let row = r0; row <= r1; row++) {
      for (let col = c0; col <= c1; col++) {
        const x = Math.round(col * ts - left);
        const y = Math.round(row * ts - top);
        const inLayer = col >= lr.c0 && col <= lr.c1 && row >= lr.r0 && row <= lr.r1;
        if (!inLayer) {
          ctx.fillStyle = "#e7eaee";
          ctx.fillRect(x, y, ts, ts);
          continue;
        }
        const key = this.level + "/" + col + "/" + row;
        let img = this.tiles.get(key);
        if (img === undefined) {
          this.tiles.set(key, null);
          this._fetchTile(this.level, col, row, key);
          img = null;
        }
        if (img instanceof Image && img.complete && img.naturalWidth) {
          ctx.drawImage(img, x, y, ts, ts);
        } else {
          ctx.fillStyle = "#d3d8de";
          ctx.fillRect(x, y, ts, ts);
        }

        if (this.showStatus && this.status && this.statusLevel === this.level) {
          const st = this.status.at(col, row);
          if (st === TILE_OK) { ctx.fillStyle = "rgba(46,158,91,.18)"; ctx.fillRect(x, y, ts, ts); }
          else if (st === TILE_INVALID) { ctx.fillStyle = "rgba(217,48,37,.30)"; ctx.fillRect(x, y, ts, ts); }
        }

        if (this.showGrid) {
          ctx.strokeStyle = "rgba(40,48,60,.20)";
          ctx.lineWidth = 1;
          ctx.strokeRect(x + .5, y + .5, ts, ts);
          const label = this.level + "/" + col + "/" + row;
          ctx.font = "11px ui-monospace,SFMono-Regular,Consolas,monospace";
          const tw = ctx.measureText(label).width;
          ctx.fillStyle = "rgba(255,255,255,.82)";
          ctx.fillRect(x + 3, y + 3, tw + 8, 17);
          ctx.fillStyle = "#3a4149";
          ctx.fillText(label, x + 7, y + 15);
        }
      }
    }

    const worldToX = (lon) => (lon - this.meta.origin_x) / res - left;
    const worldToY = (lat) => (this.meta.origin_y - lat) / res - top;

    // 图层有效范围
    const [bxmin, bymin, bxmax, bymax] = this.meta.bbox;
    ctx.strokeStyle = "rgba(232,164,0,.9)";
    ctx.lineWidth = 1.5;
    ctx.strokeRect(worldToX(bxmin), worldToY(bymax),
      worldToX(bxmax) - worldToX(bxmin), worldToY(bymin) - worldToY(bymax));

    // 当前下载范围（虚线蓝框）
    if (this.downloadRange) {
      const dr = this.downloadRange;
      const ds = this.tileSpan(dr.level);
      const x0 = worldToX(this.meta.origin_x + dr.colStart * ds);
      const x1 = worldToX(this.meta.origin_x + (dr.colEnd + 1) * ds);
      const y0 = worldToY(this.meta.origin_y - dr.rowStart * ds);
      const y1 = worldToY(this.meta.origin_y - (dr.rowEnd + 1) * ds);
      ctx.save();
      ctx.setLineDash([7, 5]);
      ctx.strokeStyle = "rgba(26,115,232,.95)";
      ctx.lineWidth = 2;
      ctx.strokeRect(x0, y0, x1 - x0, y1 - y0);
      ctx.restore();
    }

    // 选区
    if (this.selection) {
      const { x0, y0, x1, y1 } = this.selection;
      const rx = Math.min(x0, x1), ry = Math.min(y0, y1);
      const rw = Math.abs(x1 - x0), rh = Math.abs(y1 - y0);
      ctx.fillStyle = "rgba(26,115,232,.16)";
      ctx.fillRect(rx, ry, rw, rh);
      ctx.strokeStyle = "rgba(26,115,232,.95)";
      ctx.lineWidth = 1.5;
      ctx.strokeRect(rx + .5, ry + .5, rw, rh);
    }

    // 中心准星
    ctx.strokeStyle = "rgba(217,48,37,.75)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(vw / 2 - 8, vh / 2); ctx.lineTo(vw / 2 + 8, vh / 2);
    ctx.moveTo(vw / 2, vh / 2 - 8); ctx.lineTo(vw / 2, vh / 2 + 8);
    ctx.stroke();

    ctx.restore();

    this._maybeLoadStatus(c0, c1, r0, r1);
  }

  /* ================= 交互 ================= */

  _pos(e) {
    const r = this.canvas.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }

  _emitCursor(px, py) {
    if (!this.meta || this.level == null) return;
    const [lon, lat] = this.screenToLonLat(px, py);
    const t = this.tileAt(lon, lat, this.level);
    this.onCursor(lon, lat, {
      level: this.level, col: t.col, row: t.row,
      resolution: this.resolution(this.level),
    });
  }

  _bind() {
    const c = this.canvas;

    c.addEventListener("wheel", (e) => {
      e.preventDefault();
      const p = this._pos(e);
      this.zoomBy(e.deltaY < 0 ? 1 : -1, p.x, p.y);
    }, { passive: false });

    c.addEventListener("mousedown", (e) => {
      if (e.button !== 0 && !(e.buttons & 4)) return;
      const p = this._pos(e);
      const selecting = this.tool === "select" || e.shiftKey;
      this._drag = {
        x0: p.x, y0: p.y, lon0: this.lon, lat0: this.lat,
        moved: false, mode: selecting ? "select" : "pan",
      };
      if (selecting) this.selection = { x0: p.x, y0: p.y, x1: p.x, y1: p.y };
      e.preventDefault();
    });

    window.addEventListener("mousemove", (e) => {
      const p = this._pos(e);
      if (p.x >= 0 && p.y >= 0 && p.x <= this.vw && p.y <= this.vh) {
        this._emitCursor(p.x, p.y);
      }
      if (!this._drag) return;
      const dx = p.x - this._drag.x0, dy = p.y - this._drag.y0;
      if (Math.abs(dx) + Math.abs(dy) > 4) this._drag.moved = true;
      if (this._drag.mode === "pan" && this._drag.moved) {
        const res = this.resolution(this.level);
        this.lon = this._drag.lon0 - dx * res;
        this.lat = this._drag.lat0 + dy * res;
        this._clamp();
        this.draw();
      } else if (this._drag.mode === "select" && this.selection) {
        this.selection.x1 = p.x;
        this.selection.y1 = p.y;
        this.draw();
      }
    });

    window.addEventListener("mouseup", (e) => {
      if (!this._drag) return;
      const d = this._drag;
      this._drag = null;
      const p = this._pos(e);
      if (d.mode === "select") {
        const s = this.selection;
        if (!s || Math.abs(s.x1 - s.x0) < 5 || Math.abs(s.y1 - s.y0) < 5) {
          this.selection = null;
          this.draw();
          return;
        }
        const [lonA, latA] = this.screenToLonLat(s.x0, s.y0);
        const [lonB, latB] = this.screenToLonLat(s.x1, s.y1);
        this.draw();
        this.onSelect([
          Math.min(lonA, lonB), Math.min(latA, latB),
          Math.max(lonA, lonB), Math.max(latA, latB),
        ]);
      } else if (!d.moved) {
        const t = this.tileAtScreen(p.x, p.y);
        if (t) this.onTileClick(this.level, t.col, t.row);
      }
    });

    c.addEventListener("dblclick", (e) => {
      const p = this._pos(e);
      this.zoomBy(1, p.x, p.y);
    });
    c.addEventListener("contextmenu", (e) => e.preventDefault());
  }

  tileAtScreen(px, py) {
    if (!this.meta || this.level == null) return null;
    const [lon, lat] = this.screenToLonLat(px, py);
    const t = this.tileAt(lon, lat, this.level);
    const lr = this.layerTileRange(this.level);
    if (t.col < lr.c0 || t.col > lr.c1 || t.row < lr.r0 || t.row > lr.r1) return null;
    return t;
  }
}
