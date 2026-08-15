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

// ------------------------------------------------------------- source panel

const SOURCE_TYPES = [
  { value: 'lumos', label: 'Lumos Cam (phone)' },
  { value: 'scrcpy', label: 'scrcpy (phone fallback)' },
  { value: 'v4l2', label: 'USB camera' },
  { value: 'rtsp', label: 'RTSP' },
  { value: 'file', label: 'File / image' },
  { value: 'synthetic', label: 'Synthetic' },
];

let sourcePanelState = {
  source: 'v4l2',
  device: '',
  rtsp_url: '',
  rtsp_saved: false,
  path: '',
  transport: 'tcp',
  process_width: 0,
  replay_fps: 0,
  loop: true,
  devices: [],
};

function normalizeSourceType(value) {
  const source = String(value || '').toLowerCase();
  if (source === 'usb') return 'v4l2';
  if (source === 'image') return 'file';
  if (SOURCE_TYPES.some((item) => item.value === source)) return source;
  return 'v4l2';
}

function syncSourceFieldsVisibility() {
  const type = $('source-type')?.value || 'v4l2';
  for (const el of document.querySelectorAll('[data-source-for]')) {
    const allowed = el.getAttribute('data-source-for').split(/\s+/);
    el.hidden = !allowed.includes(type);
  }
}

async function loadCaptureDevices() {
  try {
    const data = await api('/api/camera/devices');
    sourcePanelState.devices = data.devices || [];
    if (data.selected) sourcePanelState.device = data.selected;
    return data;
  } catch {
    sourcePanelState.devices = [];
    return null;
  }
}

function fillDeviceSelect(selected) {
  const select = $('source-device');
  if (!select) return;
  const devices = sourcePanelState.devices;
  select.innerHTML = '';
  if (!devices.length) {
    const opt = document.createElement('option');
    opt.value = selected || '';
    opt.textContent = selected
      ? `${selected} (not enumerated)`
      : 'No USB capture devices found';
    select.append(opt);
    return;
  }
  for (const device of devices) {
    const opt = document.createElement('option');
    opt.value = device.id_path;
    const bus = device.bus_info ? ` · ${device.bus_info}` : '';
    opt.textContent = `${device.name}${bus}`;
    if (
      device.selected
      || device.id_path === selected
      || device.video_path === selected
    ) {
      opt.selected = true;
    }
    select.append(opt);
  }
  if (selected && !devices.some((d) => d.id_path === selected || d.video_path === selected)) {
    const opt = document.createElement('option');
    opt.value = selected;
    opt.textContent = `${selected} (configured)`;
    opt.selected = true;
    select.prepend(opt);
  }
}

function applySourcePanelFromConfig(config) {
  if (!config || !config.camera) return;
  const camera = config.camera;
  sourcePanelState.source = normalizeSourceType(camera.source);
  sourcePanelState.device = camera.device || '';
  sourcePanelState.path = camera.path || '';
  sourcePanelState.transport = camera.transport || 'tcp';
  sourcePanelState.process_width = Number(camera.process_width || 0);
  sourcePanelState.replay_fps = Number(camera.replay_fps || 0);
  sourcePanelState.loop = camera.loop !== false;
  const url = camera.rtsp_url || '';
  sourcePanelState.rtsp_saved = Boolean(url);
  // Redacted URLs must not be re-posted; leave the field empty for edits.
  sourcePanelState.rtsp_url = '';

  const type = $('source-type');
  if (type) type.value = sourcePanelState.source;
  const device = $('source-device');
  if (device && sourcePanelState.device) fillDeviceSelect(sourcePanelState.device);
  const path = $('source-path');
  if (path) path.value = sourcePanelState.path;
  const transport = $('source-transport');
  if (transport) transport.value = sourcePanelState.transport;
  const processWidth = $('source-process-width');
  if (processWidth) processWidth.value = String(sourcePanelState.process_width || 0);
  const replayFps = $('source-replay-fps');
  if (replayFps) replayFps.value = String(sourcePanelState.replay_fps || 0);
  const loop = $('source-loop');
  if (loop) loop.checked = sourcePanelState.loop !== false;
  const rtsp = $('source-rtsp');
  if (rtsp) {
    rtsp.value = '';
    rtsp.placeholder = sourcePanelState.rtsp_saved
      ? 'URL saved (enter a new one to change)'
      : 'rtsp://user:pass@host:554/path';
  }
  const meta = $('source-meta');
  if (meta) {
    const bits = [`source: ${sourcePanelState.source}`];
    if (camera.device) bits.push(camera.device);
    if (camera.path) bits.push(camera.path);
    if (sourcePanelState.rtsp_saved) bits.push('RTSP credentials hidden');
    meta.textContent = bits.join(' · ');
  }
  syncSourceFieldsVisibility();
}

// ------------------------------------------------------------- Lumos Cam panel
let lumosPanelState = {
  enabled: false,
  running: false,
  serial: '',
  camera_id: '0',
  camera_size: '1920x1080',
  camera_fps: 30,
  codec: 'h264',
  camera_zoom: 1,
  pan_x: 0,
  pan_y: 0,
  af: 'auto',
  ae: 'auto',
  awb: 'auto',
  cal_mode: false,
  last_error: '',
  app_version: '',
  ui_rotation: 0,
  frame_rotation: 0,
  flip_h: false,
  flip_v: false,
};

let lumosFormDirty = false;
let lumosBusy = false;

const LUMOS_FIELD_IDS = [
  'lumos-serial',
  'lumos-camera-id',
  'lumos-camera-size',
  'lumos-camera-fps',
  'lumos-codec',
  'lumos-zoom',
];

function markLumosDirty() {
  lumosFormDirty = true;
}

function applyLumosPanelFromConfig(config) {
  const lc = config?.lumos_cam || {};
  lumosPanelState = {
    ...lumosPanelState,
    enabled: Boolean(lc.enabled),
    serial: lc.serial || '',
    camera_id: lc.camera_id || '0',
    camera_size: lc.camera_size || '1920x1080',
    camera_fps: lc.camera_fps || 30,
    codec: lc.codec || 'h264',
    camera_zoom: Number(lc.camera_zoom || 1),
    pan_x: Number(lc.pan_x || 0),
    pan_y: Number(lc.pan_y || 0),
    af: lc.af || 'auto',
    ae: lc.ae || 'auto',
    awb: lc.awb || 'auto',
  };
  lumosFormDirty = false;
  syncLumosForm({ force: true });
}

