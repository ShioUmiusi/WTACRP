# ACE-COMBAT 独立验收报告（终版）

> 说明：文中 `web/dev/selftest*`、`dev/verify/*.png` 等为开发期自测页与截图，未随仓库提供。

> 复算工具：`tools/check_ace_mission.py`（`--selftest` → **36/36 通过**，13 个场景）
> 机器可读：`build/verify/report.json`（工具 46 项）／人读摘要 `report.md`／工具自证 `NOTES.md`
> **本文档共 50 项** = 工具 46 项 + 外部对拍 4 项（`8.browser-parse` 第 32 行／§3.18；**Worker 解析路径** 第 33 行／§3.19；**上传页文案与错误态** 第 34 行；**地形图层（交付+网页）** 第 35 行／§3.20）
> **被验文件指纹**（`file_fingerprint()` 自动写进每条证据；换文件后重跑即可对比）：
> - `mission-data.js`：mtime **2026-10-20 02:47:32**，size **4416089**，sha256(前16) **`72a93a77ef1a14a8`**
>   （全量 64 位：`72a93a77ef1a14a85d2eb3f97160ebeb615a009f52874d12520ca8a6bbcd6774`）—— **交付数据全部结论绑定此版**
>   （上一版 `9dc75ffdd1d4964b` / 4416074 B；本次只改了一句 limits 文案，**Δ+15 B**；terrain 与对齐数字经复跑逐项不变）
> - `web/index.html`：mtime **2026-10-20 02:49:54**，size **13862**，sha256(前16) **`0541fe1d61cc2714`**（全量 `0541fe1d61cc27141caf5c70854260e859aaa86b474fb92beb1df1e22b88053b`；此版：地形加载器同步 counts 三键 + limits 措辞更新）
> - `web/wrpl-parse.js`：mtime **2026-10-20 02:47:28**，size **138562**，sha256(前16) **`e1b06498554d4109`**（全量 `e1b06498554d41093f646bd264f7a1ece8a21a7de340ad1aaedad243d4cbca25`；本次改了一处文案，`node --check` 通过）
> - `web/wrpl-worker.js`：mtime **2026-10-19 13:54:15**，size **4589**，sha256(前16) **`5798f3628601a1f5`**
> - `web/vendor/fzstd.min.js`：8950 B（第三方，未改；`vendor/README.md` 有许可说明）
> - 地形素材：`terrain/air_skyscraper_city_contours.json` **506572 B / `5218de5e5ed80c43`**（与 `warthunder-maps/out/` 同名文件同字节）
> - 上一版（`2149b0b8e3472e61` / 3902218 B）的字节级证明见 §0 第 15o 行；本版相对上一版只多"类别层"，**并已逐字节证明**：把新增的 5 块去掉后重建的字节与该版**完全相同**（见第 15q 行，含独立复算与扰动对照）（236 units = 52 空中（全 10 Hz）/ 184 地面静态点；`shots[]` 1734；`runways[]` 4 条；`airfields[]` 4 点；**`terrain: null`**；side 236/236、型号名 236/236；`unitsWithDeath=31`）
> - `build/wrpl/tracks.json`：mtime 2026-10-19 06:54:34（回滚后重写，**内容与 06:36:13 版逐字节相同**），size 1555857，sha256(前16) **9a0acf452b63f04a**
> - `build/wrpl/header.json`：mtime **2026-10-19 06:38:46**（`generatedAtUtc` 2026-10-18T22:38:46Z，新增 `clock.deprecatedKeyNames`，数值未变）
>
> **格式解析四个中间产物的身份**（供按 sha 对齐“这一版验过”；`mpi_names.json` 的 `generatedAtUtc` 这次补齐）：
> | 文件 | 字节 | `generatedAtUtc` | sha256[0:16] |
> |---|---|---|---|
> | `header.json` | 35527 | 2026-10-18T22:38:46Z | `89057ccca4efa535` |
> | `entities.json` | 45461 | 2026-10-18T22:38:46Z | `878448dcb1f1000e` |
> | `kill_events.json` | 28899 | 2026-10-18T21:41:34Z | `f60a0e8d2f525baf` |
> | `mpi_names.json` | 9246 | 2026-10-18T22:42:53Z | `6884b2762d750429` |
> | `ground.json` | 25300±| — | （见 §3.10/3.12 的 mtime/sha，工具自动打印） |
> | `projectile_launches.json` | 454342 | — | `efdc162aae3ef56a`（mtime 07:21:04） |
> | `side_final.json` | 1673 | — | `20e72bfa4023a307`（mtime 07:19:50） |
>
> **终版结论：PASS 48 / WARN 2 / FAIL 0**（50 项；地形三处文本/计数不一致**已全部改掉并复验**（交付 limits 改为"已并入"、网页 counts 三键已同步、网页 limits 措辞已更新），见 §3.20 收口段；余下 WARN 仅 `2b` 开发期对照与 `7.ground`；此前一句为（网页 counts.terrainContours 未更新、网页 limits 留「浏览器版未做 terrain 图层」旧句），见 §3.20；`2b` 开发期对照；`7.ground` 见 §3.12）

## 0. 逐条判定

