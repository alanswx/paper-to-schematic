const $ = (sel) => document.querySelector(sel);

const PIN_RADIUS_PX = 4;
const PIN_HIT_PAD_PX = 3;
const CLICK_THRESHOLD_PX = 3;
const HANDLE_SIZE_PX = 9;
const HANDLE_HIT_PAD_PX = 3;

const state = {
  board: null,
  chips: null,
  graph: null,
  sheetIndex: 1,
  image: null,
  view: { scale: 1, x: 0, y: 0 },
  mode: "view",
  drawStart: null,
  drawCurrent: null,
  pendingBox: null,
  selectedComponent: null,
  pinDrag: null,
  bboxResize: null,
  bboxTranslate: null,
  pan: null,
};

const canvas = $("#canvas");
const ctx = canvas.getContext("2d");

function fitCanvas() {
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.floor(rect.width * dpr);
  canvas.height = Math.floor(rect.height * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  render();
}

const imgToCanvas = (ix, iy) => ({
  x: ix * state.view.scale + state.view.x,
  y: iy * state.view.scale + state.view.y,
});
const canvasToImg = (cx, cy) => ({
  x: (cx - state.view.x) / state.view.scale,
  y: (cy - state.view.y) / state.view.scale,
});

// Default DIP-style pin layout: pins 1..N/2 down the left edge top-to-bottom,
// pins N/2+1..N up the right edge bottom-to-top. Returns null for odd-pin chips.
function defaultPinPositions(part, bbox) {
  const n = part.pins.length;
  if (n % 2 !== 0) return null;
  const half = n / 2;
  const [x1, y1, x2, y2] = bbox;
  const h = y2 - y1;
  const out = {};
  for (let i = 1; i <= n; i++) {
    let x, y;
    if (i <= half) {
      x = x1;
      y = y1 + (i - 0.5) * (h / half);
    } else {
      const slot = n - i;
      x = x2;
      y = y1 + (slot + 0.5) * (h / half);
    }
    out[String(i)] = [x, y];
  }
  return out;
}

function getComponent(refdes) {
  return state.graph.components.find(c => c.refdes === refdes);
}

function pinTypeColor(type) {
  switch (type) {
    case "power":     return "#f5a";
    case "ground":    return "#888";
    case "input":     return "#3df";
    case "output":    return "#fc3";
    case "bidir":     return "#9f6";
    case "tri_state": return "#fc3";
    case "clock":     return "#f93";
    default:          return "#aaa";
  }
}

function render() {
  const w = canvas.clientWidth, h = canvas.clientHeight;
  ctx.fillStyle = "#111";
  ctx.fillRect(0, 0, w, h);
  if (!state.image) return;

  const { scale, x, y } = state.view;
  ctx.imageSmoothingEnabled = scale < 1;
  ctx.drawImage(state.image, x, y, state.image.width * scale, state.image.height * scale);

  const comps = state.graph?.components?.filter(c => c.sheet === state.sheetIndex) ?? [];
  for (const c of comps) drawComponent(c, state.selectedComponent === c.refdes);

  if (state.mode === "drawing" && state.drawStart && state.drawCurrent) {
    const a = imgToCanvas(state.drawStart.x, state.drawStart.y);
    const b = imgToCanvas(state.drawCurrent.x, state.drawCurrent.y);
    ctx.lineWidth = 2;
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = "#fc3";
    ctx.strokeRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y));
    ctx.setLineDash([]);
  }
}

