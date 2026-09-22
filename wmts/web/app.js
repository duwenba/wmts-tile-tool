/* app.js — 前端主控：配置 / 任务 / SSE / 各标签页逻辑 */
"use strict";

/* ================= 基础工具 ================= */

async function api(path, opts = {}) {
  const resp = await fetch(path, {
    headers: opts.body ? { "Content-Type": "application/json" } : undefined,
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!resp.ok) {
    let msg = `HTTP ${resp.status}`;
    try { msg = (await resp.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return resp.json();
}

let toastTimer = 0;
function toast(msg, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = isError ? "error" : "";
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3500);
}

function fmtBytes(n) {
  if (n == null) return "-";
  if (n > 1 << 30) return (n / (1 << 30)).toFixed(2) + " GB";
  if (n > 1 << 20) return (n / (1 << 20)).toFixed(1) + " MB";
  if (n > 1024) return (n / 1024).toFixed(1) + " KB";
  return n + " B";
}
function fmtTime(ts) {
  if (!ts) return "-";
  return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
}

/* ================= 全局状态 ================= */

const state = {
  config: null,
  busy: false,
  taskId: null,
  es: null,          // 任务 SSE
  logEs: null,       // 日志 SSE
  grid: null,
  map: null,
  pendingRange: null // 区域选择结果
};

const $ = (id) => document.getElementById(id);
const CFG_FIELDS = ["base_url", "layer", "style", "tilematrixset", "tile_matrix",
  "epsg", "col_start", "col_end", "row_start", "row_end",
  "output_dir", "output_file", "max_workers", "timeout"];

/* ================= 标签页 ================= */

document.querySelectorAll("#tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    btn.classList.add("active");
    $(`tab-${btn.dataset.tab}`).classList.add("active");
    if (btn.dataset.tab === "region" && state.map) state.map.draw();
    if (btn.dataset.tab === "download" && state.grid) state.grid.render();
  });
});

/* ================= 配置 ================= */

function readConfigForm() {
  const body = {};
  for (const f of CFG_FIELDS) body[f] = $(`c-${f}`).value;
  const cookie = $("c-cookie").value.trim();
  if (cookie) body.headers = { Cookie: cookie };
  body.auto_geo = $("c-auto_geo").checked;
  return body;
}

function fillConfigForm(cfg) {
  for (const f of CFG_FIELDS) $(`c-${f}`).value = cfg[f] ?? "";
  $("c-cookie").value = "";
  $("c-cookie").placeholder = cfg.headers?.Cookie
    ? `当前: ${cfg.headers.Cookie}（不修改请留空）` : "未设置 Cookie";
  $("c-auto_geo").checked = !!cfg.auto_geo;
  updateRangeHint();
}

function updateRangeHint() {
  const cs = +$("c-col_start").value, ce = +$("c-col_end").value;
  const rs = +$("c-row_start").value, re = +$("c-row_end").value;
  const m = +$("c-tile_matrix").value;
  const el = $("range-hint"), hint = $("cfg-hint");
  if ([cs, ce, rs, re].some(Number.isNaN) || ce < cs || re < rs) {
    el.textContent = "范围无效"; hint.textContent = "范围无效";
    return;
  }
  const total = (ce - cs + 1) * (re - rs + 1);
  const txt = `级别 ${m}: ${(ce - cs + 1)} 列 × ${(re - rs + 1)} 行 = ${total.toLocaleString()} 片`;
  el.textContent = txt;
  hint.textContent = txt + ` · 约 ${fmtBytes(total * 18 * 1024)}`;
}

async function saveConfig() {
  try {
    const r = await api("/api/config", { method: "PUT", body: readConfigForm() });
    state.config = r.config;
    fillConfigForm(r.config);
    toast(r.errors.length ? `已保存，但有警告: ${r.errors.join("；")}` : "配置已保存",
          r.errors.length > 0);
    if (!r.errors.length) await refreshGridStatus();
  } catch (e) { toast("保存失败: " + e.message, true); }
}

async function resetConfig() {
  try {
    const r = await api("/api/config/reset", { method: "POST" });
    state.config = r.config;
    fillConfigForm(r.config);
    toast("已恢复默认配置");
  } catch (e) { toast("失败: " + e.message, true); }
}