| # | 条款 / 检查项 | 判定 | 关键证据 |
|---|---|---|---|
| 1 | §5.1 build 脚本不读 8111 | **PASS** | `tools/build_ace_mission.py` 全文无 8111 抓包路径 |
| 2 | §5.1 `meta.source=="wrpl"` | **PASS** | `source='wrpl'` |
| 3 | §5.1 index.html 不引 `dev/**` | **PASS** | 无 `dev/` 引用；脚本仅 `mission-data.js → three3d/scene.js → mission.js` |
| 4 | §5.1 无外部资源/无 ES module | **PASS** | 外部资源 `[]`、无 `type=module`；页面 `performance` 资源条目 **0** |
| 5 | §5.1 units 的 z 存在且空中非常数 | **PASS** | 52 条 units：缺失 z 0 个、空中常数 0 个 |
| 6 | §5.1 devValidation 不带坐标 | **PASS** | 只有数字与说明（含 `speedViolations=0 / trackSegments / ownSegmentIds / ownP99ErrM …`） |
| 7 | §5.2 本机 z vs 8111（整段） | **PASS** | n=5573，**max 22.5 m / rms 2.3 m**（去常数 datum −0.4 m） |
| 8 | §5.2 本机 ias / mach vs 8111 | **PASS** | ias（×3.6 折算）max 63.8 / rms 8.5 km/h；mach max 0.055 / rms 0.008 |
| 9 | §5.2 本机平面 max/p99/rms | **PASS** | **max 35.0 m / p99 5.0 m / rms 2.1 m**（n=2793，两段拼接 + 平面残差最优对齐；p50 1.01 / p90 3.10） |
| 10 | §5.2 他机关联率 | **PASS** | 67.9%（半径 2 km）；作者口径 58.9%（150 m/0.15 s），同量级 |
| 11 | §5.2 速度包线（交付产物） | **PASS** | 53 条航迹 67804 步：>600 m/s **0 步**、单步 >5000 m **0 处**，最大 587 m/s |
| 12 | 速度包线（tracks 原始样本） | **PASS** | 52014 步：0 步 >600、0 处 >5000，最大 591 m/s（源头断轨已生效） |
| 13 | §5.2 与作者 devValidation 对照 | **PASS** | **6/6 项**同量级或"复算不差于作者"（见 §3.8；含作者新加的 `ownP99ErrM/ownMaxErrMExclSpawn/ownRmsErrMExclSpawn/ownSamplesOver50M`） |
| 14 | §5.3 结构 / 时间轴单调 | **PASS** | 契约 §2 字段齐全、events 严格单调、counts 逐项自洽（52 航迹/65 事件/33 击杀/2 本机杀） |
| 15 | §5.3 窗口与回放时钟 | **PASS** | 轴判定 tick0 原点轴（65/65 吻合）；`window.t1+31.103 = 589.949 = EndMarkerSec`（差 **0.000 s**）；own 覆盖 100%、units 99.9%；clock 旧名走 `deprecatedKeyNames` 机读映射核对 |
| 15b | 交付范围与解码产物一致 | **PASS** | tracks 54 段 − own 2 段 = **52 空中**；`ground.json` 184 → 交付 **184 地面**；合计 **236** 条 units（air 52 + ground 184），`counts.groundUnits=184` 自洽；无 `missiles` 等未说明字段（§3.9） |
| 15c | `ground.json` 源产物（可选） | **PASS** | 184 条（ground 115 / structure 69）；`spawnTickRaw×secPerTick` 5.53–546.76 s 在窗口内；**0/184** 落在航迹包围盒（放宽 3 km）外 |
| 15d | **地面层独立复算**（交付文件 → 8111） | **WARN** | 从交付文件取 184 点：与源 `ground.json` 坐标 **max 0.000 m**（合并无改动）；独立复算最近邻**中位 6.81 m**、**≤100 m 61.4%**、错轴对照 **4759 m**；透传为 0.03 m / 66.3%。比例与对照一致，**0.03 m 的绝对精度用本机航迹拟合的变换无法复现**（§3.12） |
| 15e | **弹药发射点独立复算**（`3e`，只用回放自身数据） | **PASS** | 1734 条：按"同刻 ±0.6 s **全部**航迹样本"算最近飞机**中位 15.05 m / p90 34.2 / p99 45.6 / max 74.5 m / ≤100 m 100.00%**、>1000 m **0 条**；对照组 y/z 互换 **12405 m**、北向取反 **6135 m**（米级 vs 公里级）；模板计数与 `byTemplate` 一致（1589/95/29/17/4）；`|q|` 全部在 [0.98,1.02]；无 (0,0,0) 填充、无近原点 |
| 15f | **阵营消白核对**（`7.sides`） | **PASS** | **全部 236 条 units 都有侧别**（空/unknown **0**）：空中 ally 24 / enemy 28；地面 ally 125 / enemy 59（`sideInferred=true` 184 条，纯回放推断）；8111 颜色族交叉核对（52 条全部可核对）：**as-is 45/52（87%）** vs **取反 5/52（10%）**；`side_final.json`（sha `20e72bfa4023a307`，锚点=33 个已定 FM uid、自检 knownCorrect 32 / knownWrong 1） |
| 15g | **`shots[]` 结构与合并保真**（`3f`） | **PASS** | 1734 条；模板计数 1589/95/29/17/4 与 `byTemplate` 一致；`counts.shots=1734`；`t` 单调（4.413–532.456 s mission 轴）；`tickRaw` 全在 `[7962368, 151026944]`；坐标有限；`side` ∈ 枚举或 null（null 66 条）；与源 `projectile_launches.json` 按 `(tickRaw, template)` 取最近候选配对 **1734/1734**、最大差 **0.70 m**（整数米舍入，理论 ≤0.87 m）；**`yaw` 1734 条全 null**（作者口径，见 §3.11） |
| 15h | **`deathTickRaw` ↔ `killPositions`**（`3g`） | **PASS** | 31 个 `deathTickRaw` **31/31** 对上 `tracks.killPositions` 的 victimUid+tick（±2 tick）；`killPositions` 侧 3 条未被引用（victimUid 216/215/299，无 FM 航迹的受击者），已列出 |
| 15i | **空中段 10 Hz 上采样与 null 断点**（`7b`） | **PASS** | 52 段 hz 全 10、**总点数 124512**（与预期一致）、有限步 124355；上采样后**超 600 m/s 0 步、单步 >5000 m 0 处、最大 591 m/s**；null 样本 100 个；**落在源航段范围外却非 null 的 0 个**（断点未被连线）；own n=5589、接缝处 null 16 个。点数口径和解：连续段满足 `n = 源跨度×10+1`（含两端点）；差为负的段是有 null 或该 uid 由多段组成（本单元只覆盖其中一段），**不是桥接** |
| 15j | **显示名零非 ASCII / 无回退泄漏**（`7c`，加严口径） | **PASS** | **四个来源全部零非 ASCII**：`units[].vehicleName` 0、`shots[].templateName` 0（`ROCKET`/`BOMB`/`ARH ROCKET`/`PAYLOAD`/`JETTISONED`）、`runways[].name` 0（`AF-0x RUNWAY`）、`airfields[].name` 0；31 个 vehicle 映射一致、无 template 回退泄漏、全文件无 `AIR`/`GND`；覆盖 215/236（差的 21 个是 hidden 单位、名字按约定为空串），`counts.unitsWithDisplayName=215` 相符。另核：精确模式 `"vehicleName":"ARTILLERY"` **恰好 40 次**、`LARGE ARTILLERY FORTIFICATION` **0 次残留**。`limits` 里的中文说明属说明文字、不算显示名 |
| 15k | **`runways[]` 线段独立复算**（`runways`） | **PASS** | 4 条线段：两端距离 **2999.9–3000.6 m**（报 3000.0，差 ≤0.60 m）；由位移反算航向 155.30/323.63/168.15/347.78°（与 `headingDeg` 差 ≤0.05°）；**两端 z 差全为 0.00 m**（自证阈值 ≤8 m）；两队成对反向：af-01↔af-02 夹角 168.30°（离 180° 差 **11.70°**）、af-03↔af-04 179.60°（差 **0.40°**）；`airfields[]` 结构数 **13/13/3/3**（主/备）；机场点到最近跑道线垂距报告但不断言（如 af-01→rw-af-01 = 434 m，投影 t=0.00） |
| 15l | **`terrain` 层：结构 / 地图一致性 / 数值 / 对齐**（`terrain`） | **PASS** | 结构齐全（`meta/contours/water/coastlines`）、`crs="replay-world-meters"`、`meta.map` 与本局关卡一致；**26 层等高线（elev 0–5000，间隔恒为 200 = contourIntervalM）、5 水体（393 点）、5 海岸线（393 点）**，共 30676 点：**非有限 0、越界 0**，`counts.terrainContours=26 == contours.length`。**独立对齐复算**（独立脚本，用生成源 1024² 栅格，重跑于新哈希上）：184 地面点 r=**0.9950**、中位 **3.89 m**、p90 **21.31 m**、**92.9% ≤30 m**；对照：翻转 y r=0.2986/82.01 m、转置 r=0.1173/373.44 m、x/y 平移 10 格 r≈0.975–0.980/中位 11.6–13.6 m；本机 5571 采样低于地形 **0 个**、最小离地 +11.5 m。**原 WARN 已消除**：交付 `limits` 不再写"当前为 null"，改为「已并入…已用 184 个地面点核对（r=0.995…）」 |
| 15m | **`hidden` 集合与其消费**（`7e`） | **PASS** | 数据层：hidden **21 = 9 + 8 + 4**（`dummy_immortal` 9 / 机场飞行雷达 8 / 机场主雷达 4）、`counts.unitsHidden=21`、21 条 `vehicleName` 全空串、`limits` 写明理由；**消费侧已接上**：`three3d/scene.js` 有 `if (u.hidden === true) { continue; }`（静态定位到该行）；node 驱动与浏览器两端分别实测 `stats.units = 215 = 236 − 21`，三者一致 |
| 15n | **`meta.squad` 与回放内 kill/death 指纹**（`meta.squad`） | **PASS** | `squadId=2002`、2 名成员、`authorUid` 归一化后 == `tracks.ownEntityId`（368）；**独立复算** 33 条 `killPositions` 得每 uid 的 (kills,deaths)：`f_15a` 候选 `{351,357,359,370}` → `{351:0/1, 357:2/0, 359:2/1, 370:2/1}`，按结算指纹 `kills=2, deaths=0` 过滤后**唯一剩 357** == 声明 uid（与作者 `fingerprintPerCandidate` 逐项一致，无差异）；作者成员（八幡 海鈴）无 `uidEvidence`，按 own-uid 判据只核 uid 对齐（368）✓；结算表 `humanKills` 32/32 全 0（`aiKills` 不在 `matchResults` 里，在 `header.json` 的 players 中）|
| 15o | **窄口径补跑** | **PASS** | ① `7c` 四类零非 ASCII；`ARTILLERY` 40 次、长名 0 残留。② 字节级"只差一处"证明成立（还原后 size/sha 与上一版逐位相同；扰动后立即不同）。③ 发射点关联率（`shots[].shooterUid` 推断命中）**1694/1734 = 97.7%**（按交付数据直接统计，与导出脚本一致；该指标≠ `3e`「发射点到最近飞机」15.05 m/≤100 m 100%，两者口径不同）|
| 15p | **单位类别层**（`7f`，新增） | **PASS** | 236/236 有 `vehicleCategory`；独立算的分布 **FIGHTER 49 / TANK 48 / ARTILLERY 40 / SPAA 35 / TRUCK 32 / RADAR 12 / BUNKER 8 / FORTIFICATION 9 / ATTACKER 3 = 236**（与预期分布逐项一致）；**9 个词、零非 ASCII、无 UNKNOWN/NONE/OTHER/NULL 兜底字样**；`meta.vehicleCategories`：`source`（datamine v2.59.0.73；显示约定、非回放数据）、`field="units[].vehicleCategory"`、`keys=37`、`vocabulary` 齐全，`unitsWithCategory=236`、`unitsUncategorized=0`、`uncategorizedValues=[]`；**语义交叉核对 0 处矛盾**（域 vs 类别、内部名含 radar/artillery/bunker 的对应）；hidden 21 条的类别 = RADAR 12 + FORTIFICATION 9 且 `vehicleName` 仍为空串；类别源表按**磁盘字节**：`vehicle_categories.json` mtime 10:38:06 / size 5326 / sha256(前16) `a3ccafaef47f0773` |
| 15q | **"只多一层字段"的独立核对** | **PASS** | **已逐字节证明**：把类别层的 5 块从定版里去掉后，重建字节 **size 3902218、全量 sha256 `2149b0b8e3472e613bfda263d1c1f7b0e0d2465f6f8e96a4333b03e8b07db9d6`**，与上一版**完全相同**；扰动 1 个字节后 sha 立刻不同（比对本身能失败）。实测字节账（含 JSON 逗号，UTF-8）：① 236 个 `,"vehicleCategory":"X"` **6400 B** + ② `,"vehicleCategories":{…}` **345 B** + ②b `,"unitsWithCategory":236` **24 B** + ③ hidden limit 追加子句 **114 B** + ④ 类别 limit 元素 **404 B** = **7287 B** = 实际字节差。**结论：既有字段（`provenance`/`meta` 其它字段/`counts` 键序/全部既有文本）"一字未改"在字节层面**成立**，重建逐字节相同即为证明。**更正**：上一版报告里的"净差 247 B、疑似既有文本被改写"是**量测错误**，不是数据问题 —— 错在 ① 用 `rfind("{")` 找 meta 块，匹配到了它前面的嵌套对象（`formationDistM`），量成 616 B（实际 345 B，多 271 B）；② 漏了 `counts` 里新增的 `unitsWithCategory`（少 24 B）。271 − 24 = 247，与净差完全对上。**该说法已撤回** |
| 16 | §5.4 events ↔ 击杀包 | **PASS** | 33 击杀 **tick 逐条相等 33/33**；独立分帧 `payload.bin` 也是 33/86773 |
| 17 | §5.4 位流 ↔ 前缀扫描两条路径 | **PASS** | 33/33（tick+载具）完全一致；三向归属吻合（见 §1） |
| 18 | §5.4 玩家击杀数 | **PASS** | 四源一致 = 2（事件归属/ownKillsFromResults/matchResults/player.kills），与 8111 窗口内原文 2 条相符 |
| 19 | §5.4 奖项对齐 | **PASS** | `events(award)+events(lastblow)=27=len(awards[])`，逐类名与 `firstSec` 对上 |
| 20 | §1 tick 口径 | **PASS** | `tick=raw 256 kHz`、`tsMs=毫秒`，`tick/256==tsMs` 33/33；`secPerRawTick` 与 `meta.replayTicks.secPerTick` 一致；`replayTick` 与 killPackets 精确相等；旧键名已移除并有 `deprecatedKeyNames` 映射 |
| 21 | §5.5 `file://` 打开 | **PASS** | 三个全局对象齐全；`ready=true`；无非有限值 |
| 22 | §5.5 0 网络 / 0 外部资源 | **PASS** | `performance.getEntriesByType('resource')` **0 条**、`external=[]`；DOM 无外链 |
| 23 | §5.5 播到结尾 | **PASS** | 8× 播到 **met=558.846 = duration**，随后 `playing=false`、角落显示"已到结尾" |
| 24 | §5.7 时间轴单调 / seek | **PASS** | seek 0/50/300 精确、越界钳到 558.846；`←/→` ±10 s |
| 25 | §5.7 暂停不前进 | **PASS** | pause 后 900 ms `met` 不动（200 → 200） |
| 26 | §5.7 可倍速 | **PASS** | 4× 实测 **4.00×**（墙钟 38.34 s ↔ 任务时间 +153.33 s） |
| 27 | §5.7 无 NaN 崩坏 | **PASS** | 全通道扫描（own 15 + **236 units×4**）非有限值 **0**；DOM 无 `NaN/Infinity`；`trailSkipped=0`；结尾 own 缺失时显示 `--` 而非 0 |
| 27b | 静态地面点**始终可见** | **PASS** | 源码：`three3d/scene.js:231` `isStatic: !!u.static \|\| u.n <= 1`、`:261` `if (u.isStatic) { return (t >= u.t0 - 0.001) ? 0 : -1; }` → 静态点在任意 `t ≥ t0(=0)` 都取样本 0，**不按存活窗口消失**；运行期：`stats.units=236` 在 t=0.5/120/300/558.8 四点均为 236，琥珀色地标像素四点均 >0（485/92/42/17，随 top 机位移动变化属预期），`05-ground.png` 可见地面标记 |
| 28 | §5.6 截图 | **PASS** | `dev/verify/{01-cruise,02-kill,03-top,04-end,05-ground}.png`（均 1280×800；前四张为 52-air 版，`05-ground.png` 为 236-units 版、可见地面标记） |
| 29 | §2.4 composed 标注 | **PASS** | 65 条事件全有布尔 `composed`（true 60 / false 5，chat 为原文） |
| 30 | 缺值用 null/`--` | **PASS** | own 通道无整列 0/常数的填充痕迹；界面对缺失显示 `--` |
| 31 | `2e/2c` 口径文档化 | **PASS** | 引用 `DATA-NOTES.md §1.3`（奖项）与修订后 §2.6（击杀归属）；本文档实测指纹（磁盘字节）：`a9c873560730a7bd` / 30531 B / 2026-10-19 11:54:10（含 §8 的类别层字节账双向留痕，与导出脚本自报一致） |
| 32 | **浏览器内解析 == Python 产物**（`8.browser-parse`） | **PASS** | 同一份 `.wrpl`（5133517 B）：另一侧 Node 自验 **0 FAIL**；**本侧实现**（`build/verify/js/dump_parse.js` 导出 + `build/verify/compare_js_vs_py.py` 逐项比对）**54/54**；浏览器内（`web/dev/selftest.html`）**18 项检查 FAIL 0、解析 1.475 s**。counts **26 项全等**；`own.n=5589` 且逐点 **max 0.100 m**（x/y=0.0000，23/5589 落在 0.1 m 舍入边界）；t=55/233/500 s 坐标逐位相同；空中 52 段逐点 ≤0.100 m；**33 条击杀 (raw tick, victimUid, killerUid) 逐条一致**；**184 个地面点按 key 逐条一致**（entityId/坐标/显示名/类别）；4 条跑道端点 0.000 m；4 个机场点；**1734 个发射点逐字段一致**（含 yaw）；`meta.replayHeader`(24 键)/`player`(10)/`matchResults.players`(32 行×8 字段)/`vehicleRoster`(20 键) 的值**与键序**全等；三条限制如实标注（见 §3.18）。耗时：Node 0.84–0.86 s、浏览器内 1.475 s。**上传页真实 drop 路径**：另一侧端到端 **1.55 s**（截图 07），**独立复现 1.58 s**（截图 08），两侧都读到 `#upload-note` 完整回执、`units=215`（236−21 hidden）、无错误框、`__ACE_ERRORS=null`；`ACEUI.state()`（**调用**，不是读属性）mount 后为 10 键、`playing=true`、`contacts=116`、`own` 13 通道 |
| 33 | **Worker 解析路径**（性能 / 帧间隔 / 与主线程一致性 / 回退说明） | **PASS**（缺陷已修并复验） | **逐字段一致性**：worker 与 `force:'main'` 的 `own` 都是 **18 个通道**，逐通道、逐样本比较 **0 处差异**（含 `Number.isNaN` 位置一致），`x[0]=-26954`、`z[10]=1063.6` 两侧相同。**性能**：worker 端到端 **1698 ms**、主线程最大帧间隔 **17.7 ms**（中位 16.7、**>50 ms 帧 0**）；main **1629 ms**、**132.4 ms**（>50 ms 帧 4）。**真页面 drop（默认 worker 路径）**：`ACEUI.state().own` **13 通道**、`ownHasPos=true`、`ownMissing=false`、`ACEScene.getStats().ownScreen={x:634.7,y:457.8}`、角落读数 **高度 145 m / 空速 323 m/s M 0.95**、**0 报错**。**回退两条都验过**：`worker-unavailable` → limits「解析路径：主线程回退（Worker 不可用：Worker 创建失败：worker-unavailable）」；环境无 `Worker` → limits「解析路径：主线程（环境不支持 Web Worker）」，两条都同步写 provenance 且 `own` 仍 18 通道。**meta 键集合已一致**（worker/main 都含 `__workerHeapPeakMB`）—— 仅取值不同：worker `0`、main `null`（见 §3.19 的建议） |
| 34 | **上传页文案与错误态**（`web/index.html`） | **PASS** | 源码与 DOM 里 `技术细节`/`座舱`/`移植自` 均 **0** 次；`#err-hint` DOM 为 **null**（无悬空引用）；错误态实测 `#err-title`=「不支持的回放格式」、`#err-detail`=「该回放可能是旧版本游戏生成的，本工具只支持当前版本的回放格式。」、重试按钮可见；`本机属性表（ReplayCockpitParams）` 已统一（源码 9 处）；垃圾字节在两条路径都 1–15 ms 内报「不支持的回放格式」 |
| 35 | **网页地形图层**（`web/index.html` 按地图名加载） | **PASS** | 真实 drop（新哈希）：同源请求地形素材 1 次、**外部请求 0**；挂载对象 `terrain` 26/5/5、`meta.map` 与关卡一致、`crs` 正确；**`meta.counts` 三键已同步**：`terrainContours=26 / terrainWater=5 / terrainCoastlines=5`；`limits` 措辞已更新为「terrain 等高线不内嵌，由页面按 meta.replayHeader.level 的地图名向服务器加载静态素材（加载时校验 map 一致 + crs 正确，否则留空不编）」，**不再有"未做 terrain"旧句**；像素证据见 §3.20（`setOption("map")` 两帧差 125125 像素），截图 `dev/verify/09-web-terrain.png` |
| 32 | `2b` hudmsg 行数对照 | **WARN（设计内）** | events 65 vs 窗口内去重 hudmsg 153；v5 §1 规定权威时间轴与事件数来自回放，8111 仅开发期对照 |

