/* Screen Sight -- calibration wizard front end.
 *
 * No build step and no framework on purpose: this runs on the same machine as
 * the pipeline, and the pipeline needs the CPU more than the UI does.
 */
'use strict';

// --------------------------------------------------------------- utilities

const $ = (id) => document.getElementById(id);
const clamp01 = (v) => Math.min(1, Math.max(0, v));

function get(obj, path) {
  return path.split('.').reduce((node, key) => (node == null ? undefined : node[key]), obj);
}

let toastTimer = null;
function toast(message, kind = '') {
  const el = $('toast');
  el.textContent = message;
  el.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast'; }, 2600);
}

async function api(path, body) {
  const options = body === undefined
    ? {}
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const response = await fetch(path, options);
  const text = await response.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { /* non-JSON error page */ }
  if (!response.ok) throw new Error(data.error || `${response.status} ${response.statusText}`);
  return data;
}

// ------------------------------------------------------------- tuner spec

const CONTROLS = [
  {
    group: 'Crop & reflections',
    items: [
      { path: 'crop.inset_percent', label: 'Panel inset', min: 0, max: 15, step: 0.1, unit: '%' },
      { path: 'reflection.margin_percent', label: 'Reflection margin', min: 0, max: 15, step: 0.1, unit: '%' },
    ],
  },
  {
    group: 'Black bars',
    items: [
      { path: 'blackbars.enabled', type: 'toggle', label: 'Remove black bars' },
      { path: 'blackbars.luma_threshold', label: 'Darkness threshold', min: 2, max: 80, step: 1 },
      { path: 'blackbars.percentile', label: 'Tolerance percentile', min: 60, max: 100, step: 0.5 },
      { path: 'blackbars.max_crop_top_bottom_percent', label: 'Max letterbox (top/bottom)', min: 0, max: 25, step: 0.5, unit: '%' },
      { path: 'blackbars.max_crop_left_right_percent', label: 'Max pillarbox (left/right)', min: 0, max: 25, step: 0.5, unit: '%' },
      { path: 'blackbars.window', label: 'Median window', min: 1, max: 60, step: 1, unit: ' fr' },
      { path: 'blackbars.hold_frames', label: 'Hysteresis hold', min: 1, max: 60, step: 1, unit: ' fr' },
      { path: 'blackbars.max_step_percent', label: 'Animation speed', min: 0.05, max: 5, step: 0.05, unit: '%/fr' },
      { path: 'blackbars.symmetric', type: 'toggle', label: 'Assume symmetric bars' },
    ],
  },
  {
    group: 'Colour (software — after the camera)',
    items: [
      { path: 'color.white_balance', type: 'select', label: 'White balance', options: ['off', 'auto', 'manual'] },
      { path: 'color.wb_strength', label: 'White balance strength', min: 0, max: 1, step: 0.01 },
      { path: 'color.gamma', label: 'Gamma (higher = brighter)', min: 0.4, max: 2.5, step: 0.01 },
      { path: 'color.saturation', label: 'Saturation', min: 0, max: 2.5, step: 0.01 },
      { path: 'color.brightness', label: 'Brightness', min: 0.3, max: 2, step: 0.01 },
      { path: 'color.contrast', label: 'Contrast', min: 0.3, max: 2, step: 0.01 },
      { path: 'color.exposure.enabled', type: 'toggle', label: 'Software auto exposure' },
      { path: 'color.exposure.target_luma', label: 'Exposure target', min: 20, max: 220, step: 1 },
    ],
  },
  {
    group: 'Detection',
    items: [
      { path: 'boundary.min_confidence', label: 'Min confidence', min: 0, max: 1, step: 0.01 },
      { path: 'boundary.smoothing_alpha', label: 'Corner smoothing', min: 0.01, max: 1, step: 0.01 },
      { path: 'boundary.corner_deadband_px', label: 'Corner deadband', min: 0, max: 10, step: 0.1, unit: ' px' },
      { path: 'boundary.auto_recalibrate', type: 'toggle', label: 'Recalibrate when bumped' },
      { path: 'movement.enabled', type: 'toggle', label: 'Watch for camera movement' },
      { path: 'movement.ncc_threshold', label: 'Movement sensitivity', min: 0.1, max: 10, step: 0.1 },
      { path: 'movement.check_interval', label: 'Movement check every', min: 0.1, max: 5, step: 0.1, unit: ' s' },
    ],
  },
  {
    group: 'Output',
    items: [
      { path: 'output.fps', label: 'Target frame rate', min: 1, max: 60, step: 1, unit: ' fps' },
      { path: 'perspective.width', label: 'Working width', min: 320, max: 1920, step: 16, unit: ' px' },
      { path: 'perspective.height', label: 'Working height', min: 180, max: 1080, step: 9, unit: ' px' },
      { path: 'resize.mode', type: 'select', label: 'Fit mode', options: ['stretch', 'letterbox', 'crop'] },
    ],
  },
];