async function applyLayerRange() {
  try {
    const meta = await api("/api/layer/meta");
    const lr = meta.layer_range_at_current_level;
    $("c-tile_matrix").value = state.config.tile_matrix;
    $("c-col_start").value = lr.col_start; $("c-col_end").value = lr.col_end;
    $("c-row_start").value = lr.row_start; $("c-row_end").value = lr.row_end;
    updateRangeHint();
    toast(`已填入整个图层范围（${lr.total.toLocaleString()} 片，请记得保存配置）`);
  } catch (e) { toast("获取图层范围失败: " + e.message, true); }
}

/* ================= 任务流水线 ================= */

const STEP_OF_STAGE = { download: 1, merge: 2, geo: 3 };

function setPipelineSteps(fn) {
  document.querySelectorAll("#pipeline li").forEach((li, i) => {
    li.className = fn(i) || "";
  });
}

function resetPipeline(type) {
  const upto = type === "merge" ? 1 : type === "geo" ? 2 : 0;
  setPipelineSteps((i) => (i === 0 || i <= upto && i > 0) ? "done" : "");
  setOverall(0, "准备中...");
}

function setOverall(pct, text) {
  $("overall-bar").style.width = `${Math.min(100, pct)}%`;
  $("overall-text").textContent = text ?? `${pct.toFixed(1)}%`;
}

function setBusy(busy) {
  state.busy = busy;
  for (const id of ["btn-pipeline", "btn-download", "btn-merge", "btn-geo"])
    $(id).disabled = busy;
  $("btn-stop").disabled = !busy;
  $("task-state").className = busy ? "busy" : "";
}

function handleTaskEvent(ev) {
  if (ev.task_id && ev.task_id !== state.taskId) return;
  const type = ev.type;

  if (type === "progress" || type === "state" || type === "log" ||
      type === "error" || type === "done") {
    if (ev.message) appendLog(ev.message, type === "error" ? "err" : null);
  }

  if (type === "progress") {
    const stage = ev.stage, pct = ev.percent ?? 0;
    if (stage === "pipeline") {
      setOverall(pct, `总进度 ${pct.toFixed(1)}%`);
    } else {
      setOverall(pct, `${{ download: "下载", merge: "拼接", geo: "地理标签" }[stage] || stage} ${pct.toFixed(1)}%`);
    }
    if (stage === "download") {
      $("dl-bar").style.width = pct + "%";
      $("dl-percent").textContent = `${pct.toFixed(1)}% (${ev.done ?? 0}/${ev.total ?? 0})`;
      const c = ev.counters || {};
      const speed = ev.speed ? ` | ${ev.speed.toFixed(1)} 片/秒` : "";
      const eta = ev.eta ? ` | 剩余 ${Math.ceil(ev.eta)}s` : "";
      $("dl-stats").textContent =
        `总计 ${ev.total} | 成功 ${c.success ?? 0} | 失败 ${c.fail ?? 0} | 跳过 ${c.skip ?? 0}` +
        ` | 无效 ${c.invalid ?? 0} | 重试 ${c.retried ?? 0}${speed}${eta}`;
      if (ev.tile) state.grid.mark(ev.tile.col, ev.tile.row, ev.tile.result);
      state.grid.render();
      setPipelineSteps((i) => i === 0 ? "done" : i === 1 ? "running" : "");
    } else if (stage === "merge") {
      $("mg-bar").style.width = pct + "%";
      $("mg-percent").textContent = pct.toFixed(1) + "%";
      $("mg-stats").textContent = ev.message || "";
      setPipelineSteps((i) => i <= 1 ? "done" : i === 2 ? "running" : "");
    } else if (stage === "geo") {
      setPipelineSteps((i) => i <= 2 ? "done" : i === 3 ? "running" : "");
    }
  } else if (type === "state") {
    if (ev.state === "failed" || ev.state === "cancelled") {
      const step = STEP_OF_STAGE[ev.stage];
      setPipelineSteps((i) => i === 0 || (step && i < step) ? "done"
        : i === step ? "failed" : "");
      setOverall(0, ev.message || ev.state);
    }
  } else if (type === "done") {
    setBusy(false);
    if (ev.state === "done") {
      setPipelineSteps(() => "done");
      setOverall(100, "任务完成");
      toast("任务完成");
    } else {
      setOverall(0, ev.message || ev.state);
    }
    if (state.es) { state.es.close(); state.es = null; }
    state.taskId = null;
    $("task-state").textContent = "";
    refreshAfterTask();
  }
}