## 1. 独立复算的原始证据（命令与关键输出）

```powershell
python tools/check_ace_mission.py              # 全量：PASS 44 / WARN 2 / FAIL 0（46 项）
python tools/check_ace_mission.py --selftest   # 36/36
```

- **本机高度**：`own.z vs 8111 H,m：n=5573 原始 max=22.9 rms=2.4 m；去常数 datum −0.4 m 后 max=22.5 rms=2.3 m`
- **本机平面**：`offset 4a 映射 84.52 s → 按平面残差微调后 84.56 s；相似变换后 max=35.0 p99=5.0 rms=2.1 m（scale=1.000010 det=+1.000）`；
  误差分布 `p50=1.01 / p90=3.10 / p99=4.99 / max=34.96 m，>50 m 样本 0 个`
- **速度包线**：`交付 53 条航迹共 67804 步：超 600 m/s 0 步，单步 >5000 m 0 处，最大 587 m/s`；
  `tracks.json 原始 52014 步：0 步 >600、0 处 >5000，最大 591 m/s`
- **事件**：`events(kind=kill) 33 条与 entities.json killPackets 按 sec 逐条一一对上（tick 精确相等 33/33）`
- **三向归属吻合**（独立复算）：`kill_events` 里 `KillerUid==368` 的击杀 **2 条**（tick 51533056 / 144747520，
  即 raw 秒 201.301 / 565.420）↔ mission-data 里 `isPlayerActor` 的击杀 **2 条**（同一组 tick）↔ 内嵌结算本机 kills **2**
  ↔ 8111 hudmsg 窗口内原文 **2 条**。→ "回放侧无法归属玩家"已被推翻，且这条链**不依赖玩家名、不依赖 8111**。
