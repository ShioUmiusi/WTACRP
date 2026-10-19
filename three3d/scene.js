/* =====================================================================
 * scene3d3.js —— 三维沙盘渲染器（three.js r186）
 * ---------------------------------------------------------------------
 * 与旧 scene3d.js 的关系：完全独立的实现，数据零改动（喂同一份 ACE_MISSION）。
 *
 * 核心约束：
 *   1. **整场景常态渲染**：所有对象 frustumCulled = false，不做任何"只渲染镜头内"的剔除；
 *   2. 飘带是**真三维 Mesh**（LineBasicMaterial 线宽恒为 1px，粗线只能靠三角带）；
 *   3. 静态几何全部合并（等高线/海岸线各一个 LineSegments，击杀 X 一个，网格一个），
 *      重复物体用 InstancedMesh（机体、地面方块）—— draw call 控制在个位数；
 *   4. 全部由时间 t 驱动，不读墙钟（宣传片可逐帧确定性渲染）。
 *
 * 对外接口与旧实现一致：window.ACEScene = { init, render, setCamera, setOption, getReady, getStats }
 * ===================================================================== */
import * as THREE from 'three';
import { OrbitControls } from '../vendor/three/OrbitControls.js';

const COL = {
  own: 0x5bff8a, squad: 0x5bff8a, ally: 0x3d8bff, enemy: 0xff3b47, unknown: 0x6b7c8c,
  ground: 0xc9702a, groundFriend: 0xdce9f5,
  terrain: 0x2a6f9e, coast: 0x8fd0ff, water: 0x2f7fd0,
  kill: 0xff8a3d, grid: 0x18314a, runway: 0xcfe6ff, label: '#cfe3f5'
};
const RIBBON_HW = 55;          /* 飘带世界半宽（米） */
const PLATE_ALT = 720;         /* 地面名牌的世界高度（米）＝ 延长线长度 */
const TRAIL_WIN_AIR = 22;      /* 他机航迹回看秒数 */
const TRAIL_WIN_OWN = 60;
const BANK_WIN = 6;

const state = {
  ready: false, canvas: null, renderer: null, scene: null, camera: null, controls: null,
  meta: null, units: [], own: null, kills: [], runways: [], ground: [], shots: [],
  camMode: 'follow', opts: { grid: true, trails: true, labels: true, map: true, kills: true, water: true },
  labels: [], labelBox: null, t: 0, stats: { units: 0, calls: 0, fps: 0, tris: 0 }
};

/* ------------------------------------------------------------------ 坐标
 * 数据：世界 (东 x, 北 y, 上 z) 米
 * three：(x, 上 z, -北 y) —— 标准右手系，+Y 向上                        */
const V3 = (x, y, z) => new THREE.Vector3(x, z, -y);

/* 标签样式由渲染器自己注入：交付页面不依赖任何外部 CSS */
function injectLabelCss() {
  if (document.getElementById('ac-label-css')) { return; }
  const st = document.createElement('style');
  st.id = 'ac-label-css';
  st.textContent = [
    '.ac-labels{position:absolute;inset:0;pointer-events:none;overflow:hidden;z-index:6}',
  '#controls{z-index:20 !important}',
  '#corner{z-index:20 !important}',
    '.ac-label{position:absolute;left:0;top:0;white-space:nowrap;',
    '  font:11.5px Consolas,"Cascadia Mono",ui-monospace,monospace;letter-spacing:.02em;',
    '  text-shadow:0 0 6px rgba(0,0,0,.9);will-change:transform}',
    '.ac-label.own,.ac-label.squad{color:#5bff8a;font-weight:600}',
    '.ac-label.ally{color:#8fc2ff}',
    '.ac-label.enemy{color:#ff5a66}',
    '.ac-label.unknown{color:#6b7c8b}'
  ].join('\n');
  document.head.appendChild(st);
}

function newMat(color, opts) {
  return new THREE.MeshBasicMaterial(Object.assign({
    color: color, transparent: true, opacity: 0.85, depthWrite: false,
    blending: THREE.AdditiveBlending, side: THREE.DoubleSide
  }, opts || {}));
}

/* ---------------------------------------------------- 静态几何：地形
 * 预烘焙：把水体填充 / 等高线 / 海岸线一次性画进 4096² 离屏画布，再作为纹理贴到地面平面。
 * 为什么不用 ShapeGeometry：本局水岸线是复杂/自交多边形，三角化会画出大面积错误填充（陆地被盖住）。
 * 画布 2D 的填充规则对我们这些轮廓是可靠的，而且"整张地图预先渲染好"正是用户要的形态。 */
const MAP_TEX_PX = 4096;

function buildTerrain(scene, M) {
  const tr = M && M.terrain;
  if (!tr) { return { segs: 0 }; }
  let segs = 0;
  const cpos = [], clpos = [];
  /* 等高线按 elev **抬到真实高度**：地面单位用的是 ECS 的真实 z、跑道用 altM（绝对海拔），
   * 而原来这里全压成 y=0，所以看上去"单位不在 0、线在 0"（2026-10-22 用户指出）。
   * 底色平面保持在 0，不影响；海岸线是海平面，仍留 0。 */
  const TERRAIN_EXAG = 1.0;
  (tr.contours || []).forEach((c) => {
    const y = (typeof c.elev === 'number' ? c.elev : 0) * TERRAIN_EXAG;
    for (const line of (c.lines || [])) {
      const p = line.pts || line;
      for (let j = 0; j + 3 < p.length; j += 2) {
        cpos.push(p[j], y, -p[j + 1], p[j + 2], y, -p[j + 3]);
        segs++;
      }
    }
  });
  (tr.coastlines || []).forEach((c) => {
    const p = c.pts || c;
    for (let j = 0; j + 3 < p.length; j += 2) {
      clpos.push(p[j], 0, -p[j + 1], p[j + 2], 0, -p[j + 3]);
      segs++;
    }
  });
  if (cpos.length) {
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(cpos, 3));
    const lines = new THREE.LineSegments(g, new THREE.LineBasicMaterial({
      color: 0x2a6f9e, transparent: true, opacity: 0.55
    }));
    lines.frustumCulled = false;
    lines.name = 'contours';
    scene.add(lines);
  }
  if (clpos.length) {
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(clpos, 3));
    const lines = new THREE.LineSegments(g, new THREE.LineBasicMaterial({
      color: 0x8fd0ff, transparent: true, opacity: 0.85
    }));
    lines.frustumCulled = false;
    lines.name = 'coast';
    scene.add(lines);
  }
  return { segs: segs };
}

/* ------------------------------------------------------------ 网格地面 */
/* 高度标示：空中单位从机身垂到地面的一条线（AC 风格） */
function buildDrops(scene, maxCount) {
  const arr = new Float32Array(Math.max(1, maxCount) * 2 * 3);
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(arr, 3));
  const lines = new THREE.LineSegments(g, new THREE.LineBasicMaterial({
    color: 0x9fd0ff, transparent: true, opacity: 0.5
  }));
  lines.frustumCulled = false;
  lines.name = 'drops';
  scene.add(lines);
  return { geo: g, arr: arr, mesh: lines, max: Math.max(1, maxCount) };
}

/* 地图边界：白色发光正方框（外框 + 内框两层，做出辉光感） */
function buildBounds(scene, bounds) {
  if (!bounds) { return null; }
  const minX = bounds.minX, maxX = bounds.maxX, minY = bounds.minY, maxY = bounds.maxY;
  const H = 9000;
  const cv = document.createElement('canvas');
  cv.width = 4; cv.height = 128;
  const g = cv.getContext('2d');
  const grad = g.createLinearGradient(0, 0, 0, 128);
  grad.addColorStop(0.00, 'rgba(150,215,255,0)');
  grad.addColorStop(0.78, 'rgba(150,215,255,0.14)');
  grad.addColorStop(0.96, 'rgba(210,240,255,0.50)');
  grad.addColorStop(1.00, 'rgba(255,255,255,0.92)');
  g.fillStyle = grad; g.fillRect(0, 0, 4, 128);
  const tex = new THREE.CanvasTexture(cv);
  /* 保持默认 flipY = true：平面的底边(v=0)去采画布的底行(亮)，所以发光在**底部** */
  const mat = new THREE.MeshBasicMaterial({
    map: tex, transparent: true, opacity: 0.9, side: THREE.DoubleSide,
    depthWrite: false, blending: THREE.AdditiveBlending
  });
  const w = maxX - minX, d = maxY - minY;
  const mk = (width, px, pz, ry, name) => {
    const m = new THREE.Mesh(new THREE.PlaneGeometry(width, H, 1, 1), mat);
    m.rotation.y = ry;
    m.position.set(px, H / 2, pz);
    m.frustumCulled = false;
    m.name = name;
    scene.add(m);
    return m;
  };
  mk(w, (minX + maxX) / 2, -minY, 0, 'boundsWallS');
  mk(w, (minX + maxX) / 2, -maxY, 0, 'boundsWallN');
  mk(d, minX, -(minY + maxY) / 2, Math.PI / 2, 'boundsWallW');
  mk(d, maxX, -(minY + maxY) / 2, Math.PI / 2, 'boundsWallE');
  const pos = [];
  pos.push(minX, 1, -minY, maxX, 1, -minY);
  pos.push(maxX, 1, -minY, maxX, 1, -maxY);
  pos.push(maxX, 1, -maxY, minX, 1, -maxY);
  pos.push(minX, 1, -maxY, minX, 1, -minY);
  const gg = new THREE.BufferGeometry();
  gg.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  const line = new THREE.LineSegments(gg, new THREE.LineBasicMaterial({
    color: 0xdff2ff, transparent: true, opacity: 0.9
  }));
  line.frustumCulled = false;
  line.name = 'bounds';
  scene.add(line);
  return true;
}

/* 地图范围：优先用地形自报的 bounds；**没有地形时按本局数据推算** ——
 * 每局的地图与外框大小都不一样，绝不能落回某一张图的固定范围（例如 ±32768）。 */
function resolveBounds(M) {
  const tb = M && M.terrain && M.terrain.meta && M.terrain.meta.bounds;
  if (tb && isFinite(tb.minX) && isFinite(tb.maxX)) { return tb; }
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  const add = (x, y) => {
    if (!isFinite(x) || !isFinite(y)) { return; }
    if (x < minX) { minX = x; }
    if (x > maxX) { maxX = x; }
    if (y < minY) { minY = y; }
    if (y > maxY) { maxY = y; }
  };
  for (const u of (M && M.units) || []) {
    if (u.x && u.y) { for (let i = 0; i < u.n; i++) { add(u.x[i], u.y[i]); } }
  }
  for (const e of (M && M.events) || []) { add(e.x, e.y); }
  for (const r of (M && M.runways) || []) { add(r.x1, r.y1); add(r.x2, r.y2); }
  const o = M && M.own;
  if (o && o.x && o.y) { for (let i = 0; i < o.n; i++) { add(o.x[i], o.y[i]); } }
  if (!isFinite(minX)) { return { minX: -32768, maxX: 32800, minY: -32768, maxY: 32800 }; }
  const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
  let half = Math.max(maxX - minX, maxY - minY) / 2 * 1.15;
  half = Math.max(half, 4000);
  console.log('[bounds] 无地形数据，按本局实际范围推算：中心(%.0f, %.0f) 半边长 %.0f m', cx, cy, half);
  return { minX: cx - half, maxX: cx + half, minY: cy - half, maxY: cy + half };
}