function startTask(type, params = {}) {
  return api("/api/tasks", { method: "POST", body: { type, params } })
    .then((task) => {
      state.taskId = task.id;
      $("task-state").textContent = `任务 ${task.id}（${type}）`;
      setBusy(true);
      resetPipeline(type);
      if (type === "download" || type === "pipeline" || type === "retry_failed") {
        $("dl-bar").style.width = "0%";
        $("dl-stats").textContent = "准备中...";
      }
      if (type === "merge" || type === "pipeline") {
        $("mg-bar").style.width = "0%";
        $("mg-stats").textContent = "准备中...";
      }
      subscribeTask(task.id);
      return task;
    })
    .catch((e) => { toast(e.message, true); throw e; });
}

function subscribeTask(taskId) {
  if (state.es) state.es.close();
  const es = new EventSource(`/api/tasks/${taskId}/events`);
  state.es = es;
  es.onmessage = (m) => {
    try { handleTaskEvent(JSON.parse(m.data)); } catch (e) { console.error(e); }
  };
  es.onerror = () => { /* EventSource 自动重连；任务结束后由 done 事件关闭 */ };
}

async function refreshAfterTask() {
  await Promise.allSettled([
    refreshGridStatus(), refreshOutputs(), refreshCache(), refreshResume(),
  ]);
}

/* ================= 下载标签页 ================= */

async function refreshGridStatus() {
  const cfg = state.config;
  try {
    await state.grid.setRange(cfg.tile_matrix, cfg.col_start, cfg.col_end,
                              cfg.row_start, cfg.row_end);
  } catch (e) { console.warn("状态加载失败:", e.message); }
}

async function refreshResume() {
  try {
    const r = await api("/api/download/progress");
    const parts = [];
    if (r.progress_file) parts.push(`断点进度: <b>${r.done_lines}</b> 片`);
    if (r.failed_file) parts.push(`失败列表: <b>${r.failed_count ?? r.failed.length}</b> 片`);
    $("dl-resume").innerHTML = parts.length
      ? parts.join(" · ") + ` <button id="dl-retry">重试失败瓦片</button>`
      : "";
    const btn = $("dl-retry");
    if (btn) btn.addEventListener("click", () => startTask("retry_failed").catch(() => {}));
  } catch { $("dl-resume").textContent = ""; }
}

/* ================= 区域选择标签页 ================= */

async function initRegion() {
  try {
    const meta = await state.map.loadMeta();
    const selT = $("rg-matrix"), selO = $("rg-overview");
    selT.innerHTML = ""; selO.innerHTML = "";
    for (let lv = meta.start_level; lv <= meta.end_level; lv++) {
      selT.add(new Option(`级别 ${lv}`, lv));
      selO.add(new Option(`级别 ${lv}`, lv));
    }
    selT.value = String(state.config.tile_matrix);
    // 概览级别：整图瓦片数 ≤ 120 的最大级别
    let best = meta.start_level;
    for (let lv = meta.start_level; lv <= meta.end_level; lv++) {
      const s = state.map.tileSpan(lv);
      const [xmin, ymin, xmax, ymax] = meta.bbox;
      const n = Math.ceil((xmax - xmin) / s + 1) * Math.ceil((ymax - ymin) / s + 1);
      if (n <= 120) best = lv;
    }
    selO.value = String(best);
    state.map.setLevel(best);
  } catch (e) {
    $("rg-result").textContent = "图层元数据获取失败: " + e.message;
  }
}

function regionSelectResult(bbox) {
  $("rg-lonmin").value = bbox[0].toFixed(6);
  $("rg-latmin").value = bbox[1].toFixed(6);
  $("rg-lonmax").value = bbox[2].toFixed(6);
  $("rg-latmax").value = bbox[3].toFixed(6);
  convertBBox();
}

async function convertBBox() {
  const bbox = [$("rg-lonmin").value, $("rg-latmin").value,
                $("rg-lonmax").value, $("rg-latmax").value].map(Number);
  if (bbox.some(Number.isNaN)) { toast("经纬度无效", true); return; }
  try {
    const r = await api("/api/grid/from-bbox",
                        { method: "POST", body: { bbox, matrix: +$("rg-matrix").value } });
    state.pendingRange = r;
    $("rg-result").textContent =
      `级别 ${r.matrix}: ${r.cols} 列 × ${r.rows} 行 = ${r.total.toLocaleString()} 片` +
      ` · 约 ${fmtBytes(r.estimated_bytes)}`;
    $("rg-apply-config").disabled = false;
  } catch (e) { $("rg-result").textContent = "换算失败: " + e.message; }
}