- **tick**：`tick=raw 256 kHz、tsMs=毫秒、两者精确互换；与 mission-data.replayTick 同空间（逐条相符）`
- **窗口**：`window.t1=558.846（mission）+ 原点 31.103 = 589.949 raw s vs EndMarkerSec=589.949（差 0.000 s）`；
  33 个击杀包 sec 全在窗口 raw 范围 [31.103, 589.949] 内
- **8111 原始基线（不依赖任何交付物）**：hudmsg 去重 154 行（kill 33 / damage 47 / disconnect 58 / award 2 / crash 1 / other 13）；
  `payload.bin` 独立分帧 86773 条记录、击杀包 33 个；state 7038 帧 / 跨度 715.8 s；接触点 85647

## 2. 浏览器实测（`file://`，定版数据）

| 项 | 结果 | 原始证据 |
|---|---|---|
| 全局对象 | PASS | `ACE_MISSION:object`、`ACEScene:object`、`ACEUI:object` |
| 资源请求 | **0** | `performance.getEntriesByType('resource')` → `[]`；`external=[]`；`script[src]` 只有 3 个本地文件 |
| 数据规模 | — | `counts: ownSamples 5589 / trackCount 52 / events 65 / kills 33 / ownSegments 2 / envelopeFiltered 15` |
| 播到结尾 | PASS | `554.846 → 558.846 = duration`，`playing=false`，角落"已到结尾" |
| 帧率 | PASS | `fps≈144`、`frameMs≈6.9`、`ready=true`、`units=52`、`trailSkipped=0` |
| NaN | PASS | 全通道非有限值 `[]`；`domHasNaN=false` |
| 缺值显示 | PASS | 结尾处 own 无样本时角落显示 `高度-- m 空速--`（不是 0） |
| 倍速/暂停/seek/机位 | PASS | 见 §0 第 24–26 项与"机位循环 follow→orbit→top→free" |

截图（`dev/verify/`，全部在定版数据上重拍）：

| 文件 | 内容 |
|---|---|
| `01-cruise.png` | T+02:00 follow：黑底绿网格、青/橙发光航迹、`◇+大写名`标签、**垂直投影竖线（底端小十字）**、角落读数（含 8111 开发期对照数字与来源/局限）、底部控制条（带击杀刻度） |
| `02-kill.png` | T+02:50.6（本机首杀 t=170.198）：击杀标记（橙色 ✳ + 扩散环）与 `◇MIRAGE`（本机）/`◇F-20A`（受击者）标签 |
| `03-top.png` | 同一时刻 `top` 机位俯视全战场 |
| `04-end.png` | `met=558.846` 结尾画面与到点状态 |
| `05-ground.png` | 236-units 版、`top` 机位 t=300 s：可见**地面静态标记**（暗琥珀小十字散在网格上）与 22/236 目标计数 |

## 3. 判定口径与过程透明

1. **`4d` 曾判 FAIL，已在定版文件上转 PASS**：第一次跑的是导出脚本做包线滤除**之前**的版本（19 步 >600 m/s）。
   当前文件已滤除 15 点，0 违规。→ 判定必须与被验文件指纹绑定，已给每项证据加 mtime+sha256。
2. **`4c` 判定口径按裁定**：`rms≤15 m 且 p99≤50 m` 达标，`max` 单独列出并注明离群；定版三项全部达标。
3. **工具侧偏差（已修）**：`fit_time_mapping` 原把 tick rate 当自由参数扫（候选含早期人肉拟合的 256472），
   在断轨后的较短航段上会把 offset 拉偏 ~1 s（85.54 vs 正确 84.52），曾造成 1/1b/4c 假 FAIL。
   现固定 `rate = 1/tracks.secPerTick`（与 `header.clock.secondsPerRawTick`、独立分帧的击杀包 tick 三重互证），
   并按 `ownSegmentIds` 分段取样本后汇总。相关记录：导出脚本 `DATA-NOTES.md §6`、`REPLAY-FORMAT.md §3.1`。
4. **`tick` 单位口径的跨产物缺陷**（报告后由格式解析修好）：早先 `entities.json.tick` 是毫秒、
   `mission-data.replayTick` 是原始 256 kHz 计数器，比值恒为 256.0000。统一为 `tick=raw`、`tsMs=毫秒`。
5. **`side` 方向**：按"两种极性都算"判。当前交付可交叉核对 **48 个空中实体**，as-is 一致 **42/48（88%）**、取反 **5/48（10%）**，
   另 34 个 `unknown`。结论：**枚举合法、未用 8111 颜色猜补**；方向本身**未被独立闭合**（见 `QUICK-REF.md §3.1` 与导出脚本 `limits`）。
6. **`4d2` 的定位**：它查 `tracks.json` 原始样本（上游质量项）。航迹解析已按"相邻采样隐含速度 >600 m/s"在源头断轨
   （44 uid → 54 段、丢 6 个碎片段），现 0 违规；`mission-data.js` 侧另有自己的包线滤除（15 点），
   `meta.devValidation.speedViolations=0` 与 `speedViolationsBeforeSplit=16` 并存，不会再误读。
7. **`det` 符号的澄清（航迹解析提出，已核实）**：用**原始 8111 map y**（屏幕轴、向下为南）直接拟合，
   必然得到 `det=−1`；把 8111 map y 先取反再拟合才得到 `det=+1`。`world_xy()` 已经做了这个翻转，
   所以终版结果是 **`det=+1.000`、旋转 0.00°、scale 1.000010** —— 即两套坐标**同向、无镜像**。
   `det=−1` 是"用原始 map y"时的应然中间结果，不是异常。
8. **本机精度的四种口径（都是对的，勿混用）**：作者按自己的 4 Hz 样本逐点比，得到
   `max 130.2 m（t_rel=53.45 s 出生/起飞过渡）/ p99 6.43 / rms 4.00 / p50 1.47 / p90 3.79`，
   并有 `ownSamplesOver50M=2`、`ownMaxErrMExclSpawn=16.71`、`ownRmsErrMExclSpawn=2.44`；
   把两段航迹插值到 8111 平面位置帧（5 Hz）并按平面残差最优对齐，得到
   `max 34.96 / p99 4.99 / rms 2.12 / p50 1.01 / p90 3.10，>50 m 样本 0 个`。
   两者 rms/p99/p50 都在米级且互相吻合；差异来自采样点集合与是否剔除出生/重生过渡样本。
   `4e` 对 `ownMaxErrMExclSpawn` 明确标注"口径不同、只报告不判定"。