// --------------------------------------------------------- config plumbing

const pendingUpdates = {};
let pushTimer = null;
const boundInputs = new Map();
let suppressEcho = false;

function queueUpdate(path, value) {
  pendingUpdates[path] = value;
  clearTimeout(pushTimer);
  // Dragging a slider fires continuously; batching keeps it to a few requests.
  pushTimer = setTimeout(pushUpdates, 120);
}

async function pushUpdates() {
  const updates = { ...pendingUpdates };
  for (const key of Object.keys(pendingUpdates)) delete pendingUpdates[key];
  if (!Object.keys(updates).length) return;
  const status = $('tune-status');
  try {
    suppressEcho = true;
    if (status) status.textContent = 'applying…';
    const result = await api('/api/config', { updates });
    applyConfig(result.config, { skipFocused: true });
    if (status) {
      const keys = Object.keys(updates);
      status.textContent = `applied ${keys[0]}${keys.length > 1 ? ` +${keys.length - 1}` : ''}`;
    }
  } catch (err) {
    if (status) status.textContent = 'update failed';
    toast(err.message, 'error');
  } finally {
    suppressEcho = false;
  }
}

const pendingCameraControls = {};
let cameraPushTimer = null;

function queueCameraControl(name, value) {
  pendingCameraControls[name] = value;
  clearTimeout(cameraPushTimer);
  cameraPushTimer = setTimeout(pushCameraControls, 120);
}

async function pushCameraControls() {
  const controls = { ...pendingCameraControls };
  for (const key of Object.keys(pendingCameraControls)) delete pendingCameraControls[key];
  if (!Object.keys(controls).length) return;
  const status = $('tune-status');
  try {
    if (status) status.textContent = 'applying camera…';
    const result = await api('/api/camera/controls', { controls });
    if (!result.ok && result.error) throw new Error(result.error);
    if (result.errors && Object.keys(result.errors).length) {
      toast(Object.values(result.errors)[0], 'error');
    }
    if (status) {
      const keys = Object.keys(result.applied || controls);
      status.textContent = `camera ${keys[0] || 'ok'}`;
    }
  } catch (err) {
    if (status) status.textContent = 'camera update failed';
    toast(err.message, 'error');
  }
}

async function buildCameraControls() {
  const root = $('camera-controls');
  if (!root) return;
  root.innerHTML = '';
  let data;
  try {
    data = await api('/api/camera/controls');
  } catch {
    return;
  }
  if (!data.supported || !data.controls || !data.controls.length) {
    return;
  }

  const section = document.createElement('div');
  section.className = 'control-group';
  section.innerHTML = `
    <h3>Camera hardware (${data.device || 'USB'})</h3>
    <p class="hint">These change the sensor (exposure, gain, WB). Use these first;
    the Colour sliders below only recolour frames after capture.</p>`;

  for (const ctrl of data.controls) {
    const row = document.createElement('div');
    row.className = 'control';
    const label = ctrl.name.replace(/_/g, ' ');

    if (ctrl.type === 'menu' && ctrl.menu) {
      row.innerHTML = `<label>${label}</label>`;
      const select = document.createElement('select');
      for (const [value, text] of Object.entries(ctrl.menu)) {
        const opt = document.createElement('option');
        opt.value = value;
        opt.textContent = `${text} (${value})`;
        if (Number(value) === Number(ctrl.value)) opt.selected = true;
        select.append(opt);
      }
      select.addEventListener('change', () => {
        queueCameraControl(ctrl.name, parseInt(select.value, 10));
      });
      row.append(select);
    } else if (ctrl.type === 'bool' || (ctrl.min === 0 && ctrl.max === 1 && ctrl.step === 1)) {
      const wrap = document.createElement('label');
      wrap.className = 'switch';
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.checked = Boolean(ctrl.value);
      input.addEventListener('change', () => {
        queueCameraControl(ctrl.name, input.checked ? 1 : 0);
      });
      wrap.append(input, document.createTextNode(label));
      row.append(wrap);
    } else {
      row.innerHTML = `<label>${label}</label><output>${ctrl.value}</output>`;
      const input = document.createElement('input');
      input.type = 'range';
      input.min = ctrl.min;
      input.max = ctrl.max;
      input.step = ctrl.step || 1;
      input.value = ctrl.value;
      const out = row.querySelector('output');
      input.addEventListener('input', () => {
        const value = parseInt(input.value, 10);
        out.textContent = String(value);
        queueCameraControl(ctrl.name, value);
      });
      row.append(input);
    }
    section.append(row);
  }
  root.append(section);
}