function syncLumosMeta() {
  const meta = $('lumos-meta');
  if (!meta) return;
  const bits = [
    lumosPanelState.running ? 'running' : 'stopped',
    `${Number(lumosPanelState.camera_zoom).toFixed(2)}×`,
    `pan ${Number(lumosPanelState.pan_x).toFixed(2)},${Number(lumosPanelState.pan_y).toFixed(2)}`,
    `AF ${lumosPanelState.af}`,
    `AE ${lumosPanelState.ae}`,
    `AWB ${lumosPanelState.awb}`,
  ];
  if (lumosPanelState.cal_mode) bits.push('cal mode');
  bits.push(`UI ${Number(lumosPanelState.ui_rotation || 0)}°`);
  bits.push(`frame ${Number(lumosPanelState.frame_rotation || 0)}°`);
  if (lumosPanelState.flip_h) bits.push('flip H');
  if (lumosPanelState.flip_v) bits.push('flip V');
  if (lumosPanelState.app_version) bits.push(`app ${lumosPanelState.app_version}`);
  if (lumosPanelState.last_error) bits.push(lumosPanelState.last_error);
  if (lumosFormDirty) bits.push('unsaved edits');
  meta.textContent = bits.join(' · ');
}

function syncLumosForm({ force = false } = {}) {
  const active = document.activeElement;
  const set = (id, value) => {
    const el = $(id);
    if (!el) return;
    if (el.type === 'checkbox' || el.type === 'radio') el.checked = Boolean(value);
    else el.value = value;
  };
  if (force || !lumosFormDirty) {
    set('lumos-serial', lumosPanelState.serial);
    set('lumos-camera-id', lumosPanelState.camera_id);
    set('lumos-camera-size', lumosPanelState.camera_size);
    set('lumos-camera-fps', lumosPanelState.camera_fps);
    set('lumos-codec', lumosPanelState.codec);
    set('lumos-zoom', lumosPanelState.camera_zoom);
  }
  const zoomLabel = $('lumos-zoom-label');
  if (zoomLabel && (force || active !== $('lumos-zoom'))) {
    zoomLabel.textContent = Number(
      force || !lumosFormDirty
        ? lumosPanelState.camera_zoom
        : ($('lumos-zoom')?.value || lumosPanelState.camera_zoom)
    ).toFixed(2);
  }
  const setLock = (id, value) => {
    const el = $(id);
    if (el) el.checked = value === 'locked';
  };
  if (force || !lumosFormDirty) {
    setLock('lumos-af', lumosPanelState.af);
    setLock('lumos-ae', lumosPanelState.ae);
    setLock('lumos-awb', lumosPanelState.awb);
    const cal = $('lumos-cal-mode');
    if (cal) cal.checked = Boolean(lumosPanelState.cal_mode);
  }
  syncLumosMeta();
}

function readLumosFields() {
  return {
    serial: ($('lumos-serial')?.value || '').trim(),
    camera_id: ($('lumos-camera-id')?.value || '0').trim(),
    camera_size: ($('lumos-camera-size')?.value || '1920x1080').trim(),
    camera_fps: Number($('lumos-camera-fps')?.value || 30),
    codec: ($('lumos-codec')?.value || 'h264').trim(),
    camera_zoom: Number($('lumos-zoom')?.value || 1),
    pan_x: Number(lumosPanelState.pan_x || 0),
    pan_y: Number(lumosPanelState.pan_y || 0),
    af: $('lumos-af')?.checked ? 'locked' : 'auto',
    ae: $('lumos-ae')?.checked ? 'locked' : 'auto',
    awb: $('lumos-awb')?.checked ? 'locked' : 'auto',
  };
}

async function postLumos(action, fields, { save = false } = {}) {
  if (lumosBusy) throw new Error('Lumos Cam is busy — wait a moment');
  lumosBusy = true;
  const status = $('tune-status');
  if (status) status.textContent = `Lumos Cam ${action}…`;
  try {
    const response = await fetch('/api/lumos-cam', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, fields, save }),
    });
    const result = await response.json();
    if (result.lumos_cam) {
      lumosPanelState.running = Boolean(result.lumos_cam.running);
      lumosPanelState.last_error = result.lumos_cam.last_error || result.error || '';
      lumosPanelState.camera_zoom = Number(result.lumos_cam.zoom || lumosPanelState.camera_zoom);
      lumosPanelState.pan_x = Number(result.lumos_cam.pan_x ?? lumosPanelState.pan_x);
      lumosPanelState.pan_y = Number(result.lumos_cam.pan_y ?? lumosPanelState.pan_y);
      lumosPanelState.af = result.lumos_cam.af || lumosPanelState.af;
      lumosPanelState.ae = result.lumos_cam.ae || lumosPanelState.ae;
      lumosPanelState.awb = result.lumos_cam.awb || lumosPanelState.awb;
      lumosPanelState.cal_mode = Boolean(result.lumos_cam.cal_mode);
      lumosPanelState.app_version = result.lumos_cam.app_version || '';
      if (result.lumos_cam.ui_rotation != null) lumosPanelState.ui_rotation = Number(result.lumos_cam.ui_rotation);
      if (result.lumos_cam.frame_rotation != null) lumosPanelState.frame_rotation = Number(result.lumos_cam.frame_rotation);
      lumosPanelState.flip_h = Boolean(result.lumos_cam.flip_h);
      lumosPanelState.flip_v = Boolean(result.lumos_cam.flip_v);
    }
    if (result.config) {
      applyLumosPanelFromConfig(result.config);
      lumosFormDirty = false;
    } else {
      syncLumosForm({ force: true });
    }
    if (!response.ok || result.ok === false) {
      throw new Error(result.error || result.lumos_cam?.last_error || 'Lumos Cam action failed');
    }
    return result;
  } finally {
    lumosBusy = false;
  }
}