/* 陆地着色：海岸线+边界框做屏障 → 四角灌水标出"海" → 陆地铺淡色 */
function buildLandFill(scene, M) {
  const tr0 = M && M.terrain;
  if (!tr0) { return null; }
  const b0 = (tr0.meta && tr0.meta.bounds) || resolveBounds(M);

  /* ---- 方案 A：RLE 位图（地图团队推荐） ---- */
  if (tr0.land && tr0.land.encoding === 'rle-rows' && tr0.land.runs) {
    const L = tr0.land, W = L.width | 0, H = L.height | 0;
    const mask = new Uint8Array(W * H);
    let v = (L.startsWith === 0 || L.startsWith === 1) ? L.startsWith : 1;
    let k = 0;
    for (let i = 0; i < L.runs.length && k < mask.length; i++) {
      const n = L.runs[i] | 0;
      if (v === 1) { for (let j = 0; j < n && k < mask.length; j++) { mask[k++] = 1; } }
      else { k += n; }
      v ^= 1;
    }
    let landCount = 0;
    for (let i = 0; i < mask.length; i++) { landCount += mask[i]; }
    const frac = landCount / mask.length;
    console.log('[land] RLE 解码：%dx%d，陆地占比 %.1f%%', W, H, frac * 100);

    /* 自证行序：地面单位应当落在"陆"格上。声明与实际不符时**大声警告**——
     * 地图侧曾把 rowZero 写成 maxY 而数据其实是"行 0 = 南"，导致整体上下颠倒。
     * 这里只做检测与告警，不自动翻转（翻转由 rowZero 决定，避免二次翻转）。 */
    try {
      const gs = (M.units || []).filter(u => u.domain === 'ground' && isFinite(u.x && u.x[0]) === false
        ? false : (u.domain === 'ground' && u.x && u.y && isFinite(u.x[0]) && isFinite(u.y[0])));
      if (gs.length >= 20) {
        const cx = (b0.maxX - b0.minX) / W, cy = (b0.maxY - b0.minY) / H;
        const score = (rowNorth) => {
          let hit = 0, tot = 0;
          for (const u of gs) {
            const col = Math.floor((u.x[0] - b0.minX) / cx);
            const row = rowNorth ? Math.floor((b0.maxY - u.y[0]) / cy)
                                 : Math.floor((u.y[0] - b0.minY) / cy);
            if (col < 0 || col >= W || row < 0 || row >= H) { continue; }
            tot++; if (mask[row * W + col]) { hit++; }
          }
          return tot ? hit / tot : 0;
        };
        const sN = score(true), sS = score(false);
        const _rzLocal = String(L.rowZero || '').toLowerCase();
        const declaredNorth = _rzLocal === 'maxy';
        const declaredScore = declaredNorth ? sN : sS;
        const otherScore = declaredNorth ? sS : sN;
        console.log('[land] 行序自证：地面单位落在陆地 row0=北 %.1f%% / row0=南 %.1f%%（声明 %s，采用 %.1f%%）',
          sN * 100, sS * 100, L.rowZero || '缺失', declaredScore * 100);
        if (otherScore - declaredScore > 0.10) {
          console.warn('[land] ⚠ 声明的 rowZero=%s 与数据不符（另一行序高 %.1f 个百分点）——' +
            '请让地图侧核对其 rowZero 声明，本渲染器只按声明走，避免二次翻转。',
            L.rowZero || '缺失', (otherScore - declaredScore) * 100);
        }
      }
    } catch (e) { console.warn('[land] 行序自证跳过：', e && e.message); }

    const cv2 = document.createElement('canvas');
    cv2.width = W; cv2.height = H;
    const g2 = cv2.getContext('2d');
    const img = g2.createImageData(W, H);
    for (let i = 0; i < mask.length; i++) {
      if (!mask[i]) { continue; }
      const o = i * 4;
      img.data[o] = 26; img.data[o + 1] = 52; img.data[o + 2] = 76; img.data[o + 3] = 165;
    }
    g2.putImageData(img, 0, 0);
    const tex = new THREE.CanvasTexture(cv2);
    /* 行序**以数据自报为准**，不再写死：
     * 地图侧曾把 rowZero 写成 'maxY'，而实际数据是"行 0 = 南(minY)"，导致整体上下颠倒。
     * 现在按 L.rowZero 决定要不要翻：
     *   rowZero === 'maxY'（行 0 = 北） → flipY=false（贴图第 0 行落在 v=1=北）
     *   rowZero === 'minY'（行 0 = 南） → flipY=true （第 0 行落在 v=0=南）
     * 缺失时保守按"行 0 = 北"处理，并在控制台说明。 */
    const _rz = String(L.rowZero || '').toLowerCase();
    /* 实测修正（用户 top 视图指出水平镜像）：本渲染器平面的 v=1 落在**南**侧，
     * 所以 row0=北(maxY) 的数据要 flipY=true 才能把第 0 行放到北；row0=南(minY) 反过来。 */
    tex.flipY = (_rz === 'maxy');
    console.log('[land] rowZero=%s → flipY=%s（数据自报行序，避免二次翻转）',
      L.rowZero || '(缺失)', tex.flipY);
    tex.colorSpace = THREE.SRGBColorSpace;
    const geo = new THREE.PlaneGeometry(b0.maxX - b0.minX, b0.maxY - b0.minY);
    const mesh = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({
      map: tex, transparent: true, opacity: 0.55, depthWrite: false
    }));
    mesh.rotation.x = -Math.PI / 2;
    mesh.position.set((b0.minX + b0.maxX) / 2, 0.6, -(b0.minY + b0.maxY) / 2);
    mesh.frustumCulled = false;
    mesh.name = 'landFill';
    scene.add(mesh);
    state._landFrac = frac;
    return mesh;
  }

  /* ---- 方案 B：闭合环（even-odd） ---- */
  if (tr0.landRings && tr0.landRings.rings && tr0.landRings.rings.length) {
    const N = 1024;
    const cv3 = document.createElement('canvas');
    cv3.width = N; cv3.height = N;
    const g3 = cv3.getContext('2d');
    const sx3 = N / (b0.maxX - b0.minX), sy3 = N / (b0.maxY - b0.minY);
    g3.fillStyle = 'rgba(26,52,76,0.65)';
    g3.beginPath();
    for (const ring of tr0.landRings.rings) {
      if (!ring || ring.length < 6) { continue; }
      g3.moveTo((ring[0] - b0.minX) * sx3, N - (ring[1] - b0.minY) * sy3);
      for (let i = 2; i + 1 < ring.length; i += 2) {
        g3.lineTo((ring[i] - b0.minX) * sx3, N - (ring[i + 1] - b0.minY) * sy3);
      }
      g3.closePath();
    }
    g3.fill('evenodd');
    const tex3 = new THREE.CanvasTexture(cv3);
    tex3.flipY = false;
    tex3.colorSpace = THREE.SRGBColorSpace;
    const geo3 = new THREE.PlaneGeometry(b0.maxX - b0.minX, b0.maxY - b0.minY);
    const mesh3 = new THREE.Mesh(geo3, new THREE.MeshBasicMaterial({
      map: tex3, transparent: true, opacity: 0.6, depthWrite: false
    }));
    mesh3.rotation.x = -Math.PI / 2;
    mesh3.position.set((b0.minX + b0.maxX) / 2, 0.6, -(b0.minY + b0.maxY) / 2);
    mesh3.frustumCulled = false;
    mesh3.name = 'landFill';
    scene.add(mesh3);
    return mesh3;
  }

  /* ---- 回落：海岸线 + 边界灌水近似 ---- */
  const tr = M && M.terrain;
  if (!tr || !tr.coastlines || !tr.coastlines.length) { return null; }
  const b = (tr.meta && tr.meta.bounds) || resolveBounds(M);
  const N = 512;
  const mc = document.createElement('canvas');
  mc.width = N; mc.height = N;
  const mg = mc.getContext('2d');
  mg.fillStyle = '#fff'; mg.fillRect(0, 0, N, N);
  const sx = N / (b.maxX - b.minX), sy = N / (b.maxY - b.minY);
  const px = (x) => (x - b.minX) * sx;
  const py = (y) => N - (y - b.minY) * sy;
  /* 海岸线作为屏障（开放折线，不闭合） */
  mg.strokeStyle = '#000'; mg.lineWidth = 2;
  for (const c of tr.coastlines) {
    const q = c.pts || c;
    if (!q || q.length < 4) { continue; }
    mg.beginPath();
    mg.moveTo(px(q[0]), py(q[1]));
    for (let i = 2; i + 1 < q.length; i += 2) { mg.lineTo(px(q[i]), py(q[i + 1])); }
    mg.stroke();
  }
  /* 地图边界也当屏障：让海域连成一整片 */
  mg.strokeRect(0.5, 0.5, N - 1, N - 1);
  /* 四角灌水（黑色 = 海） */
  const img = mg.getImageData(0, 0, N, N);
  const data = img.data;
  const isBarrier = (i) => (data[i] < 128 && data[i + 1] < 128 && data[i + 2] < 128);
  const seen = new Uint8Array(N * N);
  const stack = [];
  const push = (x, y) => {
    if (x < 0 || y < 0 || x >= N || y >= N) { return; }
    const k = y * N + x;
    if (seen[k]) { return; }
    if (isBarrier(k * 4)) { return; }
    seen[k] = 1; stack.push(k);
  };
  /* 种子要放在边框屏障的**内侧**，否则一个都灌不进去（整张图会被判成陆地） */
  const I = 3;
  push(I, I); push(N - 1 - I, I); push(I, N - 1 - I); push(N - 1 - I, N - 1 - I);
  while (stack.length) {
    const k = stack.pop();
    const x = k % N, y = (k - x) / N;
    push(x + 1, y); push(x - 1, y); push(x, y + 1); push(x, y - 1);
  }
  /* 陆地 = 未被灌到的地方 → 画成淡色 */
  const lc = document.createElement('canvas');
  lc.width = N; lc.height = N;
  const lg = lc.getContext('2d');
  const out = lg.createImageData(N, N);
  for (let k = 0; k < N * N; k++) {
    const sea = seen[k] === 1;
    if (sea) { continue; }
    const o = k * 4;
    out.data[o] = 26; out.data[o + 1] = 52; out.data[o + 2] = 76; out.data[o + 3] = 150;
  }
  lg.putImageData(out, 0, 0);
  const tex = new THREE.CanvasTexture(lc);
  tex.colorSpace = THREE.SRGBColorSpace;
  const geo = new THREE.PlaneGeometry(b.maxX - b.minX, b.maxY - b.minY);
  const mesh = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({
    map: tex, transparent: true, opacity: 0.55, depthWrite: false
  }));
  mesh.rotation.x = -Math.PI / 2;
  mesh.position.set((b.minX + b.maxX) / 2, 0.6, -(b.minY + b.maxY) / 2);
  mesh.frustumCulled = false;
  mesh.name = 'landFill';
  scene.add(mesh);
  return mesh;
}