function buildControls() {
  const root = $('controls');
  root.innerHTML = '';

  for (const group of CONTROLS) {
    const section = document.createElement('div');
    section.className = 'control-group';
    section.innerHTML = `<h3>${group.group}</h3>`;

    for (const item of group.items) {
      const row = document.createElement('div');
      row.className = 'control';
      const label = item.label || item.path.split('.').pop().replace(/_/g, ' ');

      if (item.type === 'toggle') {
        const wrap = document.createElement('label');
        wrap.className = 'switch';
        const input = document.createElement('input');
        input.type = 'checkbox';
        input.addEventListener('change', () => queueUpdate(item.path, input.checked));
        wrap.append(input, document.createTextNode(label));
        row.append(wrap);
        boundInputs.set(item.path, { input, kind: 'toggle' });
      } else if (item.type === 'select') {
        row.innerHTML = `<label>${label}</label><output></output>`;
        const select = document.createElement('select');
        select.innerHTML = item.options.map((o) => `<option value="${o}">${o}</option>`).join('');
        select.addEventListener('change', () => queueUpdate(item.path, select.value));
        row.append(select);
        boundInputs.set(item.path, { input: select, kind: 'select' });
      } else {
        row.innerHTML = `<label>${label}</label><output></output>`;
        const input = document.createElement('input');
        input.type = 'range';
        input.min = item.min; input.max = item.max; input.step = item.step;
        const out = row.querySelector('output');
        input.addEventListener('input', () => {
          const value = item.step >= 1 ? parseInt(input.value, 10) : parseFloat(input.value);
          out.textContent = format(value, item);
          queueUpdate(item.path, value);
        });
        row.append(input);
        boundInputs.set(item.path, { input, out, kind: 'range', item });
      }
      section.append(row);
    }
    root.append(section);
  }
}

function format(value, item) {
  const decimals = item.step >= 1 ? 0 : (item.step >= 0.1 ? 1 : 2);
  return `${Number(value).toFixed(decimals)}${item.unit || ''}`;
}

function applyConfig(config, { skipFocused = false } = {}) {
  if (!config) return;
  for (const [path, bound] of boundInputs) {
    const value = get(config, path);
    if (value === undefined) continue;
    if (skipFocused && document.activeElement === bound.input) continue;
    if (bound.kind === 'toggle') {
      bound.input.checked = Boolean(value);
    } else if (bound.kind === 'select') {
      bound.input.value = String(value);
    } else {
      bound.input.value = value;
      bound.out.textContent = format(value, bound.item);
    }
  }
  const mode = $('boundary-mode');
  if (document.activeElement !== mode) mode.value = get(config, 'boundary.mode') || 'hybrid';
}

// ------------------------------------------------------------ corner picker

const picker = {
  canvas: null,
  ctx: null,
  img: null,
  corners: [],      // normalised [x, y]
  dragging: -1,
  hover: -1,
  pointer: null,
  dirty: false,
};

const CORNER_LABELS = ['TL', 'TR', 'BR', 'BL'];

function orderCorners(points) {
  // Sort clockwise around the centroid, then rotate so the top-left is first.
  // Mirrors what the server does, so the labels the user sees are the labels
  // the pipeline uses.
  const cx = points.reduce((s, p) => s + p[0], 0) / points.length;
  const cy = points.reduce((s, p) => s + p[1], 0) / points.length;
  const sorted = [...points].sort(
    (a, b) => Math.atan2(a[1] - cy, a[0] - cx) - Math.atan2(b[1] - cy, b[0] - cx)
  );
  let start = 0;
  let best = Infinity;
  sorted.forEach((p, i) => {
    const score = p[0] + p[1];
    if (score < best) { best = score; start = i; }
  });
  return sorted.slice(start).concat(sorted.slice(0, start));
}