9. **交付范围（导弹/武器实体）**：`tracks.json` 顶层无 `missiles/rockets/bombs` 字段，交付 52 条 units 的 domain 全为 `air`，
   且 `tracks 54 段 − ownSegmentIds 2 段 = 52` 与交付条数自洽（工具项 `3c.entity-scope`）。
   航迹解析 `REPLAY-DECODE.md §12` 记录：`ptype=2` 里没有导弹（44 个 uid 全是飞机，最短存活 99.2 s），
   另有 1589 个 rocket 实体但逐帧位置未在任何已解出通道，故不写 `missiles` 字段。
   **无法独立验证“回放里确实没有导弹”**，只能确认交付范围与该说明一致、且没有未说明的实体类别混进交付。

12. **地面层（184 个静态点）并入交付物后的复核**：交付 `units` 从 52 → **236**（air 52 + ground 184），与源 `ground.json` 逐点坐标 **max 0.000 m**（合并未改坐标）；`3.structure` 对静态单位只要求 `key/side/domain/hasAlt/t0/hz/n/x/y/z`（`hdg` 允许缺，已标注为提醒）；`6c` 按 `domain` 判定：**空中 0 个常数**、地面 184 个常数（合理）；`4d` 只统计航迹（地面 `n=1` 不进包线，避免误报）。**唯一的数字分歧**：独立复算的最近邻中位 **6.81 m** vs 透传 **0.03 m**（差 227×）——原因是 replay→8111 变换由**本机航迹**拟合（本身 rms 2–35 m），无法达到 ECS 侧变换的绝对精度；同一方法下 **≤100 m 比例 61.4% vs 66.3%**、**错轴对照 4759 m vs 3632 m** 两项都一致，轴向结论可复现。因此 `7.ground` 记 WARN 并**明确不采信 0.03 m、也不据此判 FAIL**。

13. **工具注意事项（两条"判据必须先能失败"的实测教训，两侧各有一条）**：
    - **最近邻要取时间窗内全部候选**：航迹解析第一次只取时间上相邻的 3 条飞机样本，得到"发射点离所有飞机 15 km"的假否定；改为同刻 ±0.6 s 内全部样本后中位 15.05 m。`3e` 按「全部候选」实现。
    - **量级检查 ≠ 有效性检查**：`projectile_launches.json` 修前有 2 条位置是 `(0,-0,-0)` 全零填充，而量级判据里全零**永远合法**，于是被当成有效点收下（表现为 2 条 >1000 m 离群、max 6468 m）。现在 `3e` 单独挡"全 0 填充"与"离原点 <100 m"，并在证据里写明这条。
    这两条与此前修掉的 `fit_time_mapping` "把 tick rate 当自由参数扫"属同一类：**判据本身先要能失败**。
    - **（又一次踩到）配对键重复要按候选列表处理**：`shots[]` 里同一 `(tickRaw, template)` 会有多发齐射（本局 **322 组**重复键），第一版用 dict 建索引，后写的条目覆盖先写的 → 配对错位，一度报出"与源文件坐标差 38782 m"的假缺陷。改成"同键候选列表 + 取最近者并消耗"后，最大差回到 **0.70 m**（整数米舍入）。**同一类教训：判据的配对逻辑本身也必须先能自证。**
    - **（第三次自查）NaN 会吞掉"最大值"判据**：`envelope_stats` 原来用 `v.max()`，而数组里只要有一个 `null`（空中段共 100 个），`np.max` 就返回 `nan`，`if m > max_v` 于是永不成立 → `7b` 一度打印"最大 **0 m/s**"却仍判 PASS。改成 `np.nanmax` + 只统计有限步后，真实最大值 **591 m/s**（0 违规的结论不变，但"最大 0"本身就是判据失效的信号）。**教训：任何聚合判据都要先问"全是 NaN 时它会说什么"。**

    - **（第四次自查）量一块文本时，要锚定"这个键对应的值"，不要就近找括号**：核对 `meta.vehicleCategories` 的大小时用了 `rfind("{")`，结果匹配到它前面那个嵌套对象（`formationDistM`），把一个 345 B 的块量成 616 B，并据此推断"数据里有一处既有文本被改写"——**推理方向完全错了**。改成"从该键的冒号后第一个 `{` 开始做括号配平"后，重建字节与上一版逐字节相同。**教训：定位结构要用键，不要用位置最近的分隔符。**

### 3.20 地形图层（第 15l / 35 行）

**交付侧（`mission-data.js`，`9dc75ffdd1d4964b` / 4416074 B）**：`terrain` 由 `null` 变为实体；判据走非空分支——契约 §9.2 结构、`crs`、`meta.map` 与本局关卡（`levels/air_skyscraper_city.bin` → `air_skyscraper_city`）一致；另加四项：`counts.terrainContours` 与层数一致、等高线 `elev` 严格递增且间隔等于 `contourIntervalM`、嵌套坐标（`contours[].lines` / `water[]` / `coastlines[]`）全部有限且在 `bounds` 内、`terrain` 非空而 `limits` 仍写 null 时报警。

**独立对齐复算**（`build/verify/verify_terrain_align.py`，**不复用既有脚本**；高度栅格用 `warthunder-maps/out/air_skyscraper_city_heightmap.npz` 的 `h`(1024²)，origin/cell 取自交付 `terrain.meta`）：184 个地面点 r=**0.9950**、中位 **3.89 m**、p90 **21.31 m**、**92.9% ≤30 m**；对照：翻转 y → r=0.2986 / 中位 82.01 m；转置 → r=0.1173 / 373.44 m；x/y 平移 10 格 → r≈0.975–0.980 / 中位 11.6–13.6 m。本机 5571 采样**低于地形 0 个**、最小离地 **+11.5 m**。结果落 `build/verify/terrain-align-mine.json`。

**网页侧（`web/index.html`，`4fef917324af76e1` / 13430 B）**：加载器对 `../terrain/<map>_contours.json` 有两道硬闸（`crs` 正确、`meta.map` 与关卡名一致，否则整层留空）；实测命中 1 次同源请求、**外部请求 0**、挂载对象 `terrain` 已填、`meta.limits` 追加地形说明；像素法证明图层真的画出来了（125125 像素差）。

**三处文本/计数不一致（均已列出，不影响几何与渲染）**：
1. 交付 `limits` 仍写「terrain 当前为 **null**」——与实体地形矛盾（建议改成「由 LandMesh 等高线/高度栅格生成」）；
2. 网页侧 `counts.terrainContours` 仍为 **0**（加载器只写 `terrain`，没同步 `counts`）；
3. 网页 `limits` 仍留「浏览器版未做…terrain 等高线图层」旧句（现在已做）。

**收口复验（新哈希 `72a93a77ef1a14a8` / 4416089 B；`web/index.html` `0541fe1d61cc2714`；`web/wrpl-parse.js` `e1b06498554d4109`）**
- 交付 `limits`：**不再出现「当前为 **null**」**，出现「**已并入**…（source=landmesh、1024×1024、格距 64.03 m、等高距 200 m、26 层；5 水体 + 5 海岸线）…184 个地面点核对（r=0.995、中位 3.89 m、92.9% ≤30 m；翻转对照 r=0.300）」；
- `terrain` 仍 26/5/5，`counts.terrainContours=26`；对齐脚本在新文件上**重跑逐项不变**（r=0.9950 / 3.89 / 21.31 / 92.9%；本机低于地形 0）；
- 网页：`meta.counts` 三键 **26/5/5 已同步**、`limits` 措辞已换成"按地图名向服务器加载静态素材（校验 map+crs，否则留空不编）"、旧"未做"句已消失；外部请求 0；
- 体积/文案：**Δ+15 B**（4416089 − 4416074），与"只改一句 limits 文案"一致（其余 46 项工具检查与对齐数字均未变）。

### 3.19 Worker 解析路径的验收（第 33/34 行）

**方法**：`python -m http.server 8905 --directory <repo> --bind 127.0.0.1`（托管后台任务，用后即停）→ `http://127.0.0.1:8905/web/index.html`；页面里 `fetch` 真实回放（5133517 B）→ `File`/`DataTransfer` → 真实 `drop` 事件；另外直接调 `WRPLParse.parse(buf, {force:"main"})` 与默认（worker）两条路径分别计时，并用 `requestAnimationFrame` 记录主线程帧间隔。

**结果**：见第 33/34 行。**关键缺陷**：worker 路径解出的 `own` 缺 10 个通道 → 本机在页面上完全不显示（`ownScreen=null`、读数只有标签），而且**没有任何 JS 报错**，只有数据静默丢失；同一份数据走主线程则正常。

**原因（一处，一行可修）**：`web/wrpl-parse.js` 的 `worker.onmessage` 里
`if (m.key === "units") store.units = store.units.concat(m.data); else store[m.key] = m.data;`
而 `wrpl-worker.js` 的 `sendOwn()` 把 `own` 拆成 4 条 `part` 发，于是主线程每收一条就**覆盖** `store.own`，只剩最后一条（8 个座舱通道）。修法：`if (m.key === "own") store.own = Object.assign(store.own || {}, m.data); else if (m.key === "units") …; else store[m.key] = m.data;`

