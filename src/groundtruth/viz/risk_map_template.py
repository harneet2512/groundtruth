"""HTML template for the 3D Code City hallucination risk map.

Uses Three.js for 3D rendering. Files are buildings grouped by directory into
districts. Height = reference count, width = symbol count, color = risk score.
Premium dashboard with dark/light theme, bloom, side panel, keyboard shortcuts.
"""

from __future__ import annotations

RISK_MAP_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GroundTruth — Code City Risk Map</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0f0f1a;
  --surface: #1a1a2e;
  --surface-2: #222240;
  --text: #e8e8f0;
  --text-secondary: #8888aa;
  --text-muted: #5a5a7a;
  --accent: #8b5cf6;
  --accent-hover: #a78bfa;
  --low: #4ecdc4;
  --moderate: #f59e0b;
  --high: #ef4444;
  --critical: #dc2626;
  --panel-bg: rgba(22,33,62,0.92);
  --border: rgba(255,255,255,0.08);
  --border-hover: rgba(255,255,255,0.15);
  --glow: rgba(139,92,246,0.15);
}
html.light {
  --bg: #f5f5fa;
  --surface: #ffffff;
  --surface-2: #f0f0f5;
  --text: #1a1a2e;
  --text-secondary: #555570;
  --text-muted: #8888aa;
  --accent: #7c3aed;
  --accent-hover: #6d28d9;
  --low: #0d9488;
  --moderate: #d97706;
  --high: #dc2626;
  --critical: #b91c1c;
  --panel-bg: rgba(255,255,255,0.92);
  --border: rgba(0,0,0,0.08);
  --border-hover: rgba(0,0,0,0.15);
  --glow: rgba(124,58,237,0.1);
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
  background: var(--bg);
  color: var(--text);
  overflow: hidden;
  height: 100vh;
  display: flex;
  flex-direction: column;
}

/* Top bar */
.top-bar {
  height: 48px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  z-index: 20;
  flex-shrink: 0;
}
.top-bar .logo {
  font-family: 'JetBrains Mono', monospace;
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 1px;
  color: var(--accent);
}
.top-bar .project-name {
  font-size: 12px;
  color: var(--text-secondary);
  margin-left: 12px;
}
.top-bar .controls {
  display: flex;
  gap: 6px;
  align-items: center;
}
.top-bar button {
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text-secondary);
  font-family: inherit;
  font-size: 12px;
  padding: 5px 10px;
  cursor: pointer;
  transition: all 0.2s;
}
.top-bar button:hover {
  border-color: var(--accent);
  color: var(--text);
}
.top-bar button svg { width: 14px; height: 14px; vertical-align: -2px; }

/* Stats bar */
.stats-bar {
  height: 40px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 24px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  font-size: 12px;
  z-index: 20;
  flex-shrink: 0;
}
.stats-bar .stat-item {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--text-secondary);
}
.stats-bar .stat-value {
  font-weight: 600;
  color: var(--text);
  font-family: 'JetBrains Mono', monospace;
}
.stats-bar .divider {
  width: 1px;
  height: 16px;
  background: var(--border);
}

/* Main area */
.main-area {
  flex: 1;
  display: flex;
  position: relative;
  overflow: hidden;
}
.viewport {
  flex: 1;
  position: relative;
}
.viewport canvas { display: block; width: 100%; height: 100%; }

/* Side panel */
.side-panel {
  width: 320px;
  background: var(--panel-bg);
  backdrop-filter: blur(16px);
  border-left: 1px solid var(--border);
  overflow-y: auto;
  padding: 20px;
  z-index: 10;
  transition: transform 0.3s ease;
  flex-shrink: 0;
}
.side-panel::-webkit-scrollbar { width: 4px; }
.side-panel::-webkit-scrollbar-track { background: transparent; }
.side-panel::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
.side-panel h2 {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 1px;
  color: var(--text-muted);
  margin-bottom: 12px;
}
.side-panel h3 {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 4px;
  word-break: break-all;
}
.panel-path {
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  color: var(--text-secondary);
  margin-bottom: 16px;
  word-break: break-all;
}

/* Risk badge */
.risk-badge {
  display: inline-block;
  font-size: 10px;
  font-weight: 700;
  padding: 2px 8px;
  border-radius: 10px;
  letter-spacing: 0.5px;
}
.risk-badge.critical { background: var(--critical); color: #fff; }
.risk-badge.high { background: var(--high); color: #fff; }
.risk-badge.moderate { background: var(--moderate); color: #fff; }
.risk-badge.low { background: var(--low); color: #fff; }

/* Factor bars */
.factor-list { margin: 12px 0; }
.factor-item {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 12px;
}
.factor-label {
  width: 120px;
  color: var(--text-secondary);
  font-size: 11px;
  flex-shrink: 0;
}
.factor-bar-bg {
  flex: 1;
  height: 4px;
  background: var(--border);
  border-radius: 2px;
  overflow: hidden;
}
.factor-bar {
  height: 100%;
  border-radius: 2px;
  transition: width 0.3s ease;
}
.factor-pct {
  width: 32px;
  text-align: right;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  color: var(--text-muted);
}

/* Stats grid */
.stats-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin-bottom: 16px;
}
.stat-card {
  background: var(--surface-2);
  border-radius: 8px;
  padding: 12px;
}
.stat-card .stat-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: var(--text-muted);
  margin-bottom: 4px;
}
.stat-card .stat-num {
  font-family: 'JetBrains Mono', monospace;
  font-size: 20px;
  font-weight: 700;
}

/* Risk distribution bar */
.risk-dist {
  display: flex;
  height: 8px;
  border-radius: 4px;
  overflow: hidden;
  margin-bottom: 8px;
}
.risk-dist div { transition: width 0.3s; }

/* Legend */
.legend { margin-top: 16px; }
.legend-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  color: var(--text-secondary);
  margin-bottom: 4px;
}
.legend-dot {
  width: 10px;
  height: 10px;
  border-radius: 2px;
  flex-shrink: 0;
}

/* Symbol list in panel */
.symbol-list { margin: 8px 0; }
.symbol-item {
  font-size: 12px;
  padding: 4px 0;
  border-bottom: 1px solid var(--border);
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.symbol-item:last-child { border-bottom: none; }
.symbol-name {
  font-family: 'JetBrains Mono', monospace;
  font-weight: 500;
}
.symbol-kind {
  font-size: 10px;
  color: var(--text-muted);
  padding: 1px 5px;
  border-radius: 3px;
  background: var(--surface-2);
}
.symbol-dead { color: var(--high); font-size: 10px; margin-left: 4px; }

/* Dep list */
.dep-list {
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  color: var(--text-secondary);
  margin: 4px 0;
}
.dep-list div {
  padding: 2px 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  display: flex;
  align-items: center;
  gap: 6px;
}
.dep-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}
/* Blast radius */
#d-blast { margin-bottom: 16px; }
.blast-label { font-size: 10px; color: var(--text-muted); display: block; margin-bottom: 4px; }
.blast-outgoing, .blast-incoming { margin-bottom: 8px; }
.blast-total { font-size: 12px; margin-top: 8px; color: var(--text-secondary); }
#panel-detail { position: relative; padding-right: 40px; }
#btn-close-panel {
  position: absolute;
  top: 16px;
  right: 16px;
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text-secondary);
  border-radius: 4px;
  width: 24px;
  height: 24px;
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}
#btn-close-panel:hover { border-color: var(--accent); color: var(--text); }