function setupPicker() {
  picker.canvas = $('picker-canvas');
  picker.ctx = picker.canvas.getContext('2d');
  picker.img = $('picker-img');

  const resize = () => {
    const rect = picker.img.getBoundingClientRect();
    if (!rect.width) return;
    const ratio = window.devicePixelRatio || 1;
    picker.canvas.width = Math.round(rect.width * ratio);
    picker.canvas.height = Math.round(rect.height * ratio);
    picker.ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    drawPicker();
  };
  new ResizeObserver(resize).observe(picker.img);
  picker.img.addEventListener('load', resize);
  window.addEventListener('resize', resize);
  resize();

  const toNormalised = (event) => {
    const rect = picker.canvas.getBoundingClientRect();
    return [clamp01((event.clientX - rect.left) / rect.width),
            clamp01((event.clientY - rect.top) / rect.height)];
  };

  const nearest = (point) => {
    let index = -1;
    let best = 0.035; // grab radius in normalised units
    picker.corners.forEach((corner, i) => {
      const d = Math.hypot(corner[0] - point[0], corner[1] - point[1]);
      if (d < best) { best = d; index = i; }
    });
    return index;
  };

  picker.canvas.addEventListener('pointerdown', (event) => {
    const point = toNormalised(event);
    const index = nearest(point);
    if (index >= 0) {
      picker.dragging = index;
    } else if (picker.corners.length < 4) {
      picker.corners.push(point);
      if (picker.corners.length === 4) picker.corners = orderCorners(picker.corners);
      picker.dirty = true;
      $('btn-apply').disabled = picker.corners.length !== 4;
    } else {
      return;
    }
    picker.pointer = point;
    picker.canvas.setPointerCapture(event.pointerId);
    drawPicker();
  });

  picker.canvas.addEventListener('pointermove', (event) => {
    const point = toNormalised(event);
    picker.pointer = point;
    if (picker.dragging >= 0) {
      picker.corners[picker.dragging] = point;
      picker.dirty = true;
    } else {
      picker.hover = nearest(point);
    }
    drawPicker();
  });

  const release = (event) => {
    if (picker.dragging >= 0 && picker.corners.length === 4) {
      picker.corners = orderCorners(picker.corners);
    }
    picker.dragging = -1;
    if (event && event.pointerId !== undefined && picker.canvas.hasPointerCapture(event.pointerId)) {
      picker.canvas.releasePointerCapture(event.pointerId);
    }
    drawPicker();
  };
  picker.canvas.addEventListener('pointerup', release);
  picker.canvas.addEventListener('pointercancel', release);
  picker.canvas.addEventListener('pointerleave', () => {
    picker.pointer = null; picker.hover = -1; drawPicker();
  });

  setInterval(() => { if (picker.dragging >= 0) drawPicker(); }, 100);
}

function drawPicker() {
  const { ctx, canvas } = picker;
  if (!ctx) return;
  const ratio = window.devicePixelRatio || 1;
  const w = canvas.width / ratio;
  const h = canvas.height / ratio;
  ctx.clearRect(0, 0, w, h);

  const pts = picker.corners.map(([x, y]) => [x * w, y * h]);

  if (pts.length >= 2) {
    ctx.beginPath();
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i += 1) ctx.lineTo(pts[i][0], pts[i][1]);
    if (pts.length === 4) ctx.closePath();
    ctx.strokeStyle = picker.dirty ? '#ffb020' : '#18a0fb';
    ctx.lineWidth = 2;
    ctx.stroke();
    if (pts.length === 4) {
      ctx.fillStyle = picker.dirty ? 'rgba(255,176,32,.10)' : 'rgba(24,160,251,.10)';
      ctx.fill();
    }
  }

  pts.forEach(([x, y], i) => {
    const active = i === picker.dragging || i === picker.hover;
    ctx.beginPath();
    ctx.arc(x, y, active ? 9 : 6, 0, Math.PI * 2);
    ctx.fillStyle = active ? '#ffb020' : '#18a0fb';
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = '#0b0e14';
    ctx.stroke();

    ctx.font = '600 11px ui-sans-serif, system-ui, sans-serif';
    ctx.fillStyle = '#dde5f2';
    ctx.fillText(CORNER_LABELS[i] || String(i + 1), x + 11, y - 9);
  });

  if (picker.dragging >= 0 && picker.pointer) drawLoupe(ctx, w, h, picker.pointer);
}