async function buildLumosCamPanel() {
  const root = $('lumos-cam-panel');
  if (!root) return;
  root.innerHTML = '';

  const section = document.createElement('div');
  section.className = 'control-group';
  section.innerHTML = `
    <h3>Lumos Cam</h3>
    <p class="hint">Direct Camera2 control (needs Lumos Cam ≥ 0.1.12). Leave the
    app open on the phone. Wireless adb serial looks like
    <code>192.168.1.243:37847</code> (the port changes). No USB device — frames
    come from an ffmpeg pipe. Live zoom, pan, and AF/AE/AWB locks. Colour-cal
    Start turns on cal mode. <strong>UI ⟳</strong> only moves the on-phone
    controls (camera stays landscape). <strong>Frame ⟳</strong> / Flip rotate
    or mirror the preview and the host feed.</p>
    <div class="control">
      <label for="lumos-serial">ADB serial (optional)</label>
      <input id="lumos-serial" type="text" spellcheck="false" placeholder="452ee42b0506">
    </div>
    <div class="control">
      <label for="lumos-camera-id">Camera id</label>
      <input id="lumos-camera-id" type="text" spellcheck="false" value="0">
    </div>
    <div class="control">
      <label for="lumos-camera-size">Size</label>
      <input id="lumos-camera-size" type="text" spellcheck="false" placeholder="1920x1080">
    </div>
    <div class="control">
      <label for="lumos-camera-fps">FPS</label>
      <input id="lumos-camera-fps" type="number" min="1" max="120" step="1" value="30">
    </div>
    <div class="control">
      <label for="lumos-codec">Codec</label>
      <select id="lumos-codec">
        <option value="h264">h264 (recommended)</option>
        <option value="mjpeg">mjpeg</option>
      </select>
      <p class="hint">Use h264. Many phones never attach an MJPEG encoder
      (<code>encoder_attached=false</code>, no frames).</p>
    </div>
    <div class="control">
      <label for="lumos-zoom">Zoom (<span id="lumos-zoom-label">1.00</span>×)</label>
      <input id="lumos-zoom" type="range" min="1" max="10" step="0.0625" value="1">
    </div>
    <div class="control">
      <label class="check"><input type="checkbox" id="lumos-af"> AF lock</label>
    </div>
    <div class="control">
      <label class="check"><input type="checkbox" id="lumos-ae"> AE lock</label>
    </div>
    <div class="control">
      <label class="check"><input type="checkbox" id="lumos-awb"> AWB lock</label>
    </div>
    <div class="control">
      <label class="check"><input type="checkbox" id="lumos-cal-mode"> Cal mode</label>
    </div>
    <p class="source-meta" id="lumos-meta"></p>
    <div class="source-actions">
      <button type="button" class="btn" id="btn-lumos-zoom-out">Zoom −</button>
      <button type="button" class="btn" id="btn-lumos-zoom-in">Zoom +</button>
      <button type="button" class="btn" id="btn-lumos-pan-left" title="Pan left">←</button>
      <button type="button" class="btn" id="btn-lumos-pan-right" title="Pan right">→</button>
      <button type="button" class="btn" id="btn-lumos-pan-up" title="Pan up">↑</button>
      <button type="button" class="btn" id="btn-lumos-pan-down" title="Pan down">↓</button>
      <button type="button" class="btn" id="btn-lumos-pan-center">Center</button>
      <button type="button" class="btn" id="btn-lumos-ui-rotate">UI ⟳</button>
      <button type="button" class="btn" id="btn-lumos-frame-rotate">Frame ⟳</button>
      <button type="button" class="btn" id="btn-lumos-flip-h">Flip H</button>
      <button type="button" class="btn" id="btn-lumos-flip-v">Flip V</button>
    </div>`;
  root.append(section);

  for (const id of LUMOS_FIELD_IDS) {
    const el = $(id);
    if (!el) continue;
    el.addEventListener('input', markLumosDirty);
    el.addEventListener('change', markLumosDirty);
  }
  $('lumos-zoom')?.addEventListener('input', () => {
    const label = $('lumos-zoom-label');
    if (label) label.textContent = Number($('lumos-zoom').value).toFixed(2);
    syncLumosMeta();
  });

  const liveLock = (id, actionOn, actionOff) => {
    $(id)?.addEventListener('change', async (ev) => {
      try {
        await postLumos(ev.target.checked ? actionOn : actionOff, {});
      } catch (err) {
        toast(err.message, 'error');
      }
    });
  };
  liveLock('lumos-af', 'lock_af', 'unlock_af');
  liveLock('lumos-ae', 'lock_ae', 'unlock_ae');
  liveLock('lumos-awb', 'lock_awb', 'unlock_awb');
  $('lumos-cal-mode')?.addEventListener('change', async (ev) => {
    try {
      await postLumos(ev.target.checked ? 'cal_mode_on' : 'cal_mode_off', {});
      toast(ev.target.checked ? 'Cal mode on' : 'Cal mode off');
    } catch (err) {
      toast(err.message, 'error');
    }
  });

  $('btn-lumos-zoom-in')?.addEventListener('click', async () => {
    try {
      await postLumos('zoom_in', {});
      toast(`Zoom ${Number(lumosPanelState.camera_zoom).toFixed(2)}×`);
    } catch (err) {
      toast(err.message, 'error');
    }
  });
  $('btn-lumos-zoom-out')?.addEventListener('click', async () => {
    try {
      await postLumos('zoom_out', {});
      toast(`Zoom ${Number(lumosPanelState.camera_zoom).toFixed(2)}×`);
    } catch (err) {
      toast(err.message, 'error');
    }
  });
  const pan = (action) => async () => {
    try {
      await postLumos(action, {});
      toast(`Pan ${Number(lumosPanelState.pan_x).toFixed(2)}, ${Number(lumosPanelState.pan_y).toFixed(2)}`);
    } catch (err) {
      toast(err.message, 'error');
    }
  };
  $('btn-lumos-pan-left')?.addEventListener('click', pan('pan_left'));
  $('btn-lumos-pan-right')?.addEventListener('click', pan('pan_right'));
  $('btn-lumos-pan-up')?.addEventListener('click', pan('pan_up'));
  $('btn-lumos-pan-down')?.addEventListener('click', pan('pan_down'));
  $('btn-lumos-pan-center')?.addEventListener('click', pan('pan_center'));
  const liveDisplay = (id, action, label) => {
    $(id)?.addEventListener('click', async () => {
      try {
        await postLumos(action, {});
        toast(label);
      } catch (err) {
        toast(err.message, 'error');
      }
    });
  };
  liveDisplay('btn-lumos-ui-rotate', 'ui_rotate', 'UI rotated');
  liveDisplay('btn-lumos-frame-rotate', 'frame_rotate', 'Frame rotated');
  liveDisplay('btn-lumos-flip-h', 'flip_h', 'Flip H');
  liveDisplay('btn-lumos-flip-v', 'flip_v', 'Flip V');
}

// ------------------------------------------------------------- scrcpy panel
let scrcpyPanelState = {
  enabled: false,
  binary: 'scrcpy',
  serial: '',
  camera_id: '0',
  camera_size: '1920x1080',
  camera_fps: 30,
  camera_zoom: 1,
  view_zoom: 1,
  pan_x: 0,
  pan_y: 0,
  zoom_min: 1,
  zoom_max: 10,
  v4l2_sink: '/dev/video11',
  running: false,
  last_error: '',
  crop: '',
};
/** True while the user is editing the scrcpy form; blocks status-poll overwrites. */
let scrcpyFormDirty = false;
let scrcpyBusy = false;

const SCRCPY_FIELD_IDS = [
  'scrcpy-binary',
  'scrcpy-serial',
  'scrcpy-camera-id',
  'scrcpy-camera-size',
  'scrcpy-camera-fps',
  'scrcpy-zoom',
  'scrcpy-view-zoom',
  'scrcpy-sink',
];

function markScrcpyDirty() {
  scrcpyFormDirty = true;
}