/* Bottom bar */
.bottom-bar {
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-top: 1px solid var(--border);
  background: var(--surface);
  font-size: 11px;
  color: var(--text-muted);
  z-index: 20;
  flex-shrink: 0;
}
.bottom-bar kbd {
  display: inline-block;
  padding: 1px 5px;
  border: 1px solid var(--border);
  border-radius: 3px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  margin: 0 2px;
  background: var(--surface-2);
}

/* Tooltip */
#tooltip {
  position: fixed;
  display: none;
  background: var(--panel-bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 16px;
  max-width: 360px;
  font-size: 12px;
  backdrop-filter: blur(16px);
  pointer-events: none;
  z-index: 30;
  box-shadow: 0 8px 32px rgba(0,0,0,0.3);
}
#tooltip h3 {
  font-size: 13px;
  margin-bottom: 4px;
}
#tooltip .tt-path {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: var(--text-secondary);
  margin-bottom: 8px;
  word-break: break-all;
}
#tooltip .tt-factors {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 2px 8px;
  font-size: 11px;
  align-items: center;
}
#tooltip .tt-factors .label { color: var(--text-secondary); }
#tooltip .tt-factors .bar-wrap {
  height: 3px;
  background: var(--border);
  border-radius: 2px;
  overflow: hidden;
}
#tooltip .tt-factors .bar-fill {
  height: 100%;
  border-radius: 2px;
}
#tooltip .tt-factors .val {
  text-align: right;
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: var(--text-muted);
}
#tooltip .tt-symbols {
  margin-top: 8px;
  font-size: 11px;
  color: var(--text-secondary);
  max-height: 80px;
  overflow-y: auto;
}

/* Animations */
@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
@keyframes slideIn { from { transform: translateX(20px); opacity: 0; } to { transform: translateX(0); opacity: 1; } }

/* Responsive */
@media (max-width: 768px) {
  .side-panel {
    position: absolute;
    right: 0;
    top: 0;
    bottom: 0;
    transform: translateX(100%);
    z-index: 25;
  }
  .side-panel.open { transform: translateX(0); }
}
</style>
</head>
<body>
<div class="top-bar">
  <div style="display:flex;align-items:center">
    <span class="logo">GROUNDTRUTH</span>
    <span class="project-name">Risk Map</span>
  </div>
  <div class="controls">
    <button id="btn-theme" title="Toggle theme (T)">
      <svg id="icon-theme" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>
      </svg>
    </button>
    <button id="btn-labels" title="Toggle labels (L)">Labels</button>
    <button id="btn-reset" title="Reset camera (R)">Reset</button>
  </div>
</div>

<div class="stats-bar">
  <div class="stat-item"><span>Files</span><span class="stat-value" id="s-files">0</span></div>
  <div class="divider"></div>
  <div class="stat-item"><span>Symbols</span><span class="stat-value" id="s-symbols">0</span></div>
  <div class="divider"></div>
  <div class="stat-item"><span>Refs</span><span class="stat-value" id="s-refs">0</span></div>
  <div class="divider"></div>
  <div class="stat-item">
    <span style="color:var(--critical)" class="stat-value" id="s-critical">0</span><span>critical</span>
    <span style="color:var(--high)" class="stat-value" id="s-high">0</span><span>high</span>
    <span style="color:var(--moderate)" class="stat-value" id="s-moderate">0</span><span>mod</span>
    <span style="color:var(--low)" class="stat-value" id="s-low">0</span><span>low</span>
  </div>
</div>

<div class="main-area">
  <div class="viewport" id="viewport"></div>
  <div class="side-panel" id="side-panel">
    <div id="panel-overview">
      <h2>Project Overview</h2>
      <div class="stats-grid">
        <div class="stat-card"><div class="stat-label">Files</div><div class="stat-num" id="p-files">0</div></div>
        <div class="stat-card"><div class="stat-label">Symbols</div><div class="stat-num" id="p-symbols">0</div></div>
        <div class="stat-card"><div class="stat-label">References</div><div class="stat-num" id="p-refs">0</div></div>
        <div class="stat-card"><div class="stat-label">Avg Risk</div><div class="stat-num" id="p-avg-risk">0</div></div>
      </div>
      <h2>Risk Distribution</h2>
      <div class="risk-dist" id="risk-dist"></div>
      <div class="legend">
        <div class="legend-item"><div class="legend-dot" style="background:var(--critical)"></div>Critical (&ge;0.7)</div>
        <div class="legend-item"><div class="legend-dot" style="background:var(--high)"></div>High (&ge;0.45)</div>
        <div class="legend-item"><div class="legend-dot" style="background:var(--moderate)"></div>Moderate (&ge;0.25)</div>
        <div class="legend-item"><div class="legend-dot" style="background:var(--low)"></div>Low (&lt;0.25)</div>
      </div>
    </div>
    <div id="panel-detail" style="display:none">
      <button id="btn-close-panel" type="button" title="Close">×</button>
      <h2>Selected File</h2>
      <h3 id="d-name"></h3>
      <div class="panel-path" id="d-path"></div>
      <span class="risk-badge" id="d-badge"></span>
      <div class="factor-list" id="d-factors"></div>
      <h2>Symbols</h2>
      <div class="symbol-list" id="d-symbols"></div>
      <div id="d-blast" style="display:none"></div>
      <h2>Dependencies</h2>
      <div class="dep-list" id="d-deps"></div>
    </div>
  </div>