function buildGrid(scene, bounds) {
  const b = bounds || { minX: -32768, maxX: 32800, minY: -32768, maxY: 32800 };
  const pos = [], step = 2000;
  /* 只在地图边界内画（网格止于边框） */
  for (let x = Math.ceil(b.minX / step) * step; x <= b.maxX; x += step) {
    pos.push(x, 0, -b.minY, x, 0, -b.maxY);
  }
  for (let y = Math.ceil(b.minY / step) * step; y <= b.maxY; y += step) {
    pos.push(b.minX, 0, -y, b.maxX, 0, -y);
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  const lines = new THREE.LineSegments(g, new THREE.LineBasicMaterial({
    color: COL.grid, transparent: true, opacity: 0.42
  }));
  lines.frustumCulled = false;
  lines.name = 'grid';
  lines.renderOrder = 0;
  scene.add(lines);
}

/* -------------------------------------------------- 单位：航迹 + 机体 */
function bankAt(u, i) {
  if (u.roll && u.roll.length) {
    const v = u.roll[Math.min(u.roll.length - 1, Math.max(0, i))];
    if (v != null) { return v * Math.PI / 180; }
  }
  const i0 = Math.max(0, i - BANK_WIN), i1 = Math.min(u.n - 1, i + BANK_WIN);
  if (i1 - i0 < 4) { return 0; }
  const x0 = u.x[i0], y0 = u.y[i0], x1 = u.x[i], y1 = u.y[i], x2 = u.x[i1], y2 = u.y[i1];
  let h1 = Math.atan2(y1 - y0, x1 - x0), h2 = Math.atan2(y2 - y1, x2 - x1);
  let d = h2 - h1;
  while (d > Math.PI) { d -= 2 * Math.PI; }
  while (d < -Math.PI) { d += 2 * Math.PI; }
  const dt = 2 * BANK_WIN / u.hz;
  const v = Math.hypot(x2 - x0, y2 - y0) / dt;
  if (!(v > 20)) { return 0; }
  return Math.atan2(v * (d / (BANK_WIN / u.hz)), 9.80665);
}

/** 采样点的翼展世界向量（单位长度）：up × dir 绕 dir 旋转 bank */
/* 翼展方向：为了去掉他机 4 Hz→10 Hz 重采样带来的抖动，先对航向/横滚做 ±2 平滑 */
function smoothBankAt(u, i) {
  let sum = 0, n = 0;
  for (let k = i - 2; k <= i + 2; k++) {
    if (k < 0 || k > u.n - 1) { continue; }
    sum += bankAt(u, k); n++;
  }
  return n ? (sum / n) : 0;
}

function wingAt(u, i) {
  const i0 = Math.max(0, i - 3), i1 = Math.min(u.n - 1, i + 3);
  let dx = u.x[i1] - u.x[i0], dy = u.y[i1] - u.y[i0], dz = (u.z ? u.z[i1] - u.z[i0] : 0) || 0;
  const dl = Math.hypot(dx, dy, dz) || 1;
  dx /= dl; dy /= dl; dz /= dl;
  let nx = -dy, ny = dx, nz = 0;                /* up(0,0,1) × dir */
  const nl = Math.hypot(nx, ny, nz) || 1; nx /= nl; ny /= nl; nz /= nl;
  /* upPerp = dir × n */
  const ux = dy * nz - dz * ny, uy = dz * nx - dx * nz, uz = dx * ny - dy * nx;
  const b = smoothBankAt(u, i), cb = Math.cos(b), sb = Math.sin(b);   /* 与三角标记同向、全坡度 */
  return [nx * cb + ux * sb, ny * cb + uy * sb, nz * cb + uz * sb];
}

function buildUnit(scene, u) {
  const n = u.n;
  /* --- 飘带 --- */
  const pos = new Float32Array(n * 2 * 3);
  const col = new Float32Array(n * 2 * 4);
  const idx = new Uint32Array(Math.max(0, (n - 1) * 6));
  for (let i = 0; i < n - 1; i++) {
    const a = i * 2, b = a + 1, c = a + 2, d = a + 3, o = i * 6;
    idx[o] = a; idx[o + 1] = b; idx[o + 2] = c;
    idx[o + 3] = b; idx[o + 4] = d; idx[o + 5] = c;
  }
  const geo = new THREE.BufferGeometry();
  geo.setIndex(new THREE.BufferAttribute(idx, 1));
  geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  geo.setAttribute('color', new THREE.BufferAttribute(col, 4));
  /* 有效阵营：本机 / 小队 → 绿；其余按 side */
  const color = (u.isOwn || u.isSquad) ? COL.own
              : (u.side === 'enemy' ? COL.enemy : (u.side === 'ally' ? COL.ally : COL.unknown));
  const mat = new THREE.MeshBasicMaterial({
    vertexColors: true, transparent: true, opacity: 0.9, side: THREE.DoubleSide,
    blending: THREE.AdditiveBlending, depthWrite: false
  });
  const mesh = new THREE.Mesh(geo, mat);
  mesh.frustumCulled = false;
  mesh.renderOrder = 2;
  scene.add(mesh);

  /* --- 机体三角：InstancedMesh --- */
  /* 平面三角（局部 XY 平面：+Y 机头、+Z 法线）。平面才能用滚转侧倾，像真飞机。 */
  const cone = new THREE.BufferGeometry();
  cone.setAttribute('position', new THREE.Float32BufferAttribute([
    0, 0, -1.55,          /* 机头（局部 -Z） */
    -0.85, 0, 0.85,       /* 左翼根 */
    0.85, 0, 0.85         /* 右翼根 */
  ], 3));
  cone.setIndex([0, 1, 2]);
  cone.computeVertexNormals();
  const im = new THREE.InstancedMesh(cone, new THREE.MeshBasicMaterial({
    color: color, transparent: true, opacity: 0.95, depthWrite: false,
    side: THREE.DoubleSide          /* 普通混合：加色会让小三角过曝成白点 */
  }), 1);
  im.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  im.frustumCulled = false;
  im.renderOrder = 3;
  scene.add(im);

  return { geo, pos, col, mesh, im, color };
}

/* ------------------------------------------------------ 地面单位与击杀 */
function buildGround(scene, units) {
  if (!units.length) { return null; }
  /* 方块（InstancedMesh，贴地） */
  const sq = new THREE.PlaneGeometry(1, 1);
  sq.rotateX(-Math.PI / 2);
  const im = new THREE.InstancedMesh(sq, new THREE.MeshBasicMaterial({
    color: COL.ground, transparent: true, opacity: 0.9, depthWrite: false,
    blending: THREE.AdditiveBlending, side: THREE.DoubleSide
  }), units.length);
  im.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  im.frustumCulled = false;
  im.renderOrder = 2;
  scene.add(im);
  /* 延长线：每帧按"聚合阵地中心"重建（一个阵地一条，直连标牌） */
  const cap = Math.max(1, units.length) * 2 * 3;
  const arr = new Float32Array(cap);
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(arr, 3));
  const stems = new THREE.LineSegments(g, new THREE.LineBasicMaterial({
    color: COL.ground, transparent: true, opacity: 0.6
  }));
  stems.frustumCulled = false;
  stems.name = 'gstems';
  scene.add(stems);
  return { im, stems, geo: g, arr: arr, cap: Math.max(1, units.length) };
}

function buildKills(scene, killsIn) {
  const list = (killsIn || []).filter(k => isFinite(k.x));
  const maxVerts = Math.max(1, list.length) * 3 * 2;          /* 每个击杀 3 段（X 两段 + 高度线一段） */
  const arr = new Float32Array(maxVerts * 3);
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(arr, 3));
  const mesh = new THREE.LineSegments(g, new THREE.LineBasicMaterial({
    color: COL.kill, transparent: true, opacity: 0.95
  }));
  mesh.frustumCulled = false;
  mesh.name = 'kills';
  scene.add(mesh);
  state._kills = { list: list, geo: g, mesh: mesh, arr: arr, max: maxVerts };
  return mesh;
}

/* 每帧重建：只画"已经发生"的击杀；空中目标面向相机，地面目标朝天 */
function updateKills(t) {
  const K = state._kills;
  if (!K) { return; }
  const cam = state.camera;
  const cr = new THREE.Vector3().setFromMatrixColumn(cam.matrixWorld, 0);
  const cu = new THREE.Vector3().setFromMatrixColumn(cam.matrixWorld, 1);
  const R = 90;
  const arr = K.arr;
  let n = 0;
  function push(ax, ay, az, bx2, by2, bz2) {
    if (n * 3 + 6 > arr.length) { return; }
    arr[n * 3] = ax; arr[n * 3 + 1] = ay; arr[n * 3 + 2] = az;
    arr[n * 3 + 3] = bx2; arr[n * 3 + 4] = by2; arr[n * 3 + 5] = bz2;
    n += 2;
  }
  for (const k of K.list) {
    if (k.t != null && t < k.t) { continue; }                 /* 击杀前不出现 */
    const c = V3(k.x, k.y, (k.z || 0) + 6);
    if ((k.z || 0) > 100) {
      /* 空中：用相机右/上向量在屏幕内画 X */
      const a = c.clone().addScaledVector(cr, -R).addScaledVector(cu, -R);
      const b = c.clone().addScaledVector(cr, R).addScaledVector(cu, R);
      const d = c.clone().addScaledVector(cr, -R).addScaledVector(cu, R);
      const e = c.clone().addScaledVector(cr, R).addScaledVector(cu, -R);
      push(a.x, a.y, a.z, b.x, b.y, b.z);
      push(d.x, d.y, d.z, e.x, e.y, e.z);
    } else {
      /* 地面：朝天（水平面内的 X）。three: 位置 = (x, 高度, -y) */
      const gz = c.y;
      push(c.x - R, gz, -k.y + R, c.x + R, gz, -k.y - R);
      push(c.x - R, gz, -k.y - R, c.x + R, gz, -k.y + R);
    }
    /* 高度线：从击杀点垂到地面 */
    push(c.x, c.y, c.z, c.x, 0, c.z);
  }
  K.geo.setDrawRange(0, n);
  K.geo.attributes.position.needsUpdate = true;
  K.geo.computeBoundingSphere();
  K.mesh.visible = (n > 0);
}

function buildAirfields(scene, af) {
  /* 机场位置十字已移除（用户指出那是多余的装饰）；跑道本身用 I 字表示 */
  return null;
}

