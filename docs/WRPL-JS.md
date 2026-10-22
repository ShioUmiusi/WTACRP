# WRPL-JS：浏览器内解析 .wrpl（移植范围 · 对比结果 · 未做项）

> 说明：文中 `web/dev/` 下的自测页为开发期产物，未随仓库提供。

> 产物：`web/index.html`（上传页）、`web/wrpl-parse.js`、
> `web/vendor/fzstd.min.js`（+ `fzstd.LICENSE.txt`、`README.md`）。
> 开发期自验脚本：`web/dev/selftest-node.js`、`web/dev/selftest.html`。
> 核对日期：2026-10-19。对比基线：同一份回放的 Python 解码链
> （`tools/wrpl_{header,decode,kill_events,decode_ecs}.py` + `build_ace_mission.py`）。

## 0. 一句话结论

**浏览器里解出的数据与 Python 链的交付文件 `mission-data.js` 逐项一致**：
26 项 `meta.counts` 全等、236 条实体（52 空中 + 184 地面）、每段样本数逐个一致、
随机 3 个时刻的本机坐标逐位相同、33 条击杀的 tick/uid 逐条一致、184 个地面点按 `entityId`
逐条一致、4 条跑道端点 ≤1 m、1734 个发射点一致；整份解析**约 1.5 s**（本机 Chromium，
上限 10 s），不联网、不需要构建。

## 1. 架构（目标形态）

```
用户浏览器                     服务器（只发静态文件）
  ├ 选/拖 .wrpl ──┐
  ├ FileReader → ArrayBuffer     index.html
  ├ fzstd 解压 zstd 帧           wrpl-parse.js
  ├ 位流解码（FM/击杀/ECS）      vendor/fzstd.min.js
  └ ACEUI.mount(ACE_MISSION)     three3d/scene.js / mission.js / *.css
                                 数据表（显示名/分类）
```
- **回放文件不上传**：`FileReader` 在本机读，解析全在页面里。
- **页面不联网**：`wrpl-parse.js` 里没有 `fetch` / `XMLHttpRequest` / 动态 `import()`；
  只有 `<script src>` 引本机文件。可复核：`Select-String -Path web\wrpl-parse.js -Pattern 'fetch\(|XMLHttpRequest'`（0 命中）。
- **不依赖构建**：纯经典脚本（非 ES module），`file://` 双击也能开（用文件选择/拖拽喂文件）。

## 2. 移植范围（做了什么）