function drawComponent(comp, selected) {
  const part = state.chips.parts[comp.part];
  const a = imgToCanvas(comp.bbox[0], comp.bbox[1]);
  const b = imgToCanvas(comp.bbox[2], comp.bbox[3]);
  const verified = !!comp.verified;

  let stroke, label;
  if (selected) { stroke = "#fc3"; label = "rgba(255, 204, 51, 0.95)"; }
  else if (verified) { stroke = "#5d5"; label = "rgba(85, 221, 85, 0.9)"; }
  else { stroke = "#3df"; label = "rgba(51, 221, 255, 0.85)"; }

  ctx.lineWidth = selected ? 3 : 2;
  ctx.strokeStyle = stroke;
  ctx.strokeRect(a.x, a.y, b.x - a.x, b.y - a.y);

  ctx.fillStyle = label;
  ctx.font = "12px ui-monospace, monospace";
  const tag = verified ? "✓ " : "";
  ctx.fillText(`${tag}${comp.refdes} ${comp.part}`, a.x, a.y - 4);

  // (A) Pin dots only render for the selected component — for unselected
  // bboxes the auto-DIP pin positions don't match the schematic symbol's
  // actual pin locations, so they're visual noise.
  if (selected && part && comp.pin_positions) {
    const r = PIN_RADIUS_PX + 1;
    const xMid = (comp.bbox[0] + comp.bbox[2]) / 2;
    for (const [pinNum, [ix, iy]] of Object.entries(comp.pin_positions)) {
      const cp = imgToCanvas(ix, iy);
      const pinDef = part.pins.find(p => String(p.n) === pinNum);

      ctx.beginPath();
      ctx.arc(cp.x, cp.y, r, 0, Math.PI * 2);
      ctx.fillStyle = pinDef ? pinTypeColor(pinDef.type) : "#888";
      ctx.fill();
      ctx.strokeStyle = "#000";
      ctx.lineWidth = 1;
      ctx.stroke();

      if (pinDef) {
        const isLeft = ix <= xMid;
        ctx.fillStyle = "#fff";
        ctx.font = "10px ui-monospace, monospace";
        ctx.textAlign = isLeft ? "right" : "left";
        ctx.textBaseline = "middle";
        ctx.fillText(`${pinDef.n} ${pinDef.name}`, cp.x + (isLeft ? -8 : 8), cp.y);
        ctx.textAlign = "start";
        ctx.textBaseline = "alphabetic";
      }
    }
  }

  // (B) Resize handles at the four corners of the selected bbox.
  if (selected) {
    const corners = [[a.x, a.y], [b.x, a.y], [a.x, b.y], [b.x, b.y]];
    ctx.fillStyle = "#fc3";
    ctx.strokeStyle = "#000";
    ctx.lineWidth = 1;
    const s = HANDLE_SIZE_PX;
    for (const [hx, hy] of corners) {
      ctx.fillRect(hx - s / 2, hy - s / 2, s, s);
      ctx.strokeRect(hx - s / 2, hy - s / 2, s, s);
    }
  }
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(`${path}: ${res.status} ${res.statusText}`);
  const ct = res.headers.get("Content-Type") || "";
  return ct.includes("json") ? res.json() : res.blob();
}

function normalizeGraph() {
  // Backfill default pin positions for any component that lacks them.
  for (const comp of state.graph.components) {
    if (comp.pin_positions) continue;
    const part = state.chips.parts[comp.part];
    if (!part) continue;
    const pos = defaultPinPositions(part, comp.bbox);
    if (pos) comp.pin_positions = pos;
  }
}

async function loadAll() {
  state.board = await api("/api/board");
  state.chips = await api("/api/chips");
  state.graph = await api("/api/graph");
  normalizeGraph();

  $("#board-title").textContent = `${state.board.title} (${state.board.drawing_number})`;

  const sel = $("#sheet-select");
  sel.innerHTML = "";
  for (const s of state.board.sheets) {
    const o = document.createElement("option");
    o.value = String(s.index);
    o.textContent = `Sheet ${s.index}: ${s.title}`;
    sel.appendChild(o);
  }
  sel.value = String(state.sheetIndex);
  sel.addEventListener("change", () => {
    state.sheetIndex = parseInt(sel.value, 10);
    state.selectedComponent = null;
    refreshSelection();
    loadSheet().catch(err => setStatus("error: " + err.message));
  });

  const dl = $("#parts-list");
  dl.innerHTML = "";
  for (const p of Object.keys(state.chips.parts)) {
    const o = document.createElement("option");
    o.value = p;
    dl.appendChild(o);
  }

  await loadSheet();
  refreshComponents();
  refreshSelection();
}

