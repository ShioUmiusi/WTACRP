# ACE-COMBAT 接口契约 v4（冻结）

> 说明：文中 `dev/`、`build/` 下的脚手架、截图与中间产物属开发期文件，未随仓库提供。

目标：把本局**真实回放** `2026.10.18 05.45.06.wrpl` 渲染成皇牌空战 7 结算画面那种
**极简 3D 回放**：黑底透视网格、发光航迹（带垂直投影线）、目标名标签、地面单位标记、
击杀位置标记，加一条播放控制条。纯静态、双击即开、不连网。

**不要两侧面板、不要字幕、不要仪表盘 HUD。** 参考图只有网格 + 航迹 + 标签 + 标记。

## 0. 硬约束

1. **改动只落在本仓库内**：不修改仓库之外的上游目录，回放文件只读，
   不引入运行时依赖。
2. **必须能 `file://` 双击打开**：只用普通 `<script src>`（**禁止 ES module**、
   `fetch`/`XHR`/CDN/外部字体/外部图片/three.js）。3D 自己写投影。
3. **不编造数据**：未知就 `--`，不要填 0 或估算值；约定（机位、配色、竖向放大）要写明。
4. 目标平台 1920×1080 Chromium（含 headless），≥ 40 fps；`mission-data.js` ≤ 12 MB。
5. 文案与注释用中文；标识符用英文。

## 1. 数据来源原则（v4 修订，最高优先级）

1. **交付数据 100% 来自 `.wrpl`**：航迹（含他机三维）、击杀事件、玩家名、载具、时间轴，
   全部从回放包里解出。最终 `mission-data.js` **不得包含任何 8111 数据**，
   生成脚本 `tools/build_ace_mission.py` **不得读取任何 8111 抓取数据**（出现即判不通过）。
2. **8111 只是开发期验证手段**：用它验证解码是否正确（本机 x/y/z/hdg/ias/g 作真值；
   `map_obj` 平面接触点与侧别作他机交叉校验）。验证数字写进 `REPLAY-DECODE.md` / `DATA-NOTES.md`
   的「开发期验证」一节；**不进数据文件**。
   `meta.devValidation` 只放数字与来源说明，**不放任何 8111 坐标**。
   唯一例外：若回放坐标不是米制，用 8111 拟合出的**单位换算常数**可以应用，
   但必须在 `meta` 与说明里写明"尺度常数由开发期对比 8111 得出"。
3. **时间轴用回放自身的 tick 轴**：`secPerTick` 由回放拟合（前人数字 ≈256000/256472 Hz，需自行复核），
   窗口 `t0/t1` 用 tick 折算秒。
4. **解不出来的部分留空**（`--`、缺字段），**不许用 8111 补**。宁可少画，不许张冠李戴。
5. 前端开发要用的 8111 数据只能放在**明确的开发脚手架**里：`dev/**`，
   文件名带 `dev`，不进最终产物，不被 `index.html` 引用。
6. 解码过程（成功/失败原因、误差、试过什么、排除了哪些假设）写进
   `REPLAY-DECODE.md` / `REPLAY-FORMAT.md`；失败也要如实写。

### 1.0 解码中间产物（两条解码实现之间的约定）

- `build/wrpl/tracks.json`（航迹解析写）：实体三维航迹 + 坐标尺度 + 自检数字。
- `build/wrpl/header.json`、`build/wrpl/entities.json`（格式解析写）：
  内嵌结算 JSON、玩家表、实体↔载具/玩家名映射。

```jsonc
// tracks.json
// tick 口径（v5 钉死）：tick 一律是**回放原始计数器** rawTick = tsMs × 256，
// 与 mission-data.js 同空间；secPerTick = 3.90625e-06；秒 = tick × secPerTick = tsMs / 1000。
// 严禁在 tick 字段里塞毫秒；毫秒只能放在 *Ms 命名的字段里。
{ "secPerTick": <num>, "tick0": <int>, "tickLast": <int>,
  "scale": { "toMeters": <num>, "source": "packet_units"|"dev_fit_8111", "note": "…" },
  "playerEntityId": "0x…"|null,
  "units": [ { "entityId": "0x…", "vehicle": "f_15a"|null, "player": "…"|null,
               "side": "ally"|"enemy"|null, "domain": "air"|"ground",
               "samples": [[tick, x, y, z], …] } ],
  "devValidation": { "ownMaxErrM": <num>, "ownRmsErrM": <num>, "samples": <int>,
                     "unitAssocRate": <num>, "speedViolations": <int> } }
// 坐标：回放自身框架；若 toMeters != 1 则 x/y/z 已乘该常数（米）
```