| 层 | 内容 | 来源（Python） |
|---|---|---|
| 容器 | magic `DBLD3x64` 定长头 1224 B、settings BLK、zstd 帧定位、结算 BLK 偏移 | `wrpl_header.parse_replay_header` / `locate_packet_stream` |
| 解压 | zstd 帧 → 包流（vendored fzstd；zlib 分支给明确报错） | `decompress_stream` |
| 包帧 | VLQ 长度前缀 + 2 字节头（`h0 & 0x10` = 沿用上一 tick）；否则紧随 u32 LE 毫秒 | `wrpl_header.parse_packets` |
| `ptype=2` | FlightModel 位流（MSB-first）：HasUID → compressed uid → noData/skip → `n11/t1` 增量 → PosX/PosY/PosZ f32 → EulerBytes → Unk12 → Align+7B → engines/sensors/cm/targets | `wrpl_decode.parse_fm` |
| 坐标 | `x=PosX, y=PosZ, z=PosY`（米）；断轨判据用三维 (PosX,PosY,PosZ) 的 600 m/s 包线 | `wrpl_decode.decode_positions` + `cmd_build` |
| 本机属性表 | `ptype=4` 签名 `03 58 4a`（0xD04A）→ `[3B sig][5B hdr][N×(u16 pid + f32)]` → 10 列（alt_m 由 ft×0.3048） | `wrpl_decode.decode_cockpit_props` |
| 击杀/伤害 | `IdFieldSerializer32`：u16 LE 尺寸表偏移 + LEB128 位掩码 + 位宽表；0xF058 / 0xF056 / 0xF157 字段号语义 | `wrpl_kill_events.*` / `wrpl_decode.decode_kills_full` |
| `ptype=6` ECS | 控制码 0x24（明文构造）+ **0x25（LZ4 压缩构造，自写 LZ4 block 解压）**：模板定义、`eid`、块、组件值起点 | `wrpl_decode_ecs.EcsParser` |
| 地面/工事 | 模板 `tank` / `fortification`：位置 = **组件值起点 + 10** 处 3×f32 (x, 上, z)；`unit__className` 逐偏移找长度前缀串 | `enumerate_entities` / `find_ground_classname` |
| 机场/跑道 | 实体块关键词聚类成 4 个机场点；`00025839` 包里的 `t{1,2}_airfield_top[_reload]` 标记点（位置重复采样 + 单位航向）+ 模型名给的 3000 m → 4 条跑道线段 | `extract_runway_markers` / `build_runways` |
| 弹药发射点 | 弹类模板（rocket/bomb/payload/jettisoned）用「块内 \|q\|≈1 的 float4 之前 12 字节」锚定位置 | `find_projectile_transform` |
| uid → 载具/玩家 | 飞机构造块：`uid = 值起点+29` 的 u16 低 11 位；`spawnSlotName` 做兜底键；机型 = 块内符合机型形状的串 | `build_uid_map` |
| 阵营 | 纯回放：击杀关系二色（剔除疑似 TK 边）+ uid 映射已知值优先 + AI 出生位置聚类 | `side_final2.py` |
| 小队 | 结算 BLK 的 `squadId` + 机型候选 + 结算 kills/deaths 指纹 → 队友 uid（本局 357） | `build_squad` |
| 组装 | 时间轴（tick0/tickLast/secPerTick）、窗口、本机 10 Hz、他机 Catmull-Rom 10 Hz、包线滤除、事件、meta/counts/limits | `build_ace_mission.py` |

本机 uid 的判定（Python 用开发期真值锚定 uid=368）：浏览器版用**纯回放判据**
——把座舱属性表的高度序列插值到每条航迹的采样时刻（最近样本 ≤0.3 s），比
`rms(z − alt)`，取最小者。本局最优 `uid-368` 的 rms ≈ **2.0 m**（合并段）/ 2.0–6.5 m（分段），
次优 **414 m**，判据非常干净。本来打算用的「Pearson 相关 ≥0.9」不可靠：全表最优只有
0.898（uid-360）、真机 368 只有 0.822 —— 已弃用，写在这里以免后来者重蹈。

## 3. 对比结果（本局 `2026.10.18 05.45.06.wrpl`）

回放文件 5,133,517 B → 86,773 个包 → 解压后包流 14,180,794 B（zstd 帧 5,116,892 B）。

### 3.1 验收项逐条