**顺带核到的两处小缺口**（**均已修并复验**）：① 环境里 `Worker` 不存在时静默走主线程、limits 无说明 → 现在写入「解析路径：主线程（环境不支持 Web Worker）」（limits + provenance 各一条）；② worker 路径多写 `meta.__workerHeapPeakMB`、两路径 meta 结构不同 → 现在主线程路径也带该键（值 `null`），**键集合已一致**。

**收口复验（修复后，`wrpl-parse.js` `ae3ba4044d193723` / 138409 B）**
- `own` 与主线程**逐字段比较**：18 通道 × 5589 样本，**0 处数值差异、0 处 NaN 位置差异**（这次不只看通道数）；
- 真页面真实 drop：`own` 从 worker 路径进入 UI —— `ACEUI.state().own` 13 通道、`ownScreen={x:634.7,y:457.8}`、角落读数「高度 145 m / 空速 323 m/s M 0.95」、`__ACE_ERRORS=null`；
- 两条回退分支的 limits/provenance 文案均已实测命中；
- 唯一遗留的小瑕疵（**建议，不判 FAIL**）：`__workerHeapPeakMB` 在 worker 路径是 **`0`**、主线程是 `null`。worker 里 `performance.memory` 取不到，`0` 读起来像"堆占用 0 MB"这一事实陈述，而实际是"测不到"；建议统一成"取不到就 `null`"。这属诊断字段的语义问题，键集合与交付数据都不受影响。

### 3.18 浏览器内解析 == Python 产物（`8.browser-parse`）

**被测对象**：`web/`（上传页 `index.html` 7515 B、`wrpl-parse.js` 129299 B、vendored `fzstd.min.js`）；输入 `<game-dir>\Replays\2026.10.18 05.45.06.wrpl`（**5133517 B**，mtime 2026-10-18 05:44:54）。

**本侧实现路径**（不是只跑他的脚本）：
1. `build/verify/js/dump_parse.js` —— 独立取数脚本：载入 vendored fzstd + `wrpl-parse.js`，解析后把counts/own/units/events/shots/runways/airfields/meta 各部分（含 `Object.keys()` 顺序）导出为 `js-parse-dump.json`（2.83 MB），并记录解析耗时；
2. `build/verify/compare_js_vs_py.py` —— 独立比对脚本：把该 dump 与交付文件 `mission-data.js` 以及`kill_events.json` 逐项比，判定逻辑独立编写；结果落 `browser-parse-check.json`；
3. 交叉参照：另跑 `web/dev/selftest-node.js`（0 FAIL）作为第三方旁证；
4. 浏览器侧：`python -m http.server 8877 --directory <repo> --bind 127.0.0.1`（**托管后台任务**，用后即停）→ `http://127.0.0.1:8877/web/dev/selftest.html` → **18 项检查 FAIL 0、解析 1.475 s**，截图 `dev/verify/06-browser-parse-selftest.png`；上传页 `web/index.html` 也能打开（1 个文件输入、**0 个外部资源**，脚本顺序 fzstd → wrpl-parse → scene3d → mission.js）。

**逐项结果**：counts **26/26 相等**（含 ownSamples 5589、trackCount 236、airTracks 52、groundTracks/groundUnits 184、shots 1734、runways 4、airfields 4、unitsHidden 21、unitsWithCategory 236、unitsWithDeath 31、events 65、kills 33、envelopeFiltered 20、unitSeamsSplit 0、ownSegments 2、squadSize 2、terrainContours 0）；本机 5589 点逐点 **max |Δz|=0.100 m**（x/y 全 0.0000；交付侧 z 只保留 1 位小数，23/5589 落在边界）；三个时刻坐标逐位相同；空中 52 段逐点 ≤0.100 m（最差 eny-mig15bi-03）；击杀 33 条按 mission 轴折算 raw tick 后与 `(victimUid, killerUid)` 逐条一致；184 地面点按 **key** 配对全等（注意 `entityId=0x4006de` 被两个 `dummy_immortal` 共用，**两侧都一样**）；4 条跑道端点差 0.000 m；1734 发射点逐字段一致；`replayHeader`/`player`/`matchResults.players`/`vehicleRoster` 的值与**键序**全等（键序用 `Object.keys()` 导出后逐位比）。

**三条限制"是否如实标注"**：① 8111 派生字段在浏览器侧一律 `null` —— 实测 `devValidation` 里 17 项为 null 且带 note，`WRPL-JS.md §4.1` 与 limits 都写明；② 不读 `ecshashes.json`（780 KB）—— 源码里该词只出现 1 次，在"已知差异：不读"注释里，**没有任何读取动作**（`readFileSync/fetch/require/XHR` 邻近搜索为 0）；③ `matchResults.raw` 无 `uiScriptsData`（实测 raw 键为 `status/timePlayed/authorUserId/author/matchingInfo/player`）、`terrain` 仍为 `null`、显示名与类别取自页面内嵌表（源码含 `DISPLAY_NAMES`/类别表）。三条均**如实标注**。

**上传页真实 drop 路径（独立复现）**：起 `python -m http.server 8901 --directory <repo> --bind 127.0.0.1`（托管后台任务，用后即停），打开 `http://127.0.0.1:8901/web/index.html`，在页面里 `fetch` 真实回放（**5133517 B**）→ `new File(...)` → `new DataTransfer()` 塞入 1 个文件 → 对 `#upload-drop` 派发**真实 `drop` DragEvent**（页面的处理挂在 `window` 上，事件冒泡到它，走的是页面自己的拖拽路径，而不是直接调解析函数）。结果：`#upload-note` = 「完成：窗口 558.8 s · 本机 5589 采样 · 航迹 52（空中）/ 184（地面） · 击杀 33 · 发射点 1734 · 跑道 4 · 机场 4（**1.58 s**）」；`#upload-error` 空且未显示；`window.__ACE_ERRORS = null`；`ACEScene.getStats()`：`ready=true`、**`units=215`**（236 − 21 hidden）、`trailSkipped=0`、fps ≈60；连续两次读 `stats.t` 从 54.87 → 56.07（+1.20 s/1.2 s）说明在正常播放。截图 `dev/verify/08-upload-drop-verifier.png`；另一侧的独立一遍（1.55 s）见 `07-upload-drop-e2e.png`。

**取证口径更正（量法错误，已撤回一句话）**：此前说"上传页里 `ACEUI.state` 是空对象"——**错在量法**。`mission.js` 里 `window.ACEUI = { state: buildState, … }`，**`state` 是函数**，读的是属性（函数对象本身），所以 `Object.keys(ACEUI.state).length === 0`、`.met === undefined` —— 这正是当时看到的"空对象"，与页面无关。改为**调用** `ACEUI.state()` 后复测（同一上传页、真实 drop、解析 1.52 s）：
- `typeof ACEUI.state === 'function'`；两种读法的差别实测：`Object.keys(ACEUI.state).length = 0`、`ACEUI.state.met === undefined`；
- **mount 之前**：10 个键 `met/t/playing/speed/own/contacts/ownMissing/options/recentKills/identity`，`met=0`、`playing=false`、`own=null`、`contacts=0`、`ownMissing=true`；
- **mount 之后**：同一组 10 个键，`met=t=0.85`、**`playing=true`**、`speed=1`、**`own` 13 个通道**、**`contacts=116`**（示例 `aly-f15a-03`：`d=2417 m`、`hasAlt=true`）、`ownMissing=false`、`recentKills=0`、`identity` 6 键。
  这与 JS 解析报的两次测量（10 键、mount 前 0/false/null/[]、mount 后 playing=true/speed=1/own 13 通道）**一致**。

**顺带记录一个已修的外观问题**：上传页 `<title>` 曾是"ACE-COMBAT 回放回放 · 浏览器内解析"（"回放"重复）；现磁盘上已是 **「ACE-COMBAT 回放 · 浏览器内解析」**（文件与浏览器标题两处都已确认）。

**本次的判据错误（3 处，已修正，供留痕）**：① 按 `entityId` 建索引，撞上两侧都存在的重复 `0x4006de` → 报"183≠184"；② 坐标容差用了 0.001 m，而交付侧 z 是 1 位小数 → 报 5 条"字段差"（实际都恰好差 0.1 m 的舍入边界）；③ "不读 ecshashes" 用关键词是否存在来判断，而该词恰好出现在"不读它"的注释里 → 假失败。三条都属 §3.13 同一族：**判据自己要先站得住**。
    - **（第五条）"估算字节差"不能当判据**：上一版拿**估值**（616 B 是估算、不是实测）去对账，差了 247 B 就得出"既有文本被改写"的错误结论；正确做法只有两条 —— **逐块实测**，或者**做逆操作重建后比全量哈希**（后者更强，因为能直接证伪）。这与反复用到的「判据必须先能失败」是同一类：**账要算到能被推翻的程度**。