## 2. 数据产物 `mission-data.js`（导出脚本）

经典脚本，只设置全局变量：

```js
window.ACE_MISSION = { meta, own, units, events, names };
```

### 2.1 `meta`

```js
meta: {
  source: "wrpl",                    // 只能是 "wrpl"
  replayFile: "2026.10.18 05.45.06.wrpl",
  replayHeader: {...}|null,          // .wrpl 头里读到的字符串（读不到 null）
  player: { name: "=LyRe= 八幡 海鈴", vehicle: null|"F-15A", callsign: "LYRE" },
  matchResults: { players: [ { userId, name, clanTag, kills, humanKills,
                              groundKills, navalKills, timePlayed } ], raw } | null,
  replayTicks: { tick0, tickLast, secPerTick, packets, killPackets },
  window: { t0, t1, duration },      // 秒，回放 tick 折算；t0 通常 0
  grid: { stepX: 6500, stepY: 6500,      // 显示约定：本局地图网格实测 6500 m
          note: "显示约定，非实测数据；开发期由 map_info 读出" },
  counts: { ownSamples, trackCount, airTracks, groundTracks, events, kills, ownKills },
  devValidation: {                   // 开发期用 8111 验证的结果（数字，不带坐标）
    ownMaxErrM, ownRmsErrM, ownSamplesCompared,
    unitAssocRate, speedViolations, note
  },
  provenance: [ "…" ],
  limits: [ "…" ]
}
```

坐标：`x/y/z` 单位为米，`z` 向上；水平朝向取回放自身框架（渲染只需相对关系），
网格步长按米使用；航向统一**度、0=正北、顺时针**（若回放不给航向，则由相邻采样位移推）。
时间：**秒，回放 tick 轴**。

### 2.2 本机轨迹 `own`（10 Hz 或回放原生采样率，三维）

```js
own: { t0, hz, n,
  x: [], y: [], z: [],
  ias: [], mach: [],            // 回放给了就给，没给就 null
  hdg: [], roll: [], pitch: [], g: [], aoa: [],
  ammo: [], fuel: [] }
```

玩家实体定位：用内嵌玩家表/author 与实体↔玩家映射；映射不到时用
"回放里被摄像机/控制包指向的实体"判定；仍判定不了就**如实说明**并退化为"最长/最完整的一条航迹"
（这种退化必须在 `limits` 里写明）。

### 2.3 其他目标 `units[]`（三维）

```js
units: [ { key: "eny-ftr-07", side: "ally"|"enemy"|"squad"|"unknown",
           domain: "air"|"ground", vehicle: "f_15a"|null, player: null|"…",
           hasAlt: bool, t0, hz, n, x: [], y: [], z: []|null, hdg: [],
           // 地面同步位置（ptype=4 MPI 0xF073/0xF074，可选）
           static: bool, groundUid: number|null } ]
```

- 全部来自回放。`Player` 不进 `units`。`hasAlt=false` 只用于回放里确实没有高度的实体。
- 地面单位两种形态：`static:true` 是 ECS 构造点单采样；`static:false` 来自地面同步包，
  `groundUid` 是回放里的地面 uid（绑不上 ECS 构造点时 `vehicle=null`、`vehicleName="GROUND UNIT"`、
  `side="unknown"`，不许按距离猜型号/阵营）。`groundTeam` 是 ECS 的 team 组件（1=蓝/2=红），
  `side` 由它和作者 team 比较得出；取不到 team 才回落到出生中心推断。地面航迹是 1 Hz 阶梯保持，不插值。
- 侧别：回放给了就用；给不了用 `"unknown"`，**不要**用 8111 的颜色去补。

### 2.3.1 导弹弹道 `missiles[]`（可选）

```js
missiles: [ { entity, t0, n, t: [], x: [], y: [], z: [] } ]
```

`t` 是轴秒（与 units 同轴），x/y/z 是回放世界米；来源是 WeaponSync 的 class 0 记录
（`ptype=4` `0xF0DB` / `0xF11A`）。弹体不画，渲染层用白色虚线表示已飞过的部分；
`options.showMissiles=false` 时整层不显示。