function loadSheet() {
  return new Promise((resolve, reject) => {
    setStatus(`loading sheet ${state.sheetIndex}…`);
    const img = new Image();
    img.onload = () => {
      state.image = img;
      const w = canvas.clientWidth, h = canvas.clientHeight;
      const scale = Math.min(w / img.width, h / img.height);
      state.view = {
        scale,
        x: (w - img.width * scale) / 2,
        y: (h - img.height * scale) / 2,
      };
      setStatus("");
      render();
      resolve();
    };
    img.onerror = () => reject(new Error(`failed to load sheet ${state.sheetIndex}`));
    img.src = `/api/sheet/${state.sheetIndex}.png`;
  });
}

function refreshComponents() {
  const ul = $("#component-list");
  ul.innerHTML = "";
  for (const c of state.graph.components) {
    const li = document.createElement("li");
    const tag = c.verified ? "✓ " : "  ";
    li.textContent = `${tag}[s${c.sheet}] ${c.refdes} — ${c.part}`;
    if (c.verified) li.classList.add("verified");
    li.style.cursor = "pointer";
    li.addEventListener("click", () => {
      if (c.sheet !== state.sheetIndex) {
        state.sheetIndex = c.sheet;
        $("#sheet-select").value = String(c.sheet);
        loadSheet().then(() => selectComponent(c.refdes));
      } else {
        selectComponent(c.refdes);
      }
    });
    ul.appendChild(li);
  }
  const total = state.graph.components.length;
  const verified = state.graph.components.filter(c => c.verified).length;
  $("#component-count").textContent = `(${verified}/${total} verified)`;
}

function refreshSelection() {
  const el = $("#selection-info");
  if (!state.selectedComponent) {
    el.textContent = "(click a chip to select)";
    return;
  }
  const comp = getComponent(state.selectedComponent);
  if (!comp) {
    state.selectedComponent = null;
    el.textContent = "(click a chip to select)";
    return;
  }
  const part = state.chips.parts[comp.part];
  if (!part) {
    el.innerHTML = `<strong>${comp.refdes}</strong> · ${comp.part} <small>(unknown part)</small>`;
    return;
  }
  const tag = comp.verified ? `<span class="badge-verified">✓ verified</span>` : `<span class="badge-unverified">unverified</span>`;
  let html = `<div class="header"><strong>${comp.refdes}</strong> · ${comp.part} ${tag}<br>`;
  html += `<small>${part.description} · ${part.package}</small><br>`;
  if (comp.verified_at) html += `<small>verified ${comp.verified_at}</small><br>`;
  html += `<small>drag pins on canvas to refine positions</small></div>`;
  html += `<table class="pin-table"><tbody>`;
  for (const p of part.pins) {
    html += `<tr><td>${p.n}</td><td>${p.name}</td><td class="pt-${p.type}">${p.type}</td></tr>`;
  }
  html += `</tbody></table>`;
  el.innerHTML = html;
}

function selectComponent(refdes) {
  state.selectedComponent = refdes;
  refreshSelection();
  render();
}

function setMode(m) {
  state.mode = m;
  const el = $("#mode");
  el.textContent = m;
  el.className = `mode mode-${m}`;
  canvas.style.cursor = m === "drawing" ? "crosshair" : "grab";
}
function setStatus(s) { $("#status").textContent = s; }

async function save() {
  try {
    const r = await api("/api/graph", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.graph),
    });
    setStatus(`saved → ${r.path?.split("/").slice(-2).join("/") ?? "graph.json"}`);
    setTimeout(() => setStatus(""), 2000);
  } catch (e) {
    setStatus("save failed: " + e.message);
  }
}

function hitTestPin(cx, cy) {
  if (!state.selectedComponent) return null;
  const comp = getComponent(state.selectedComponent);
  if (!comp || !comp.pin_positions || comp.sheet !== state.sheetIndex) return null;
  for (const [pinNum, [ix, iy]] of Object.entries(comp.pin_positions)) {
    const cp = imgToCanvas(ix, iy);
    if (Math.hypot(cp.x - cx, cp.y - cy) <= PIN_RADIUS_PX + PIN_HIT_PAD_PX) {
      return { refdes: comp.refdes, pin: pinNum };
    }
  }
  return null;
}