</div>

<div class="bottom-bar">
  <kbd>R</kbd> reset &nbsp; <kbd>L</kbd> labels &nbsp; <kbd>T</kbd> theme &nbsp; <kbd>F</kbd> fog &nbsp; <kbd>Esc</kbd> deselect &nbsp; <kbd>1-4</kbd> filter
</div>

<div id="tooltip">
  <h3 id="tt-name"></h3>
  <div class="tt-path" id="tt-path"></div>
  <div class="tt-factors" id="tt-factors"></div>
  <div class="tt-symbols" id="tt-symbols"></div>
</div>

<script type="importmap">
{
  "imports": {
    "three": "https://cdn.jsdelivr.net/npm/three@0.170.0/build/three.module.js",
    "three/addons/": "https://cdn.jsdelivr.net/npm/three@0.170.0/examples/jsm/"
  }
}
</script>
<script type="module">
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const DATA = __GRAPH_DATA_JSON__;
const CONFIG = __CONFIG_JSON__;

// ── State ──────────────────────────────────────────
let showLabels = true;
let selectedMesh = null;
let hoveredMesh = null;
let focusMode = false;
let focusConnected = new Set();
let focusSecondary = new Set();
let focusEdgeSet = new Set(); // 'source|target' for edgeEntries to highlight
let fogLevel = 1; // 0=off, 1=light, 2=heavy
let riskFilter = null; // null or 1-4
const labelSprites = [];
const buildingMeshes = [];
const edgeLines = [];
const edgeMap = {}; // nodeId -> [{line, glowLine, otherNodeId, direction}]

// ── Theme ──────────────────────────────────────────
if (CONFIG.theme === 'light') document.documentElement.classList.add('light');