### 2.4 事件 `events[]`（按 `t` 升序，位置是击杀点）

```js
events: [ { t, kind: "kill"|"damage"|"award"|"disconnect"|"lastblow"|"chat",
            text,                       // 回放里的原文；若是拼合出来的，加 composed:true
            composed: bool,
            actor, actorVehicle, target, targetVehicle, entityId, victimEntityId,
            isPlayerActor, isPlayerTarget, replayTick, x, y, z } ]
```

- 事件来源：回放的击杀/伤害包（前人已知 `ptype==4 && payload[:4].hex()=="00025858"` 共 33 个），
  用当事实体航迹在 tick 时刻的位置给 `x/y/z`。
- `kind: "chat"`：回放聊天原文（实测 5 条，`composed:false`，本契约对枚举的扩展）。
- 文本：回放里若带本地化原文就直接用；只能从字段拼合时，`composed=true`，
  并且**只用回放里的真实字段**（玩家名、载具名）。
- 没有 tick 的事件不许凭空插进时间轴。

### 2.5 `names`

```js
names: { "<unit key>": { name, vehicle } }
```

只给能对上的目标命名（实体↔玩家/载具表、击杀包里的击杀者/受击者）。
对不上的显示侧别+机种（`ENY FTR`）或不标。

## 3. 页面与接口

### 3.1 `index.html`（页面骨架）

```html
<link rel="stylesheet" href="ace.css">       <!-- 渲染层 -->
<link rel="stylesheet" href="mission.css">   <!-- UI 层 -->
<div id="stage">
  <canvas id="scene"></canvas>
  <div id="corner"></div>      <!-- 角落极简读数 + 来源/局限说明（H 键切换） -->
  <div id="controls"></div>    <!-- 底部播放控制条 -->
  <div id="help"></div>        <!-- ? 键帮助 -->
</div>
<script src="mission-data.js"></script>
<script src="three3d/scene.js"></script>
<script src="mission.js"></script>
```

加载顺序固定；不写业务内联脚本；**不得引用 `dev/**` 里的脚手架数据**。

### 3.2 渲染层：`three3d/scene.js` → `window.ACEScene`

```js
window.ACEScene = {
  init(canvas, data, opts) {},   // data = window.ACE_MISSION；幂等
  render(state) {},              // 每帧
  setCamera(mode) {},            // "follow" | "orbit" | "top" | "free"
  setOption(k, v) {},            // 'altScale'|'trails'|'labels'|'grid'|'glow'
  getReady() {}
};
```

### 3.3 UI 层：`mission.js` + `mission.css`

- 时钟：`rAF` + 固定步进累加，`met` 为任务内秒（0 = `meta.window.t0`），按 `speed` 推进。
- 每帧构造 `state` 调 `window.ACEScene && ACEScene.render(state)`；场景缺失也要能跑。
- 底部控制条：播放/暂停、倍速 1/2/4/8、进度条（**带击杀刻度**）、重播。
- 角落读数（H 键切换）：任务时间 / 本机高度速度 / 一行来源说明（`meta.source`、`meta.limits`）。
- 键盘：`空格` 播放暂停，`←/→` ±10 s，`1/2/4/8` 倍速，`C` 机位循环，
  `T` 航迹、`L` 标签、`G` 网格、`H` 角落读数、`R` 重播，`?` 帮助，`F` 全屏。

#### 3.3.1 `state`（UI 层提供，渲染层消费）

```js
state = {
  met, t, playing, speed,
  own: { x, y, z, ias, mach, hdg, roll, pitch, g, aoa, vy, ammo, fuel } | null,
  contacts: [ { key, side, domain, dx, dy, d } ],   // dx 向东、dy 向北，米
  ownMissing: bool,
  options: { altScale, showTrails, showLabels, showGrid, showMissiles, showOwnModel }
}
```

## 4. 场景与视觉（渲染层）

- **世界**：纯黑底，**绿色系透视网格**铺在地平面（用 `meta.grid` 的 6500 m 网格，本局实测值，作为显示约定），
  远处渐隐、近处略亮；可加极淡高空网格作高度参照。**无地形贴图**。
- **航迹**：发光折线，`globalCompositeOperation="lighter"` + 双层描边做辉光。
  颜色按侧别：`enemy` 橙红 `#FF6A2B`、`ally` 青蓝 `#6FE3FF`、`squad`/其他绿 `#5BFF8A`、
  `unknown` 暗灰。**越旧越暗**，当前端点有脉冲光点。