function applyScrcpyPanelFromConfig(config) {
  const sc = config?.scrcpy || {};
  scrcpyPanelState = {
    ...scrcpyPanelState,
    enabled: Boolean(sc.enabled),
    binary: sc.binary || 'scrcpy',
    serial: sc.serial || '',
    camera_id: String(sc.camera_id ?? '0'),
    camera_size: sc.camera_size || '1920x1080',
    camera_fps: Number(sc.camera_fps || 30),
    camera_zoom: Number(sc.camera_zoom || 1),
    view_zoom: Number(sc.view_zoom || 1),
    pan_x: Number(sc.pan_x || 0),
    pan_y: Number(sc.pan_y || 0),
    zoom_min: Number(sc.zoom_min || 1),
    zoom_max: Number(sc.zoom_max || 10),
    v4l2_sink: sc.v4l2_sink || '/dev/video11',
  };
  scrcpyFormDirty = false;
  syncScrcpyForm({ force: true });
}

function syncScrcpyMeta() {
  const meta = $('scrcpy-meta');
  if (!meta) return;
  const bits = [
    scrcpyPanelState.running ? 'running' : 'stopped',
    `phone ${Number(scrcpyPanelState.camera_zoom).toFixed(2)}×`,
    `frame ${Number(scrcpyPanelState.view_zoom).toFixed(2)}×`,
    `pan ${Number(scrcpyPanelState.pan_x).toFixed(2)},${Number(scrcpyPanelState.pan_y).toFixed(2)}`,
    scrcpyPanelState.v4l2_sink,
  ];
  if (scrcpyPanelState.crop) bits.push(`crop ${scrcpyPanelState.crop}`);
  if (scrcpyPanelState.last_error) bits.push(scrcpyPanelState.last_error);
  if (scrcpyFormDirty) bits.push('unsaved edits');
  meta.textContent = bits.join(' · ');
}

function syncScrcpyForm({ force = false } = {}) {
  const active = document.activeElement;
  const set = (id, value) => {
    const el = $(id);
    if (!el) return;
    // Never clobber a field the user is mid-edit.
    if (!force && active === el) return;
    if (el.type === 'checkbox') el.checked = Boolean(value);
    else el.value = value;
  };
  if (force || !scrcpyFormDirty) {
    set('scrcpy-binary', scrcpyPanelState.binary);
    set('scrcpy-serial', scrcpyPanelState.serial);
    set('scrcpy-camera-id', scrcpyPanelState.camera_id);
    set('scrcpy-camera-size', scrcpyPanelState.camera_size);
    set('scrcpy-camera-fps', scrcpyPanelState.camera_fps);
    set('scrcpy-zoom', scrcpyPanelState.camera_zoom);
    set('scrcpy-view-zoom', scrcpyPanelState.view_zoom);
    set('scrcpy-sink', scrcpyPanelState.v4l2_sink);
  }
  const zoomLabel = $('scrcpy-zoom-label');
  if (zoomLabel && (force || active !== $('scrcpy-zoom'))) {
    zoomLabel.textContent = Number(
      force || !scrcpyFormDirty
        ? scrcpyPanelState.camera_zoom
        : ($('scrcpy-zoom')?.value || scrcpyPanelState.camera_zoom)
    ).toFixed(2);
  }
  const viewLabel = $('scrcpy-view-zoom-label');
  if (viewLabel && (force || active !== $('scrcpy-view-zoom'))) {
    viewLabel.textContent = Number(
      force || !scrcpyFormDirty
        ? scrcpyPanelState.view_zoom
        : ($('scrcpy-view-zoom')?.value || scrcpyPanelState.view_zoom)
    ).toFixed(2);
  }
  syncScrcpyMeta();
}

function readScrcpyFields() {
  return {
    binary: ($('scrcpy-binary')?.value || 'scrcpy').trim(),
    serial: ($('scrcpy-serial')?.value || '').trim(),
    camera_id: ($('scrcpy-camera-id')?.value || '0').trim(),
    camera_size: ($('scrcpy-camera-size')?.value || '1920x1080').trim(),
    camera_fps: Number($('scrcpy-camera-fps')?.value || 30),
    camera_zoom: Number($('scrcpy-zoom')?.value || 1),
    view_zoom: Number($('scrcpy-view-zoom')?.value || 1),
    pan_x: Number(scrcpyPanelState.pan_x || 0),
    pan_y: Number(scrcpyPanelState.pan_y || 0),
    v4l2_sink: ($('scrcpy-sink')?.value || '/dev/video11').trim(),
    no_audio: true,
    no_playback: true,
  };
}