function clonePinPositions(comp) {
  if (!comp.pin_positions) return null;
  const out = {};
  for (const [k, v] of Object.entries(comp.pin_positions)) out[k] = [...v];
  return out;
}

function isInsideSelectedBbox(cx, cy) {
  if (!state.selectedComponent) return false;
  const comp = getComponent(state.selectedComponent);
  if (!comp || comp.sheet !== state.sheetIndex) return false;
  const a = imgToCanvas(comp.bbox[0], comp.bbox[1]);
  const b = imgToCanvas(comp.bbox[2], comp.bbox[3]);
  const lx = Math.min(a.x, b.x), hx = Math.max(a.x, b.x);
  const ly = Math.min(a.y, b.y), hy = Math.max(a.y, b.y);
  return cx >= lx && cx <= hx && cy >= ly && cy <= hy;
}

function hitTestResizeHandle(cx, cy) {
  if (!state.selectedComponent) return null;
  const comp = getComponent(state.selectedComponent);
  if (!comp || comp.sheet !== state.sheetIndex) return null;
  const corners = [
    ["tl", comp.bbox[0], comp.bbox[1]],
    ["tr", comp.bbox[2], comp.bbox[1]],
    ["bl", comp.bbox[0], comp.bbox[3]],
    ["br", comp.bbox[2], comp.bbox[3]],
  ];
  const reach = HANDLE_SIZE_PX / 2 + HANDLE_HIT_PAD_PX;
  for (const [corner, ix, iy] of corners) {
    const cp = imgToCanvas(ix, iy);
    if (Math.abs(cp.x - cx) <= reach && Math.abs(cp.y - cy) <= reach) {
      return { refdes: comp.refdes, corner };
    }
  }
  return null;
}

function cursorForCorner(corner) {
  return (corner === "tl" || corner === "br") ? "nwse-resize" : "nesw-resize";
}

function hitTestBbox(cx, cy) {
  const comps = state.graph.components;
  for (let i = comps.length - 1; i >= 0; i--) {
    const c = comps[i];
    if (c.sheet !== state.sheetIndex) continue;
    const a = imgToCanvas(c.bbox[0], c.bbox[1]);
    const b = imgToCanvas(c.bbox[2], c.bbox[3]);
    if (cx >= Math.min(a.x, b.x) && cx <= Math.max(a.x, b.x) &&
        cy >= Math.min(a.y, b.y) && cy <= Math.max(a.y, b.y)) {
      return c.refdes;
    }
  }
  return null;
}

canvas.addEventListener("mousedown", (e) => {
  const rect = canvas.getBoundingClientRect();
  const cx = e.clientX - rect.left;
  const cy = e.clientY - rect.top;

  if (state.mode === "drawing") {
    const p = canvasToImg(cx, cy);
    state.drawStart = p;
    state.drawCurrent = p;
    return;
  }

  // (B) Bbox resize handles take priority over pins (they're at the corners).
  const handleHit = hitTestResizeHandle(cx, cy);
  if (handleHit) {
    const comp = getComponent(handleHit.refdes);
    state.bboxResize = {
      refdes: handleHit.refdes,
      corner: handleHit.corner,
      originalBbox: [...comp.bbox],
      originalPins: clonePinPositions(comp),
    };
    canvas.style.cursor = cursorForCorner(handleHit.corner);
    return;
  }

  const pinHit = hitTestPin(cx, cy);
  if (pinHit) {
    state.pinDrag = pinHit;
    canvas.style.cursor = "grabbing";
    return;
  }

  // Drag inside the selected component's bbox to translate the whole component.
  if (isInsideSelectedBbox(cx, cy)) {
    const comp = getComponent(state.selectedComponent);
    state.bboxTranslate = {
      refdes: comp.refdes,
      originalBbox: [...comp.bbox],
      originalPins: clonePinPositions(comp),
      mouseStart: canvasToImg(cx, cy),
    };
    canvas.style.cursor = "move";
    return;
  }

  state.pan = { x0: cx, y0: cy, vx0: state.view.x, vy0: state.view.y, moved: false };
  canvas.style.cursor = "grabbing";
});