async function applyRegionToConfig() {
  const r = state.pendingRange;
  if (!r) return;
  try {
    const resp = await api("/api/config", { method: "PUT", body: {
      tile_matrix: r.matrix,
      col_start: r.col_start, col_end: r.col_end,
      row_start: r.row_start, row_end: r.row_end,
    }});
    state.config = resp.config;
    fillConfigForm(state.config);
    await refreshGridStatus();
    toast("已应用到下载范围（记得保存配置）");
  } catch (e) { toast("失败: " + e.message, true); }
}

/* ================= 拼接标签页 ================= */

function mergeParams() {
  const fmt = document.querySelector('input[name="mg-fmt"]:checked').value;
  const threads = +$("mg-threads").value || 0;
  const level = +$("mg-level").value;
  const p = { format: fmt };
  if (threads > 0) p.threads = threads;
  if (Number.isFinite(level)) p.level = level;
  return p;
}

/* ================= 缓存标签页 ================= */

async function refreshCache() {
  try {
    const r = await api("/api/cache/stats");
    const tb = $("ca-table tbody");
    tb.innerHTML = "";
    const layers = Object.keys(r.per_layer).sort();
    if (!layers.length) {
      tb.innerHTML = `<tr><td colspan="7" class="hint">缓存为空（${r.base}）</td></tr>`;
    }
    for (const label of layers) {
      const isCurrent = label === r.current_layer;
      for (const m of Object.keys(r.per_layer[label]).sort((a, b) => a - b)) {
        const v = r.per_layer[label][m];
        tb.insertAdjacentHTML("beforeend", `<tr>
          <td>${label}${isCurrent ? " ★" : ""}</td>
          <td class="num">${m}</td><td class="num">${v.tiles.toLocaleString()}</td>
          <td class="num">${fmtBytes(v.size)}</td>
          <td class="num">${v.min_col ?? "-"} ~ ${v.max_col ?? "-"}</td>
          <td class="num">${v.min_row ?? "-"} ~ ${v.max_row ?? "-"}</td>
          <td class="num">${v.rows}</td></tr>`);
      }
    }
    $("ca-hint").textContent =
      `总计 ${r.total_tiles.toLocaleString()} 片 / ${fmtBytes(r.total_bytes)}` +
      ` · 当前图层 ${r.current_layer} ★（清理默认只作用于它）`;
  } catch (e) { $("ca-hint").textContent = "统计失败: " + e.message; }
}

async function cachePrune() {
  const body = {};
  if ($("ca-matrix").value) body.matrix = +$("ca-matrix").value;
  if ($("ca-older").value) body.older_than = +$("ca-older").value;
  if ($("ca-maxsize").value) body.max_size = +$("ca-maxsize").value;
  for (const [k, id] of [["col_start", "ca-cs"], ["col_end", "ca-ce"],
                         ["row_start", "ca-rs"], ["row_end", "ca-re"]]) {
    if ($(id).value) body[k] = +$(id).value;
  }
  if (!Object.keys(body).length) { toast("请至少填写一个清理条件", true); return; }
  if (!confirm("确认按所选条件删除瓦片？此操作不可恢复。")) return;
  try {
    const r = await api("/api/cache/prune", { method: "POST", body });
    toast(`已删除 ${r.removed} 片，释放 ${fmtBytes(r.freed_bytes)}`);
    refreshCache(); refreshGridStatus();
  } catch (e) { toast("清理失败: " + e.message, true); }
}

async function cacheClear() {
  if (!confirm("将删除缓存目录下的全部瓦片，确认？")) return;
  if (!confirm("再次确认：真的要清空缓存吗？")) return;
  try {
    const r = await api("/api/cache/clear", { method: "POST", body: { confirm: true } });
    toast(`已清空：删除 ${r.removed} 项，释放 ${fmtBytes(r.freed_bytes)}`);
    refreshCache(); refreshGridStatus();
  } catch (e) { toast("失败: " + e.message, true); }
}

/* ================= 预览标签页 ================= */

let previewObjectUrl = null;