// ── Scene setup ────────────────────────────────────
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 2000);
const container = document.getElementById('viewport');
const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
container.appendChild(renderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.maxPolarAngle = Math.PI / 2.05;
controls.minDistance = 5;
controls.maxDistance = 500;

function isDark() { return !document.documentElement.classList.contains('light'); }
function applyTheme() {
  const bg = isDark() ? 0x0f0f1a : 0xf5f5fa;
  scene.background = new THREE.Color(bg);
  updateFog();
  groundMeshes.forEach(g => { g.material.color.set(isDark() ? 0xffffff : 0x000000); });
}
function updateFog() {
  const bg = isDark() ? 0x0f0f1a : 0xf5f5fa;
  if (fogLevel === 0) scene.fog = null;
  else if (fogLevel === 1) scene.fog = new THREE.FogExp2(bg, 0.008);
  else scene.fog = new THREE.FogExp2(bg, 0.02);
}
applyTheme();

// ── Lighting ───────────────────────────────────────
scene.add(new THREE.AmbientLight(0xffffff, 0.4));
const dir1 = new THREE.DirectionalLight(0xffffff, 0.6);
dir1.position.set(10, 20, 10);
scene.add(dir1);
const dir2 = new THREE.DirectionalLight(0x6366f1, 0.15);
dir2.position.set(-10, 5, -10);
scene.add(dir2);

// ── Risk color ─────────────────────────────────────
function riskColor(score) {
  if (score >= 0.7) return 0xdc2626;
  if (score >= 0.45) return 0xef4444;
  if (score >= 0.25) return 0xf59e0b;
  return 0x4ecdc4;
}
function riskColorHex(score) {
  if (score >= 0.7) return '#dc2626';
  if (score >= 0.45) return '#ef4444';
  if (score >= 0.25) return '#f59e0b';
  return '#4ecdc4';
}
function riskClass(tag) { return tag.toLowerCase(); }

// ── Grid ───────────────────────────────────────────
const gridHelper = new THREE.GridHelper(200, 40, 0x333355, 0x222244);
gridHelper.material.opacity = 0.15;
gridHelper.material.transparent = true;
scene.add(gridHelper);

function makeWindowTexture(hexColor) {
  const c = document.createElement('canvas');
  c.width = 64;
  c.height = 128;
  const ctx = c.getContext('2d');
  ctx.fillStyle = '#0a0a18';
  ctx.fillRect(0, 0, 64, 128);
  const r = (hexColor >> 16) & 255, g = (hexColor >> 8) & 255, b = hexColor & 255;
  ctx.fillStyle = 'rgb(' + r + ',' + g + ',' + b + ')';
  for (let y = 4; y < 128; y += 12) {
    for (let x = 4; x < 64; x += 10) {
      ctx.fillRect(x, y, 6, 8);
    }
  }
  const tex = new THREE.CanvasTexture(c);
  tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  return tex;
}
const windowTextureCache = {};

// ── Layout ─────────────────────────────────────────
const dirs = {};
DATA.nodes.forEach(n => {
  const d = n.directory || '.';
  if (!dirs[d]) dirs[d] = [];
  dirs[d].push(n);
});

const dirNames = Object.keys(dirs).sort();
const DISTRICT_PAD = 6;
const BUILDING_PAD = 1.2;
const MAX_HEIGHT = 8;
const MIN_HEIGHT = 0.5;
const MAX_WIDTH = 4;
const MIN_WIDTH = 0.4;
const groundMeshes = [];

let cursorX = 0;
const nodeIdToMesh = {};

dirNames.forEach((dirName, dirIdx) => {
  const files = dirs[dirName];
  const cols = Math.ceil(Math.sqrt(files.length));
  const districtWidth = cols * (MAX_WIDTH + BUILDING_PAD) + BUILDING_PAD;
  const districtDepth = Math.ceil(files.length / cols) * (MAX_WIDTH + BUILDING_PAD) + BUILDING_PAD;

  // Ground plane
  const groundGeo = new THREE.PlaneGeometry(districtWidth, districtDepth);
  const groundMat = new THREE.MeshStandardMaterial({
    color: isDark() ? 0xffffff : 0x000000, opacity: 0.03, transparent: true
  });
  const ground = new THREE.Mesh(groundGeo, groundMat);
  ground.rotation.x = -Math.PI / 2;
  ground.position.set(cursorX + districtWidth / 2, 0.01, districtDepth / 2);
  scene.add(ground);
  groundMeshes.push(ground);

  // Ground border
  const borderGeo = new THREE.EdgesGeometry(groundGeo);
  const borderMat = new THREE.LineBasicMaterial({ color: 0x6366f1, opacity: 0.1, transparent: true });
  const border = new THREE.LineSegments(borderGeo, borderMat);
  border.rotation.x = -Math.PI / 2;
  border.position.copy(ground.position);
  scene.add(border);

  // District label
  const distLabel = makeTextSprite(dirName.split('/').pop() || '.', 0.7, '#8888aa');
  distLabel.position.set(cursorX + districtWidth / 2, 0.1, -1);
  scene.add(distLabel);
  labelSprites.push(distLabel);

  files.forEach((node, i) => {
    const col = i % cols;
    const row = Math.floor(i / cols);
    const nh = node.normalized_height || 0;
    const nw = node.normalized_width || 0;

    const w = Math.max(MIN_WIDTH, MIN_WIDTH + nw * (MAX_WIDTH - MIN_WIDTH));
    const h = Math.max(MIN_HEIGHT, MIN_HEIGHT + nh * (MAX_HEIGHT - MIN_HEIGHT));
    const elevOffset = (node.directory_depth || 0) * 0.05;

    const color = riskColor(node.risk_score);
    const emissiveColor = new THREE.Color(color);

    const x = cursorX + BUILDING_PAD + col * (MAX_WIDTH + BUILDING_PAD) + w / 2;
    const z = BUILDING_PAD + row * (MAX_WIDTH + BUILDING_PAD) + w / 2;

    // Foundation
    const baseGeo = new THREE.BoxGeometry(w * 1.15, 0.08, w * 1.15);
    const baseMat = new THREE.MeshStandardMaterial({ color: 0x111122, roughness: 0.9, metalness: 0 });
    const base = new THREE.Mesh(baseGeo, baseMat);
    base.position.set(x, 0.04, z);
    scene.add(base);

    // Main building (tapered)
    const geo = new THREE.BoxGeometry(w, h, w);
    const pos = geo.attributes.position;
    for (let i = 0; i < pos.count; i++) {
      if (pos.getY(i) > 0) {
        pos.setX(i, pos.getX(i) * 0.9);
        pos.setZ(i, pos.getZ(i) * 0.9);
      }
    }
    pos.needsUpdate = true;
    geo.computeVertexNormals();
    const tag = node.risk_score >= 0.7 ? 'critical' : node.risk_score >= 0.45 ? 'high' : node.risk_score >= 0.25 ? 'mod' : 'low';
    if (!windowTextureCache[tag]) windowTextureCache[tag] = makeWindowTexture(color);
    const mat = new THREE.MeshStandardMaterial({
      color: color,
      map: windowTextureCache[tag],
      roughness: 0.7,
      metalness: 0.1,
      emissive: emissiveColor,
      emissiveIntensity: 0.15,
      transparent: true,
      opacity: node.has_dead_code ? 0.3 : 1.0,
    });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.position.set(x, h / 2 + elevOffset, z);
    mesh.userData = node;
    mesh.userData._originalEmissiveIntensity = 0.15;
    mesh.userData._originalOpacity = mat.opacity;
    mesh.userData._targetEmissive = 0.15;
    mesh.userData._targetOpacity = mat.opacity;
    mesh.userData._targetScale = 1;
    mesh.userData._riseComplete = false;
    mesh.userData._targetY = h / 2 + elevOffset;
    scene.add(mesh);
    buildingMeshes.push(mesh);
    nodeIdToMesh[node.id] = mesh;

    const glowGeo = new THREE.BoxGeometry(w * 1.03, h * 1.03, w * 1.03);
    const posG = glowGeo.attributes.position;
    for (let i = 0; i < posG.count; i++) {
      if (posG.getY(i) > 0) {
        posG.setX(i, posG.getX(i) * 0.9);
        posG.setZ(i, posG.getZ(i) * 0.9);
      }
    }
    posG.needsUpdate = true;
    glowGeo.computeVertexNormals();
    const glowMat = new THREE.MeshBasicMaterial({
      color: color,
      transparent: true,
      opacity: 0,
      side: THREE.BackSide,
      depthWrite: false,
    });
    const glowMesh = new THREE.Mesh(glowGeo, glowMat);
    glowMesh.position.set(x, h / 2 + elevOffset, z);
    glowMesh.userData._targetOpacity = 0;
    scene.add(glowMesh);
    mesh.userData._glowMesh = glowMesh;

    // Top cap (bevel effect)
    const capH = 0.08;
    const capW = w * 0.92;
    const capGeo = new THREE.BoxGeometry(capW, capH, capW);
    const capMat = new THREE.MeshStandardMaterial({
      color: color, roughness: 0.5, metalness: 0.2,
      transparent: true, opacity: mat.opacity,
    });
    const cap = new THREE.Mesh(capGeo, capMat);
    cap.position.set(x, h + elevOffset + capH / 2, z);
    scene.add(cap);

    if (node.risk_score > 0.8) {
      const antGeo = new THREE.CylinderGeometry(0.02, 0.02, 1.5, 8);
      const antMat = new THREE.MeshBasicMaterial({ color: 0xdc2626 });
      const antenna = new THREE.Mesh(antGeo, antMat);
      antenna.position.set(x, h + elevOffset + capH + 1.5 / 2 + 0.5, z);
      scene.add(antenna);
      const tipGeo = new THREE.SphereGeometry(0.08, 8, 6);
      const tipMat = new THREE.MeshBasicMaterial({ color: 0xdc2626, transparent: true, opacity: 1 });
      const tip = new THREE.Mesh(tipGeo, tipMat);
      tip.position.set(x, h + elevOffset + capH + 1.5 + 0.5, z);
      scene.add(tip);
      mesh.userData._antenna = antenna;
      mesh.userData._antennaTip = tip;
    }

    // File label
    const lbl = makeTextSprite(node.label, 0.4, '#aaaacc');
    lbl.position.set(x, h + elevOffset + 0.6, z);
    scene.add(lbl);
    labelSprites.push(lbl);
  });

  cursorX += districtWidth + DISTRICT_PAD;
});

// ── Edges (ground-level glowing lines) ─────────────
const GROUND_Y = 0.05;
const edgeEntries = []; // { line, glowLine, source, target }
DATA.edges.forEach((e, edgeIdx) => {
  const sm = nodeIdToMesh[e.source];
  const tm = nodeIdToMesh[e.target];
  if (!sm || !tm) return;
  const sp2d = new THREE.Vector3(sm.position.x, GROUND_Y, sm.position.z);
  const tp2d = new THREE.Vector3(tm.position.x, GROUND_Y, tm.position.z);
  const mid = sp2d.clone().lerp(tp2d, 0.5);
  const dx = tp2d.x - sp2d.x, dz = tp2d.z - sp2d.z;
  const offset = (edgeIdx % 5 - 2) * 0.4;
  mid.x += -dz * 0.15 + offset * 0.1;
  mid.z += dx * 0.15;
  const curve = new THREE.QuadraticBezierCurve3(sp2d, mid, tp2d);
  const pts = curve.getPoints(32);

  const srcRisk = sm.userData.risk_score ?? 0;
  const tgtRisk = tm.userData.risk_score ?? 0;
  const avgRisk = (srcRisk + tgtRisk) / 2;
  const lineColor = new THREE.Color(riskColor(avgRisk));

  const geo = new THREE.BufferGeometry().setFromPoints(pts);
  const lineMat = new THREE.LineBasicMaterial({
    color: lineColor, opacity: 0.08, transparent: true
  });
  const line = new THREE.Line(geo, lineMat);
  scene.add(line);
  line.userData._targetOpacity = 0.08;
  line.userData._color = lineColor.clone();
  edgeLines.push(line);

  const tubeRadius = 0.08;
  const tubeGeo = new THREE.TubeGeometry(curve, 24, tubeRadius, 6, false);
  const glowMat = new THREE.MeshBasicMaterial({
    color: lineColor, transparent: true, opacity: 0.04,
    side: THREE.DoubleSide, depthWrite: false
  });
  const glowLine = new THREE.Mesh(tubeGeo, glowMat);
  scene.add(glowLine);
  glowLine.userData._targetOpacity = 0.04;
  glowLine.userData._color = lineColor.clone();
  edgeLines.push(glowLine);

  if (!edgeMap[e.source]) edgeMap[e.source] = [];
  if (!edgeMap[e.target]) edgeMap[e.target] = [];
  const entry = { line, glowLine, direction: 'out', otherNodeId: e.target };
  edgeMap[e.source].push(entry);
  edgeMap[e.target].push({ line, glowLine, direction: 'in', otherNodeId: e.source });
  edgeEntries.push({ line, glowLine, source: e.source, target: e.target });
});

const reverseDepMap = {};
DATA.edges.forEach(e => {
  if (!reverseDepMap[e.target]) reverseDepMap[e.target] = [];
  reverseDepMap[e.target].push(e.source);
});

// ── Camera ─────────────────────────────────────────
const cx = cursorX / 2;
const cz = 20;
camera.position.set(cx, 25, cz + 40);
controls.target.set(cx, 0, cz);
const defaultCam = { pos: camera.position.clone(), target: controls.target.clone() };

// ── Bloom (optional) ───────────────────────────────
let composer = null;
if (CONFIG.bloom) {
  try {
    const { EffectComposer } = await import('three/addons/postprocessing/EffectComposer.js');
    const { RenderPass } = await import('three/addons/postprocessing/RenderPass.js');
    const { UnrealBloomPass } = await import('three/addons/postprocessing/UnrealBloomPass.js');
    composer = new EffectComposer(renderer);
    composer.addPass(new RenderPass(scene, camera));
    const bloomPass = new UnrealBloomPass(new THREE.Vector2(innerWidth, innerHeight), 0.3, 0.5, 0.8);
    composer.addPass(bloomPass);
  } catch(e) { composer = null; }
}

// ── HUD stats ──────────────────────────────────────
const rs = DATA.metadata.risk_summary;
document.getElementById('s-files').textContent = DATA.metadata.total_files;
document.getElementById('s-symbols').textContent = DATA.metadata.total_symbols;
document.getElementById('s-refs').textContent = DATA.metadata.total_refs;
document.getElementById('s-critical').textContent = rs.critical || 0;
document.getElementById('s-high').textContent = rs.high || 0;
document.getElementById('s-moderate').textContent = rs.moderate || 0;
document.getElementById('s-low').textContent = rs.low || 0;

// ── Panel overview ─────────────────────────────────
document.getElementById('p-files').textContent = DATA.metadata.total_files;
document.getElementById('p-symbols').textContent = DATA.metadata.total_symbols;
document.getElementById('p-refs').textContent = DATA.metadata.total_refs;
const avgRisk = DATA.nodes.length > 0
  ? (DATA.nodes.reduce((a,n) => a + n.risk_score, 0) / DATA.nodes.length).toFixed(2)
  : '0';
document.getElementById('p-avg-risk').textContent = avgRisk;

// Risk distribution bar
const total = (rs.critical||0) + (rs.high||0) + (rs.moderate||0) + (rs.low||0);
const distEl = document.getElementById('risk-dist');
if (total > 0) {
  distEl.innerHTML = [
    { count: rs.critical||0, color: 'var(--critical)' },
    { count: rs.high||0, color: 'var(--high)' },
    { count: rs.moderate||0, color: 'var(--moderate)' },
    { count: rs.low||0, color: 'var(--low)' },
  ].map(d => `<div style="width:${(d.count/total*100).toFixed(1)}%;background:${d.color}"></div>`).join('');
}

// ── Tooltip ────────────────────────────────────────
const tooltip = document.getElementById('tooltip');
const raycaster = new THREE.Raycaster();
const mouse = new THREE.Vector2();

renderer.domElement.addEventListener('mousemove', e => {
  const rect = renderer.domElement.getBoundingClientRect();
  mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
  mouse.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;

  raycaster.setFromCamera(mouse, camera);
  const hits = raycaster.intersectObjects(buildingMeshes);

  if (hits.length > 0) {
    const mesh = hits[0].object;
    if (mesh !== hoveredMesh) {
      unhover();
      hoveredMesh = mesh;
      mesh.material.emissiveIntensity = 0.4;
      if (mesh.userData._glowMesh) mesh.userData._glowMesh.userData._targetOpacity = 0.25;

      // Highlight ground edges
      const nodeId = mesh.userData.id;
      edgeEntries.forEach(({ line, glowLine }) => {
        line.material.opacity = 0.03;
        glowLine.material.opacity = 0.01;
      });
      (edgeMap[nodeId] || []).forEach(({ line, glowLine, direction }) => {
        line.material.opacity = 0.7;
        glowLine.material.opacity = 0.25;
        line.material.color.set(direction === 'out' ? 0x8b5cf6 : 0x6366f1);
        glowLine.material.color.set(direction === 'out' ? 0x8b5cf6 : 0x6366f1);
      });
    }
    const node = mesh.userData;
    document.getElementById('tt-name').innerHTML =
      node.label + ' <span class="risk-badge ' + riskClass(node.risk_tag) + '">' + node.risk_tag + '</span>';
    document.getElementById('tt-path').textContent = node.id;

    let factorsHtml = '';
    for (const [k, v] of Object.entries(node.risk_factors)) {
      const pct = (v * 100).toFixed(0);
      const c = riskColorHex(v);
      factorsHtml += '<span class="label">' + k.replace(/_/g,' ') + '</span>' +
        '<span class="bar-wrap"><span class="bar-fill" style="width:' + pct + '%;background:' + c + '"></span></span>' +
        '<span class="val">' + pct + '%</span>';
    }
    document.getElementById('tt-factors').innerHTML = factorsHtml;

    let symHtml = '';
    if (node.symbols && node.symbols.length > 0) {
      symHtml = node.symbols.slice(0, 6).map(s => {
        const dead = s.is_dead ? ' <span style="color:var(--high)">[dead]</span>' : '';
        return '<b>' + s.name + '</b> <span style="color:var(--text-muted)">' + s.kind + '</span> (' + s.usage_count + ')' + dead;
      }).join('<br>');
      if (node.symbols.length > 6) symHtml += '<br><span style="color:var(--text-muted)">+' + (node.symbols.length - 6) + ' more</span>';
    }
    document.getElementById('tt-symbols').innerHTML = symHtml;

    tooltip.style.display = 'block';
    tooltip.style.left = Math.min(e.clientX + 16, innerWidth - 380) + 'px';
    tooltip.style.top = Math.min(e.clientY + 16, innerHeight - 200) + 'px';
  } else {
    unhover();
    tooltip.style.display = 'none';
  }
});

function unhover() {
  if (hoveredMesh && hoveredMesh !== selectedMesh) {
    hoveredMesh.material.emissiveIntensity = hoveredMesh.userData._originalEmissiveIntensity;
    if (hoveredMesh.userData._glowMesh) hoveredMesh.userData._glowMesh.userData._targetOpacity = 0;
  }
  hoveredMesh = null;
  if (!focusMode) {
    edgeEntries.forEach(({ line, glowLine }) => {
      line.material.opacity = 0.08;
      glowLine.material.opacity = 0.04;
      if (line.userData._color) line.material.color.copy(line.userData._color);
      if (glowLine.userData._color) glowLine.material.color.copy(glowLine.userData._color);
    });
  }
}

// ── Click to select ────────────────────────────────
renderer.domElement.addEventListener('click', e => {
  const rect = renderer.domElement.getBoundingClientRect();
  mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
  mouse.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;

  raycaster.setFromCamera(mouse, camera);
  const hits = raycaster.intersectObjects(buildingMeshes);

  if (hits.length > 0) {
    selectBuilding(hits[0].object);
  } else if (focusMode) {
    deselectBuilding();
  }
});

function selectBuilding(mesh) {
  selectedMesh = mesh;
  focusMode = true;
  const node = mesh.userData;
  const nid = node.id;

  focusConnected = new Set();
  focusSecondary = new Set();
  focusEdgeSet = new Set();
  (node.imports_from || []).forEach(d => focusConnected.add(d));
  (reverseDepMap[nid] || []).forEach(d => focusConnected.add(d));
  focusConnected.forEach(id => {
    const other = nodeIdToMesh[id]?.userData;
    if (!other) return;
    (other.imports_from || []).forEach(d => { if (!focusConnected.has(d)) focusSecondary.add(d); });
    (reverseDepMap[id] || []).forEach(d => { if (!focusConnected.has(d)) focusSecondary.add(d); });
  });

  DATA.edges.forEach(e => {
    if (e.source === nid || e.target === nid) focusEdgeSet.add(e.source + '|' + e.target);
  });

  buildingMeshes.forEach(m => {
    const id = m.userData.id;
    if (m === mesh) {
      m.userData._targetScale = 1.05;
      m.userData._targetEmissive = 0.6;
      m.userData._targetOpacity = m.userData._originalOpacity;
      if (m.userData._glowMesh) m.userData._glowMesh.userData._targetOpacity = 0.25;
    } else if (focusConnected.has(id)) {
      m.userData._targetScale = 1;
      m.userData._targetEmissive = 0.3;
      m.userData._targetOpacity = Math.min(1, (m.userData._originalOpacity || 1) * 0.85);
      if (m.userData._glowMesh) m.userData._glowMesh.userData._targetOpacity = 0;
    } else if (focusSecondary.has(id)) {
      m.userData._targetScale = 1;
      m.userData._targetEmissive = 0.1;
      m.userData._targetOpacity = Math.min(1, (m.userData._originalOpacity || 1) * 0.5);
      if (m.userData._glowMesh) m.userData._glowMesh.userData._targetOpacity = 0;
    } else {
      m.userData._targetScale = 1;
      m.userData._targetEmissive = 0.02;
      m.userData._targetOpacity = 0.12;
      if (m.userData._glowMesh) m.userData._glowMesh.userData._targetOpacity = 0;
    }
  });

  edgeEntries.forEach(({ line, glowLine, source, target }) => {
    const key = source + '|' + target;
    const key2 = target + '|' + source;
    if (focusEdgeSet.has(key) || focusEdgeSet.has(key2)) {
      line.userData._targetOpacity = 0.6;
      glowLine.userData._targetOpacity = 0.35;
      line.userData._pulse = true;
      glowLine.userData._pulse = true;
    } else {
      line.userData._targetOpacity = 0;
      glowLine.userData._targetOpacity = 0;
      line.userData._pulse = false;
      glowLine.userData._pulse = false;
    }
  });

  document.getElementById('panel-overview').style.display = 'none';
  document.getElementById('panel-detail').style.display = 'block';
  document.getElementById('d-name').textContent = node.label;
  document.getElementById('d-path').textContent = node.id;
  const badge = document.getElementById('d-badge');
  badge.textContent = node.risk_tag;
  badge.className = 'risk-badge ' + riskClass(node.risk_tag);

  let fhtml = '';
  for (const [k, v] of Object.entries(node.risk_factors)) {
    const pct = (v * 100).toFixed(0);
    const c = riskColorHex(v);
    fhtml += '<div class="factor-item"><span class="factor-label">' + k.replace(/_/g,' ') +
      '</span><div class="factor-bar-bg"><div class="factor-bar" style="width:' + pct + '%;background:' + c +
      '"></div></div><span class="factor-pct">' + pct + '%</span></div>';
  }
  document.getElementById('d-factors').innerHTML = fhtml;

  let shtml = '';
  (node.symbols || []).forEach(s => {
    const dead = s.is_dead ? '<span class="symbol-dead">[dead]</span>' : '';
    shtml += '<div class="symbol-item"><span><span class="symbol-name">' + s.name + '</span> ' + dead +
      '</span><span class="symbol-kind">' + s.kind + '</span></div>';
  });
  document.getElementById('d-symbols').innerHTML = shtml || '<div style="color:var(--text-muted);font-size:12px">No exported symbols</div>';

  const nodeById = {};
  DATA.nodes.forEach(n => { nodeById[n.id] = n; });
  let dhtml = '<div style="font-size:10px;color:var(--text-muted);margin-bottom:4px">Imports from:</div>';
  (node.imports_from || []).forEach(d => {
    const c = riskColorHex((nodeById[d] && nodeById[d].risk_score) ?? 0);
    dhtml += '<div><span class="dep-dot" style="background:' + c + '"></span>' + d + '</div>';
  });
  dhtml += '<div style="font-size:10px;color:var(--text-muted);margin:8px 0 4px">Imported by:</div>';
  (node.imported_by || []).forEach(d => {
    const c = riskColorHex((nodeById[d] && nodeById[d].risk_score) ?? 0);
    dhtml += '<div><span class="dep-dot" style="background:' + c + '"></span>' + d + '</div>';
  });
  document.getElementById('d-deps').innerHTML = dhtml;

  const blastEl = document.getElementById('d-blast');
  if (blastEl) {
    const totalAffected = 1 + focusConnected.size + focusSecondary.size;
    blastEl.innerHTML = '<h2>Blast Radius</h2><div class="blast-outgoing"><span class="blast-label">Outgoing</span><div class="dep-list">' +
      (node.imports_from || []).map(d => { const c = riskColorHex((nodeById[d] && nodeById[d].risk_score) ?? 0); return '<div><span class="dep-dot" style="background:' + c + '"></span>' + d + '</div>'; }).join('') + '</div></div>' +
      '<div class="blast-incoming"><span class="blast-label">Incoming</span><div class="dep-list">' +
      (node.imported_by || []).map(d => { const c = riskColorHex((nodeById[d] && nodeById[d].risk_score) ?? 0); return '<div><span class="dep-dot" style="background:' + c + '"></span>' + d + '</div>'; }).join('') + '</div></div>' +
      '<div class="blast-total">Total affected: <strong>' + totalAffected + '</strong> modules</div>';
    blastEl.style.display = 'block';
  }

  document.getElementById('side-panel').classList.add('open');
}

function deselectBuilding() {
  selectedMesh = null;
  focusMode = false;
  focusConnected = new Set();
  focusSecondary = new Set();
  focusEdgeSet = new Set();
  buildingMeshes.forEach(m => {
    m.userData._targetScale = 1;
    m.userData._targetEmissive = m.userData._originalEmissiveIntensity;
    m.userData._targetOpacity = m.userData._originalOpacity;
    if (m.userData._glowMesh) m.userData._glowMesh.userData._targetOpacity = 0;
  });
  edgeEntries.forEach(({ line, glowLine }) => {
    line.userData._targetOpacity = 0.08;
    glowLine.userData._targetOpacity = 0.04;
    line.userData._pulse = false;
    glowLine.userData._pulse = false;
  });
  document.getElementById('panel-overview').style.display = 'block';
  document.getElementById('panel-detail').style.display = 'none';
  const blastEl = document.getElementById('d-blast');
  if (blastEl) blastEl.style.display = 'none';
  document.getElementById('side-panel').classList.remove('open');
}

// ── Text sprites ───────────────────────────────────
function makeTextSprite(text, scale, color) {
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d');
  canvas.width = 512;
  canvas.height = 64;
  ctx.font = '24px "JetBrains Mono", monospace';
  ctx.fillStyle = color || '#aaaacc';
  ctx.textAlign = 'center';
  ctx.fillText(text, 256, 40);
  const tex = new THREE.CanvasTexture(canvas);
  tex.minFilter = THREE.LinearFilter;
  const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest: false });
  const sprite = new THREE.Sprite(mat);
  sprite.scale.set(scale * 8, scale, 1);
  return sprite;
}