- **垂直投影线**：有高度的航迹，在端点（或每 N 个采样）向地平面拉暗色竖线，底端小十字。
- **地面单位标记**：暗琥珀小点/短十字，压在地网格上，不做航迹（太多）。
- **击杀位置**：橙色 `✳`/`×`，短暂扩散环，数秒后淡出；`isPlayerActor` 高亮。
- **标签**：贴在航迹当前端点，`◇` + 等宽大写名（有 `names` 用真名，否则 `ENY FTR`），
  细引线连接，简单避让重叠。
- **机位**：`follow`（默认）、`orbit`、`top`、`free`（鼠标拖拽 + 滚轮）。任何机位不得
  NaN 跳变或瞬移。
- **银幕化**：轻 letterbox、四角括号、扫描线、暗角；字体只用本机等宽
  `Consolas, "Cascadia Mono", ui-monospace, monospace`。
- **竖向放大**：`altScale` 默认 1（真实比例）；可 2/4 倍，倍数须显示在角落并写进说明。

## 5. 维护范围与验收

| 文件 | 负责部分 |
|---|---|
| `tools/wrpl_decode.py`、`build/wrpl/tracks.json`、`docs/REPLAY-DECODE.md` | 航迹解析 |
| `tools/wrpl_header.py`、`build/wrpl/{header,entities}.json`、`docs/REPLAY-FORMAT.md` | 格式解析 |
| `mission-data.js`、`tools/build_ace_mission.py`、`docs/DATA-NOTES.md` | 导出脚本 |
| `three3d/scene.js`、`ace.css` | 渲染层 |
| `index.html`、`mission.js`、`mission.css` | UI 层 |
| `tools/check_ace_mission.py`、`docs/VERIFY.md`、`build/verify/**` | 独立复算 |
| `CONTRACT.md`、`README.md` | 契约与项目说明 |

验收：

1. **回放独立性（最硬的一条）**：
   - `tools/build_ace_mission.py` 里出现 8111 抓包路径一律判不通过；
   - `mission-data.js` 里每个 `units[]` 都有 `z` 且不是常数（8111 根本没有他机高度，
     z 有真实起伏即证明来自回放）；
   - `meta.source == "wrpl"`。
2. **解码质量**：本机航迹与 8111 真值的 max/rms 误差有数字（目标 max≤50 m、rms≤15 m，
   达不到如实写）；他机与 8111 平面接触的关联率有数字；速度包线自检（无 >600 m/s、无瞬移）。
3. `mission-data.js` 结构符合 §2，浏览器可加载；时间轴单调。
4. 事件数与回放击杀包/内嵌结算一致；玩家击杀数与内嵌结算一致。
5. `file://` 打开 `index.html`：无 console error、**0 个网络请求**、能播到结尾。
6. 截图至少：巡航（网格+航迹+竖线+标签）、击杀瞬间、俯视机位、结尾。
7. 播放：时间轴单调、可 seek、可倍速、暂停后 `met` 不前进；画面无 NaN 崩坏。

## 6. 目录结构（v4 增补）

仓库自包含：交付页面、文档、脚本与中间产物都在同一棵树里。

```
index.html  ace.css  mission.css  three3d/scene.js  mission.js  mission-data.js   ← 交付页面
CONTRACT.md  README.md
docs/      REPLAY-DECODE.md  REPLAY-FORMAT.md  DATA-NOTES.md  VERIFY.md
tools/     wrpl_decode.py  wrpl_header.py  build_ace_mission.py  check_ace_mission.py
build/     wrpl/  verify/  recon/        ← 中间产物（可删，不进仓库）
```

脚本只读本地回放与中间产物，输出一律写 `build/**`。
开发期夹具（截图、原型页、8111 脚手架）留在工作区，不进发布物。

## 7. 参考实现与许可（v5 增补）

**不要重新发明轮子**：既有实现整包拉取为本地只读参考（不随仓库分发）：