| 验收项 | Python 基线 | 浏览器 JS | 结论 |
|---|---|---|---|
| `meta.counts`（26 项） | 见下表 | **全等** | ✅ |
| 实体数 | 236（空中 52 + 地面 184） | 236（52 + 184） | ✅ |
| 每段样本数 | `(domain,t0,hz,n)` 序列逐条一致；样本合计 124,696 | 同 | ✅ |
| 随机 3 个时刻本机坐标 | 55 s `[-26955,13828,986.9]`、233 s `[-15284,-2735,132.2]`、500 s `[-15032,12655,833]` | **逐位相同**（偏差 0.000 m） | ✅（≤1 m） |
| 本机 own 逐点 | n=5589 | 最大偏差 **0.100 m** | ✅（≤1 m） |
| 空中 52 条航迹逐点 | — | 最大偏差 **0.100 m**（`eny-mig15bi-03`） | ✅（≤1 m） |
| 33 条击杀 tick/uid | 33 条 | `(replayTick, victimUid, killerUid)` **逐条一致** | ✅ |
| 击杀事件坐标 | 33/33 有坐标 | `(t, x, y, z, uid)` 逐条一致 | ✅（≤1 m） |
| 地面点 | 184 | 184，按 `entityId` 逐条一致（坐标/机型/显示名/分类） | ✅ |
| 跑道线段 | 4 条 | 4 条，端点 ≤1 m、`headingDeg`/`confidence` 一致 | ✅ |
| 机场点 | 4 个 | 4 个，位置与结构数一致 | ✅ |
| 发射点 | 1734 | 1734，坐标与 `shooterUid` 逐条一致 | ✅ |
| 事件 | 65（kill 33 / chat 5 / award 26 / lastblow 1） | 同 | ✅ |
| 时间窗口 | t1 = 558.846 s | 558.846 s | ✅ |
| `replayTicks` | tick0 7,962,368 / tickLast 151,026,944 / packets 86,773 | 同 | ✅ |
| `meta.player` / `matchResults.players` / `vehicleRoster` / `replayHeader` | — | 逐字节一致（含键序） | ✅ |
| `meta.squad` | squadId 2002，队友 uid 357 | 同 | ✅ |
| 输出体积 | 3.62 MB | 3.61 MB | ✅（≤12 MB） |

`meta.counts` 的 26 项（两侧完全相同）：
`ownSamples 5589, trackCount 236, airTracks 52, groundTracks 184, groundUnits 184, shots 1734,
unitsWithSide 236, unitsWithDisplayName 215, unitsWithCategory 236, unitsHidden 21, squadSize 2,
runways 4, airfields 4, terrainContours 0, unitsWithDeath 31, events 65, kills 33,
ownKillsByEvents 2, ownKillsFromResults 2, unitSeamsSplit 0, ownSegments 2, envelopeFiltered 20,
unitsWithIdentity 236, unitsVehicleFromPackets 0, killsWithWeapon 30, killsWithWeaponName 30`。

### 3.2 自验怎么跑（两条路，都是对拍 Python 基线）

1. **Node**（快、可反复跑；不需要浏览器）：
   ```powershell
   cd <repo>            # 仓库根目录，其中含 
   node web\dev\selftest-node.js
   ```
   输出：`== 结果：0 FAIL`（约 0.88 s 解析）。

2. **真浏览器**（验收要求；同一份对拍逻辑做成页面）：
   ```powershell
   cd <repo>            # 仓库根目录
   python -m http.server 8877 --bind 127.0.0.1
   # 浏览器打开：
   # http://127.0.0.1:8877/web/dev/selftest.html
   ```
   输出：`完成：18 项检查，FAIL 0；解析耗时 1.56 s`。
   （HTTP 是**开发期自验**需要——页面要 fetch 本地回放；交付页 `index.html` 不需要服务器，
   它靠文件选择/拖拽拿数据。）

3. **交付页**：`http://127.0.0.1:8877/web/index.html` → 拖入/选择
   `2026.10.18 05.45.06.wrpl` → 约 1.6 s 后自动 `ACEUI.mount`，场景开始播。

### 3.3 性能与「不卡死 UI」

| 指标 | 数值 |
|---|---|
| 解压（fzstd，5.1 MB → 14.2 MB） | ≈100 ms（单次同步调用，是唯一一段较长的阻塞） |
| 整份解析（Node / Chromium） | 0.88 s / **1.45–1.58 s** |
| 进度回调 | 44 次（`container → zstd → packets → fm → props → kills → ecs → blk → own → side → units → done`） |
| 解析期间 `requestAnimationFrame` 帧数 | 76 帧，**最大帧间隔 57.1 ms** |

分块与让出主线程：每处理 `yieldEvery`（默认 400）个包 `await new Promise(r => setTimeout(r, 0))`，
各阶段之间也各让一次；所以进度条会走、页面不会假死。

## 4. 未做 / 与 Python 的差异（如实列出）

