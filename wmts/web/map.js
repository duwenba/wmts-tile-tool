/* map.js — 图层概览地图 + 区域框选（Canvas）。
   瓦片经服务端 /api/tiles/{z}/{col}/{row} 代理获取（Cookie 不下发浏览器）。 */
"use strict";

class RegionMap {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.onSelect = opts.onSelect || (() => {});
    this.onCursor = opts.onCursor || (() => {});
    this.meta = null;            // /api/layer/meta
    this.level = null;           // 概览级别
    this.view = null;            // {lon0, lat0, lon1, lat1}
    this.tiles = new Map();      // "col,row" → Image | null(加载中)
    this.selection = null;       // 画布像素矩形 {x0,y0,x1,y1}
    this._drag = null;
    this._raf = 0;
    this._bind();
    this._resizeObserver();
  }

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
    this.vw = r.width; this.vh = r.height;
  }

  async loadMeta() {
    this.meta = await api("/api/layer/meta");
    return this.meta;
  }

  tileSpan(level) { return this.meta.tile_size * this.meta.resolutions[String(level)]; }

  layerTileRange(level) {
    const s = this.tileSpan(level);
    const [xmin, ymin, xmax, ymax] = this.meta.bbox;
    return {
      c0: Math.floor((xmin + 180) / s), c1: Math.floor((xmax + 180) / s),
      r0: Math.floor((90 - ymax) / s), r1: Math.floor((90 - ymin) / s),
    };
  }

  setLevel(level) {
    this.level = level;
    this.tiles.clear();
    this.fitLayer();
  }

  fitLayer() {
    const [xmin, ymin, xmax, ymax] = this.meta.bbox;
    const dx = (xmax - xmin) * 0.04, dy = (ymax - ymin) * 0.04;
    this.view = { lon0: xmin - dx, lat0: ymin - dy, lon1: xmax + dx, lat1: ymax + dy };
    this.selection = null;
    this.draw();
  }

  fitSelection(bbox, margin = 0.2) {
    const [xmin, ymin, xmax, ymax] = bbox;
    const dx = (xmax - xmin) * margin, dy = (ymax - ymin) * margin;
    this.view = { lon0: xmin - dx, lat0: ymin - dy, lon1: xmax + dx, lat1: ymax + dy };
    this.draw();
  }

  // ---- 经纬度 ⇄ 画布 ----
  lonToX(lon) { return (lon - this.view.lon0) / (this.view.lon1 - this.view.lon0) * this.vw; }
  latToY(lat) { return (this.view.lat1 - lat) / (this.view.lat1 - this.view.lat0) * this.vh; }
  xToLon(x) { return this.view.lon0 + x / this.vw * (this.view.lon1 - this.view.lon0); }
  yToLat(y) { return this.view.lat1 - y / this.vh * (this.view.lat1 - this.view.lat0); }

  zoom(factor, px, py) {
    const lonP = this.xToLon(px), latP = this.yToLat(py);
    const k = 1 / factor;
    this.view = {
      lon0: lonP - (lonP - this.view.lon0) * k, lon1: lonP + (this.view.lon1 - lonP) * k,
      lat0: latP - (latP - this.view.lat0) * k, lat1: latP + (this.view.lat1 - latP) * k,
    };
    this.draw();
  }

  pan(dxPx, dyPx) {
    const dlon = dxPx / this.vw * (this.view.lon1 - this.view.lon0);
    const dlat = dyPx / this.vh * (this.view.lat1 - this.view.lat0);
    this.view = { lon0: this.view.lon0 - dlon, lon1: this.view.lon1 - dlon,
                  lat0: this.view.lat0 + dlat, lat1: this.view.lat1 + dlat };
    this.draw();
  }

  // ---- 绘制 ----
  draw() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => { this._raf = 0; this._draw(); });
  }

  _draw() {
    const { ctx, vw, vh } = this;
    ctx.save();
    ctx.fillStyle = "#141519";
    ctx.fillRect(0, 0, vw, vh);
    if (!this.meta || !this.view || this.level == null) { ctx.restore(); return; }

    const s = this.tileSpan(this.level);
    const lon0 = Math.min(this.view.lon0, this.view.lon1);
    const lon1 = Math.max(this.view.lon0, this.view.lon1);
    const lat0 = Math.min(this.view.lat0, this.view.lat1);
    const lat1 = Math.max(this.view.lat0, this.view.lat1);
    const c0 = Math.floor((lon0 + 180) / s), c1 = Math.floor((lon1 + 180) / s);
    const r0 = Math.floor((90 - lat1) / s), r1 = Math.floor((90 - lat0) / s);
    const lr = this.layerTileRange(this.level);

    let pending = 0;
    for (let row = r0; row <= r1; row++) {
      for (let col = c0; col <= c1; col++) {
        const x = this.lonToX(-180 + col * s), y = this.latToY(90 - row * s);
        const w = this.lonToX(-180 + (col + 1) * s) - x + 0.5;
        const h = this.latToY(90 - (row + 1) * s) - y + 0.5;
        const inLayer = col >= lr.c0 && col <= lr.c1 && row >= lr.r0 && row <= lr.r1;
        if (!inLayer) { ctx.fillStyle = "#1d1f24"; ctx.fillRect(x, y, w, h); continue; }
        const key = col + "," + row;
        let img = this.tiles.get(key);
        if (img === undefined) {
          this.tiles.set(key, null); // 占位：加载中
          pending++;
          this._fetchTile(col, row, key);
        } else if (img instanceof Image) {
          ctx.drawImage(img, x, y, w, h);
        } else {
          pending++;
        }
      }
    }
    // 图层范围边框
    const [bxmin, bymin, bxmax, bymax] = this.meta.bbox;
    ctx.strokeStyle = "#f4b400"; ctx.lineWidth = 1.5;
    ctx.strokeRect(this.lonToX(bxmin), this.latToY(bymax),
                   this.lonToX(bxmax) - this.lonToX(bxmin),
                   this.latToY(bymin) - this.latToY(bymax));
    // 选区
    if (this.selection) {
      const { x0, y0, x1, y1 } = this.selection;
      ctx.fillStyle = "rgba(26,115,232,.18)";
      ctx.strokeStyle = "#8ab4f8"; ctx.lineWidth = 1.5;
      ctx.fillRect(Math.min(x0, x1), Math.min(y0, y1), Math.abs(x1 - x0), Math.abs(y1 - y0));
      ctx.strokeRect(Math.min(x0, x1), Math.min(y0, y1), Math.abs(x1 - x0), Math.abs(y1 - y0));
    }
    ctx.restore();
    this._pending = pending;
  }

  _fetchTile(col, row, key) {
    const img = new Image();
    img.onload = () => { if (this.tiles.get(key) === null) { this.tiles.set(key, img); this.draw(); } };
    img.onerror = () => { if (this.tiles.get(key) === null) this.tiles.set(key, false); };
    img.src = `/api/tiles/${this.level}/${col}/${row}?source=remote&_=${Date.now()}`;
  }

  // ---- 交互 ----
  _bind() {
    const c = this.canvas;
    c.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = c.getBoundingClientRect();
      this.zoom(e.deltaY < 0 ? 1.2 : 1 / 1.2, e.clientX - r.left, e.clientY - r.top);
    }, { passive: false });

    c.addEventListener("mousedown", (e) => {
      const r = c.getBoundingClientRect();
      const p = { x: e.clientX - r.left, y: e.clientY - r.top };
      this._drag = { ...p, mode: e.button === 2 || e.buttons & 4 ? "pan" : "select" };
      if (this._drag.mode === "select") this.selection = { x0: p.x, y0: p.y, x1: p.x, y1: p.y };
      e.preventDefault();
    });
    window.addEventListener("mousemove", (e) => {
      const r = c.getBoundingClientRect();
      const p = { x: e.clientX - r.left, y: e.clientY - r.top };
      if (!this._drag) {
        if (p.x >= 0 && p.y >= 0 && p.x <= r.width && p.y <= r.height && this.view) {
          this.onCursor(this.xToLon(p.x), this.yToLat(p.y));
        }
        return;
      }
      if (this._drag.mode === "pan") {
        this.pan(p.x - this._drag.x, p.y - this._drag.y);
        this._drag.x = p.x; this._drag.y = p.y;
      } else if (this.selection) {
        this.selection.x1 = p.x; this.selection.y1 = p.y;
        this.draw();
      }
    });
    window.addEventListener("mouseup", (e) => {
      if (!this._drag) return;
      const mode = this._drag.mode;
      this._drag = null;
      if (mode === "select" && this.selection) {
        const s = this.selection;
        if (Math.abs(s.x1 - s.x0) < 5 || Math.abs(s.y1 - s.y0) < 5) { this.selection = null; this.draw(); return; }
        const bbox = [
          Math.min(this.xToLon(s.x0), this.xToLon(s.x1)),
          Math.min(this.yToLat(s.y0), this.yToLat(s.y1)),
          Math.max(this.xToLon(s.x0), this.xToLon(s.x1)),
          Math.max(this.yToLat(s.y0), this.yToLat(s.y1)),
        ];
        this.onSelect(bbox);
      }
    });
    c.addEventListener("contextmenu", (e) => e.preventDefault());
    c.addEventListener("dblclick", () => this.fitLayer());
  }
}