canvas.addEventListener("mousemove", (e) => {
  const rect = canvas.getBoundingClientRect();
  const cx = e.clientX - rect.left;
  const cy = e.clientY - rect.top;
  const ip = canvasToImg(cx, cy);

  if (!state.pan && !state.pinDrag && state.mode !== "drawing") {
    setStatus(`x=${Math.round(ip.x)} y=${Math.round(ip.y)} zoom=${state.view.scale.toFixed(3)}×`);
  }

  if (state.bboxResize) {
    const comp = getComponent(state.bboxResize.refdes);
    if (comp) {
      const MIN = 8;
      const orig = state.bboxResize.originalBbox;
      let nx1 = orig[0], ny1 = orig[1], nx2 = orig[2], ny2 = orig[3];
      switch (state.bboxResize.corner) {
        case "tl": nx1 = Math.min(ip.x, orig[2] - MIN); ny1 = Math.min(ip.y, orig[3] - MIN); break;
        case "tr": nx2 = Math.max(ip.x, orig[0] + MIN); ny1 = Math.min(ip.y, orig[3] - MIN); break;
        case "bl": nx1 = Math.min(ip.x, orig[2] - MIN); ny2 = Math.max(ip.y, orig[1] + MIN); break;
        case "br": nx2 = Math.max(ip.x, orig[0] + MIN); ny2 = Math.max(ip.y, orig[1] + MIN); break;
      }
      comp.bbox = [nx1, ny1, nx2, ny2];

      // Pins are anchored to the bbox: transform each pin's position from the
      // original bbox to the new bbox by its proportional location.
      if (state.bboxResize.originalPins) {
        const ow = orig[2] - orig[0], oh = orig[3] - orig[1];
        const nw = nx2 - nx1, nh = ny2 - ny1;
        for (const [num, [ox, oy]] of Object.entries(state.bboxResize.originalPins)) {
          const fx = ow > 0 ? (ox - orig[0]) / ow : 0.5;
          const fy = oh > 0 ? (oy - orig[1]) / oh : 0.5;
          comp.pin_positions[num] = [nx1 + fx * nw, ny1 + fy * nh];
        }
      }
      render();
    }
  } else if (state.bboxTranslate) {
    const comp = getComponent(state.bboxTranslate.refdes);
    if (comp) {
      const dx = ip.x - state.bboxTranslate.mouseStart.x;
      const dy = ip.y - state.bboxTranslate.mouseStart.y;
      const orig = state.bboxTranslate.originalBbox;
      comp.bbox = [orig[0] + dx, orig[1] + dy, orig[2] + dx, orig[3] + dy];
      if (state.bboxTranslate.originalPins) {
        for (const [num, [ox, oy]] of Object.entries(state.bboxTranslate.originalPins)) {
          comp.pin_positions[num] = [ox + dx, oy + dy];
        }
      }
      render();
    }
  } else if (state.pinDrag) {
    const comp = getComponent(state.pinDrag.refdes);
    if (comp && comp.pin_positions) {
      comp.pin_positions[state.pinDrag.pin] = [ip.x, ip.y];
      render();
    }
  } else if (state.pan) {
    const dx = cx - state.pan.x0, dy = cy - state.pan.y0;
    if (Math.hypot(dx, dy) > CLICK_THRESHOLD_PX) state.pan.moved = true;
    state.view.x = state.pan.vx0 + dx;
    state.view.y = state.pan.vy0 + dy;
    render();
  } else if (state.mode === "drawing" && state.drawStart) {
    state.drawCurrent = ip;
    render();
  }
});