// ── Critical pulse ─────────────────────────────────
const criticalMeshes = buildingMeshes.filter(m => m.userData.risk_score >= 0.7);

// ── Keyboard ───────────────────────────────────────
addEventListener('keydown', e => {
  const key = e.key.toLowerCase();
  if (key === 'r') {
    camera.position.copy(defaultCam.pos);
    controls.target.copy(defaultCam.target);
  } else if (key === 'l') {
    showLabels = !showLabels;
    labelSprites.forEach(s => { s.visible = showLabels; });
  } else if (key === 'f') {
    fogLevel = (fogLevel + 1) % 3;
    updateFog();
  } else if (key === 't') {
    document.documentElement.classList.toggle('light');
    applyTheme();
  } else if (key === 'escape') {
    deselectBuilding();
  } else if (key >= '1' && key <= '4') {
    const level = parseInt(key);
    if (riskFilter === level) {
      riskFilter = null;
      buildingMeshes.forEach(m => { m.material.opacity = m.userData._originalOpacity; });
    } else {
      riskFilter = level;
      buildingMeshes.forEach(m => {
        const s = m.userData.risk_score;
        let matches = false;
        if (level === 1) matches = s < 0.25;
        else if (level === 2) matches = s >= 0.25 && s < 0.45;
        else if (level === 3) matches = s >= 0.45 && s < 0.7;
        else if (level === 4) matches = s >= 0.7;
        m.material.opacity = matches ? m.userData._originalOpacity : 0.08;
        m.material.transparent = true;
      });
    }
  }
});