14. **`7.sides`（消白）的状态**：**已 PASS** —— 交付空中 52 条全部有色（ally 24 / enemy 28 / **unknown 0**），地面 184 条为 unknown（属另一类，不计入）；导出脚本已说明会把 `side` 换成 `side_final.json` 的结果（44 个 uid 全有 ally/enemy、unknown=0）。核对口径是：① 空中 `unknown` 必须为 0；② 用 8111 接触点颜色族给出 **as-is / 取反两种极性的一致率**（52 条全部可核对：**87% vs 10%**）；③ 如实写明"**方向仍未能独立闭合**，但这是目前唯一自洽的纯回放推断"（`side_final.json` 方法=出生位置最近中心，锚点=击杀图已定的 33 个 FM uid；自检 knownCorrect 32 / knownWrong 1）。换成新文件后需重跑并更新本行。

10. **`ground.json`（可选的静态点位产物）**：独立核对 184 条 = ground 115 + structure 69（与作者自报一致）；
    `spawnTickRaw×secPerTick` 落在 5.53–546.76 s（窗口 0–558.846 s 内）；**0/184** 落在 `tracks.json` 航迹包围盒（放宽 3 km）之外 → 与 tracks 同向同框架；
    用同一 replay→8111 相似变换比 8111 地面接触点（73379 个，**开发期**）：最近距离中位 **6.8 m**、≤100 m 占 **61.4%**，
    对照（把北向取反）中位 **4759 m** → 轴向结论成立。作者口径为中位 0.03 m / 66.3% / 取反 3632 m，差异来自对照点集与变换来源（用本机航迹拟合的变换）。
    它**是静态点位、无速度，未计入 `4d/4d2` 速度包线统计**（避免误报瞬移）。
11. **弹药/场景实体的范围**（措辞按航迹解析新版更正）：航迹解析 `REPLAY-DECODE.md §14.1/§14.3` 现在写的是**"逐帧弹道不可得，但发射点已解出并经独立复核"**（旧版"弹药位置不可解"已作废）。
    ECS 侧另有 1734 条弹药创建事件（rocket 1589 / bomb 95 / arh_shell 29 / payload 17 / jettisoned 4）与 1457 条场景实体；弹药没有 uid 组件，因此**不进 FM 航迹**（设计使然，不是漏解），而是以 `projectile_launches.json` 的单点形式交付（见 §0 第 15e 行）。
    交付文件里这些点又并进了顶层 `shots[]`（1734 条，见 §0 第 15g 行）。**`shots[].yaw` 一律为 null**：导出脚本试过两种四元数分量顺序，与"发射机航向"的中位差分别是 137.6° / 103.5°，口径不可验证 → 按契约留 null、不编（**这不能读成"朝向数据丢失"**，只能读成"朝向未解"）。`shooterUid/side` 是"同刻最近飞机 ≤100 m"的**推断**，命中 **1694/1734 = 97.7%**（定版数据实测；早前文档里的 96.2%/1668 已作废），已在 `limits` 标明。
    另外新增的 `runways[]` **不是从出生点猜的**：来自回放自身的机场标记点 `t1/t2_airfield_top[_reload]`（4 个点的位置 + 单位航向），再按这些标记引用的模型名 `dynaf_pg_1line_3000_universal` 取其 3000 m 长度反推线段；`confidence:"high"`、`source:"ecs-airfield-marker"`（原样透传）。三条自证均独立复算通过（见 §0 第 15k 行）：两端高差 0.00 m、两队航向成对相反（11.70°/0.40°）、主/备机场结构数 13/13/3/3。回放里确实没有跑道多边形/几何（limits 写明：4427 个实体块按 9 个关键词搜索只命中点状结构；467 条灯具落在机场中心 1500 m 内的为 0）。
    核对过 `tracks.json` 与交付 `mission-data.js` 的顶层字段里**没有** `missiles/rockets/bombs` 之类的未说明类别。

## 4. 未能验证的部分（如实列出）

1. **console error 的直接证据**：本会话没有 CDP console/网络面板通道。间接证据：
   `dev/ui/real-check.html` 在加载期做探针（0 window error / 0 console.error / 0 未处理 Promise）、
   页面三个全局对象齐全、脚本按序执行、交互全程无异常、rAF 稳定 144 fps、渲染无 NaN。
   **要 100% 闭合请在 DevTools Console/Network 面板看一眼（预期 0 error、0 request）。**
2. **1920×1080 下的帧率**：实测视口 1280×800（fps 144）；1920×1080 未测。
3. **`side` 阵营方向的绝对正确性**：见 §3.5，8111 颜色无法闭合方向；只能确认「未猜补」。
4. **击杀点 x/y/z 的绝对正确性**：33 条 kill 的 x/y/z 全为有限数（x −19288..8735、y −21799..12261、z 1.7..2361.4），
   但没有第二套独立真值可对（8111 不给他机坐标）。
5. **内嵌结算 32 名玩家的逐字段比对**：只核了本机 kills 与玩家数（32），未逐人比对 kills/timePlayed。
6. **地面点 0.03 m 的绝对精度**：见 §3.12，方法下限是米级（变换来自本机航迹），无法独立确认 0.03 m。
   另：**本局地图没有等高线数据**（`air_skyscraper_city.bin` 无 `HM2` 块），所以 `terrain` 在交付里是 `null`；渲染侧会先用另一张**有 HM2 的样本地图**在 dev 页面验证管线，但那不进交付文件。因此"地形渲染效果"这一项**无法在任何交付数据上验证**，只能核对占位与说明是否自洽（见 §0 第 15l 行）。
7. **`hidden` 单位的界面行为**：已补上消费侧静态核对 + 引用 node 驱动与浏览器两端的 `stats.units=215` 实测；**仍未在浏览器里亲自数地面标记**（浏览器时段待终验）。
8. **"显示名"的范围已按加严口径落定**：`units[].vehicleName` / `shots[].templateName` / `runways[].name`（+ `airfields[].name`）四类零非 ASCII 即 PASS，`limits`/`provenance` 里的中文说明文字不算显示名。本版四类全 0，故 `7c` PASS。
9. **浏览器会话被并发抢占**：定版数据上的断言在重导航后取到（`url=file:///.../index.html`、`units=52`），
   四张截图也已在定版数据上重拍；若期间 `mission.js`/`three3d/scene.js` 又有改动，UI 层需再跑一次（未见其变更）。
10. **浏览器内解析 == Python 产物**：**已完成**（见 §0 第 32 行与 §3.18）。其中"上传页真实拖拽/选文件流程未验证"这条**~~已划掉~~** —— 2026-10-19 用`python -m http.server 8899 --directory <repo> --bind 127.0.0.1` + `fetch` 真实回放 + `File`/`DataTransfer` + 对 `#upload-drop` 派发真实 `drop` 事件补验通过（解析 1.55 s、`units=215`、无错误，截图 `07-upload-drop-e2e.png`），**独立复现了一遍**（1.58 s，截图 `08-upload-drop-verifier.png`）。
    仍然未验证的两条：**(a) 操作系统文件选择框本身** —— 上面走的都是 drop 路径，`input[type=file]` 的点击选择没有被驱动过（自动化浏览器无法操作系统文件选择框）；**(b) 浏览器侧 8111 派生数字** —— 浏览器里取不到 8111，这部分只能验"为 null 且如实标注"，**不能验数值**（数值只在 Python 侧独立复算过）。
11. **方法论留痕（反复用到）**："估算字节差"不能当判据 —— 上一版拿估值对账，差了 247 B 就误判"既有文本被改写"；正确做法是**逐块实测**或**做逆操作重建后比全量哈希**。同族的还有"最近邻要取时间窗内全部候选""量级检查不等于有效性检查""NaN 会吞掉最大值""结构定位要用键而不是最近的分隔符"，都已记在 §3.13。
12. **地形对齐的“真值”来源**：对齐校验用的是**生成这份地形的同一个高度栅格**（`warthunder-maps/out/*_heightmap.npz`），属于“自洽性/对齐性”验证（地面点贴地表、本机不穿地、翻转/平移会明显变差），**不是独立测绘真值**；交付文件里没有高度栅格本身（只有等高线/水体/海岸线），所以这项要在“数据来自同一来源”的前提下理解。

## 5. 建议与更正（不阻塞交付）

1. **更正一条误读**：早先曾写"`header.clock` 残留 `ticksPerSecond/endMarkerTick` 等 null 旧键"——
   实测**没有残留**：`clock` 现有 18 个键，旧名已移除并给出 `deprecatedKeyNames` 映射；
   此前看到的那四个 `null` 是"按旧键名 lookup"的转储。工具的转储文案已改（区分"实际键"与"旧名 lookup"），
   本条不再作为问题。格式解析的 `clock` 数值未变（`endMarkerRaw=151026944`，`/256=589949`）。