window.addEventListener("mouseup", (e) => {
  if (state.bboxResize) {
    state.bboxResize = null;
    canvas.style.cursor = state.mode === "drawing" ? "crosshair" : "grab";
    setStatus(`resized — ⌘S to save`);
    return;
  }
  if (state.bboxTranslate) {
    state.bboxTranslate = null;
    canvas.style.cursor = state.mode === "drawing" ? "crosshair" : "grab";
    setStatus(`moved — ⌘S to save`);
    return;
  }
  if (state.pinDrag) {
    state.pinDrag = null;
    canvas.style.cursor = state.mode === "drawing" ? "crosshair" : "grab";
    return;
  }
  if (state.pan) {
    if (!state.pan.moved) {
      const rect = canvas.getBoundingClientRect();
      const cx = e.clientX - rect.left;
      const cy = e.clientY - rect.top;
      selectComponent(hitTestBbox(cx, cy));
    }
    state.pan = null;
    canvas.style.cursor = state.mode === "drawing" ? "crosshair" : "grab";
    return;
  }
  if (state.mode === "drawing" && state.drawStart && state.drawCurrent) {
    const a = state.drawStart, b = state.drawCurrent;
    const box = {
      x1: Math.min(a.x, b.x), y1: Math.min(a.y, b.y),
      x2: Math.max(a.x, b.x), y2: Math.max(a.y, b.y),
    };
    state.drawStart = null; state.drawCurrent = null;
    const wPx = Math.abs(box.x2 - box.x1) * state.view.scale;
    const hPx = Math.abs(box.y2 - box.y1) * state.view.scale;
    if (wPx < 3 || hPx < 3) {
      setStatus(`box too small (${Math.round(wPx)}×${Math.round(hPx)} canvas px) — try again`);
      render();
      return;
    }
    state.pendingBox = box;
    openDialog();
  }
});

canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  const rect = canvas.getBoundingClientRect();
  const cx = e.clientX - rect.left;
  const cy = e.clientY - rect.top;
  const factor = Math.exp(-e.deltaY * 0.001);
  const ip = canvasToImg(cx, cy);
  state.view.scale = Math.max(0.02, Math.min(8, state.view.scale * factor));
  state.view.x = cx - ip.x * state.view.scale;
  state.view.y = cy - ip.y * state.view.scale;
  render();
}, { passive: false });

window.addEventListener("keydown", (e) => {
  if (e.target.matches("input, textarea")) return;
  if (document.querySelector("dialog[open]")) return;
  if (e.key.toLowerCase() === "b") {
    e.preventDefault();
    setMode(state.mode === "drawing" ? "view" : "drawing");
  } else if (e.key.toLowerCase() === "d" && state.selectedComponent) {
    e.preventDefault();
    deleteSelected();
  } else if (e.key.toLowerCase() === "e" && state.selectedComponent) {
    e.preventDefault();
    openEditDialog();
  } else if (e.key.toLowerCase() === "v" && state.selectedComponent) {
    e.preventDefault();
    toggleVerified();
  } else if (e.key === "Escape") {
    e.preventDefault();
    state.drawStart = null; state.drawCurrent = null;
    if (state.mode === "drawing") setMode("view");
    if (state.selectedComponent) selectComponent(null);
    render();
  } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    save();
  }
});

function toggleVerified() {
  const comp = getComponent(state.selectedComponent);
  if (!comp) return;
  if (comp.verified) {
    delete comp.verified;
    delete comp.verified_at;
    delete comp.verified_by;
    setStatus(`unverified ${comp.refdes} — ⌘S to save`);
  } else {
    comp.verified = true;
    comp.verified_at = new Date().toISOString().replace(/\.\d+Z$/, "Z");
    setStatus(`verified ${comp.refdes} — ⌘S to save`);
  }
  refreshComponents();
  refreshSelection();
  render();
}

function deleteSelected() {
  const refdes = state.selectedComponent;
  if (!refdes) return;
  const comp = getComponent(refdes);
  if (!comp) return;
  if (!confirm(`Delete ${refdes} (${comp.part})?`)) return;
  state.graph.components = state.graph.components.filter(c => c.refdes !== refdes);
  selectComponent(null);
  refreshComponents();
  setStatus(`deleted ${refdes} — ⌘S to save`);
}

$("#save").addEventListener("click", save);

const dlg = $("#component-dialog");
const refdesInput = $("#refdes-input");
const partInput = $("#part-input");
const partInfo = $("#part-info");

function openDialog() {
  refdesInput.value = "";
  partInput.value = "";
  partInfo.textContent = "";
  dlg.showModal();
  refdesInput.focus();
}