// ── Button controls ────────────────────────────────
document.getElementById('btn-theme').addEventListener('click', () => {
  document.documentElement.classList.toggle('light');
  applyTheme();
});
document.getElementById('btn-reset').addEventListener('click', () => {
  camera.position.copy(defaultCam.pos);
  controls.target.copy(defaultCam.target);
});
document.getElementById('btn-labels').addEventListener('click', () => {
  showLabels = !showLabels;
  labelSprites.forEach(s => { s.visible = showLabels; });
});
document.getElementById('btn-close-panel').addEventListener('click', () => deselectBuilding());

// ── Resize ─────────────────────────────────────────
function onResize() {
  const w = container.clientWidth;
  const h = container.clientHeight;
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  renderer.setSize(w, h);
  if (composer) composer.setSize(w, h);
}
addEventListener('resize', onResize);
onResize();

// ── Load animation ─────────────────────────────────
buildingMeshes.forEach((mesh, i) => {
  const targetY = mesh.userData._targetY;
  mesh.position.y = 0;
  mesh.scale.y = 0.01;
  const delay = i * 30;
  setTimeout(() => {
    const start = performance.now();
    function rise(now) {
      const t = Math.min(1, (now - start) / 500);
      const ease = 1 - Math.pow(1 - t, 3);
      mesh.position.y = targetY * ease;
      mesh.scale.y = 0.01 + 0.99 * ease;
      if (t >= 1) mesh.userData._riseComplete = true;
      if (t < 1) requestAnimationFrame(rise);
    }
    requestAnimationFrame(rise);
  }, delay);
});