- `build/wrpl-inspector/` —— [maxsupermanhd/wrpl-inspector](https://github.com/maxsupermanhd/wrpl-inspector)，
  Go，**AGPL-3.0**，默认分支 `v3`。含 wrpl/danet/bitStream.go、wrpl/packet/parser/{movement,ecs2,slot,kill,damage,cameraAnglesParser,fm}、
  wrpl/game/position.go、inspector/tabs/mapview/、data/ecshashes.json（组件哈希表）。

规则：

1. **移植知识，不整段照抄**：把格式理解移植成独立的 Python 实现。
2. **必须署名**：REPLAY-DECODE*.md、REPLAY-FORMAT.md、DATA-NOTES.md 与页面 ? 帮助里写明
   「回放格式解析参考 wrpl-inspector（AGPL-3.0）」并给 URL；若某段实现近乎逐行对应，指明对应文件。
3. **参考实现只用于开发**：不进交付产物、不作为运行时依赖、index.html 不得引用。
4. 产物必须离线可跑、只读本地回放文件，不依赖网络。

### 2.6 击杀归属的硬限制（v5 增补）

回放的击杀包**只有击杀方载具名与 unitId32，没有击杀者身份**，因此**无法**把击杀归属到具体玩家。所以：

- `counts.ownKillsByEvents`：事件里能归属到本机的击杀数，回放侧**恒为 0**；
- `counts.ownKillsFromResults`：回放**内嵌结算**里本机的 `kills`（权威数字，本局为 2）；
- 界面凡说「本机击杀」只能用后者，并注明来自内嵌结算；不得声称由事件归属得出。

## 8. UI 规格（v6）

### 8.1 配色（对齐参考图：黑底 + 细网格 + 发光航迹）

| 用途 | 颜色 |
|---|---|
| UI 主色（控件、边框、读数、进度条、机位/时间文字） | **蓝** `#4DA6FF`，次要 `#2E6BFF` |
| 网格 | 冷绿 `#1E5C46`（细、远处渐隐），底座近黑 `#04070B` |
| 本机航迹 | 橙红 `#FF6A2B`（参考图 MIRAGE 2000-5） |
| 队友（squad）航迹 | 绿 `#5BFF8A`（参考图 REX） |
| 其他友军（ally）航迹 | 青蓝 `#6FE3FF`（参考图 LCAC） |
| 敌军（enemy）航迹 | 红 `#FF3B47` |
| 未知（unknown） | 灰蓝 `#6B7C8C` |
| 地面单位标记 | 暗琥珀 `#C9A227` |
| 击杀标记 | 橙 `#FF8A3D`；`isPlayerActor` 用亮橙 `#FFC24A` |

### 8.2 单位形状

- **空中单位 = 有指向性的三角形**（按该实体当前航向旋转；航向缺失时朝屏幕上方），
  端点处画在航迹末端。
- **地面单位 = 方块**（不旋转）。
- 标签仍用 `◇` + 等宽大写文本 + 细引线（参考图样式）。

### 8.3 呼号显示规则（呼号由用户自定义，不来自数据）

- **本机**：显示用户设定的呼号（默认 `ALPHA 01`）。
- **队友（squad，绿色）**：显示用户设定的呼号。**War Thunder 小队最多 4 人**，
  所以默认预填 3 个队友呼号：`ALPHA 02` / `ALPHA 03` / `ALPHA 04`（`DEFAULT_SQUAD`）。
- **其他友军 / 敌军 / 未知**：**不显示呼号**，只显示**型号**（如 `F-15A`，来自 `units[].vehicle`；
  没有型号就显示 `AIR`/`GND`）。没有型号时**不要**编造呼号。

自定义入口：**右上角齿轮**，面板内可改
1) 本机呼号；2) 队友呼号（每行一个，按航迹长度顺序绑定到除本机外的目标，被绑定的视为 `squad`）。
设置存 `localStorage`，刷新后保留；并在 `state.identity` 里下发给场景。

### 8.4 `state` 增补字段

```js
state.identity = {
  own: "LYRE",                     // 本机呼号
  squad: ["REX", "TASHA"],         // 队友呼号，按顺序绑定
  squadKeys: ["uid-349", ...]      // 由 mission.js 解析出的实体 key（可选）
}
```

`three3d/scene.js` 只消费 `state.identity`，不读 `localStorage`。

### 8.5 补充参考图细则（v6.1，依据第二张 AC7 地图式结算页）

以该图为准的**配色与画法**（**不要**加它左右两侧的面板）：

- **背景**：深藏蓝渐层 `#050A12` → `#08111C`（不是纯黑）。
- **网格**：细冷白蓝线 `#5A7A99`，alpha 0.12–0.22，随距离渐隐（替换原来的绿网格）。
- **航迹**：橙红 `#FF6A2B` 为主；队友绿 `#5BFF8A`；其他友军青蓝 `#6FE3FF`；
  端点带**垂直落线 + 底端小十字**。