1. **8111 开发期校验数字一律 `null`**：`meta.devValidation` 里
   `ownMaxErrM / ownRmsErrM / unitAssocRate / groundNearestContactMedianM / groundFracWithin100M /
   ownCorrVs8111 / killDist* / shotsMedianDistToNearestAircraftM …`。浏览器里拿不到 8111
   抓取数据（也不该拿），`meta.devValidation.note` 已写明；可自算的
   `speedViolations / envSteps / envTeleports / trackSegments / segmentsDroppedShort` 仍给出。
2. **不读 `ecshashes.json`（780 KB 组件哈希表）**：本文件只解下游真正用到的字段
   （位置 / uid / `unit__className` / `spawnSlotName` / 弹药位置），它们都在块内**固定偏移**
   或**长度前缀串**上。因此「第一个组件是不是 transform」用**模板白名单**
   （`tank` / `fortification` / `aircraft` / `aircraft+player_unit`，Python 已验证 transform 打头）
   代替哈希派发；ECS 组件值本身不解析（Python 用它算 `blockUsed`，下游未用）。
   副作用：`ecs_entities.json` 里那些字段（`values`/`blockUsed`）在浏览器版没有对应产物。
3. **控制码 0x25**：Python 端要先解 LZ4 block 才能拿到实体；这里**自写 LZ4 block 解压**
   （约 40 行，无外部依赖），本局 542 个 0x25 包 → 与 1066 个明文 0x24 包一起解出
   **4427 条实体构造记录**，与 Python 完全相同，0 失败。0x21/0x22/0x23/0x26 不产出实体（照 Python 实测跳过）。
4. **`meta.matchResults.raw`**：保留结算 BLK 的树，但去掉 `uiScriptsData`（与交付文件的取舍一致）；
   没有 Python 侧加的 `raw._note` 注释（那是 Python 侧的说明字段）。`players[]` 逐项一致。
5. **`terrain` 仍是 `null`**、`names` 仍是 `{}`（与 Python 相同：本局地图没有 HM2 块；玩家名大多不唯一）。
6. **显示名/分类是内嵌的显示约定表**：`units[].vehicleName` / `vehicleCategory` 取自页面内嵌的
   37 条键值（源头 `build/wrpl/vehicle_display_names.json` + `vehicle_categories.json`，
   最终源头是 War-Thunder-Datamine 的英文名列），**不是回放数据**；
   取不到就留 `null`/`UNKNOWN`，场景原样打印 `units[].vehicle`。
7. **边界**：只测过这一份回放（`bfd_norespawn`、单 zstd 帧、无 compCount≠0 分支）；
   旧格式（zlib 包流）会给明确报错而不是猜。
8. Python 的 `wrpl_decode.py --validate` 那一整套 8111 拟合（`REL_OFFSET=53.52` 等）没有移植
   —— 它是开发期校验，不是产物的一部分。

## 5. 开发期踩到的两个坑（留给后来者）

1. **`f32` 读写用了两块不同的 ArrayBuffer**：写进 `fb`、读 `dv`，于是 `getFloat32` 恒为 0.0。
   表现极隐蔽：航迹全是 0、断轨 0 处、实体数看着"差不多"。改成单一的
   `{buf, dv, u8}` 暂存后才对。**教训**：这类"静默 0"必须靠逐项对拍抓，不能只看有没有报错。
2. **ECS 的 0x25 不能跳过**：早先按「0x25 = lz4，产出 0 条实体」处理（那条结论来自把
   `parse_packet` 的返回值当 control 统计——lz4 分支会把 control 改写成 0x24，
   于是 542 个 0x25 包被算进了 0x24）。实际这 542 个包里全是实体构造，
   不实现 LZ4 就会丢地面点、跑道和发射点。

## 6. 许可与署名