function buildRunways(scene, rw) {
  if (!rw || !rw.length) { return null; }
  const pos = [];
  for (const r of rw) {
    const x1 = r.x1, y1 = r.y1, z1 = r.z1 || 0, x2 = r.x2, y2 = r.y2, z2 = r.z2 || 0;
    const dx = x2 - x1, dy = y2 - y1, len = Math.hypot(dx, dy) || 1;
    const ux = dx / len, uy = dy / len;
    const px = -uy, py = ux;
    const HALF = 45;         /* 粗线半宽（米） */
    const CAP = 380;         /* 端横杠半长（米） */
    for (const o of [-HALF, 0, HALF]) {
      pos.push(x1 + px * o, z1, -(y1 + py * o), x2 + px * o, z2, -(y2 + py * o));
    }
    for (const e of [[x1, y1, z1], [x2, y2, z2]]) {
      const cx = e[0], cy = e[1], cz = e[2];
      pos.push(cx + px * CAP, cz, -(cy + py * CAP), cx - px * CAP, cz, -(cy - py * CAP));
      pos.push(cx + px * CAP * 0.9 + ux * 45, cz, -(cy + py * CAP * 0.9 + uy * 45),
               cx - px * CAP * 0.9 + ux * 45, cz, -(cy - py * CAP * 0.9 + uy * 45));
    }
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  const lines = new THREE.LineSegments(g, new THREE.LineBasicMaterial({
    color: COL.runway, transparent: true, opacity: 0.95
  }));
  lines.frustumCulled = false;
  lines.name = 'runways';
  scene.add(lines);
  return lines;
}

/* ---------------------------------------------------------------- 标签 */
function labelClass(u) {
  if (u.isOwn) { return 'own'; }
  if (u.isSquad) { return 'squad'; }
  return (u.side === 'enemy' || u.side === 'ally') ? u.side : 'unknown';
}

function buildLabels(container, units, own) {
  const list = [];
  function mk(text, cls) {
    const d = document.createElement('div');
    d.className = 'ac-label ' + (cls || '');
    d.textContent = text;
    container.appendChild(d);
    return d;
  }
  if (own) { list.push({ u: null, own: true, el: mk(own, 'own') }); }
  for (const u of units) {
    if (!u.label) { continue; }
    list.push({ u: u, el: mk(u.label, labelClass(u)) });
  }
  return list;
}

/* ------------------------------------------------------------ 主入口 */
let _squadCache = null;
function squadMeta() {
  if (_squadCache === null) {
    _squadCache = (window.ACE_MISSION && window.ACE_MISSION.meta && window.ACE_MISSION.meta.squad) || {};
  }
  return _squadCache;
}
function squadRankOf(uid) {
  const mem = squadMeta().members || [];
  let rank = 0;
  for (let i = 0; i < mem.length; i++) {
    const m = mem[i];
    if (!m || m.uid == null) { continue; }
    if (m.isAuthor || m.self) { continue; }
    rank++;
    if (String(m.uid) === String(uid)) { return rank; }
  }
  return null;
}

function prepUnits(M) {
  /* 小队 uid（来自结算表 squadId）：标为绿色 */
  const squadUids = new Set();
  const sq = (M.meta && M.meta.squad) || null;
  if (sq && sq.members) {
    sq.members.forEach(m => { if (m && m.uid != null) { squadUids.add(String(m.uid)); } });
  }
  const ownUid = (sq && sq.authorUid != null) ? String(sq.authorUid) : null;
  const ownD = M.own || null;
  const own = ownD ? {
    isOwn: true, key: 'own', n: ownD.n, hz: ownD.hz, t0: ownD.t0,
    x: ownD.x, y: ownD.y, z: ownD.z, hdg: ownD.hdg, roll: ownD.roll,
    side: 'ally', label: (M.names && M.names.own) || 'ALPHA 01',
    uid: null, deathT: null, endT: ownD.t0 + (ownD.n - 1) / ownD.hz
  } : null;

  const units = [];
  for (const u of (M.units || [])) {
    if (u.hidden === true) { continue; }
    const dom = (u.domain === 'ground') ? 'ground' : 'air';
    const n = u.n | 0, hz = u.hz || 10, t0 = u.t0 || 0;
    let deathT = null;
    const rt = (M.meta && M.meta.replayTicks) || {};
    if (u.deathTickRaw != null && rt.secPerTick) { deathT = (u.deathTickRaw - (rt.tick0 || 0)) * rt.secPerTick; }
    units.push({
      isOwn: false, key: u.key, uid: u.uid, domain: dom, side: u.side || 'unknown',
      n: n, hz: hz, t0: t0, x: u.x, y: u.y, z: u.z, hdg: u.hdg, roll: null,
      label: (u.vehicleName || u.vehicle || u.entityId || '').toString().toUpperCase().slice(0, 22),
      att: (window.ACE_ATTITUDE && window.ACE_ATTITUDE.units && window.ACE_ATTITUDE.units['uid-' + u.uid]) || null,
      squadRank: squadRankOf(u.uid),
      category: u.vehicleCategory || '', deathT: deathT, endT: t0 + (n - 1) / hz,
      isSquad: (u.uid != null && squadUids.has(String(u.uid)) && String(u.uid) !== ownUid) === true
    });
  }
  return { own, units };
}

function init(canvas, M, opts) {
  if (!canvas) { throw new Error('init: 缺少 canvas'); }
  state.canvas = canvas;
  /* preserveDrawingBuffer: 让 WebGL 画布在合成后仍可被 drawImage 读回。
     * 宣传片（promo）要把这块画布 drawImage 到 2D 主画布上，默认 false 时缓冲可能已被清空
     * （表现为偶发黑块 / 帧间抖动）。对产品页只多一点点显存开销。 */
  const renderer = new THREE.WebGLRenderer({ canvas: canvas, antialias: true, alpha: false, preserveDrawingBuffer: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setClearColor(0x05080d, 1);
  state.renderer = renderer;

  const scene = new THREE.Scene();
  state.scene = scene;
  const camera = new THREE.PerspectiveCamera(52, 1, 20, 500000);
  camera.position.set(0, 26000, 30000);
  state.camera = camera;

  const controls = new OrbitControls(camera, canvas);
  controls.enabled = false;      /* 默认代码驱动相机；free 模式才交给 OrbitControls */
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.maxPolarAngle = Math.PI * 0.495;
  state.controls = controls;

  state.meta = (M && M.meta) || {};
  const p = prepUnits(M || {});
  state.own = p.own;
  state.units = p.units;

  /* 换回放必须把相机状态清干净：否则会拿**上一局**的重锚点去比，
   * 开局就误判"55 km 瞬移"→ 一上来进远景并来回切换（2026-10-22 用户反馈）。 */
  try {
    CAMS.oSinit = false; CAMS.init = false; CAMS.hold = false; CAMS.cut = false;
    CAMS.cutAt = null; CAMS.spin = null; CAMS.over = null; CAMS.wasNull = false;
    CAMS.gapDone = {};
  } catch (e) { /* CAMS 在模块后面定义，运行时已就绪 */ }

  /* 本机在"选择挂载/重生界面"期间采样是 null（实测 TEST1：t=31–34 s 全 null，飞机停在机场 1.5 km 处、速度为 0）。
   * 把 ≥1.5 s 的空档记下来，播放到这些窗口时切"战场全局远景" —— 比"位移>800 m"更准，
   * 能覆盖"原地换挂载（位置没变）"这种情况。 */
  (function computeOwnGaps() {
    const own = state.own;
    state.ownGaps = [];
    if (!own || !own.x) { return; }
    const hz = own.hz || 4, tbase = own.t0 || 0, gaps = [];
    let runStart = -1;
    for (let i = 0; i < own.n; i++) {
      const isNull = own.x[i] === null;
      if (isNull && runStart < 0) { runStart = i; }
      if (!isNull && runStart >= 0) {
        if ((i - runStart) / hz >= 1.5) { gaps.push({ a: tbase + runStart / hz, b: tbase + i / hz }); }
        runStart = -1;
      }
    }
    state.ownGaps = gaps;
    if (gaps.length) {
      console.log('[cam] 本机空档(装载/重生) %d 段：%s', gaps.length,
        gaps.map(g => g.a.toFixed(1) + '–' + g.b.toFixed(1) + 's').join(', '));
    }
  })();

  /* 地图侧交付的陆地掩膜（wt-landmask/1）：**先挂**（它的 bounds 可能更全），再算网格与外框。
   * 产品页会按本局关卡名动态喂 window.ACE_LANDMASK；地图名不一致则忽略（防止串图）。 */
  if (window.ACE_LANDMASK && window.ACE_LANDMASK.land) {
    const LM = window.ACE_LANDMASK;
    const lvl = ((M.meta && M.meta.replayHeader && M.meta.replayHeader.level) || '').toLowerCase();
    const lm = (LM.map || '').toLowerCase();
    if (!lm || !lvl || lvl.indexOf(lm) >= 0) {
      if (!M.terrain) { M.terrain = { meta: {}, contours: [], water: [], coastlines: [] }; }
      M.terrain.land = LM.land;
      if (LM.bounds) {
        M.terrain.meta = M.terrain.meta || {};
        M.terrain.meta.bounds = LM.bounds;
      }
    } else {
      console.warn('[land] 掩膜地图名与本局关卡不一致，已忽略：', LM.map, 'vs', lvl);
    }
  }
  {
    const tb0 = resolveBounds(M);
    state.bounds = tb0;
    console.log('[bounds] 网格/外框范围：x[%.0f, %.0f] y[%.0f, %.0f]（%.0f x %.0f m）',
      tb0.minX, tb0.maxX, tb0.minY, tb0.maxY, tb0.maxX - tb0.minX, tb0.maxY - tb0.minY);
    buildGrid(scene, tb0);
    buildBounds(scene, tb0);
  }
  state.landFill = buildLandFill(scene, M);
  state.terrainInfo = buildTerrain(scene, M || {});
  buildKills(scene, (M.events || []).filter(e => e.kind === 'kill' && isFinite(e.x)));
  state.runways = buildRunways(scene, (M.runways || []).filter(r => isFinite(r.x1)));
  (function logRunways() {
    const rw = (M.runways || []).filter(r => isFinite(r.x1));
    console.log('[runway] 画了 %d 条（来源 %s）', rw.length,
      rw.length ? (rw[0].source || 'replay-ecs') : '-');
    rw.slice(0, 4).forEach((r, i) => console.log(
      '  #%d %s (%.0f,%.0f)→(%.0f,%.0f) len=%s', i, r.name || r.key,
      r.x1, r.y1, r.x2, r.y2, r.lengthM));
  })();
  state.airfields = state.runways;      /* 机场开关 → 跑道组 */

  const airUnitsForDrops = p.units.filter(u => u.domain === 'air');
  state.drops = buildDrops(scene, airUnitsForDrops.length + 1);
  let _dropN = 0;
  for (const u of airUnitsForDrops) { u.dropIdx = _dropN++; }
  if (p.own) { p.own.dropIdx = _dropN++; }
  const groundUnits = p.units.filter(u => u.domain === 'ground');
  state.ground = buildGround(scene, groundUnits);

  /* 空中单位：飘带 + 机体 */
  const airUnits = p.units.filter(u => u.domain === 'air');
  for (const u of airUnits) { u.vis = buildUnit(scene, u); }
  if (p.own) { p.own.vis = buildUnit(scene, p.own); }
  state.kills = (M.events || []).filter(e => e.kind === 'kill' && isFinite(e.x));

  /* 标签层：换回放时 init 会被再调用一次 —— 必须先把上一份标签层摘掉，
   * 否则旧回放的标签会一直留在 DOM 上（用户报的「换 wrpl 没清掉标签」）。 */
  injectLabelCss();
  try {
    const stale = document.querySelectorAll('.ac-labels');
    for (let i = 0; i < stale.length; i++) { stale[i].parentNode.removeChild(stale[i]); }
  } catch (e) {}
  state.labels = [];
  const box = document.createElement('div');
  box.className = 'ac-labels';
  canvas.parentNode.appendChild(box);
  state.labelBox = box;
  state.labels = buildLabels(box, airUnits.concat(groundUnits), p.own ? p.own.label : null);

  state.ready = true;
  window.__ACE_OPTS = state.opts;   /* 排查用：外部可直接改图层开关 */
  resize();
  return true;
}

function resize() {
  const renderer = state.renderer, camera = state.camera;
  if (!renderer || !camera) { return; }
  const el = renderer.domElement;
  const w = el.clientWidth || el.width, h = el.clientHeight || el.height;
  renderer.setSize(w, h, false);
  camera.aspect = (w || 1) / (h || 1);
  camera.updateProjectionMatrix();
}

function setCamera(mode) {
  if (state.controls) { state.controls.enabled = (mode === 'free'); }
  state.camMode = mode || 'follow';
  CAMS.spin = null;          /* 切机位时让环绕/俯视的方位角重新起算 */
  CAMS.init = false;         /* 下一帧直接贴合，不做长距离插值 */
}
function setOption(k, v) { if (k in state.opts) { state.opts[k] = v; } }

/* ------------------------------------------------------------- 每帧 */
const _v = new THREE.Vector3(), _q = new THREE.Quaternion();
const _upAxis = new THREE.Vector3(0, 1, 0);
const _fwdAxisZ = new THREE.Vector3(0, 0, 1);   /* 机头轴（局部 Z） */
const _m = new THREE.Matrix4(), _s = new THREE.Vector3(), _p = new THREE.Vector3();

function sampleAt(u, t, out) {
  if (t < u.t0 || t > u.endT + 0.5) { return false; }
  const fi = (t - u.t0) * u.hz;
  const i0 = Math.floor(fi), i1 = Math.min(u.n - 1, i0 + 1), f = fi - i0;
  if (i0 < 0 || i0 > u.n - 1) { return false; }
  if (!validAt(u, i0) || !validAt(u, i1)) {
    const gp = gapAround(u, i0);
    if (!gp) { return false; }
    const a = gp[0] - 1, b = gp[1];
    if (a < 0 || b >= u.n || (b - a) / u.hz > BRIDGE_GAP_S) { return false; }
    const tt = (fi - a) / (b - a);
    out.x = u.x[a] + (u.x[b] - u.x[a]) * tt;
    out.y = u.y[a] + (u.y[b] - u.y[a]) * tt;
    const za = u.z ? u.z[a] : 0, zb = u.z ? u.z[b] : 0;
    out.z = za + (zb - za) * tt;
    return true;
  }
  const z0 = u.z ? u.z[i0] : 0, z1 = u.z ? u.z[i1] : 0;
  out.x = u.x[i0] + (u.x[i1] - u.x[i0]) * f;
  out.y = u.y[i0] + (u.y[i1] - u.y[i0]) * f;
  out.z = z0 + (z1 - z0) * f;
  return true;
}

/* ---------------------------------------------------------------- 采样
 * 三次样条（Catmull-Rom）：位置 / 航向（解缠）/ 横滚 全部连续可导。
 * 为什么航向要"解缠"：罗盘角跨 0/360 时直接插值会出现 359→1 的跳变（看起来像突然掉头）。
 */
const CLAMP_BANK = 1.05;         /* ±60°：反推的坡度限幅 */
const DEG = Math.PI / 180;

function cr(p0, p1, p2, p3, f) {
  /* Catmull-Rom（张力 0.5） */
  const f2 = f * f, f3 = f2 * f;
  return 0.5 * ((2 * p1) + (-p0 + p2) * f +
                (2 * p0 - 5 * p1 + 4 * p2 - p3) * f2 +
                (-p0 + 3 * p1 - 3 * p2 + p3) * f3);
}

function ensureSeries(u) {
  if (u._series) { return; }
  const n = u.n;
  /* 横滚：本机有真值；他机由位移反推，然后 5 点平均 + 限幅 */
  const bank = new Float32Array(n);
  for (let k = 0; k < n; k++) {
    let b;
    if (u.roll && u.roll[k] != null) { b = u.roll[k] * DEG; }
    else { b = bankAt(u, k); }
    if (!isFinite(b)) { b = 0; }
    if (b > CLAMP_BANK) { b = CLAMP_BANK; }
    if (b < -CLAMP_BANK) { b = -CLAMP_BANK; }
    bank[k] = b;
  }
  const bankSm = new Float32Array(n);
  for (let k = 0; k < n; k++) {
    let sum = 0, c = 0;
    for (let j = Math.max(0, k - 2); j <= Math.min(n - 1, k + 2); j++) { sum += bank[j]; c++; }
    bankSm[k] = sum / c;
  }
  /* 低速压平：只在实际低速（如机场重生滑行）时生效；空中出生速度高，不触发 */
  const _spd = (u._stepMed || 30) * (u.hz || 10);
  const _speedW = Math.max(0, Math.min(1, (_spd - 40) / 80));
  if (_speedW < 1) {
    for (let k = 0; k < n; k++) { bankSm[k] *= _speedW; }
  }

  /* 若交付里有 FM(EulerBytes) 的 bank → 用它作为机体滚转（用户决定肉眼验收） */
  if (u.att && u.att.bank && u.att.bank.length) {
    const A = u.att, bt = A.t0 || 0, bdt = A.dt || 0.25;
    for (let k = 0; k < n; k++) {
      const fi = ((u.t0 + k / (u.hz || 10)) - bt) / bdt;
      if (fi < 0 || fi > A.bank.length - 1) { continue; }
      const i0 = Math.floor(fi), i1 = Math.min(A.bank.length - 1, i0 + 1), f = fi - i0;
      let b = (A.bank[i0] + (A.bank[i1] - A.bank[i0]) * f) * Math.PI / 180;
      if (b > CLAMP_BANK) { b = CLAMP_BANK; }
      if (b < -CLAMP_BANK) { b = -CLAMP_BANK; }
      bankSm[k] = b;
    }
    /* 按 wrpl-acmi 的口径定标：**幅度取 FM 的 |bank|，符号取位置转弯方向**。
     * （wrpl-acmi/cmd/acmi/attitude_test.go 断言：右转时 roll 与转弯同号，
     *   且 WT 的 bank 符号与 ACMI 相反 → aircraftRollToACMI(b) = -b。）
     * 再做 5 点平滑 + 3° 死区：只表现大机动，滤掉小抖动。 */
    const tmpB = Float32Array.from(bankSm);
    for (let k = 0; k < n; k++) {
      let sum = 0, c = 0;
      for (let j = Math.max(0, k - 2); j <= Math.min(n - 1, k + 2); j++) { sum += tmpB[j]; c++; }
      const mag = Math.abs(sum / c);
      /* 位置转弯方向（±0.35 s 两端切线，压噪声） */
      const i0 = Math.max(0, k - 4), i1 = Math.min(n - 1, k + 4);
      let sgn = 0;
      if (i1 - i0 >= 4) {
        const ddx = u.x[i1] - u.x[i0], ddy = u.y[i1] - u.y[i0];
        const iA = Math.max(0, i0 - 4), iB = Math.min(n - 1, i0 + 1);
        const iC = Math.max(0, i1 - 1), iD = Math.min(n - 1, i1 + 4);
        const h0 = Math.atan2(u.y[iB] - u.y[iA], u.x[iB] - u.x[iA]);
        const h1 = Math.atan2(u.y[iD] - u.y[iC], u.x[iD] - u.x[iC]);
        let d = h1 - h0;
        while (d > Math.PI) { d -= 2 * Math.PI; }
        while (d < -Math.PI) { d += 2 * Math.PI; }
        sgn = Math.abs(d) > 0.03 ? Math.sign(d) : 0;
      }
      const fmSgn = (sum / c) >= 0 ? 1 : -1;
      let v = (sgn !== 0 ? sgn : fmSgn) * mag;
      if (Math.abs(v) < 0.052) { v = 0; }
      bankSm[k] = v;
    }
  }

  /* 航向解缠：以第一个有效值为基准，逐个累加最近角的增量 */
  const hdgU = new Float32Array(n);
  let ref = null;
  for (let k = 0; k < n; k++) {
    const h = (u.hdg && u.hdg[k] != null) ? u.hdg[k] : null;
    if (h == null) { hdgU[k] = ref == null ? 0 : ref; continue; }
    let hh = h;
    if (ref != null) {
      while (hh - ref > 180) { hh -= 360; }
      while (hh - ref < -180) { hh += 360; }
    }
    hdgU[k] = hh; ref = hh;
  }
  u._series = { bank: bankSm, hdg: hdgU };
}

/** 三次样条采样：位置 + 航向(弧度) + 横滚(弧度)；同一 t 结果唯一（可用于逐帧确定性渲染） */
/* ---- 断口处理（2026-10-22）--------------------------------------------
 * 问题：FM 通道只上报"关注范围内"的飞机，飞远了会出现 null 断口（TEST4 里 22/97 架有，
 * 最大 4.1 km / 32 s）。而 `sampleFull` / `sampleAt` 直接对数组做算术，
 * JS 里 `null` 会被当 0 → 断口期间会被插到**坐标原点**再飞回来，看起来像"瞬移"。
 * 处理：短断口（≤3 s）线性桥接；长断口一律返回"无采样"（不猜）。
 * -------------------------------------------------------------------- */
const BRIDGE_GAP_S = 3.0;

function ensureValid(u) {
  if (u._valid) { return; }
  const n = u.n, v = new Uint8Array(n), gaps = [];
  for (let i = 0; i < n; i++) { v[i] = (u.x[i] !== null && u.y[i] !== null && isFinite(u.x[i])) ? 1 : 0; }
  let i = 0;
  while (i < n) {
    if (v[i]) { i++; continue; }
    let j = i;
    while (j < n && !v[j]) { j++; }
    gaps.push([i, j]);            /* [起, 止) —— 止是第一个有效下标 */
    i = j;
  }
  u._valid = v; u._gaps = gaps;
}

function validAt(u, i) {
  ensureValid(u);
  return i >= 0 && i < u.n && u._valid[i] === 1;
}

function gapAround(u, i) {
  ensureValid(u);
  const g = u._gaps;
  let lo = 0, hi = g.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (i < g[mid][0]) { hi = mid - 1; }
    else if (i >= g[mid][1]) { lo = mid + 1; }
    else { return g[mid]; }
  }
  return null;
}

/* 短断口桥接：返回 true 表示已填好 out；长断口或端点缺失返回 false */
function bridgeSample(u, fi, out) {
  const n = u.n;
  const i = Math.min(Math.max(0, Math.floor(fi)), n - 1);
  const gp = gapAround(u, i);
  if (!gp) { return false; }
  const a = gp[0] - 1, b = gp[1];
  if (a < 0 || b >= n) { return false; }                      /* 断口贴边：不猜 */
  const gapS = (b - a) / u.hz;
  if (gapS > BRIDGE_GAP_S) { return false; }
  const tt = (fi - a) / (b - a);
  out.x = u.x[a] + (u.x[b] - u.x[a]) * tt;
  out.y = u.y[a] + (u.y[b] - u.y[a]) * tt;
  const za = u.z ? u.z[a] : 0, zb = u.z ? u.z[b] : 0;
  out.z = za + (zb - za) * tt;
  out.hdg = Math.atan2(u.x[b] - u.x[a], u.y[b] - u.y[a]);      /* 沿桥接方向 */
  out.roll = 0;
  out.bridged = true;
  return true;
}

function sampleFull(u, t, out) {
  ensureSeries(u);
  const fi = (t - u.t0) * u.hz;
  if (fi < 0 || fi > u.n - 1) { return false; }
  const i1 = Math.floor(fi), f = fi - i1;
  out.bridged = false;
  if (!validAt(u, i1) || !validAt(u, Math.min(u.n - 1, i1 + 1))) {
    if (bridgeSample(u, fi, out)) { return true; }
    return false;                                    /* 长断口：不猜，交给上层隐藏 */
  }
  const i0 = Math.max(0, i1 - 1), i2 = Math.min(u.n - 1, i1 + 1), i3 = Math.min(u.n - 1, i1 + 2);
  const X = u.x, Y = u.y, Z = u.z || null;
  out.x = cr(X[i0], X[i1], X[i2], X[i3], f);
  out.y = cr(Y[i0], Y[i1], Y[i2], Y[i3], f);
  out.z = Z ? cr(Z[i0], Z[i1], Z[i2], Z[i3], f) : 0;
  const H = u._series.hdg;
  out.hdg = cr(H[i0], H[i1], H[i2], H[i3], f) * DEG;      /* 回到弧度 */
  const B = u._series.bank;
  out.roll = cr(B[i0], B[i1], B[i2], B[i3], f);
  return true;
}

/* 每单位**预计算一次**平滑后的翼展方向（±2 采样平均），逐帧只做坐标变换。
 * 逐帧重算 wingAt 会把他机 4Hz→10Hz 重采样的抖动放大成"轨迹狂抖"。 */
function updateRibbon(u, t, hwScale) {
  const vis = u.vis;
  if (!vis) { return; }
  const win = u.isOwn ? TRAIL_WIN_OWN : TRAIL_WIN_AIR;
  if (t < u.t0) { vis.geo.setDrawRange(0, 0); vis.im.visible = false; return; }

  const cv = state.renderer.domElement;
  const cvH = Math.max(1, cv.clientHeight || cv.height || 800);
  const _p0 = { x: 0, y: 0, z: 0, hdg: 0, roll: 0 };
  /* 已死亡：飘带**截止在击杀处并保留**（不画残骸螺旋，但历史航迹留痕） */
  const tRibbon = (u.deathT != null && t > u.deathT) ? u.deathT : t;
  if (!sampleFull(u, tRibbon, _p0)) { vis.geo.setDrawRange(0, 0); vis.im.visible = false; return; }
  /* 在地面上（刚重生/机场补给）：不画飘带，避免贴地堆叠出亮点 */
  if ((_p0.z || 0) < 25) { vis.geo.setDrawRange(0, 0); }
  const distNow = state.camera.position.distanceTo(V3(_p0.x, _p0.y, _p0.z));
  const worldPerPx = 2 * distNow * Math.tan((state.camera.fov * Math.PI / 180) / 2) / cvH;
  /* 三角标记的后掠长度（世界米）：飘带头要退到它的尾巴上，不能盖住三角 */
  const _pxPerM0 = cvH / (2 * Math.max(1, distNow) * Math.tan((state.camera.fov * Math.PI / 180) / 2));
  const _markerSc = Math.min(Math.max(28, distNow * 0.006), 14 / Math.max(1e-6, _pxPerM0));
  const HEAD_BACK = _markerSc * 0.95;
  const HW = Math.max(3, 3.5 * worldPerPx) * (hwScale || 1);   /* 半宽减半 */

  /* 每帧按时间重采样：从当前时刻往回 k*step（与三角同帧、同源） */
  const step = 1 / Math.max(4, (u.hz || 10) * 2);
  const NMAX = Math.min(600, Math.round(win / step) + 2, Math.max(2, u.n - 1));
  const pos = vis.pos, col = vis.col;
  const cr_ = ((vis.color >> 16) & 255) / 255, cg_ = ((vis.color >> 8) & 255) / 255, cb_ = (vis.color & 255) / 255;
  const P = { x: 0, y: 0, z: 0, hdg: 0, roll: 0 };
  let w = 0, prev = null, prevAxis = null, firstPt = null, pathLen = 0;
  const qy = new THREE.Quaternion(), qr = new THREE.Quaternion(), axis = new THREE.Vector3();

  for (let k = 0; k < NMAX; k++) {
    const tt = tRibbon - k * step;
    if (tt < u.t0 + 0.4) { break; }      /* 最开头 0.4 s 不画：样条端点钳制 + bank 无邻点 → 抖动 */
    if (!sampleFull(u, tt, P)) { break; }
    if (Math.abs(P.x) < 60 && Math.abs(P.y) < 60) { break; }
    if (prev) {
      const dx = P.x - prev.x, dy = P.y - prev.y, dz = P.z - prev.z;
      const d = Math.sqrt(dx * dx + dy * dy + dz * dz);
      if (d > Math.max(60, (u._stepMed || 30) * 4)) { break; }
      pathLen += d;
    }
    prev = { x: P.x, y: P.y, z: P.z };

    /* 带宽方向 = 与三角同一四元数的局部 X 轴（= 右侧机翼方向）。
     * 机翼线扫出的曲面：左右两条边就是左右翼尖。
     * **连续性**：FM 的 bank 可能跨越 ±180°，会让翼轴反向；与上一点点积为负就整体翻转，
     * 否则左右边互换、曲面自交（就是之前那堆交叉直线）。 */
    qy.setFromAxisAngle(_upAxis, -(P.hdg || 0));
    qr.setFromAxisAngle(_fwdAxisZ, P.roll || 0);
    qy.multiply(qr);
    axis.set(1, 0, 0).applyQuaternion(qy);
    if (prevAxis) {
      const d0 = axis.x * prevAxis.x + axis.y * prevAxis.y + axis.z * prevAxis.z;
      if (d0 < 0) { axis.multiplyScalar(-1); }
    }
    if (!prevAxis) { prevAxis = new THREE.Vector3(); }
    prevAxis.copy(axis);

    /* three 空间：位置 = (x, z, -y)；翼轴 axis 也是 three 空间向量，所以直接相加。
     * （之前把 -P.y + az 写成了 -(P.y + az)，两条边的竖向偏移符号不一致 → 带面被拉歪） */
    let hx = P.x, hy = P.y;
    if (k === 0) {                                   /* 首点沿机头方向退到三角尾巴 */
      const hh = P.hdg || 0;
      hx -= Math.sin(hh) * HEAD_BACK;
      hy -= Math.cos(hh) * HEAD_BACK;
    }
    if (!firstPt) { firstPt = { x: hx, y: hy }; }
    const bx = hx, by = P.z, bz = -hy;
    const kk = w * 6, cc = w * 8;
    pos[kk] = bx + axis.x * HW;
    pos[kk + 1] = by + axis.y * HW;
    pos[kk + 2] = bz + axis.z * HW;
    pos[kk + 3] = bx - axis.x * HW;
    pos[kk + 4] = by - axis.y * HW;
    pos[kk + 5] = bz - axis.z * HW;
    /* 头部实色，尾部渐隐；带体本身仍不透明混色 */
    const fade = Math.max(0.12, 1.0 - 0.88 * (k / Math.max(1, NMAX)));
    col[cc] = cr_; col[cc + 1] = cg_; col[cc + 2] = cb_; col[cc + 3] = fade;
    col[cc + 4] = cr_; col[cc + 5] = cg_; col[cc + 6] = cb_; col[cc + 7] = fade;
    w++;
  }

  /* 原地/刚起飞：窗口内路径 < 350 m 或首尾水平跨度 < 130 m → 不画（消抖动） */
  const _span = (firstPt && prev) ? Math.hypot(prev.x - firstPt.x, prev.y - firstPt.y) : 0;
  vis.geo.setDrawRange(0, (pathLen < 350 || _span < 130) ? 0 : Math.max(0, (w - 1)) * 6);
  vis.geo.attributes.position.needsUpdate = true;
  vis.geo.attributes.color.needsUpdate = true;
  vis.geo.computeBoundingSphere();

  const _unplaced = Math.abs(_p0.x) < 60 && Math.abs(_p0.y) < 60;
  const _okPos = !_unplaced && Math.abs(_p0.x) < 60000 && Math.abs(_p0.y) < 60000 &&
    (u.deathT == null || t <= u.deathT);
  if (_okPos) {
    qy.setFromAxisAngle(_upAxis, -(_p0.hdg || 0));
    qr.setFromAxisAngle(_fwdAxisZ, _p0.roll || 0);
    _q.copy(qy).multiply(qr);
    const pxPerM = cvH / (2 * Math.max(1, distNow) * Math.tan((state.camera.fov * Math.PI / 180) / 2));
    const sc = Math.min(Math.max(28, distNow * 0.006), 14 / Math.max(1e-6, pxPerM));
    _m.compose(V3(_p0.x, _p0.y, _p0.z), _q, _s.set(sc, sc, sc));
    vis.im.setMatrixAt(0, _m);
    vis.im.instanceMatrix.needsUpdate = true;
    vis.im.visible = true;
    const D = state.drops;
    if (D && u.dropIdx != null) {
      const o = u.dropIdx * 6;
      const capAlt = Math.min(Math.max(0, _p0.z), 2500);
      /* <500 m 不画：低空飞机的高度标太短，在机场附近会被看成"奇怪的垂直线" */
      const dropLen = capAlt >= 500 ? capAlt : 0;
      D.arr[o] = _p0.x; D.arr[o + 1] = _p0.z; D.arr[o + 2] = -_p0.y;
      D.arr[o + 3] = _p0.x; D.arr[o + 4] = _p0.z - dropLen; D.arr[o + 5] = -_p0.y;
      D.dirty = true;
    }
  } else {
    vis.im.visible = false;
    vis.im.setMatrixAt(0, _m.makeScale(0, 0, 0));
    vis.im.instanceMatrix.needsUpdate = true;
    const D2 = state.drops;
    if (D2 && u.dropIdx != null) {
      const o2 = u.dropIdx * 6;
      for (let q2 = 0; q2 < 6; q2++) { D2.arr[o2 + q2] = 0; }
      D2.dirty = true;
    }
  }
}

/* ------------------------------------------------------------ 相机运镜
 * 参数与旧 scene3d.js 一致：follow = 后侧 3/4（az 偏 0.45）+ 焦点抬高 600 m + r=7000
 * 并用指数阻尼平滑（位置 k=1-exp(-2.2dt)、角度 k=1-exp(-3dt)、距离 k=1-exp(-1.6dt)）。 */
const FOLLOW = { az: 0.45, el: 0.55, r: 7000, up: 600 };
const ORBIT = { el: 0.62, r: 14000, drift: 0.055 };
const TOP = { el: 1.30, r: 38000, drift: 0.012 };
const CAMS = { fx: 0, fy: 0, fz: 0, az: 0, el: 0.55, r: 7000, init: false,
               oSx: 0, oSy: 0, oSz: 0, oSinit: false, spin: null };

const DEG2 = Math.PI / 180;

function centroid(t, out) {
  let sx = 0, sy = 0, sz = 0, n = 0;
  for (const u of state.units) {
    if (u.domain !== 'air') { continue; }
    if (!sampleAt(u, t, out)) { continue; }
    sx += out.x; sy += out.y; sz += out.z; n++;
  }
  if (!n) { out.x = 0; out.y = 0; out.z = 0; return false; }
  out.x = sx / n; out.y = sy / n; out.z = sz / n;
  return true;
}

function updateCamera(t, dt) {
  const cam = state.camera, ctl = state.controls;
  const mode = state.camMode;

  /* 拖动进度条会瞬间跨很大时间：这一帧**只做重锚**，不判瞬移、也不保留远景。
   * （否则每次 seek 都会被判成"回机场补给"而跳到全图） */
  let seeked = false;
  if (CAMS.lastT != null && Math.abs(t - CAMS.lastT) > 1.0) {
    seeked = true;
    CAMS.oSinit = false;
    CAMS.over = null;
    CAMS.hold = false;
    CAMS.cut = false;
  }
  CAMS.lastT = t;
  /* 本机航迹在断轨处是 null：这段**镜头停在原位**，等新坐标出现再切 */
  if (mode === 'follow' && state.own) {
    const probe = { x: 0, y: 0, z: 0, hdg: 0, roll: 0 };
    if (!sampleAt(state.own, t, probe)) { CAMS.wasNull = true; }
  }
  let fx, fy, fz, az, el, rad;
  /* 装载/重生空档 → 直接进"战场全局远景"（与瞬移共用同一套 CAMS.over） */
  if (state.ownGaps && state.ownGaps.length && !CAMS.gapDone) { CAMS.gapDone = {}; }
  if (state.ownGaps && state.ownGaps.length) {
    for (let gi = 0; gi < state.ownGaps.length; gi++) {
      const g = state.ownGaps[gi];
      /* 每段空档**只触发一次**：否则"远景被提前结束 → 又满足条件 → 再进远景"会来回跳 */
      if (t >= g.a - 0.5 && t <= g.b + 0.6 && !CAMS.gapDone[gi + '']) {
        CAMS.gapDone[gi + ''] = 1;
        CAMS.over = { t0: g.a - 0.5, t1: g.b + 5.0, az: CAMS.az, gap: g };
        break;
      }
    }
  }
  /* 全局远景窗口：本机停下/瞬移期间给"战场全局"，本机重新动起来就提前结束 */
  let _over = false;
  if (CAMS.over && t < CAMS.over.t1 && state.bounds) {
    let moving = false;
    if (state.own) {
      const a = { x: 0, y: 0, z: 0, hdg: 0, roll: 0 };
      const b2 = { x: 0, y: 0, z: 0, hdg: 0, roll: 0 };
      if (sampleAt(state.own, t, a) && sampleAt(state.own, Math.max(0, t - 0.6), b2)) {
        const d = Math.hypot(a.x - b2.x, a.y - b2.y);
        moving = d > 40;                       /* 0.6 s 内移动 >40 m ⇒ 已经在飞 */
      }
    }
    if (moving && t > CAMS.over.t0 + 1.5) {
      CAMS.over = null;                        /* 起飞 → 立刻回原机位 */
    } else {
      const bd = state.bounds;
      const cx = (bd.minX + bd.maxX) / 2, cy = (bd.minY + bd.maxY) / 2;
      const span = Math.max(bd.maxX - bd.minX, bd.maxY - bd.minY);
      fx = cx; fy = 0; fz = -cy;
      az = CAMS.over.az + 0.10 * (t - CAMS.over.t0);
      el = 1.18; rad = span * 0.66;
      _over = true;
    }
  }
  if (_over) {
    /* 远景期间**持续刷新重锚点**：否则退出远景后拿旧锚点一比较就是"几公里跳变"，
     * 又立刻进远景 → 用户看到的"每隔一会儿就切一次"（2026-10-22 反馈）。 */
    if (state.own && sampleAt(state.own, t, _v)) {
      CAMS.oSx = _v.x; CAMS.oSy = _v.y; CAMS.oSz = _v.z; CAMS.oSinit = true;
    }
  } else if (mode === 'follow' && state.own && sampleAt(state.own, t, _v)) {
    if (!CAMS.oSinit) { CAMS.oSx = _v.x; CAMS.oSy = _v.y; CAMS.oSz = _v.z; CAMS.oSinit = true; }
    /* 重生瞬移（机场重生）：一帧内跳变很大 → 镜头**不飞过去**，直接重锚 */
    const jx = _v.x - CAMS.oSx, jy = _v.y - CAMS.oSy, jz = _v.z - CAMS.oSz;
    const jump = Math.sqrt(jx * jx + jy * jy + jz * jz);
    /* 开局 2 s 内不做瞬移判定：第一帧的锚点可能是 null 边界插出来的 (0,0)，
     * 会误判成"55 km 瞬移"→ 一上来就切远景并来回跳（2026-10-22 实测）。 */
    if (jump > 800 && t > 2.0 && !seeked) {
      CAMS.oSx = _v.x; CAMS.oSy = _v.y; CAMS.oSz = _v.z;
      /* 重生会连跳两次：第一次落在**地图外**（重生点）→ 镜头不动；
       * 第二次落在地图内（真出生）→ 才切镜头。 */
      /* 重生（含"先到地图外重生点、再回机场"的两次跳）：镜头**先冻结不动**，
       * 1.2 s 后再一次性切过去；不做长距离插值飞行。 */
      if (CAMS.wasNull) {
        /* null 段刚结束：等新坐标出现再切 */
        CAMS.wasNull = false;
        CAMS.hold = false;
        CAMS.cut = true;
      } else {
        /* 中途瞬移 = 回机场补给/换挂载（典型案例：TEST1 30–36 s 落回机场；
         * skyscraper 06:40–06:43 补给）。这时**不追着飞**，先切到"战场全局"远景，
         * 等本机重新动起来（起飞）再回到原机位。 */
        CAMS.over = { t0: t, t1: t + 14.0, az: CAMS.az };
        CAMS.hold = false;
        CAMS.cut = true;
        console.log('[cam] 检测到瞬移 %.0f m @ t=%.1f s → 切战场全局远景', jump, t);
      }
    } else {
      const kF = 1 - Math.exp(-6 * dt);
      CAMS.oSx += (_v.x - CAMS.oSx) * kF;
      CAMS.oSy += (_v.y - CAMS.oSy) * kF;
      CAMS.oSz += (_v.z - CAMS.oSz) * kF;
    }
    const i = Math.min(state.own.n - 1, Math.max(0, Math.floor((t - state.own.t0) * state.own.hz)));
    const hdg = (state.own.hdg && state.own.hdg[i] != null) ? state.own.hdg[i] : 0;
    fx = CAMS.oSx; fy = CAMS.oSz + FOLLOW.up; fz = -CAMS.oSy;
    az = -(hdg * DEG2) + FOLLOW.az;         /* 机尾后方 + 3/4 偏置 */
    el = FOLLOW.el; rad = FOLLOW.r;
  } else if (mode === 'orbit' || mode === 'top') {
    /* 机位要跟着"正在发生的事"，而不是全图的几何中心：
     * 所有存活空中单位的平均位置在单位铺满全图时会退化到地图中心（用户反馈）。
     * 规则：优先**本机**（与 follow 同源，观感连续），取不到再退回活跃单位重心。 */
    if (!(state.own && sampleAt(state.own, t, _v))) { centroid(t, _v); }
    fx = _v.x; fy = _v.z * (mode === 'orbit' ? 0.5 : 0); fz = -_v.y;
    const P = (mode === 'orbit') ? ORBIT : TOP;
    if (CAMS.spin == null) { CAMS.spin = (state.camMode === 'top') ? 0.6 : 0.9; }
    CAMS.spin += dt * P.drift;
    az = CAMS.spin; el = P.el; rad = P.r;
  } else if (mode === 'free') {
    if (ctl) { ctl.enabled = true; ctl.update(); }
    return;                       /* 自由镜头：相机完全交给 OrbitControls */
  } else {
    fx = CAMS.fx; fy = CAMS.fy; fz = CAMS.fz; az = CAMS.az; el = CAMS.el; rad = CAMS.r;
  }
  if (!CAMS.init) {
    CAMS.fx = fx; CAMS.fy = fy; CAMS.fz = fz; CAMS.az = az; CAMS.el = el; CAMS.r = rad; CAMS.init = true;
  } else if (CAMS.hold) {
    /* 冻结期间镜头完全不动；到 cutAt 后一次性切过去 */
    if (CAMS.cutAt != null && t >= CAMS.cutAt) {
      CAMS.fx = fx; CAMS.fy = fy; CAMS.fz = fz;
      CAMS.hold = false; CAMS.cut = false; CAMS.cutAt = null;
    }
  } else if (CAMS.cut) {
    /* 真出生：位置直接切过去，视角参数保持不变 */
    CAMS.fx = fx; CAMS.fy = fy; CAMS.fz = fz;
    CAMS.cut = false;
    CAMS.hold = false;
  } else {
    const kP = 1 - Math.exp(-2.2 * dt), kA = 1 - Math.exp(-3.0 * dt), kR = 1 - Math.exp(-1.6 * dt);
    CAMS.fx += (fx - CAMS.fx) * kP; CAMS.fy += (fy - CAMS.fy) * kP; CAMS.fz += (fz - CAMS.fz) * kP;
    let da = az - CAMS.az;
    while (da > Math.PI) { da -= 2 * Math.PI; }
    while (da < -Math.PI) { da += 2 * Math.PI; }
    CAMS.az += da * kA; CAMS.el += (el - CAMS.el) * kA; CAMS.r += (rad - CAMS.r) * kR;
  }
  const ce = Math.cos(CAMS.el), se = Math.sin(CAMS.el);
  cam.position.set(CAMS.fx + Math.sin(CAMS.az) * ce * CAMS.r,
                   CAMS.fy + se * CAMS.r,
                   CAMS.fz + Math.cos(CAMS.az) * ce * CAMS.r);
  cam.lookAt(CAMS.fx, CAMS.fy, CAMS.fz);
  ctl.target.set(CAMS.fx, CAMS.fy, CAMS.fz);
}

/* 地面标签文字：category 模式给类别，否则型号 */
function groundLabelText(u) {
  const mode = (identityMode && identityMode.labelMode) || 'category';
  if (mode === 'category') { return (u.category || u.label || '').toString().toUpperCase(); }
  return u.label || '';
}

/* 同类&相近（≤700 m）的地面单位聚成一个标签 */
function buildGroundClusters(t) {
  const list = state.units.filter(u => u.domain === 'ground');
  const groups = [];
  const of = {};
  for (const u of list) {
    const x0 = u.x[0], y0 = u.y[0], z0 = (u.z ? u.z[0] : 0);
    if (!isFinite(x0) || !isFinite(y0)) { continue; }
    const txt = groundLabelText(u);
    let hit = null;
    for (const g of groups) {
      if (g.text !== txt) { continue; }
      const dx = g.x - x0, dy = g.y - y0;
      if (dx * dx + dy * dy <= 700 * 700) { hit = g; break; }
    }
    if (hit) {
      hit.n++; hit.sx += x0; hit.sy += y0; hit.sz += z0;
      hit.x = hit.sx / hit.n; hit.y = hit.sy / hit.n; hit.z = hit.sz / hit.n;
      of[u.key] = hit;
    } else {
      const g = { text: txt, n: 1, sx: x0, sy: y0, sz: z0, x: x0, y: y0, z: z0, rep: u.key };
      groups.push(g);
      of[u.key] = g;
    }
  }
  for (const g of groups) {
    g.text = g.n > 1 ? (g.text + ' ×' + g.n) : g.text;
  }
  state._gClusterOf = of;
}

let identityMode = null;          /* 由 render 的 st.identity 每帧更新 */

function render(st) {
  if (!state.ready) { return; }
  st = st || {};
  /* 时间轴：数据数组里的 t0 是**窗口相对**时间，而 mission.js 传的 state.t = T0 + met
   * （T0 是绝对回放时钟，本局约 -149.4）。所以优先用窗口相对的 met，否则从 t 里减掉窗口起点。 */
  const winT0 = (state.meta && state.meta.window && isFinite(state.meta.window.t0)) ? state.meta.window.t0 : 0;
  const t = (st.met != null) ? st.met
          : ((st.t != null) ? (st.t - winT0) : 0);
  state.t = t;
  /* 帧步长：默认取墙钟（播放顺滑）；调用方给 dt 或 snap 时按给定值（宣传片确定性渲染） */
  if (st.snap) {
    state._dt = 0; CAMS.oSinit = false; CAMS.init = false; CAMS.spin = null;
  } else if (st.dt != null) {
    state._dt = st.dt;
  } else {
    const now = (typeof performance !== 'undefined') ? performance.now() : 0;
    state._dt = state._lastNow ? Math.min(0.1, (now - state._lastNow) / 1000) : 1 / 60;
    state._lastNow = now;
  }
  state.stats.units = state.units.length;

  /* 选项 */
  if (st.options) {
    if (typeof st.options.showGrid === 'boolean') { state.opts.grid = st.options.showGrid; }
    if (typeof st.options.showTrails === 'boolean') { state.opts.trails = st.options.showTrails; }
    if (typeof st.options.showLabels === 'boolean') { state.opts.labels = st.options.showLabels; }
  }
  const sc = state.scene;
  /* 图层开关（排查用） */
  if (typeof st.options === 'object' && st.options) {
    if (typeof st.options.showGround === 'boolean') { state.opts.ground = st.options.showGround; }
    if (typeof st.options.showKills === 'boolean') { state.opts.kills = st.options.showKills; }
    if (typeof st.options.showDrops === 'boolean') { state.opts.drops = st.options.showDrops; }
    if (typeof st.options.showMarkers === 'boolean') { state.opts.markers = st.options.showMarkers; }
  }
  sc.traverse(o => {
    if (o.name === 'grid') { o.visible = state.opts.grid; }
    if (o.name === 'kills' || o.name === 'killsAir') { o.visible = state.opts.kills !== false; }
    if (o.name === 'drops') { o.visible = state.opts.drops !== false; }
  });
  if (state.ground) { state.ground.im.visible = state.opts.ground !== false; }
  if (state.ground && state.ground.stems) { state.ground.stems.visible = state.opts.ground !== false; }
  if (state.airfields) { state.airfields.visible = state.opts.airfields !== false; }
  if (state.landFill) { state.landFill.visible = state.opts.land !== false; }
  if (state.opts.markers === false) {
    for (const u of state.units) { if (u.vis) { u.vis.im.visible = false; } }
    if (state.own && state.own.vis) { state.own.vis.im.visible = false; }
  }

  /* 高度标缓冲每帧先清零：被隐藏（阵营开关）/该时刻无采样的单位不会再往缓冲里写，
   * 不清的话它们会保留上一帧的线段 —— 表现为"不跟飞机走的幽灵竖线"（2026-10-22 反馈）。 */
  if (state.drops) { state.drops.arr.fill(0); state.drops.dirty = true; }

  /* 空中单位：飘带 + 机体 */
  if (state.opts.trails !== false) {
    for (const u of state.units) {
      if (u.domain !== 'air') { continue; }
      const hide = (u.side === 'enemy' && state.opts.enemy === false) ||
                   (u.side !== 'enemy' && state.opts.ally === false);
      if (hide) { if (u.vis) { u.vis.im.visible = false; u.vis.geo.setDrawRange(0, 0); } continue; }
      updateRibbon(u, t, 1);
      /* 该时刻没有有效采样就不要画机体（否则会停在上一位置/原点） */
      if (u.vis) {
        const hasU = sampleAt(u, t, _v);
        u.vis.im.visible = !!hasU;
        if (!hasU) { u.vis.geo.setDrawRange(0, 0); }
      }
    }
    if (state.own) {
      updateRibbon(state.own, t, 1);
      /* 本机"还没出生"时采样是 null：必须把机体藏起来，
       * 否则它停在原点，看起来像"模型僵在地图正中心"（2026-10-22 TEST1 反馈）。 */
      if (state.own.vis) {
        const hasOwn = sampleAt(state.own, t, _v);
        state.own.vis.im.visible = !!hasOwn && state.opts.markers !== false;
        if (!hasOwn) { state.own.vis.geo.setDrawRange(0, 0); }
      }
    }
  } else {
    for (const u of state.units) { if (u.vis) { u.vis.im.visible = false; u.vis.geo.setDrawRange(0, 0); } }
    if (state.own && state.own.vis) { state.own.vis.im.visible = false; state.own.vis.geo.setDrawRange(0, 0); }
  }

  /* 地面单位：方块（InstancedMesh） */
  const g = state.ground;
  if (g) {
    let n = 0;
    for (const u of state.units) {
      if (u.domain !== 'ground') { continue; }
      const x = u.x[0], y = u.y[0], z = u.z[0] + 2;
      const dist = state.camera.position.distanceTo(V3(x, y, z));
      const s = Math.max(30, dist * 0.005);
      _m.compose(V3(x, y, z), _q.set(0, 0, 0, 1), _s.set(s, s, s));
      g.im.setMatrixAt(n++, _m);
    }
    g.im.count = n;
    g.im.instanceMatrix.needsUpdate = true;
  }

  /* 相机（沿用旧 scene3d.js 的运镜参数与阻尼：后侧 3/4 跟随 / 俯视 / 环绕） */
  const cam = state.camera, ctl = state.controls;
  updateCamera(t, state._dt || 0.016);

  /* three 只在 render() 里更新相机矩阵；标签投影发生在那之前，必须手动刷新，
   * 否则用的还是上一帧的相机矩阵 —— 相机在动时标签会滞后甚至飞出屏幕（用户报的现象）。 */
  cam.updateMatrixWorld(true);
  cam.matrixWorldInverse.copy(cam.matrixWorld).invert();

  /* 标签：投影到屏幕（用当帧的画布尺寸，且在投影前就确定） */
  const cw = state.renderer.domElement.clientWidth || state.renderer.domElement.width || 1;
  const ch = state.renderer.domElement.clientHeight || state.renderer.domElement.height || 1;
  const ident = st.identity || null;
  identityMode = ident;
  if (ident && state.opts.labels !== false) { buildGroundClusters(t); }
  if (ident && state.labels) {
    for (const L of state.labels) {
      if (!L.u && L.own && ident.own) { L.el.textContent = ident.own; }
      else if (L.u && L.u.squadRank != null && ident.squad && ident.squad[L.u.squadRank - 1]) {
        L.el.textContent = ident.squad[L.u.squadRank - 1];
      }
    }
  }
  if (state.opts.labels) {
    for (const L of state.labels) {
      let wx, wy, wz, el = L.el;
      if (L.own) {
        if (!state.own || !sampleAt(state.own, t, _v)) { el.style.display = 'none'; continue; }
        wx = _v.x; wy = _v.y; wz = _v.z;
      } else if (L.u.domain === 'ground') {
        /* 地面：category 模式用类别；并在同位置聚合（由下面的 clusters 提供文本与位置） */
        const C = state._gClusterOf && state._gClusterOf[L.u.key];
        if (C && C.rep !== L.u.key) { el.style.display = 'none'; continue; }
        wx = C ? C.x : L.u.x[0];
        wy = C ? C.y : L.u.y[0];
        wz = (C ? C.z : (L.u.z ? L.u.z[0] : 0)) + PLATE_ALT;
        const txt = C ? C.text : groundLabelText(L.u);
        if (el.textContent !== txt) { el.textContent = txt; }
      } else {
        if (!sampleAt(L.u, t, _v) || (L.u.deathT != null && t > L.u.deathT)) { el.style.display = 'none'; continue; }
        wx = _v.x; wy = _v.y; wz = _v.z;
      }
      _p.copy(V3(wx, wy, wz)).project(cam);
      if (_p.z > 1 || _p.z < -1) { el.style.display = 'none'; continue; }
      const x = (_p.x * 0.5 + 0.5) * cw;
      const y = (-_p.y * 0.5 + 0.5) * ch;
      el.style.display = 'block';
      el.style.transform = 'translate(-50%,-190%) translate(' + x.toFixed(1) + 'px,' + y.toFixed(1) + 'px)';
    }
  } else {
    for (const L of state.labels) { L.el.style.display = 'none'; }
  }

  if (state.ground && state.ground.stems && state._gClusterOf) {
    const G = state.ground, A = G.arr;
    let n2 = 0;
    for (const key in state._gClusterOf) {
      const C = state._gClusterOf[key];
      if (C.rep !== key) { continue; }
      if (n2 + 2 > G.cap) { break; }
      const o = n2 * 3;
      A[o] = C.x; A[o + 1] = (C.z || 0) + 2; A[o + 2] = -C.y;
      A[o + 3] = C.x; A[o + 4] = (C.z || 0) + PLATE_ALT; A[o + 5] = -C.y;
      n2 += 2;
    }
    G.geo.setDrawRange(0, n2);
    G.geo.attributes.position.needsUpdate = true;
    G.stems.visible = (state.opts.ground !== false) && n2 > 0;
  }
  if (state.opts.kills !== false) { updateKills(t); }
  if (state.drops && state.drops.dirty) {
    state.drops.geo.attributes.position.needsUpdate = true;
    state.drops.dirty = false;
  }
  state.renderer.render(sc, cam);
  state.stats.calls = state.renderer.info.render.calls;
  state.stats.tris = state.renderer.info.render.triangles;
}

const API = {
  init: init, render: render, setCamera: setCamera, setOption: setOption,
  getReady: () => state.ready,
  getStats: () => Object.assign({}, state.stats, { t: state.t, ready: state.ready, camMode: state.camMode })
};
window.ACESceneReal = API;                   /* 供 scene3d3-loader.js 接管 */
if (!window.ACEScene || window.ACEScene.__isProxy) { window.ACEScene = API; }