async function postScrcpy(action, fields, { save = false } = {}) {
  if (scrcpyBusy) throw new Error('scrcpy is busy — wait a moment');
  scrcpyBusy = true;
  const status = $('tune-status');
  if (status) status.textContent = `scrcpy ${action}…`;
  try {
    const body = { action, save, ...(fields || {}) };
    // Use fetch directly so a failed scrcpy spawn (4xx) still returns the
    // updated config — enabled stays on even if the binary/device is wrong.
    const response = await fetch('/api/scrcpy', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const text = await response.text();
    let result = {};
    try { result = text ? JSON.parse(text) : {}; } catch { /* ignore */ }

    if (result.scrcpy) {
      scrcpyPanelState.running = Boolean(result.scrcpy.running);
      scrcpyPanelState.last_error = result.scrcpy.last_error || result.error || '';
      scrcpyPanelState.camera_zoom = Number(result.scrcpy.zoom || scrcpyPanelState.camera_zoom);
      scrcpyPanelState.view_zoom = Number(result.scrcpy.view_zoom ?? scrcpyPanelState.view_zoom);
      scrcpyPanelState.pan_x = Number(result.scrcpy.pan_x ?? scrcpyPanelState.pan_x);
      scrcpyPanelState.pan_y = Number(result.scrcpy.pan_y ?? scrcpyPanelState.pan_y);
      scrcpyPanelState.crop = result.scrcpy.crop || '';
    }
    if (result.config) {
      applyConfig(result.config);
      applyScrcpyPanelFromConfig(result.config);
      applySourcePanelFromConfig(result.config);
      fillDeviceSelect(result.config.camera?.device || '');
    } else {
      scrcpyFormDirty = false;
      syncScrcpyForm({ force: true });
    }
    if (status) status.textContent = 'changes apply live';
    if (!response.ok || result.ok === false) {
      throw new Error(result.error || result.scrcpy?.last_error || 'scrcpy action failed');
    }
    return result;
  } finally {
    scrcpyBusy = false;
  }
}

async function buildScrcpyPanel() {
  const root = $('scrcpy-panel');
  if (!root) return;
  root.innerHTML = '';

  const section = document.createElement('div');
  section.className = 'control-group';
  section.innerHTML = `
    <h3>Android cam (scrcpy fallback)</h3>
    <p class="hint">Legacy phone path. Prefer Lumos Cam when the APK is
    installed — it locks AF/AE/AWB and zooms live. Scrcpy still restarts
    the child for zoom/crop.</p>
    <div class="control">
      <label for="scrcpy-binary">Binary</label>
      <input id="scrcpy-binary" type="text" spellcheck="false" placeholder="/opt/scrcpy/scrcpy">
    </div>
    <div class="control">
      <label for="scrcpy-serial">ADB serial (optional)</label>
      <input id="scrcpy-serial" type="text" spellcheck="false" placeholder="452ee42b0506">
    </div>
    <div class="control">
      <label for="scrcpy-camera-id">Camera id</label>
      <input id="scrcpy-camera-id" type="text" spellcheck="false" value="0">
    </div>
    <div class="control">
      <label for="scrcpy-camera-size">Size</label>
      <input id="scrcpy-camera-size" type="text" spellcheck="false" placeholder="1920x1080">
    </div>
    <div class="control">
      <label for="scrcpy-camera-fps">FPS</label>
      <input id="scrcpy-camera-fps" type="number" min="1" max="120" step="1" value="30">
    </div>
    <div class="control">
      <label for="scrcpy-sink">Loopback sink</label>
      <input id="scrcpy-sink" type="text" spellcheck="false" value="/dev/video11" readonly>
    </div>
    <div class="control">
      <label for="scrcpy-zoom">Phone zoom (<span id="scrcpy-zoom-label">1.00</span>×)</label>
      <input id="scrcpy-zoom" type="range" min="1" max="10" step="0.0625" value="1">
    </div>
    <div class="control">
      <label for="scrcpy-view-zoom">Frame zoom / pan room (<span id="scrcpy-view-zoom-label">1.00</span>×)</label>
      <input id="scrcpy-view-zoom" type="range" min="1" max="4" step="0.05" value="1">
    </div>
    <p class="source-meta" id="scrcpy-meta"></p>
    <div class="source-actions">
      <button type="button" class="btn" id="btn-scrcpy-zoom-out">Zoom −</button>
      <button type="button" class="btn" id="btn-scrcpy-zoom-in">Zoom +</button>
      <button type="button" class="btn" id="btn-scrcpy-pan-left" title="Pan left">←</button>
      <button type="button" class="btn" id="btn-scrcpy-pan-right" title="Pan right">→</button>
      <button type="button" class="btn" id="btn-scrcpy-pan-up" title="Pan up">↑</button>
      <button type="button" class="btn" id="btn-scrcpy-pan-down" title="Pan down">↓</button>
      <button type="button" class="btn" id="btn-scrcpy-pan-center">Center</button>
    </div>`;
  root.append(section);

  for (const id of SCRCPY_FIELD_IDS) {
    const el = $(id);
    if (!el) continue;
    el.addEventListener('input', markScrcpyDirty);
    el.addEventListener('change', markScrcpyDirty);
  }

  const bindZoomLabel = (inputId, labelId) => {
    $(inputId)?.addEventListener('input', () => {
      const label = $(labelId);
      if (label) label.textContent = Number($(inputId).value).toFixed(2);
      syncScrcpyMeta();
    });
  };
  bindZoomLabel('scrcpy-zoom', 'scrcpy-zoom-label');
  bindZoomLabel('scrcpy-view-zoom', 'scrcpy-view-zoom-label');

  const panToast = () => {
    toast(
      `Pan ${Number(scrcpyPanelState.pan_x).toFixed(2)}, `
      + `${Number(scrcpyPanelState.pan_y).toFixed(2)} · frame `
      + `${Number(scrcpyPanelState.view_zoom).toFixed(2)}×`
    );
  };

  $('btn-scrcpy-zoom-in')?.addEventListener('click', async () => {
    try {
      await postScrcpy('zoom_in', {});
      toast(`Phone zoom ${Number(scrcpyPanelState.camera_zoom).toFixed(2)}×`);
      await loadCaptureDevices();
      fillDeviceSelect(sourcePanelState.device || scrcpyPanelState.v4l2_sink);
    } catch (err) {
      toast(err.message, 'error');
    }
  });
  $('btn-scrcpy-zoom-out')?.addEventListener('click', async () => {
    try {
      await postScrcpy('zoom_out', {});
      toast(`Phone zoom ${Number(scrcpyPanelState.camera_zoom).toFixed(2)}×`);
    } catch (err) {
      toast(err.message, 'error');
    }
  });
  for (const [id, action] of [
    ['btn-scrcpy-pan-left', 'pan_left'],
    ['btn-scrcpy-pan-right', 'pan_right'],
    ['btn-scrcpy-pan-up', 'pan_up'],
    ['btn-scrcpy-pan-down', 'pan_down'],
    ['btn-scrcpy-pan-center', 'pan_center'],
  ]) {
    $(id)?.addEventListener('click', async () => {
      try {
        await postScrcpy(action, {});
        panToast();
      } catch (err) {
        toast(err.message, 'error');
      }
    });
  }
  syncScrcpyForm({ force: true });
}

// ---------------------------------------------------- colour calibration
const PATCH_TARGET_RGB = {
  black: [0, 0, 0],
  white: [255, 255, 255],
  grey_dark: [64, 64, 64],
  grey: [128, 128, 128],
  grey_light: [192, 192, 192],
  red: [255, 0, 0],
  green: [0, 255, 0],
  blue: [0, 0, 255],
  cyan: [0, 255, 255],
  magenta: [255, 0, 255],
  yellow: [255, 255, 0],
  red_mid: [128, 0, 0],
  green_mid: [0, 128, 0],
  blue_mid: [0, 0, 128],
  yellow_mid: [128, 128, 0],
  skin_light: [225, 185, 155],
  skin_medium: [190, 140, 105],
  skin_deep: [145, 95, 65],
};

function bgrToCss(bgr) {
  if (!bgr || bgr.length < 3) return 'rgb(0,0,0)';
  const [b, g, r] = bgr.map((v) => Math.max(0, Math.min(255, Math.round(Number(v)))));
  return `rgb(${r},${g},${b})`;
}

function renderColorCal(status) {
  const cal = status?.color_calibration || status;
  if (!cal || !$('color-cal-meta')) return;

  const state = cal.state || 'idle';
  const mode = cal.mode || 'manual';
  const active = state === 'running' || state === 'ready';
  const sampling = cal.phase === 'sampling';
  const pct = Math.round((Number(cal.progress) || 0) * 100);
  const bar = $('color-cal-bar');
  if (bar) bar.style.width = `${pct}%`;

  const bits = [mode, state];
  if (cal.patch && cal.patch !== 'idle') bits.push(cal.patch);
  if (cal.measured != null && cal.total != null) {
    bits.push(`${cal.measured}/${cal.total}`);
  }
  if (active) bits.push(`${pct}%`);
  if (mode === 'auto' && cal.settle_sec != null) {
    bits.push(`settle ${Number(cal.settle_sec).toFixed(1)}s`);
  }
  if (sampling) bits.push('capturing…');
  if (cal.error) bits.push(cal.error);
  $('color-cal-meta').textContent = bits.join(' · ');

  const settleInput = $('color-cal-settle');
  if (settleInput && !active && cal.settle_sec != null
      && document.activeElement !== settleInput) {
    settleInput.value = String(Number(cal.settle_sec));
  }
  if (settleInput) settleInput.disabled = active;

  const modeManual = $('color-cal-mode-manual');
  const modeAuto = $('color-cal-mode-auto');
  if (modeManual && modeAuto && document.activeElement !== modeManual
      && document.activeElement !== modeAuto) {
    modeManual.checked = mode !== 'auto';
    modeAuto.checked = mode === 'auto';
  }
  if (modeManual) modeManual.disabled = active;
  if (modeAuto) modeAuto.disabled = active;

  const advance = $('color-cal-advance');
  if (advance && document.activeElement !== advance) {
    advance.checked = Boolean(cal.advance_after_capture);
  }
  if (advance) advance.disabled = !active || mode !== 'manual' || sampling;

  const startBtn = $('btn-color-cal-start');
  const abortBtn = $('btn-color-cal-abort');
  const captureBtn = $('btn-color-cal-capture');
  const prevBtn = $('btn-color-cal-prev');
  const nextBtn = $('btn-color-cal-next');
  const solveBtn = $('btn-color-cal-solve');
  const applyBtn = $('btn-color-cal-apply');
  const saveBtn = $('btn-color-cal-save');
  if (startBtn) startBtn.disabled = state === 'running' || sampling;
  if (abortBtn) abortBtn.disabled = !active;
  if (captureBtn) {
    captureBtn.disabled = !cal.can_capture;
    captureBtn.hidden = mode !== 'manual';
  }
  if (prevBtn) {
    prevBtn.disabled = !active || mode !== 'manual' || sampling || Number(cal.index) <= 0;
    prevBtn.hidden = mode !== 'manual';
  }
  if (nextBtn) {
    nextBtn.disabled = !active || mode !== 'manual' || sampling
      || Number(cal.index) >= Number(cal.total) - 1;
    nextBtn.hidden = mode !== 'manual';
  }
  if (solveBtn) {
    solveBtn.disabled = !cal.can_solve;
    solveBtn.hidden = mode !== 'manual';
  }
  if (applyBtn) applyBtn.disabled = state !== 'ready' || !cal.solution;
  if (saveBtn) saveBtn.disabled = state !== 'ready' || !cal.solution;

  const swatches = $('color-cal-swatches');
  if (swatches) {
    const measurements = cal.measurements || {};
    const names = Object.keys(PATCH_TARGET_RGB);
    const current = cal.patch;
    swatches.innerHTML = names.map((name) => {
      const target = PATCH_TARGET_RGB[name];
      const measured = measurements[name];
      const targetCss = `rgb(${target[0]},${target[1]},${target[2]})`;
      const measuredCss = measured ? bgrToCss(measured) : 'transparent';
      const cls = ['cal-swatch'];
      if (name === current) cls.push('current');
      if (measured) cls.push('measured');
      return `<button type="button" class="${cls.join(' ')}" data-patch="${name}"><div class="pair"><i style="background:${targetCss}"></i><i style="background:${measuredCss};outline:1px solid var(--line)"></i></div>${name}</button>`;
    }).join('');
  }

  const sol = $('color-cal-solution');
  if (sol) {
    if (cal.solution?.matrix || cal.solution?.gains) {
      const g = cal.solution.gains || {};
      const bl = cal.solution.black_level || {};
      const notes = (cal.solution.notes || []).join('; ');
      const matrixBits = cal.solution.matrix
        ? `3×3 matrix + gamma ${cal.solution.gamma}`
        : `gains R${g.r} G${g.g} B${g.b} · gamma ${cal.solution.gamma}`;
      const blackBits = (bl.r != null && (Number(bl.r) + Number(bl.g) + Number(bl.b)) > 0.5)
        ? ` · black floor R${Number(bl.r).toFixed(1)} G${Number(bl.g).toFixed(1)} B${Number(bl.b).toFixed(1)}`
        : '';
      sol.textContent = `Proposed ${matrixBits}${blackBits}`
        + (notes ? ` · ${notes}` : '');
    } else {
      sol.textContent = '';
    }
  }

  // Warn when measured white is still near black (bad capture / AE / ROI).
  const measured = cal.measurements || {};
  if (measured.white && Array.isArray(measured.white)) {
    const [wb, wg, wr] = measured.white.map(Number);
    const whiteLuma = 0.114 * wb + 0.587 * wg + 0.299 * wr;
    if (whiteLuma < 40 && sol && !cal.solution) {
      sol.textContent = `White looks almost black (luma ${whiteLuma.toFixed(0)}) — `
        + 're-capture white; check TV is showing the patch, perspective ROI is on-panel, '
        + 'and phone AE has settled.';
    }
  }
}

async function buildColorCalPanel() {
  const root = $('color-cal-panel');
  if (!root) return;
  root.innerHTML = '';

  const section = document.createElement('div');
  section.className = 'control-group';
  section.innerHTML = `
    <h3>Colour calibrate</h3>
    <p class="hint">Open the patch page fullscreen on the HDMI TV. <strong>Start</strong>
    turns on Lumos Cam cal mode (AF/AE/AWB locked) when that path is running.
    <strong>Capture</strong> replaces a colour in place. Solve, then Apply &amp; Save.
    Abort/Apply restore phone auto.</p>
    <div class="control">
      <label class="check"><input type="radio" name="color-cal-mode" id="color-cal-mode-manual" value="manual" checked> Manual capture</label>
      <label class="check"><input type="radio" name="color-cal-mode" id="color-cal-mode-auto" value="auto"> Auto-run (timed settle)</label>
    </div>
    <div class="control">
      <label for="color-cal-settle">Auto settle before sample (seconds)</label>
      <input id="color-cal-settle" type="number" min="0.5" max="30" step="0.5" value="3.5">
    </div>
    <div class="control">
      <label class="check"><input type="checkbox" id="color-cal-advance"> Advance after capture</label>
    </div>
    <div class="source-actions">
      <a class="btn" id="btn-color-cal-display" href="/calibrate/display" target="_blank" rel="noopener">Open patch page on TV (HDMI)</a>
      <button type="button" class="btn btn-primary" id="btn-color-cal-start">Start</button>
      <button type="button" class="btn btn-primary" id="btn-color-cal-capture" hidden disabled>Capture</button>
      <button type="button" class="btn" id="btn-color-cal-prev" hidden disabled>Prev</button>
      <button type="button" class="btn" id="btn-color-cal-next" hidden disabled>Next</button>
      <button type="button" class="btn" id="btn-color-cal-solve" hidden disabled>Solve</button>
      <button type="button" class="btn" id="btn-color-cal-abort" disabled>Abort</button>
      <button type="button" class="btn" id="btn-color-cal-apply" disabled>Apply</button>
      <button type="button" class="btn" id="btn-color-cal-save" disabled>Apply &amp; Save</button>
    </div>
    <div class="cal-progress"><span id="color-cal-bar"></span></div>
    <p class="source-meta" id="color-cal-meta">idle</p>
    <div class="cal-swatches" id="color-cal-swatches"></div>
    <p class="cal-solution" id="color-cal-solution"></p>`;
  root.append(section);

  const selectedMode = () => (
    $('color-cal-mode-auto')?.checked ? 'auto' : 'manual'
  );

  $('btn-color-cal-start')?.addEventListener('click', async () => {
    try {
      const settleRaw = Number($('color-cal-settle')?.value);
      const settle_sec = Number.isFinite(settleRaw) ? settleRaw : undefined;
      const result = await api('/api/calibrate/color/start', {
        settle_sec,
        mode: selectedMode(),
        advance_after_capture: Boolean($('color-cal-advance')?.checked),
      });
      if (!result.ok) throw new Error(result.error || 'start failed');
      toast(result.mode === 'manual'
        ? (result.lumos_cal_mode
          ? 'Manual calibration — camera locked, Capture when ready'
          : 'Manual calibration — Capture when focus looks good')
        : `Auto calibration (settle ${Number(result.settle_sec).toFixed(1)}s)`);
      renderColorCal(result);
    } catch (err) {
      toast(err.message, 'error');
    }
  });
  $('btn-color-cal-capture')?.addEventListener('click', async () => {
    try {
      const result = await api('/api/calibrate/color/capture', {});
      if (!result.ok) throw new Error(result.error || 'capture failed');
      renderColorCal(result);
    } catch (err) {
      toast(err.message, 'error');
    }
  });
  $('btn-color-cal-prev')?.addEventListener('click', async () => {
    try {
      const result = await api('/api/calibrate/color/navigate', { action: 'prev' });
      if (!result.ok) throw new Error(result.error || 'prev failed');
      renderColorCal(result);
    } catch (err) {
      toast(err.message, 'error');
    }
  });
  $('btn-color-cal-next')?.addEventListener('click', async () => {
    try {
      const result = await api('/api/calibrate/color/navigate', { action: 'next' });
      if (!result.ok) throw new Error(result.error || 'next failed');
      renderColorCal(result);
    } catch (err) {
      toast(err.message, 'error');
    }
  });
  $('btn-color-cal-solve')?.addEventListener('click', async () => {
    try {
      const result = await api('/api/calibrate/color/solve', {});
      if (!result.ok) throw new Error(result.error || 'solve failed');
      toast('Matrix solved — Apply & Save when happy');
      renderColorCal(result);
    } catch (err) {
      toast(err.message, 'error');
    }
  });
  $('color-cal-advance')?.addEventListener('change', async (ev) => {
    try {
      const result = await api('/api/calibrate/color/options', {
        advance_after_capture: Boolean(ev.target.checked),
      });
      renderColorCal(result);
    } catch (err) {
      toast(err.message, 'error');
    }
  });
  $('color-cal-swatches')?.addEventListener('click', async (ev) => {
    const btn = ev.target.closest('[data-patch]');
    if (!btn) return;
    try {
      const result = await api('/api/calibrate/color/navigate', { patch: btn.dataset.patch });
      if (!result.ok) throw new Error(result.error || 'goto failed');
      renderColorCal(result);
    } catch (err) {
      toast(err.message, 'error');
    }
  });
  $('btn-color-cal-abort')?.addEventListener('click', async () => {
    try {
      const result = await api('/api/calibrate/color/abort', {});
      toast('Colour calibration aborted');
      renderColorCal(result);
    } catch (err) {
      toast(err.message, 'error');
    }
  });
  const applyCal = async (save) => {
    try {
      const result = await api('/api/calibrate/color/apply', { save: Boolean(save) });
      if (!result.ok) throw new Error(result.error || 'apply failed');
      if (result.config) applyConfig(result.config);
      toast(save ? 'Colour calibration saved' : 'Colour calibration applied');
      renderColorCal(result.calibration || result);
    } catch (err) {
      toast(err.message, 'error');
    }
  };
  $('btn-color-cal-apply')?.addEventListener('click', () => applyCal(false));
  $('btn-color-cal-save')?.addEventListener('click', () => applyCal(true));
  renderColorCal({ state: 'idle', mode: 'manual', progress: 0, measurements: {} });
}

async function buildSourcePanel() {
  const root = $('source-panel');
  if (!root) return;
  root.innerHTML = '';

  const section = document.createElement('div');
  section.className = 'control-group';
  section.innerHTML = `
    <h3>Source</h3>
    <p class="hint">One capture source at a time. Only settings for this input
    type are shown. Apply switches live; Save writes config.yaml.</p>
    <div class="control">
      <label for="source-type">Input type</label>
      <select id="source-type">
        ${SOURCE_TYPES.map((t) => `<option value="${t.value}">${t.label}</option>`).join('')}
      </select>
    </div>
    <div class="control" data-source-for="v4l2">
      <label for="source-device">Camera device</label>
      <select id="source-device"></select>
    </div>
    <div class="source-fields" data-source-for="rtsp">
      <div class="control">
        <label for="source-rtsp">RTSP URL</label>
        <input id="source-rtsp" type="url" autocomplete="off" spellcheck="false">
      </div>
      <div class="control">
        <label for="source-transport">Transport</label>
        <select id="source-transport">
          <option value="tcp">tcp</option>
          <option value="udp">udp</option>
        </select>
      </div>
    </div>
    <div class="control" data-source-for="file">
      <label for="source-path">File or image path</label>
      <input id="source-path" type="text" spellcheck="false" placeholder="/path/to/clip.mp4">
    </div>
    <div class="control" data-source-for="file">
      <label class="check"><input type="checkbox" id="source-loop"> Loop file</label>
    </div>
    <div class="control" data-source-for="file synthetic">
      <label for="source-replay-fps">Replay FPS (0 = as fast as the pipeline wants)</label>
      <input id="source-replay-fps" type="number" min="0" max="120" step="1" value="0">
    </div>
    <div class="control" data-source-for="lumos scrcpy v4l2 rtsp file">
      <label for="source-process-width">Process width (0 = native)</label>
      <input id="source-process-width" type="number" min="0" max="7680" step="1" value="0">
    </div>
    <div id="lumos-cam-panel" class="source-fields" data-source-for="lumos" hidden></div>
    <div id="scrcpy-panel" class="source-fields" data-source-for="scrcpy" hidden></div>
    <p class="source-meta" id="source-meta"></p>
    <div class="source-actions">
      <button type="button" class="btn btn-primary" id="btn-source-apply">Apply</button>
      <button type="button" class="btn" id="btn-source-save">Apply &amp; Save</button>
      <button type="button" class="btn" id="btn-source-refresh" data-source-for="v4l2">Refresh USB list</button>
    </div>`;
  root.append(section);

  $('source-type').addEventListener('change', syncSourceFieldsVisibility);
  $('btn-source-refresh').addEventListener('click', async () => {
    await loadCaptureDevices();
    fillDeviceSelect(sourcePanelState.device || $('source-device').value);
    toast('USB device list refreshed');
  });
  $('btn-source-apply').addEventListener('click', () => applySource({ save: false }));
  $('btn-source-save').addEventListener('click', () => applySource({ save: true }));

  await loadCaptureDevices();
  fillDeviceSelect(sourcePanelState.device);
  const type = $('source-type');
  if (type) type.value = sourcePanelState.source;
  syncSourceFieldsVisibility();
}

async function applySource({ save }) {
  const status = $('tune-status');
  const type = $('source-type')?.value || 'v4l2';
  const body = { source: type, save: Boolean(save) };
  const processWidth = Number($('source-process-width')?.value || 0);
  if (['lumos', 'scrcpy', 'v4l2', 'rtsp', 'file'].includes(type)) {
    body.process_width = Number.isFinite(processWidth) ? processWidth : 0;
  }

  if (type === 'lumos') {
    Object.assign(body, readLumosFields());
  } else if (type === 'scrcpy') {
    Object.assign(body, readScrcpyFields());
  } else if (type === 'v4l2') {
    const device = ($('source-device')?.value || '').trim();
    if (!device) {
      toast('Select a USB camera', 'error');
      return;
    }
    body.device = device;
  } else if (type === 'rtsp') {
    const url = ($('source-rtsp')?.value || '').trim();
    if (url) body.rtsp_url = url;
    else if (!sourcePanelState.rtsp_saved) {
      toast('Enter an RTSP URL', 'error');
      return;
    }
    body.transport = $('source-transport')?.value || 'tcp';
  } else if (type === 'file') {
    const path = ($('source-path')?.value || '').trim();
    if (!path) {
      toast('Enter a file or image path', 'error');
      return;
    }
    body.path = path;
    body.loop = Boolean($('source-loop')?.checked);
    body.replay_fps = Number($('source-replay-fps')?.value || 0);
  } else if (type === 'synthetic') {
    body.replay_fps = Number($('source-replay-fps')?.value || 0);
  }

  try {
    if (status) status.textContent = save ? 'saving source…' : 'switching source…';
    const result = await api('/api/camera/source', body);
    if (!result.ok && result.error) throw new Error(result.error);
    if (result.devices) sourcePanelState.devices = result.devices;
    applyConfig(result.config, { skipFocused: true });
    applySourcePanelFromConfig(result.config);
    applyLumosPanelFromConfig(result.config);
    applyScrcpyPanelFromConfig(result.config);
    fillDeviceSelect(result.config?.camera?.device || '');
    if (normalizeSourceType(result.config?.camera?.source) === 'v4l2') {
      await buildCameraControls();
    } else {
      const root = $('camera-controls');
      if (root) root.innerHTML = '';
    }
    toast(save && result.saved ? `Source saved to ${result.saved}` : 'Source applied', 'ok');
    if (status) status.textContent = save ? 'source saved' : 'source applied';
  } catch (err) {
    if (status) status.textContent = 'source update failed';
    toast(err.message, 'error');
  }
}

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
  if (status.lumos_cam) {
    lumosPanelState.running = Boolean(status.lumos_cam.running);
    lumosPanelState.last_error = status.lumos_cam.last_error || '';
    lumosPanelState.cal_mode = Boolean(status.lumos_cam.cal_mode);
    lumosPanelState.app_version = status.lumos_cam.app_version || '';
    if (status.lumos_cam.ui_rotation != null) lumosPanelState.ui_rotation = Number(status.lumos_cam.ui_rotation);
    if (status.lumos_cam.frame_rotation != null) lumosPanelState.frame_rotation = Number(status.lumos_cam.frame_rotation);
    lumosPanelState.flip_h = Boolean(status.lumos_cam.flip_h);
    lumosPanelState.flip_v = Boolean(status.lumos_cam.flip_v);
    if (!lumosFormDirty && !lumosBusy) {
      if (status.lumos_cam.zoom != null) lumosPanelState.camera_zoom = Number(status.lumos_cam.zoom);
      if (status.lumos_cam.pan_x != null) lumosPanelState.pan_x = Number(status.lumos_cam.pan_x);
      if (status.lumos_cam.pan_y != null) lumosPanelState.pan_y = Number(status.lumos_cam.pan_y);
      if (status.lumos_cam.af) lumosPanelState.af = status.lumos_cam.af;
      if (status.lumos_cam.ae) lumosPanelState.ae = status.lumos_cam.ae;
      if (status.lumos_cam.awb) lumosPanelState.awb = status.lumos_cam.awb;
      if (status.config?.lumos_cam) {
        lumosPanelState.enabled = Boolean(status.config.lumos_cam.enabled);
      } else if (status.lumos_cam.enabled != null) {
        lumosPanelState.enabled = Boolean(status.lumos_cam.enabled);
      }
      syncLumosForm();
    } else {
      syncLumosMeta();
    }
  }
  if (status.scrcpy) {
    scrcpyPanelState.running = Boolean(status.scrcpy.running);
    scrcpyPanelState.last_error = status.scrcpy.last_error || '';
    scrcpyPanelState.crop = status.scrcpy.crop || '';
    // While the user is editing, only refresh live status text — never the inputs.
    if (!scrcpyFormDirty && !scrcpyBusy) {
      if (status.scrcpy.zoom != null) {
        scrcpyPanelState.camera_zoom = Number(status.scrcpy.zoom);
      }
      if (status.scrcpy.view_zoom != null) {
        scrcpyPanelState.view_zoom = Number(status.scrcpy.view_zoom);
      }
      if (status.scrcpy.pan_x != null) scrcpyPanelState.pan_x = Number(status.scrcpy.pan_x);
      if (status.scrcpy.pan_y != null) scrcpyPanelState.pan_y = Number(status.scrcpy.pan_y);
      if (status.config?.scrcpy) {
        scrcpyPanelState.enabled = Boolean(status.config.scrcpy.enabled);
      } else if (status.scrcpy.enabled != null) {
        scrcpyPanelState.enabled = Boolean(status.scrcpy.enabled);
      }
      syncScrcpyForm();
    } else {
      syncScrcpyMeta();
    }
  }
  if (status.color_calibration) {
    renderColorCal(status.color_calibration);
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
  await buildSourcePanel();
  await buildLumosCamPanel();
  await buildScrcpyPanel();
  await buildColorCalPanel();
  await buildCameraControls();
  setupPicker();
  wireButtons();
  try {
    const config = await api('/api/config');
    applyConfig(config);
    applySourcePanelFromConfig(config);
    applyLumosPanelFromConfig(config);
    applyScrcpyPanelFromConfig(config);
    fillDeviceSelect(config.camera?.device || '');
    syncSourceFieldsVisibility();
    if (normalizeSourceType(config?.camera?.source) !== 'v4l2') {
      const hw = $('camera-controls');
      if (hw) hw.innerHTML = '';
    }
  } catch (err) {
    toast(err.message, 'error');
  }
  await refresh();
  setInterval(refresh, 1000);
}

init();