2. `meta.devValidation` 建议补 `ownP99ErrM`（作者侧已在新 tracks 中给出 6.43）并注明采样口径（2236 vs 复算侧 2793）。
3. `2b`（hudmsg 对照）建议在 `DATA-NOTES.md` 明写"与 events 行数不相等是预期"，避免下游误判。
4. 以后同类验收：**先比对 `generatedAtUtc`/mtime+hash，再比内容**（格式解析 `QUICK-REF.md §B` 同款建议）。
5. **产物哈希口径（两处实测差异，均以磁盘字节为准）**：
   - `build/wrpl/vehicle_display_names.json`：磁盘实测 **size 5825 / sha256(前16) `e08faffe68b42972`（mtime 10:18:52、38 键）**；ECS 解析自报 `1b63d76602fb2cd4`（5669 B）—— 不一致，只引用实测值。
   - `build/wrpl/vehicle_categories.json`：磁盘实测 **size 5326 / sha256(前16) `a3ccafaef47f0773`（mtime 10:38:06）** —— 与导出侧给的磁盘口径一致；ECS 解析的"文本模式"哈希作废。
   - 校验工具一律用 `file_fingerprint()`（mtime+size+磁盘字节 sha256 前 16 位）写进证据。
6. **builder 的"写完后抛 NameError"留痕**：定版文件**完整性可确认** —— 尾部为 `names":{}};\n`（对象闭合 + 语句分号）、9 个顶层键齐全、46 项检查全过，与"文件写完后才抛异常（exit 1）"的说法一致；但"三次连跑哈希一致"需要跑导出脚本的 builder 才能复现，**无法独立验证**，此处只作为其自述记录。

---

## 17. 发布前收口（2026-10-22）

> 独立复算在复验过程中进程两次中途失败（宿主重启），本节数字由可复跑的脚本实测后补上；
> 与此前各节不冲突，作为"发布前"这一段落的收口记录。

### 17.1 三条入口的实测（无头 Playwright，不依赖共享浏览器）

| 入口 | 关键结果 |
|---|---|
| 产品页 `web/index.html`（主路径） | 解析 1.63–2.21 s；`__SCENE3D3_READY=true`；`units=215`、`tris=5702–5810`；`own` 18 通道 / `state.own` 13 通道 / `ownMissing=false`；`playing=true` 且 `t` 推进；`terrain=26/5/5`；`ACE_ATTITUDE` 44 uid；`.ac-label` 216；**外部请求 0**（16 个请求全同源）；console/pageerror 0；画面非背景像素 20% |
| 产品页（旧格式错误路径） | **52 ms** 进入错误态；标题「不支持的回放格式」、正文「该回放可能是旧版本游戏生成的，本工具只支持当前版本的回放格式。」逐字一致；重试按钮可见；外部请求 0 |
| 演示页 `index.html` | `units=215`、`tris=5750`、`t` 2.90→4.13；`ACE_ATTITUDE` 44 uid；`ACE_LANDMASK` 存在；`terrain=26/5/5`；角落只有时间/高度/空速；外部请求 0、报错 0；画面像素占比 0.68 |

可复跑：`node dev/check-upload.mjs <base> <replay> [oldReplay]`、`node dev/check-demo.mjs <url>`。

### 17.2 `ACE_ATTITUDE`：浏览器产出 vs 预计算 → **多数样本一致，但有孤立偏差（早先「完全一致」的结论作废）**

- 早先用 `build/recon/attitude_crosscheck.py` 得到"44/44 uid、Δn=0、Δbank=0.00°"，
  但**对拍输入是错的**：当时用的 `build/verify/attitude-data.json` 是 `attitude-data.js` 的逐字提取，
  等于"文件 ↔ 同一文件的副本"，Δ=0 是必然结果，**不能**作为证据。
- 独立复算用真实浏览器产物重做（搜 ±3 偏移、角度折叠）：44 uid，最佳偏移 0 占 43、−1 占 1；
  **长度差 {−1: 38, 0: 5, +15: 1}**（`uid-374` 多 15 个样本）；**42/44 的 uid 有 >2° 的样本差，
  最差 169.9°**，多数 uid 最差 13–16°。
- 结论：**两入口的坡度输入"基本一致但不完全相同"**（长度差 1 个样本、少数时刻有较大差）。
  影响面：只影响飘带扭转的极小差异；效果已由肉眼确认正确。
  若要对齐，需统一两侧的重采样边界（+15 那条是分段/断轨边界处理差异）。

### 17.3 仍在"未验证"里的

- 宣传片 `multiplane verify` 因 WebGL 面板 0.05–0.2% 像素抖动不通过（肉眼不可见，`render` 不依赖它）；
- 操作系统文件选择框未驱动（只测真实 `drop` 事件）；
- `bank` 与协调转弯公式不相关（r≈0，见 `RELEASE-CHECK.md` §5），即**坡度物理语义未闭合**。

### 17.4 独立确认 / 差异（2026-10-22，只做三件收口）

**① `ACE_ATTITUDE` 一致性：「完全相等」这个结论复现不出来，原因是对拍的两边是同一份文件。**
- `build/verify/attitude-data.json`（mtime 10-22 00:57）不是浏览器 dump，而是 `attitude-data.js` 的**逐字提取**：
  44/44 uid 的 `bank` 数组与源文件**完全相同**、`note` 相同。所以 `attitude_crosscheck.py` 比的是"文件 ↔ 同一文件的 JSON 副本"，
  结果必然是 `Δn=0、Δbank=0.00°` —— 这条**不能作为"浏览器产出与预计算一致"的证据**。
- **真正的对比**（产物页真实解析后读 `window.ACE_ATTITUDE`，与 `attitude-data.js` 比；搜索 ±3 样本偏移、角度差折叠到 ±180）：
  44 个 uid；最佳偏移 **0 占 43 个、−1 占 1 个**；**长度差分布 = {−1: 38 个, 0: 5 个, +15: 1 个}**（`uid-374` 多 15 个样本）；
  **42/44 的 uid 在同一偏移下仍有 >2° 的样本差**，最差 **169.9°**，多数 uid 的最差差值为 **13–16°**（如 uid-344 13.5°、uid-348 15.8°、uid-349 15.2°）。
- 结论：**"逐样本完全一致"不成立**。两边的差异集中在少数样本与至少一个 uid 的分段处理上；由于"飘带坡度唯一来源"是产物页现算的 `ACE_ATTITUDE`，
  这些差异会直接体现在飘带上。**建议**：把 §17.2 的表述改为"两路在多数样本上一致、但存在孤立大偏差与分段差异（清单见本条）"，并把 `attitude_crosscheck.py` 的输入换成真实浏览器 dump。

**② 两条验收脚本在当前环境跑不起来，已用页面内等价检查覆盖判据。**
- `node dev/check-upload.mjs …` 与 `check-demo.mjs …` 都在 `browserType.launch` 处报
  **`spawn EPERM`**（当前环境不允许 Playwright 启动子进程）。所以这两个脚本**没有执行**。
- 改用共享浏览器的页面内等价检查（同一服务器 `8951`、同一份回放）：
  - 产物页主路径：解析 **1934 ms**、`__SCENE3D3_READY`、`stats.ready`、**units 215**、`ACE_ATTITUDE` **44**、`terrain 26/5/5`、
    `.ac-label` **216**、`state.own` **13 通道**、外部请求 **0**、`__ACE_ERRORS`/页面 error **0**、截图 `dev/verify/10-release-product.png`；
  - 旧格式路径：**83 ms** 进入错误态、`#err-title`「不支持的回放格式」、`#err-detail` 逐字一致、重试按钮可见、外部请求 0；
  - 演示页：`__SCENE3D3_READY`、`stats.ready`、**units 215**、`ACE_ATTITUDE` 44、`ACE_LANDMASK` 存在、`terrain 26/5/5`、
    `t` 52.48→53.70、角落无"开发期验证/数据来源"、外部请求 **0**、报错 0、截图 `dev/verify/11-release-demo.png`。
  → 判据层面**全部满足**；但这两个脚本本身**未验证**（需在有子进程权限的环境里跑）。

**③ `bank` 物理语义：同意记为已知项（不是 FAIL）。**
- 补充数据：`attitude-data.js` 全部 **52074** 个样本里 **|bank|>90 占 14.2%**（范围 −180..+180；3 个 uid 超过半数样本 |bank|>90，
  25 个 uid 超过 10%），`uid-350`/`uid-361` 的中位 |bank| 分别约 168°/172°；`|bank|` 落在 150..180 的占 7.2%。
- 用交付航迹独立推「协调转弯应有 bank」（`atan(v·ω/g)`）做相关：逐 uid **r≈0（0.001–0.04，个别为负）**，但该方法受 4 Hz 航向量化噪声影响，
  **不足以证明** bank 是"另一个欧拉角"。因此**不声称能证伪**，只记录「语义未闭合、只保证原样渲染」这一已知项。