partInput.addEventListener("input", () => {
  const part = state.chips.parts[partInput.value.trim()];
  partInfo.textContent = part
    ? `${part.description} • ${part.package} • VCC=${part.vcc_pin} GND=${part.gnd_pin} • ${part.pins.length} pins`
    : (partInput.value ? "(unknown part — check chips.json)" : "");
});

dlg.addEventListener("close", () => {
  refdesInput.blur();
  partInput.blur();
  const box = state.pendingBox;
  state.pendingBox = null;
  if (dlg.returnValue !== "confirm" || !box) {
    setStatus("cancelled");
    render();
    return;
  }
  const refdes = refdesInput.value.trim();
  const partName = partInput.value.trim();
  const part = state.chips.parts[partName];
  if (!refdes) {
    setStatus("discarded — empty refdes");
    render();
    return;
  }
  if (!part) {
    setStatus(`discarded — unknown part "${partName}" (check chips.json)`);
    render();
    return;
  }
  if (state.graph.components.some(c => c.refdes === refdes)) {
    setStatus(`discarded — refdes "${refdes}" already exists`);
    render();
    return;
  }
  const bbox = [box.x1, box.y1, box.x2, box.y2];
  const pin_positions = defaultPinPositions(part, bbox) || {};
  state.graph.components.push({
    refdes,
    part: partName,
    sheet: state.sheetIndex,
    bbox,
    pin_positions,
    evidence: { source: "human", confidence: 1.0 },
  });
  console.log("[viewer] added", { refdes, partName, sheet: state.sheetIndex, bbox, pin_count: Object.keys(pin_positions).length });
  setStatus(`added ${refdes} (${partName}) — ⌘S to save`);
  setMode("view");
  refreshComponents();
  selectComponent(refdes);
});

const editDlg = $("#edit-dialog");
const editRefdesInput = $("#edit-refdes-input");
const editPartInput = $("#edit-part-input");
const editPartInfo = $("#edit-part-info");

function openEditDialog() {
  const comp = getComponent(state.selectedComponent);
  if (!comp) return;
  editRefdesInput.value = comp.refdes;
  editPartInput.value = comp.part;
  const part = state.chips.parts[comp.part];
  editPartInfo.textContent = part
    ? `${part.description} • ${part.package} • ${part.pins.length} pins`
    : "";
  editDlg.showModal();
  editRefdesInput.focus();
  editRefdesInput.select();
}

editPartInput.addEventListener("input", () => {
  const part = state.chips.parts[editPartInput.value.trim()];
  editPartInfo.textContent = part
    ? `${part.description} • ${part.package} • VCC=${part.vcc_pin} GND=${part.gnd_pin} • ${part.pins.length} pins`
    : (editPartInput.value ? "(unknown part — check chips.json)" : "");
});

editDlg.addEventListener("close", () => {
  // Blur inputs so subsequent hotkeys reach the window handler.
  editRefdesInput.blur();
  editPartInput.blur();
  if (editDlg.returnValue !== "confirm") return;
  const oldRefdes = state.selectedComponent;
  const comp = getComponent(oldRefdes);
  if (!comp) return;
  const newRefdes = editRefdesInput.value.trim();
  const newPartName = editPartInput.value.trim();
  const newPart = state.chips.parts[newPartName];
  if (!newRefdes) { setStatus("edit discarded — empty refdes"); return; }
  if (!newPart)   { setStatus(`edit discarded — unknown part "${newPartName}"`); return; }
  if (newRefdes !== oldRefdes && state.graph.components.some(c => c.refdes === newRefdes)) {
    setStatus(`edit discarded — refdes "${newRefdes}" already exists`);
    return;
  }
  const partChanged = newPartName !== comp.part;
  comp.refdes = newRefdes;
  comp.part = newPartName;
  if (partChanged) {
    const pos = defaultPinPositions(newPart, comp.bbox);
    if (pos) comp.pin_positions = pos;
  }
  state.selectedComponent = newRefdes;
  refreshComponents();
  refreshSelection();
  render();
  setStatus(`edited → ${newRefdes} (${newPartName}) — ⌘S to save`);
});

window.addEventListener("resize", fitCanvas);
fitCanvas();
loadAll().catch(e => { setStatus("error: " + e.message); console.error(e); });