async function loadPreview(source = "auto") {
  const m = $("pv-matrix").value, c = $("pv-col").value, r = $("pv-row").value;
  if (m === "" || c === "" || r === "") { toast("请填写级别/列/行", true); return; }
  $("pv-info").textContent = "加载中...";
  try {
    const resp = await fetch(`/api/tiles/${m}/${c}/${r}?source=${source}`);
    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`;
      try { detail = (await resp.json()).detail || detail; } catch {}
      throw new Error(detail);
    }
    const src = resp.headers.get("X-Tile-Source") || source;
    const blob = await resp.blob();
    if (previewObjectUrl) URL.revokeObjectURL(previewObjectUrl);
    previewObjectUrl = URL.createObjectURL(blob);
    $("pv-img").src = previewObjectUrl;
    $("pv-info").textContent = `[${c},${r}] 来源: ${src === "local" ? "本地缓存" : "网络"} · ${fmtBytes(blob.size)}`;
    $("pv-save").disabled = src === "local";
  } catch (e) {
    $("pv-img").removeAttribute("src");
    $("pv-info").textContent = "加载失败: " + e.message;
    $("pv-save").disabled = true;
  }
}

async function savePreview() {
  const m = $("pv-matrix").value, c = $("pv-col").value, r = $("pv-row").value;
  try {
    const resp = await api(`/api/tiles/${m}/${c}/${r}/save`, { method: "POST" });
    toast(`已保存 ${fmtBytes(resp.size)} → ${resp.path}`);
    $("pv-save").disabled = true;
    refreshGridStatus(); refreshCache();
  } catch (e) { toast("保存失败: " + e.message, true); }
}

/* ================= 输出标签页 ================= */

async function refreshOutputs() {
  try {
    const r = await api("/api/outputs");
    const tb = $("out-table tbody");
    tb.innerHTML = "";
    if (!r.outputs.length) {
      tb.innerHTML = `<tr><td colspan="5" class="hint">暂无输出文件</td></tr>`;
    }
    for (const o of r.outputs) {
      const geo = o.geo_attached == null ? "?" : o.geo_attached ? "✅" : "❌ 未附加";
      tb.insertAdjacentHTML("beforeend", `<tr>
        <td>${o.name}</td><td class="num">${fmtBytes(o.size)}</td>
        <td>${geo}</td><td class="num">${fmtTime(o.mtime)}</td>
        <td><a href="/api/outputs/${encodeURIComponent(o.name)}" download>下载</a></td></tr>`);
    }
  } catch (e) { console.warn(e); }
}

/* ================= 日志 ================= */

function appendLog(msg, cls = null) {
  const el = $("log-view");
  const time = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  const line = document.createElement("div");
  if (cls) line.className = "log-" + cls;
  else if (/失败|错误|error/i.test(msg)) line.className = "log-err";
  else if (/完成|成功/.test(msg)) line.className = "log-ok";
  line.textContent = `[${time}] ${msg}`;
  el.appendChild(line);
  while (el.childNodes.length > 2000) el.removeChild(el.firstChild);
  if ($("log-follow").checked) el.scrollTop = el.scrollHeight;
}

async function loadLogHistory() {
  try {
    const r = await api("/api/logs?limit=200");
    for (const ev of r.logs) appendLog(ev.message || "");
  } catch { /* 忽略 */ }
}

function subscribeLogs() {
  if (state.logEs) state.logEs.close();
  const es = new EventSource("/api/logs/stream");
  state.logEs = es;
  es.onmessage = (m) => {
    try {
      const ev = JSON.parse(m.data);
      if (ev.message) appendLog(ev.message);
    } catch { /* 忽略 */ }
  };
}

function saveLogs() {
  const text = $("log-view").innerText;
  const blob = new Blob([text], { type: "text/plain" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `wmts-log-${Date.now()}.txt`;
  a.click();
  URL.revokeObjectURL(a.href);
}

/* ================= 事件绑定与初始化 ================= */

function bindEvents() {
  // 配置
  for (const f of ["col_start", "col_end", "row_start", "row_end", "tile_matrix"])
    $(`c-${f}`).addEventListener("input", updateRangeHint);
  $("cfg-save").addEventListener("click", saveConfig);
  $("cfg-reset").addEventListener("click", resetConfig);
  $("cfg-apply-layer").addEventListener("click", applyLayerRange);
  // 任务
  $("btn-pipeline").addEventListener("click", () => startTask("pipeline").catch(() => {}));
  $("btn-download").addEventListener("click", () => startTask("download").catch(() => {}));
  $("btn-merge").addEventListener("click", () => startTask("merge", mergeParams()).catch(() => {}));
  $("btn-geo").addEventListener("click", () => startTask("geo").catch(() => {}));
  $("btn-stop").addEventListener("click", async () => {
    if (!state.taskId) return;
    try {
      const r = await api(`/api/tasks/${state.taskId}/cancel`, { method: "POST" });
      toast(r.message);
    } catch (e) { toast("取消失败: " + e.message, true); }
  });
  // 下载
  $("dl-refresh").addEventListener("click", () => refreshGridStatus().then(() => toast("状态已刷新")));
  $("dl-layer-range").addEventListener("click", applyLayerRange);
  // 区域
  $("rg-matrix").addEventListener("change", () => { state.pendingRange = null; $("rg-apply-config").disabled = true; $("rg-result").textContent = "在地图上框选区域，或输入经纬度范围"; });
  $("rg-overview").addEventListener("change", () => state.map.setLevel(+$("rg-overview").value));
  $("rg-fit").addEventListener("click", () => state.map.fitLayer());
  $("rg-apply-bbox").addEventListener("click", convertBBox);
  $("rg-apply-config").addEventListener("click", applyRegionToConfig);
  // 缓存
  $("ca-refresh").addEventListener("click", refreshCache);
  $("ca-prune").addEventListener("click", cachePrune);
  $("ca-clear").addEventListener("click", cacheClear);
  $("ca-migrate").addEventListener("click", async () => {
    try {
      const r = await api("/api/cache/migrate", { method: "POST", body: {} });
      const extra = r.skipped ? `，跳过 ${r.skipped} 个` : "";
      const left = r.legacy_left ? `，旧位置仍剩 ${r.legacy_left} 个` : "";
      toast(`迁移到图层 ${r.layer}：移动 ${r.moved} 个${extra}${left}`);
      refreshCache(); refreshGridStatus();
    } catch (e) { toast("失败: " + e.message, true); }
  });
  $("ca-verify").addEventListener("click", async () => {
    try {
      const r = await api("/api/cache/verify", { method: "POST", body: {} });
      toast(r.bad_count ? `校验完成：${r.total} 片中有 ${r.bad_count} 片损坏` : `校验完成：${r.total} 片全部有效`,
            r.bad_count > 0);
    } catch (e) { toast("失败: " + e.message, true); }
  });
  // 预览
  $("pv-load").addEventListener("click", () => loadPreview("auto"));
  $("pv-local").addEventListener("click", () => loadPreview("local"));
  $("pv-remote").addEventListener("click", () => loadPreview("remote"));
  $("pv-save").addEventListener("click", savePreview);
  for (const id of ["pv-matrix", "pv-col", "pv-row"])
    $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") loadPreview("auto"); });
  // 输出 / 日志
  $("out-refresh").addEventListener("click", refreshOutputs);
  $("log-clear").addEventListener("click", () => { $("log-view").innerHTML = ""; });
  $("log-save").addEventListener("click", saveLogs);
}

async function restoreRunningTask() {
  try {
    const snap = await api("/api/tasks");
    if (snap.current) {
      state.taskId = snap.current.id;
      $("task-state").textContent = `任务 ${snap.current.id}（${snap.current.type}）`;
      setBusy(true);
      resetPipeline(snap.current.type);
      subscribeTask(snap.current.id);
      appendLog(`检测到进行中的任务 ${snap.current.id}，已重新接入进度流`);
    }
  } catch { /* 忽略 */ }
}

async function init() {
  state.grid = new StatusGrid($("status-grid"), {
    onTileClick: (col, row) => {
      $("pv-col").value = col; $("pv-row").value = row;
      $("pv-matrix").value = state.config.tile_matrix;
      document.querySelector('#tabs button[data-tab="preview"]').click();
      loadPreview("auto");
    },
  });
  state.map = new RegionMap($("region-map"), {
    onSelect: regionSelectResult,
    onCursor: () => {},
  });

  bindEvents();

  try {
    state.config = await api("/api/config");
    fillConfigForm(state.config);
  } catch (e) { toast("加载配置失败: " + e.message, true); return; }

  await Promise.allSettled([
    refreshGridStatus(), initRegion(), refreshCache(),
    refreshOutputs(), refreshResume(), loadLogHistory(),
  ]);
  subscribeLogs();
  await restoreRunningTask();
  appendLog("欢迎使用 WMTS 瓦片工具。当前数据源: " + state.config.base_url);
}

document.addEventListener("DOMContentLoaded", init);