- **地面单位**：暗橙 `#C9702A` **方块** + 一条细的**垂直短线连到地网格** + 小号大写标签
  （`vehicle` 型号，缺则 `TANK`/`APC`/`AA`/`GND` 之类通用词）；密集处抽稀。
- **标签**：细体大写、冷白 `#DCE9F5`，前缀 `◇`，引线极细（alpha 0.4）。
- **击杀标记**：橙 `#FF8A3D` 的 `✳`，本机击杀亮橙 `#FFC24A`，落点在地网格上。
- **UI 主色**：`#4DA6FF`（控件、读数、进度条、击杀刻度小竖线）。

## 9. 颜色优先级与地图图层（v7）

### 9.1 有效阵营（effective side，渲染层覆盖，不改数据）

数据侧 `units[].side` 是 ally/enemy/unknown；渲染层的颜色必须按下面**优先级**决定
（早期缺陷：全员填 side 之后，小队绿被阵营色盖掉）：

1. 本机 → `own`（绿 `#5BFF8A`）
2. `state.identity.squadKeys` 内的实体（或 `u.side === "squad"`）→ `squad`（绿 `#5BFF8A`）
3. 其余 → `u.side`（ally 蓝 `#3D8BFF` / enemy 红 `#FF3B47` / unknown 灰蓝）

该有效阵营统一用于：航迹、三角、标签色、投影竖线、标签绘制优先级。
「阵营显示：推定/反转/未知」开关只影响第 3 档，**不得影响第 1、2 档**。

### 9.2 地图图层（来自 `warthunder-maps/` 的等高线产物）

`warthunder-maps/out/<map>_contours.json` 的格式与本项目一致
（`crs:"replay-world-meters"`、扁平数组 `[x0,y0,x1,y1,…]`、`contours[].elev`、`water`/`coastlines`、`meta.bounds`）。
接入约定：

- 数据侧：`terrain: { meta, contours, water, coastlines }`（可选图层；缺文件则该层为空）；
- 渲染侧：**海岸线**冷白细线（alpha≈0.45）、**等高线**更淡的细线并按高度分档（alpha 0.08–0.18）、
  **水体**极淡填充；网格仍压在下面；`T`/`G` 之外再加一个 `M` 键切换地图图层；
- 坐标系：等高线文件的世界米**必须与回放世界米同向**（`x` 东、`y` 北）。若方向不符，
  用 `--flip-y` 生成镜像版本或给一份仿射参数，**不得靠肉眼硬对**。
- 本局地图 `air_skyscraper_city.bin` **没有 `HM2` 块**（老格式关卡，地形只在 `lmap` 网格里），
  所以当前 `terrain` 为空；要出等高线必须先支持 LandMesh 解析。

## 10. 范围纪律（v8）

边界条款：**不做慢动作、屏幕边缘闪，也不做自行想象出来的特效。**

**规则**：

1. **只做明确要求过的内容**。任何未被要求的效果、动画、横幅、文案、装饰，
   一律**先问、不要自己加**。
2. 被否决过的（**不要再做**）：击杀慢动作、击杀镜头推近、屏幕边缘闪、
   `SPLASH ONE`/`FINAL BLOW` 之类横幅、开场/片尾仪式感卡片、界面上的解释性图例文字、
   任何"视觉糖"。
   **例外（2026-10-19 澄清）**：击杀 `X` 的**扩散环**是 AC 式结算本来就有的表现，**保留**。
3. **保留项（逐条确认过的）**：黑底 + 细网格；发光航迹；地面单位方块 + 竖线；
   击杀位置常驻 `X`；播放控制条；蓝主色；空中三角；本机与真实小队绿 / 友军蓝 / 敌军红、
   无白色；标签显示真实型号（不得出现 `AIR`/`GND` 兜底）；齿轮自定义呼号（多余呼号不绑）；
   型号/类别切换（`V`）；跑道线；地形图层（待数据）。
4. **诚实性只保留在开发侧**（文档、复算、哈希、`?` 帮助），**不进入用户界面**：
   角落不留来源与局限、不出现"推定/可能不准"字样。
5. 数据侧一律不动：`mission-data.js` 的字段与语义只由用户需求驱动，不由"看起来更酷"驱动。