function drawLoupe(ctx, w, h, [nx, ny]) {
  const img = picker.img;
  if (!img.naturalWidth) return;

  const size = 118;
  const zoom = 5;
  // Keep the loupe out from under the finger/cursor.
  const x = nx < 0.5 ? w - size - 12 : 12;
  const y = 12;

  const sw = size / zoom;
  const sh = size / zoom;
  const sx = nx * img.naturalWidth - sw / 2;
  const sy = ny * img.naturalHeight - sh / 2;

  ctx.save();
  ctx.beginPath();
  ctx.rect(x, y, size, size);
  ctx.clip();
  ctx.fillStyle = '#05070b';
  ctx.fillRect(x, y, size, size);
  try {
    ctx.drawImage(img, sx, sy, sw, sh, x, y, size, size);
  } catch { /* stream frame not decodable yet */ }
  ctx.restore();

  ctx.strokeStyle = '#232c3d';
  ctx.lineWidth = 1;
  ctx.strokeRect(x + 0.5, y + 0.5, size, size);
  ctx.strokeStyle = '#ffb020';
  ctx.beginPath();
  ctx.moveTo(x + size / 2, y); ctx.lineTo(x + size / 2, y + size);
  ctx.moveTo(x, y + size / 2); ctx.lineTo(x + size, y + size / 2);
  ctx.stroke();
}

function setCorners(corners, { dirty = false } = {}) {
  picker.corners = corners ? corners.map(([x, y]) => [clamp01(x), clamp01(y)]) : [];
  if (picker.corners.length === 4) picker.corners = orderCorners(picker.corners);
  picker.dirty = dirty;
  $('btn-apply').disabled = picker.corners.length !== 4;
  drawPicker();
}

// ---------------------------------------------------------------- status

let knownViews = [];

function renderViewSelect(views) {
  if (JSON.stringify(views) === JSON.stringify(knownViews)) return;
  knownViews = views;
  const select = $('view-select');
  const current = select.value || 'boundary';
  select.innerHTML = views.map((v) => `<option value="${v}">${v}</option>`).join('');
  select.value = views.includes(current) ? current : views[0];
  $('view-img').src = `/stream/${select.value}`;
}

function renderStages(status) {
  const list = $('stage-list');
  const descriptions = Object.fromEntries(
    (status.stages_available || []).map((s) => [s.name, s.description])
  );
  const stages = status.pipeline.stages || [];

  if (list.childElementCount !== stages.length) {
    list.innerHTML = '';
    for (const stage of stages) {
      const li = document.createElement('li');
      li.innerHTML = `<span><span class="stage-name">${stage.name}</span>
        <span class="stage-desc">${descriptions[stage.name] || ''}</span></span>`;
      const wrap = document.createElement('label');
      wrap.className = 'switch';
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.dataset.stage = stage.name;
      input.addEventListener('change', async () => {
        try {
          await api('/api/stage', { name: stage.name, enabled: input.checked });
        } catch (err) { toast(err.message, 'error'); }
      });
      wrap.append(input);
      li.append(wrap);
      list.append(li);
    }
  }
  for (const stage of stages) {
    const input = list.querySelector(`input[data-stage="${stage.name}"]`);
    if (input && document.activeElement !== input) input.checked = stage.enabled;
  }
}

function renderTimings(timings) {
  const entries = Object.entries(timings || {});
  const max = Math.max(0.01, ...entries.map(([, v]) => v));
  $('timing-list').innerHTML = entries
    .map(([name, ms]) => `<li><span>${name}</span>
      <span class="bar" style="width:${Math.max(2, (ms / max) * 100)}%"></span>
      <span class="ms">${ms.toFixed(2)}</span></li>`)
    .join('');
}

function renderDetected(status) {
  const stages = Object.fromEntries((status.pipeline.stages || []).map((s) => [s.name, s]));
  const bars = stages.blackbars || {};
  const boundary = stages.boundary || {};
  const colour = stages.color || {};
  const rows = [
    ['TV source', boundary.origin || '–'],
    ['Confidence', boundary.confidence != null ? boundary.confidence.toFixed(2) : '–'],
    ['Content', bars.content_aspect ? `${bars.content_aspect.toFixed(2)}:1` : '–'],
    ['Bars (px)', bars.pixels
      ? `t${bars.pixels.top} b${bars.pixels.bottom} l${bars.pixels.left} r${bars.pixels.right}`
      : '–'],
    ['Exposure', colour.exposure_gain != null ? `${colour.exposure_gain.toFixed(2)}x` : '–'],
    ['Mean luma', colour.mean_luma != null ? colour.mean_luma.toFixed(0) : '–'],
    ['Frames', `${status.frames_out} out / ${status.frames_dropped} dropped`],
  ];
  $('detected').innerHTML = rows
    .map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('');
}