// ── Animate ────────────────────────────────────────
const clock = new THREE.Clock();
const LERP_T = 0.08;
function animate() {
  requestAnimationFrame(animate);
  controls.update();
  const t = clock.getElapsedTime();

  buildingMeshes.forEach(m => {
    const targetO = m.userData._targetOpacity ?? m.userData._originalOpacity;
    const targetE = m.userData._targetEmissive ?? m.userData._originalEmissiveIntensity;
    m.material.opacity += (targetO - m.material.opacity) * LERP_T;
    m.material.emissiveIntensity += (targetE - m.material.emissiveIntensity) * LERP_T;
    if (m.userData._riseComplete) {
      const targetS = m.userData._targetScale ?? 1;
      const s = m.scale.x + (targetS - m.scale.x) * LERP_T;
      m.scale.set(s, s, s);
    }
    const glow = m.userData._glowMesh;
    if (glow && glow.material) {
      const gTarget = glow.userData._targetOpacity ?? 0;
      glow.material.opacity += (gTarget - glow.material.opacity) * LERP_T;
    }
  });

  edgeEntries.forEach(({ line, glowLine }) => {
    const targetL = line.userData._targetOpacity ?? 0.08;
    const targetG = glowLine.userData._targetOpacity ?? 0.04;
    let tl = targetL, tg = targetG;
    if (line.userData._pulse && focusMode) {
      const pulse = 0.15 * Math.sin(t * 3) + 1;
      tl = targetL * pulse;
      tg = targetG * pulse;
    }
    line.material.opacity += (tl - line.material.opacity) * LERP_T;
    glowLine.material.opacity += (tg - glowLine.material.opacity) * LERP_T;
  });

  criticalMeshes.forEach(m => {
    if (m !== selectedMesh && m !== hoveredMesh && !focusMode) {
      m.material.emissiveIntensity = 0.15 + 0.15 * Math.sin(t * Math.PI);
    }
    if (m.userData._antennaTip && m.userData._antennaTip.material) {
      m.userData._antennaTip.material.opacity = 0.7 + 0.3 * Math.sin(t * 2);
    }
  });

  if (composer) composer.render();
  else renderer.render(scene, camera);
}
animate();
</script>
</body>
</html>"""