| 来源 | 许可 | 用途 |
|---|---|---|
| [fzstd](https://github.com/101arrowz/fzstd) 0.1.1 | **MIT** | zstd 解压（vendored，原样，见 `web/vendor/`） |
| [maxsupermanhd/wrpl-inspector](https://github.com/maxsupermanhd/wrpl-inspector) | **AGPL-3.0** | 回放格式理解（包帧、位流语义、`IdFieldSerializer32` 字段、ECS 构造）；**代码不进产物** |
| [Warthunder-Open-Source-Foundation/wt_blk](https://github.com/Warthunder-Open-Source-Foundation/wt_blk) | **AGPL-3.0** | 二进制 BLK（FAT）格式规格 |
| [gszabi99/War-Thunder-Datamine](https://github.com/gszabi99/War-Thunder-Datamine) | 只读参考 | 显示名/分类表的英文名列（只嵌入 37 条键值，未分发其文件） |

`wrpl-parse.js` 是自写 JS，移植的是 Python 实现（`tools/*.py`）的知识与结论，
没有复制上述项目的代码。
---

## Worker 路径：2 s 超时与就绪握手（2026-10-19）

**现象**：默认路径端到端 **3.7 s**（主线程路径 1.65 s）；强制 Worker 路径（`force: "worker"`）直接失败。
**原因**：`parseInWorker()` 创建 Worker、装上 `onmessage` 之后**从未发出 `{cmd:'ready'}` 握手消息**，
而 worker 只有收到它才回 `ready` —— 主线程一直等到 2 s 超时、再回退主线程解析，
端到端正好 = 2 s 等待 + 1.58 s 解析。
**定位方法**：包装 `window.Worker`，记录构造与**向 worker 发出的每条消息** —— 日志里只有 `create wrpl-worker.js`，
**没有 `post ready`**（同页手工 `postMessage({cmd:'ready'})` 时 14–17 ms 就回 `ready`，说明 worker 本身没问题）。
**修改**：创建后立即 `worker.postMessage({ cmd: 'ready' })`（失败即回退），一行。

**修改前后（同一份回放 5,133,517 B、同一台机、同一测量方式）**

| 指标 | 主线程路径 | Worker 路径（修改后） |
|---|---|---|
| 端到端 | 1582 ms | **1645 / 1675 / 1715 ms**（三次，中位 1675）；默认路径实测 1724 ms |
| 主线程最大帧间隔 | **139 ms** | **17 / 19 / 21 ms**（三次）；默认路径含挂载 36 ms |
| >50 ms 的帧 | 1–4 | **0** |
| 主线程堆（解析前→后） | 8.6 → 97.8 MB | 8 → 13 MB（观测值，performance.memory 有噪声，仅作趋势） |
| 结果一致性 | — | units 236 / shots 1734 / kills 33 / ownSamples 5589，与主线程逐项相同 |

**结论**：用 **+6%~9% 墙钟**换掉 **约 86% 的主线程卡顿**（139 ms → 17–36 ms），属于优化而非退化。
若要把墙钟也压回主线程水平，下一步应把大数值列（`units[].x/y/z/hdg`、`own` 各通道）
改为 **TypedArray + Transferable** 零拷贝传回；当前未做，因为那会改变"断点处为 `null`"的语义
（TypedArray 只能放 NaN），需要先核对消费侧对 `null` 的判断。

---

## ACE_ATTITUDE（飘带坡度）与地面单位 hdg（2026-10-22 增补）

three3d 渲染器画"飘带"要每架飞机的滚转角；地面单位不画朝向，但**判阵营要用航向**。
两者都由 `wrpl-parse.js` 在解析末尾给出。

### A. `window.ACE_ATTITUDE`（空中飘带坡度）

```js
window.ACE_ATTITUDE = {
  note, secPerTick, tick0, dt: 0.25,
  units: { "uid-<n>": { t0: <轴秒>, dt: 0.25, n: <点数>, bank: [<度>…] } }
}
```

* 来源：`ptype=2` FM 条目的 **EulerBytes**（`unpackEuler` 的 bank 分量，弧度→度），
  **纯回放数据**，不用 8111、不用预计算的 `attitude-data.js`（上传页要通用）。
* 时间轴与交付一致：轴秒 = (tick − tick0) × secPerTick；按 **0.25 s 固定网格**线性重采样
  （与演示页 `attitude-data.js` 的 `dt:0.25` 同口径），同 tick 取后者。
* 每次解析**整体替换**（旧 uid 不残留）；解析开始时先置 `null`。Worker 路径下按 uid 分片回传。
* 本局实测：**44 个 uid / 52 051 个点**（dt=0.25），非零 48 847（93.9%），
  |bank| 最大 179.9°（真有近倒飞样本；three3d 侧本来就有 ±60° 限幅），中位 0°。
* 自证（本机 uid-368）：FM bank 与座舱真值 `roll_deg` **相关 0.9968**、差值中位 **0.0°**、
  p10/p90 = −0.28°/+0.22° —— 说明 EulerBytes 的 bank 就是机体滚转角，且零位是"平飞 ≈ 0"。
* `meta.limits` 里写明："飘带坡度来自 FM（EulerBytes 的 bank 分量）→ 度，0.25 s 固定网格
  重采样（N 个 uid / M 个点，纯回放数据）"，并给出 `meta.attitude = { uidCount, points, dt }`。

### B. 地面/工事的 `units[].hdg`（只用于判阵营）

* 读 transform 的四元数（组件值起点 **+36**，4×f32；校验有限、|分量|≤1.001、0.97<|q|<1.03），
  无效 → `null`，不猜。分量顺序 `(x, y, z, w)`，世界系 x=东、y=北、z=上。
* **航向 = atan2(2(x²+y²)−1, 2(xz+wy))**，归一化到 [0,360)，单位度、0=正北、顺时针
  （与空中 `units[].hdg` 同口径）。写成单元素数组 `hdg: [deg]`，取不到为 `[null]`。
* **口径对齐上游**（不是另立）：上游 `build/wrpl/ecs_entities.json` 的 `hdgDeg` 是
  ECS 解析用本机 uid-368 的 8111 `compass=90.0°` 锚定算出来的。按 entityId 对齐本局：

  | 指标 | 值 |
  |---|---|
  | 地面/工事单位 | 184 |
  | hdg 有值（覆盖率） | **184/184 = 100%** |
  | 与 `hdgDeg` 偏差 中位 / p90 / 最大 | **0.000° / 0.000° / 0.000°** |
  | >1° 的条目 | 0 |

  同法验证锚点：本机 uid-368 的四元数 `(0,1,0,0)` → 90.00°（等于那个锚点）。
* 自验已固化：`web/dev/selftest-node.js` 里两条检查
  （`地面单位 hdg 覆盖率`、`地面 hdg 与上游 ecs_entities.json.hdgDeg 一致（≤1°）`）。
* `meta.limits` 里写明来源、只用于判阵营、**不渲染朝向**。

### C. 与 `mission-data.js` 的 counts 差异（唯一一项）

`counts.terrainContours`：Python 交付文件带**本局预计算的地形层**（26 条等高线），
上传页按"要通用"的要求**不引** `terrain/*_landmask.js` 与 `contours` → 该项恒为 **0**。
其余 25 项 `counts` 与 Python 逐项相同；自验把这一项单列说明，不算失败
（`selftest-node.js` / `dev/selftest.html` 都是"25 项全等 + terrainContours 单列"）。

### D. 出错口径（旧版本回放）

包流压缩格式不是 zstd/zlib、文件头 magic 不符、文件短于 1224 B 的回放头 → 解析器抛
`code: 'unsupported-format'`（带 `tech` 说明），上传页渲染成专门错误态：

* 标题：**不支持的回放格式**
* 正文：**该回放可能是旧版本游戏生成的，本工具只支持当前版本的回放格式。**
* 小字：`技术细节：包流压缩格式不是 zstd/zlib`（便于反馈问题）

**不说"文件损坏"**。进度条在错误态收起、上传区换成红边框 + ✕ 图标 + "重新选择文件"按钮。