async function refresh() {
  let status;
  try {
    status = await api('/api/status');
  } catch {
    $('health-dot').className = 'dot bad';
    return;
  }

  const connected = Boolean(status.source && status.source.connected);
  const locked = (status.pipeline.state || {}).confidence > 0;
  $('health-dot').className = `dot ${connected ? (locked ? 'ok' : 'warn') : 'bad'}`;

  $('stat-in').textContent = `${status.input_fps.toFixed(1)} fps`;
  $('stat-out').textContent = `${status.output_fps.toFixed(1)} fps`;
  $('stat-latency').textContent = `${status.latency_ms.toFixed(0)} ms`;
  $('stat-cpu').textContent = `${(status.pipeline.total_ms || 0).toFixed(1)} ms`;

  const state = status.pipeline.state || {};
  $('stat-lock').textContent = state.corners_source === 'none'
    ? 'searching'
    : `${state.corners_source} ${(state.confidence || 0).toFixed(2)}`;

  $('output-size').textContent = `${status.output_size[0]}x${status.output_size[1]}`;

  renderViewSelect(status.views || []);
  renderStages(status);
  renderTimings(status.pipeline.timings_ms);
  renderDetected(status);
  // Do not re-push config into the sliders every second: it races with
  // in-flight tuner updates and makes picture controls feel broken.
  if (!suppressEcho && !Object.keys(pendingUpdates).length && pushTimer == null) {
    applyConfig(status.config, { skipFocused: true });
  }

  // Adopt the pipeline's corners only while the user is not editing them.
  if (!picker.dirty && picker.dragging < 0 && state.corners && state.frame_size) {
    const [w, h] = state.frame_size;
    if (w && h) setCorners(state.corners.map(([x, y]) => [x / w, y / h]));
  }
}

// ------------------------------------------------------------------- wiring

function wireButtons() {
  $('btn-auto').addEventListener('click', async () => {
    try {
      const result = await api('/api/calibrate/auto', {});
      if (!result.ok) return toast(result.error || 'detection failed', 'error');
      setCorners(result.corners, { dirty: true });
      toast(`Detected with confidence ${result.confidence.toFixed(2)} — press Apply to keep it`, 'ok');
    } catch (err) { toast(err.message, 'error'); }
  });

  $('btn-clear').addEventListener('click', () => setCorners([], { dirty: false }));

  $('btn-full').addEventListener('click', () => {
    setCorners([[0, 0], [1, 0], [1, 1], [0, 1]], { dirty: true });
  });

  $('btn-apply').addEventListener('click', async () => {
    if (picker.corners.length !== 4) return;
    try {
      await api('/api/calibrate/corners', { corners: picker.corners });
      picker.dirty = false;
      drawPicker();
      toast('Calibration applied — remember to save', 'ok');
    } catch (err) { toast(err.message, 'error'); }
  });

  $('btn-recal').addEventListener('click', async () => {
    try {
      await api('/api/recalibrate', {});
      setCorners([], { dirty: false });
      toast('Looking for the TV again…');
    } catch (err) { toast(err.message, 'error'); }
  });

  $('btn-save').addEventListener('click', async () => {
    try {
      const result = await api('/api/config/save', { path: $('save-path').value || null });
      toast(`Saved to ${result.path}`, 'ok');
    } catch (err) { toast(err.message, 'error'); }
  });

  $('boundary-mode').addEventListener('change', (event) => {
    queueUpdate('boundary.mode', event.target.value);
  });

  $('view-select').addEventListener('change', (event) => {
    $('view-img').src = `/stream/${event.target.value}`;
  });
}

async function init() {
  buildControls();
  await buildCameraControls();
  setupPicker();
  wireButtons();
  try {
    applyConfig(await api('/api/config'));
  } catch (err) {
    toast(err.message, 'error');
  }
  await refresh();
  setInterval(refresh, 1000);
}

init();
