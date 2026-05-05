const $ = (sel) => document.querySelector(sel);

const PIN_RADIUS_PX = 4;
const PIN_HIT_PAD_PX = 3;
const CLICK_THRESHOLD_PX = 3;
const HANDLE_SIZE_PX = 9;
const HANDLE_HIT_PAD_PX = 3;

const state = {
  boardId: null,
  boardsList: [],
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
  selectedNet: null,   // net.name (only one of selectedComponent/selectedNet active)
  pinDrag: null,
  bboxResize: null,
  bboxTranslate: null,
  pinPlace: null, // { refdes, pins:[ordered], currentIdx }
  netMode: false,
  netDrawPins: [],     // accumulated endpoints in click order: [{refdes, pin, sheet}]
  netDrawCursor: null, // {x, y} in image coords for preview line
  pendingNet: null,    // { endpoints: [{refdes, pin, sheet}, ...] } awaiting dialog confirm
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

// Function-based pin layout: classify each pin by group/type/name pattern and
// place it on the appropriate side of the bbox.
//   left:   address pins (group=addr, name~=A0..)
//   right:  data/output pins (group=data, name=D/Q/O/I/O*)
//   top:    power (VCC/VDD/VPP)
//   bottom: ground (GND/VSS) plus other control inputs (~CE/~OE/etc.)
// This matches schematic-symbol convention better than physical-DIP order
// for the common case of memories/CPUs/PLDs. Falls back to physical-DIP-style
// for chips where the heuristic doesn't classify pins (gates, latches, etc.).
function defaultPinPositions(part, bbox) {
  const pins = part.pins || [];
  if (!pins.length) return null;
  const [x1, y1, x2, y2] = bbox;
  const w = x2 - x1, h = y2 - y1;
  const out = {};

  const left = [], right = [], top = [], bottom = [];
  for (const p of pins) {
    const grp = (p.group || "").toLowerCase();
    const t = p.type;
    const nm = (p.name || "").trim();
    if (t === "power" || /^(VCC|VDD|VPP|\+5V|EVCC|TVCC)$/i.test(nm)) {
      top.push(p);
    } else if (t === "ground" || /^(GND|VSS|VEE)$/i.test(nm)) {
      bottom.push(p);
    } else if (grp === "addr" || /^(A|MCA[._])\d/i.test(nm)) {
      left.push(p);
    } else if (grp === "data" || /^(D|Q|O|I\/O|MCD[._])\d/i.test(nm) || /^(D|Q)\d/i.test(nm)) {
      right.push(p);
    } else if (t === "input" || t === "clock") {
      bottom.push(p);
    } else if (t === "output" || t === "tri_state" || t === "bidir") {
      right.push(p);
    } else {
      // Fallback: distribute across whichever side has room.
      (left.length <= right.length ? left : right).push(p);
    }
  }

  // If both left and right are empty (rare — quad-gate chips with grouped
  // pins like 'g1','g2'..), fall back to physical DIP layout.
  if (left.length === 0 && right.length === 0 && pins.length % 2 === 0) {
    const half = pins.length / 2;
    for (const p of pins) {
      let x, y;
      if (p.n <= half) {
        x = x1;
        y = y1 + (p.n - 0.5) * (h / half);
      } else {
        const slot = pins.length - p.n;
        x = x2;
        y = y1 + (slot + 0.5) * (h / half);
      }
      out[String(p.n)] = [x, y];
    }
    return out;
  }

  // Sort each side by pin number for stable, intuitive ordering.
  for (const arr of [left, right, top, bottom]) arr.sort((a, b) => a.n - b.n);

  const place = (arr, axis) => {
    const denom = arr.length || 1;
    for (let i = 0; i < arr.length; i++) {
      const f = (i + 0.5) / denom;
      let x, y;
      if (axis === "left")   { x = x1;        y = y1 + f * h; }
      else if (axis === "right")  { x = x2;        y = y1 + f * h; }
      else if (axis === "top")    { x = x1 + f * w; y = y1; }
      else /* bottom */           { x = x1 + f * w; y = y2; }
      out[String(arr[i].n)] = [x, y];
    }
  };
  place(left, "left");
  place(right, "right");
  place(top, "top");
  place(bottom, "bottom");
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

function edgeTypeColor(t) {
  switch (t) {
    case "wire":           return "#0bf";   // bright cyan
    case "label":          return "#5f5";   // green
    case "sheet_zone":     return "#f5b";   // pink
    case "off_page":       return "#fa3";   // orange
    case "bus":            return "#5af";   // blue
    case "implicit_power": return "#f5a";   // magenta
    default:               return "#aaa";
  }
}

function getPinSourcePos(refdes, pin) {
  const comp = state.graph.components.find(c => c.refdes === refdes);
  if (!comp || !comp.pin_positions) return null;
  return comp.pin_positions[String(pin)] || null;
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

  // Render nets that have endpoints on this sheet.
  drawNets();

  // Net-mode preview: dashed gold rings around each accumulated pin and a
  // dashed line through them in click order, plus a cursor-tracking segment
  // from the most-recent pin.
  if (state.netMode && state.netDrawPins.length > 0) {
    const pts = [];
    for (const ep of state.netDrawPins) {
      const pos = getPinSourcePos(ep.refdes, ep.pin);
      if (pos) pts.push(imgToCanvas(pos[0], pos[1]));
    }
    ctx.strokeStyle = "#fc3";
    ctx.lineWidth = 2;
    for (const cp of pts) {
      ctx.beginPath();
      ctx.arc(cp.x, cp.y, PIN_RADIUS_PX + 5, 0, Math.PI * 2);
      ctx.setLineDash([3, 3]);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    if (pts.length >= 2) {
      ctx.setLineDash([5, 5]);
      ctx.beginPath();
      ctx.moveTo(pts[0].x, pts[0].y);
      for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    if (state.netDrawCursor && pts.length > 0) {
      const last = pts[pts.length - 1];
      const cp = imgToCanvas(state.netDrawCursor.x, state.netDrawCursor.y);
      ctx.setLineDash([5, 5]);
      ctx.beginPath();
      ctx.moveTo(last.x, last.y);
      ctx.lineTo(cp.x, cp.y);
      ctx.stroke();
      ctx.setLineDash([]);
    }
  }

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
  // actual pin locations, so they're visual noise. Exception: in net-draw
  // mode, render every pin so they're targetable.
  const showPins = selected || state.netMode;
  if (showPins && part && comp.pin_positions) {
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

      // Pin labels only show for the SELECTED component (in net mode we just
      // want clickable dots, no clutter).
      if (selected && pinDef) {
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

  // (B) Resize handles at the four corners of the selected bbox — hidden in
  // pin-place mode so clicks at corners place a pin instead of starting a resize.
  if (selected && !state.pinPlace && !state.netMode) {
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

function drawNets() {
  const nets = state.graph?.nets || [];
  // At low zoom, scale line widths up so even short wires are visible.
  const widthScale = Math.max(1, 0.3 / Math.max(state.view.scale, 0.001));
  state.netZoneBadges = []; // updated per render; used by hit-test for click-to-navigate

  for (const net of nets) {
    if (!net.endpoints || net.endpoints.length < 1) continue;
    const points = [];
    for (const ep of net.endpoints) {
      if (ep.sheet !== undefined && ep.sheet !== state.sheetIndex) continue;
      const pos = getPinSourcePos(ep.refdes, ep.pin);
      if (pos) points.push({ pos, ep });
    }
    if (points.length === 0) continue;
    const color = edgeTypeColor((points[0] || {ep:{edge_type:'wire'}}).ep.edge_type);
    const isSelected = state.selectedNet === net.name;
    const baseLine = isSelected ? 3.5 : 2.5;
    const baseHalo = isSelected ? 8 : 5;

    // Connecting line — only if there are 2+ on-sheet endpoints to connect.
    if (points.length >= 2) {
      // Dark halo for contrast against white paper.
      ctx.strokeStyle = isSelected ? "rgba(255,204,51,0.85)" : "rgba(0,0,0,0.6)";
      ctx.lineWidth = baseHalo * widthScale;
      ctx.beginPath();
      const first = imgToCanvas(points[0].pos[0], points[0].pos[1]);
      ctx.moveTo(first.x, first.y);
      for (let i = 1; i < points.length; i++) {
        const p = imgToCanvas(points[i].pos[0], points[i].pos[1]);
        ctx.lineTo(p.x, p.y);
      }
      ctx.stroke();
      // Bright color line on top.
      ctx.strokeStyle = color;
      ctx.lineWidth = baseLine * widthScale;
      ctx.beginPath();
      ctx.moveTo(first.x, first.y);
      for (let i = 1; i < points.length; i++) {
        const p = imgToCanvas(points[i].pos[0], points[i].pos[1]);
        ctx.lineTo(p.x, p.y);
      }
      ctx.stroke();
    }

    // Endpoint rings — visible regardless of wire length, so a "wire" hugging
    // a chip bbox still shows as colored markers on each connected pin.
    const ringR = (PIN_RADIUS_PX + 3) * Math.min(widthScale, 1.6);
    ctx.lineWidth = Math.max(1.5, 2 * Math.min(widthScale, 1.4));
    ctx.strokeStyle = color;
    for (const { pos } of points) {
      const cp = imgToCanvas(pos[0], pos[1]);
      ctx.beginPath();
      ctx.arc(cp.x, cp.y, ringR, 0, Math.PI * 2);
      ctx.stroke();
    }

    // Net name label at midpoint of first segment.
    if (net.name && points.length >= 2) {
      const a = imgToCanvas(points[0].pos[0], points[0].pos[1]);
      const b = imgToCanvas(points[1].pos[0], points[1].pos[1]);
      ctx.fillStyle = color;
      ctx.font = isSelected ? "bold 12px ui-monospace, monospace" : "10px ui-monospace, monospace";
      ctx.fillText(net.name, (a.x + b.x) / 2 + 4, (a.y + b.y) / 2 - 4);
    }

    // Sheet-zone badges — render an arrow + label at each on-sheet endpoint
    // whose edge_type is sheet_zone or off_page. Captures the cross-sheet
    // linkage so the human can see "this net continues to <ref> on another
    // sheet" without leaving this view. Click navigates to that sheet.
    for (const { pos, ep } of points) {
      const isCrossSheet = (ep.edge_type === "sheet_zone" || ep.edge_type === "off_page");
      if (!isCrossSheet) continue;
      const refLabel = ep.sheet_zone_ref || (net.name);
      const cp = imgToCanvas(pos[0], pos[1]);
      const text = `→ ${refLabel}`;
      ctx.font = "bold 11px ui-monospace, monospace";
      const m = ctx.measureText(text);
      const padX = 5, padY = 3;
      const w = m.width + 2 * padX;
      const h = 14 + 2 * padY;
      const bx = cp.x + 8;
      const by = cp.y - h - 4;
      // Background pill
      ctx.fillStyle = color;
      ctx.strokeStyle = "rgba(0,0,0,0.7)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      // Rounded corners — fall back to plain rect if roundRect missing.
      if (typeof ctx.roundRect === "function") {
        ctx.roundRect(bx, by, w, h, 4);
      } else {
        ctx.rect(bx, by, w, h);
      }
      ctx.fill();
      ctx.stroke();
      // White text
      ctx.fillStyle = "#000";
      ctx.textBaseline = "middle";
      ctx.fillText(text, bx + padX, by + h / 2);
      ctx.textBaseline = "alphabetic";
      // Stash for hit testing.
      state.netZoneBadges.push({
        netName: net.name,
        refLabel,
        rect: [bx, by, bx + w, by + h],
      });
    }
  }
}

// Parse a sheet-zone ref like "4C6" → 4 (the sheet number prefix).
function parseSheetFromRef(ref) {
  if (!ref) return null;
  const m = String(ref).match(/^(\d+)/);
  if (!m) return null;
  return parseInt(m[1], 10);
}

function hitTestZoneBadge(cx, cy) {
  for (const b of (state.netZoneBadges || [])) {
    const [x1, y1, x2, y2] = b.rect;
    if (cx >= x1 && cx <= x2 && cy >= y1 && cy <= y2) {
      return b;
    }
  }
  return null;
}

// Distance from point (px,py) to line segment (ax,ay)-(bx,by).
function pointToSegmentDist(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay;
  const lenSq = dx * dx + dy * dy;
  if (lenSq === 0) return Math.hypot(px - ax, py - ay);
  let t = ((px - ax) * dx + (py - ay) * dy) / lenSq;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

function hitTestNet(cx, cy) {
  const nets = state.graph?.nets || [];
  let best = null;
  let bestDist = 6; // pixel tolerance
  for (const net of nets) {
    if (!net.endpoints || net.endpoints.length < 2) continue;
    const pts = [];
    for (const ep of net.endpoints) {
      if (ep.sheet !== undefined && ep.sheet !== state.sheetIndex) continue;
      const pos = getPinSourcePos(ep.refdes, ep.pin);
      if (pos) pts.push(imgToCanvas(pos[0], pos[1]));
    }
    for (let i = 0; i < pts.length - 1; i++) {
      const d = pointToSegmentDist(cx, cy, pts[i].x, pts[i].y, pts[i+1].x, pts[i+1].y);
      if (d < bestDist) { bestDist = d; best = net.name; }
    }
  }
  return best;
}

function withBoard(path) {
  if (!state.boardId) return path;
  return path + (path.includes("?") ? "&" : "?") + "board=" + encodeURIComponent(state.boardId);
}

async function api(path, opts = {}) {
  const url = (path.startsWith("/api/") && !path.startsWith("/api/boards") && !path.startsWith("/api/chips"))
    ? withBoard(path) : path;
  const res = await fetch(url, opts);
  if (!res.ok) throw new Error(`${url}: ${res.status} ${res.statusText}`);
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
  if (!state.graph.nets) state.graph.nets = [];
}

function suggestNetName() {
  const used = new Set((state.graph.nets || []).map(n => n.name));
  for (let i = 1; i <= 10000; i++) {
    const candidate = `N${i}`;
    if (!used.has(candidate)) return candidate;
  }
  return `N${Date.now()}`;
}

function hitTestPinAnyComponent(cx, cy) {
  const comps = state.graph.components.filter(c => c.sheet === state.sheetIndex);
  for (const comp of comps) {
    if (!comp.pin_positions) continue;
    for (const [pinNum, [ix, iy]] of Object.entries(comp.pin_positions)) {
      const cp = imgToCanvas(ix, iy);
      if (Math.hypot(cp.x - cx, cp.y - cy) <= PIN_RADIUS_PX + PIN_HIT_PAD_PX + 2) {
        return { refdes: comp.refdes, pin: pinNum, sheet: state.sheetIndex };
      }
    }
  }
  return null;
}

function toggleNetMode() {
  if (state.netMode) {
    exitNetMode();
  } else {
    state.netMode = true;
    state.netDrawPins = [];
    state.netDrawCursor = null;
    canvas.style.cursor = "crosshair";
    setMode("netting");
    setStatus("net mode — click pins; Enter to finalize, Esc to exit");
    render();
  }
}

function exitNetMode() {
  state.netMode = false;
  state.netDrawPins = [];
  state.netDrawCursor = null;
  setMode("view");
  setStatus("");
  render();
}

function netModeStatus() {
  const n = state.netDrawPins.length;
  if (n === 0) return "net mode — click pins; Esc to exit";
  if (n === 1) return `net mode — 1 pin selected; click more, Esc to cancel`;
  return `net mode — ${n} pins selected; Enter to finalize, Esc to cancel`;
}

async function loadAll() {
  // 1) populate board picker
  state.boardsList = await api("/api/boards");
  if (!state.boardId && state.boardsList.length) state.boardId = state.boardsList[0].id;
  const boardSel = $("#board-select");
  boardSel.innerHTML = "";
  for (const b of state.boardsList) {
    const o = document.createElement("option");
    o.value = b.id;
    o.textContent = `${b.title} [${b.id}]`;
    boardSel.appendChild(o);
  }
  if (state.boardId) boardSel.value = state.boardId;
  boardSel.onchange = async () => {
    state.boardId = boardSel.value;
    state.sheetIndex = 1;
    state.selectedComponent = null;
    state.selectedNet = null;
    await loadBoardData();
  };

  await loadBoardData();
}

async function loadBoardData() {
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
  refreshNetsList();
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
    // Cache-bust + per-board to avoid stale exidy_440 image on board switches.
    img.src = withBoard(`/api/sheet/${state.sheetIndex}.png`) + (state.boardId ? `&_=${Date.now()}` : `?_=${Date.now()}`);
  });
}

function refreshNetsList() {
  const ul = $("#net-list");
  if (!ul) return;
  ul.innerHTML = "";
  const nets = state.graph?.nets || [];
  for (const net of nets) {
    const li = document.createElement("li");
    const eps = net.endpoints.map(e => `${e.refdes}.${e.pin}`).join(" ⟷ ");
    const et = net.endpoints[0]?.edge_type || "?";
    li.innerHTML = `<span style="color:${edgeTypeColor(et)}">●</span> <strong>${net.name}</strong> <small>${eps}</small>`;
    if (state.selectedNet === net.name) li.classList.add("selected-net");
    li.style.cursor = "pointer";
    li.addEventListener("click", () => selectNet(net.name));
    ul.appendChild(li);
  }
  const c = $("#net-count");
  if (c) c.textContent = `(${nets.length})`;
}

function deleteSelectedNet() {
  const name = state.selectedNet;
  if (!name) return;
  if (!confirm(`Delete net ${name}?`)) return;
  state.graph.nets = (state.graph.nets || []).filter(n => n.name !== name);
  state.selectedNet = null;
  refreshNetsList();
  refreshSelection();
  render();
  setStatus(`deleted net ${name} — ⌘S to save`);
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
  if (state.selectedNet) {
    const net = (state.graph?.nets || []).find(n => n.name === state.selectedNet);
    if (!net) { state.selectedNet = null; el.textContent = "(click a chip or net)"; return; }
    const eps = net.endpoints.map(e => `${e.refdes}.${e.pin}`).join(" ⟷ ");
    const et = net.endpoints[0]?.edge_type || "?";
    let html = `<div class="header"><strong style="color:${edgeTypeColor(et)}">${net.name}</strong> <small>${net.kind}/${et}</small><br>`;
    html += `<small>${eps}</small><br>`;
    if (net.endpoints[0]?.sheet_zone_ref) html += `<small>sheet zone: ${net.endpoints[0].sheet_zone_ref}</small><br>`;
    html += `<small>D to delete · Esc to deselect</small></div>`;
    el.innerHTML = html;
    return;
  }
  if (!state.selectedComponent) {
    el.textContent = "(click a chip or net)";
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

  const pp = state.pinPlace && state.pinPlace.refdes === comp.refdes ? state.pinPlace : null;
  if (pp) {
    const cur = pp.pins[pp.currentIdx];
    if (cur) {
      html += `<div class="pin-place-indicator"><strong>Placing pin ${cur.n}</strong> · ${cur.name} <small>(${cur.type})</small><br><small>click on the schematic; Esc to exit</small></div>`;
    } else {
      html += `<div class="pin-place-indicator">all pins placed</div>`;
    }
  } else {
    html += `<small>drag pins to refine · drag corner = resize · drag inside = move · <kbd>N</kbd> renumber</small></div>`;
  }
  if (pp) html += `</div>`;

  html += `<table class="pin-table"><tbody>`;
  for (const p of part.pins) {
    const isCurrent = pp && pp.pins[pp.currentIdx] && pp.pins[pp.currentIdx].n === p.n;
    const placed = comp.pin_positions && comp.pin_positions[String(p.n)];
    const cls = isCurrent ? "pin-current" : (pp && placed ? "pin-placed" : "");
    html += `<tr class="${cls}"><td>${p.n}</td><td>${p.name}</td><td class="pt-${p.type}">${p.type}</td></tr>`;
  }
  html += `</tbody></table>`;
  el.innerHTML = html;
}

function selectComponent(refdes) {
  state.selectedComponent = refdes;
  if (refdes) state.selectedNet = null;
  refreshSelection();
  render();
}

function selectNet(name) {
  state.selectedNet = name;
  if (name) state.selectedComponent = null;
  refreshSelection();
  refreshNetsList();
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

  if (state.pinPlace) {
    const ip = canvasToImg(cx, cy);
    const comp = getComponent(state.pinPlace.refdes);
    if (comp) {
      const p = state.pinPlace.pins[state.pinPlace.currentIdx];
      if (p) {
        comp.pin_positions[String(p.n)] = [ip.x, ip.y];
        state.pinPlace.currentIdx++;
        updatePinPlaceStatus();
        refreshSelection();
        render();
      }
    }
    return;
  }

  if (state.netMode) {
    const pinHit = hitTestPinAnyComponent(cx, cy);
    if (!pinHit) return; // click empty space — ignore in net mode
    // Toggle: clicking an already-selected pin removes it from the set.
    const idx = state.netDrawPins.findIndex(p =>
      p.refdes === pinHit.refdes && String(p.pin) === String(pinHit.pin));
    if (idx >= 0) {
      state.netDrawPins.splice(idx, 1);
    } else {
      state.netDrawPins.push(pinHit);
    }
    setStatus(netModeStatus());
    render();
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

  if (state.netMode && state.netDrawPins.length > 0) {
    state.netDrawCursor = ip;
    render();
    return;
  }

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
      // Sheet-zone badge takes priority — clicking jumps to the linked sheet.
      const badgeHit = hitTestZoneBadge(cx, cy);
      if (badgeHit) {
        const targetSheet = parseSheetFromRef(badgeHit.refLabel);
        if (targetSheet && state.board?.sheets?.some(s => s.index === targetSheet)) {
          state.sheetIndex = targetSheet;
          $("#sheet-select").value = String(targetSheet);
          state.selectedComponent = null;
          state.selectedNet = badgeHit.netName;
          refreshSelection();
          loadSheet().then(() => render()).catch(err => setStatus("error: " + err.message));
        } else {
          selectNet(badgeHit.netName);
        }
        state.pan = null;
        canvas.style.cursor = state.mode === "drawing" ? "crosshair" : "grab";
        return;
      }
      // Component takes priority; if no component hit, try net.
      const refdesHit = hitTestBbox(cx, cy);
      if (refdesHit) {
        selectComponent(refdesHit);
      } else {
        const netHit = hitTestNet(cx, cy);
        if (netHit) selectNet(netHit);
        else { selectComponent(null); selectNet(null); }
      }
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
  } else if (e.key.toLowerCase() === "d" && state.selectedNet) {
    e.preventDefault();
    deleteSelectedNet();
  } else if (e.key.toLowerCase() === "e" && state.selectedComponent) {
    e.preventDefault();
    openEditDialog();
  } else if (e.key.toLowerCase() === "v" && state.selectedComponent) {
    e.preventDefault();
    toggleVerified();
  } else if (e.key.toLowerCase() === "n" && state.selectedComponent) {
    e.preventDefault();
    togglePinPlace();
  } else if (e.key.toLowerCase() === "w") {
    e.preventDefault();
    toggleNetMode();
  } else if (e.key === "Enter" && state.netMode && state.netDrawPins.length >= 2) {
    e.preventDefault();
    state.pendingNet = { endpoints: [...state.netDrawPins] };
    openNetDialog();
  } else if (e.key === "Escape") {
    e.preventDefault();
    state.drawStart = null; state.drawCurrent = null;
    if (state.netMode) {
      if (state.netDrawPins.length > 0) {
        state.netDrawPins = [];
        state.netDrawCursor = null;
        setStatus(netModeStatus());
        render();
      } else {
        exitNetMode();
      }
      return;
    }
    if (state.pinPlace) { exitPinPlace(); return; }
    if (state.mode === "drawing") setMode("view");
    if (state.selectedComponent) selectComponent(null);
    if (state.selectedNet) selectNet(null);
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

function togglePinPlace() {
  if (state.pinPlace) {
    exitPinPlace();
    return;
  }
  const comp = getComponent(state.selectedComponent);
  if (!comp) return;
  const part = state.chips.parts[comp.part];
  if (!part) { setStatus("can't renumber — unknown part"); return; }
  if (!comp.pin_positions) comp.pin_positions = {};
  state.pinPlace = {
    refdes: comp.refdes,
    pins: [...part.pins].sort((a, b) => a.n - b.n),
    currentIdx: 0,
  };
  canvas.style.cursor = "crosshair";
  updatePinPlaceStatus();
  refreshSelection();
  render();
}

function exitPinPlace() {
  const placed = state.pinPlace ? state.pinPlace.currentIdx : 0;
  state.pinPlace = null;
  canvas.style.cursor = state.mode === "drawing" ? "crosshair" : "grab";
  setStatus(placed > 0 ? `placed ${placed} pin(s) — ⌘S to save` : "");
  refreshSelection();
  render();
}

function updatePinPlaceStatus() {
  if (!state.pinPlace) return;
  const pp = state.pinPlace;
  if (pp.currentIdx >= pp.pins.length) {
    setStatus(`all ${pp.pins.length} pins placed on ${pp.refdes} — ⌘S to save`);
    exitPinPlace();
    return;
  }
  const p = pp.pins[pp.currentIdx];
  setStatus(`place pin ${p.n} (${p.name}) of ${pp.pins.length} — Esc to exit`);
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

const netDlg = $("#net-dialog");
const netNameInput = $("#net-name-input");
const netKindSelect = $("#net-kind-select");
const netEdgeTypeSelect = $("#net-edge-type-select");
const netZoneLabel = $("#net-zone-label");
const netZoneInput = $("#net-zone-input");
const netEndpointsBox = $("#net-endpoints");

function openNetDialog() {
  if (!state.pendingNet) return;
  const eps = state.pendingNet.endpoints;
  netEndpointsBox.textContent = eps.map(e => `${e.refdes}.${e.pin}`).join(" ⟷ ");
  netNameInput.value = suggestNetName();
  netKindSelect.value = "signal";
  netEdgeTypeSelect.value = "wire";
  netZoneInput.value = "";
  netZoneLabel.style.display = "none";
  netDlg.showModal();
  netNameInput.focus();
  netNameInput.select();
}

netEdgeTypeSelect.addEventListener("change", () => {
  netZoneLabel.style.display = netEdgeTypeSelect.value === "sheet_zone" ? "block" : "none";
});

netDlg.addEventListener("close", () => {
  netNameInput.blur();
  netZoneInput.blur();
  const pending = state.pendingNet;
  state.pendingNet = null;
  if (netDlg.returnValue !== "confirm" || !pending) return;

  const name = netNameInput.value.trim();
  if (!name) { setStatus("net discarded — empty name"); return; }
  if ((state.graph.nets || []).some(n => n.name === name)) {
    setStatus(`net discarded — name "${name}" already exists`);
    return;
  }

  const kind = netKindSelect.value;
  const edgeType = netEdgeTypeSelect.value;
  const sheetZoneRef = edgeType === "sheet_zone" ? netZoneInput.value.trim() : null;

  const buildEp = (ep) => {
    const out = { refdes: ep.refdes, pin: ep.pin, sheet: ep.sheet, edge_type: edgeType };
    if (sheetZoneRef) out.sheet_zone_ref = sheetZoneRef;
    out.evidence = { source: "human" };
    return out;
  };

  const net = {
    name,
    kind,
    endpoints: pending.endpoints.map(buildEp),
  };
  state.graph.nets.push(net);

  // Reset accumulator so user can immediately start the next net.
  state.netDrawPins = [];
  state.netDrawCursor = null;
  refreshNetsList?.();
  const epsLabel = pending.endpoints.map(e => `${e.refdes}.${e.pin}`).join("→");
  setStatus(`net ${name} added (${epsLabel}) — ⌘S to save`);
  render();
});

window.addEventListener("resize", fitCanvas);
fitCanvas();
loadAll().catch(e => { setStatus("error: " + e.message); console.error(e); });
