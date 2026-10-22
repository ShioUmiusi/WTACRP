# DATA-NOTES — 数据产物说明（契约 v4）

覆盖两个文件，**只有第一个是交付物**：

| 文件 | 生成脚本 | 数据来源 | 用途 |
|---|---|---|---|
| `mission-data.js`（1.90 MB） | `tools/build_ace_mission.py` | **100% 来自 `.wrpl` 解码产物** | 交付数据，`index.html` 引用 |
| `dev/data/mission-data.8111-dev.js`（1.87 MB） | `tools/build_ace_mission_dev8111.py` | 8111 抓取（开发期） | 脚手架：几何没解码出来时给前端开发，**不发布、不被 `index.html` 引用** |

契约 v4 §1 硬约束：交付数据不得含 8111 数据。本文件里出现的 8111 数字**只用于开发期交叉验证**，
一个字节都没进 `mission-data.js`；`build_ace_mission.py` 全文没有 8111 抓包相关字样（已自查并复核）。

---

## 1. 交付文件 `mission-data.js`

### 1.1 输入（全部只读，全部来自 `.wrpl`）

| 文件 | 内容 |
|---|---|
| `build/wrpl/header.json` | 容器布局、`.wrpl` 头、时钟、内嵌结算、32 人玩家表 |
| `build/wrpl/entities.json` | 27 条奖项、5 条聊天、玩家↔载具表、单位名表 |
| `build/wrpl/tracks.json` | 44 个实体三维航迹（52 074 采样）、33 条 `killPositions`、自检数字 |
| `build/wrpl/own_props.json` | 本机仪表属性表（ReplayCockpitParams，2 236 行 × 4 Hz × 10 列） |
| `build/wrpl/uid_map.json` | uid → 玩家/载具/阵营（ECS 解析给出的映射，44 个 uid） |
| `build/wrpl/kill_events.json` | 33 条击杀的位流字段：击杀者/受击者 uid、载具、武器 |
| `build/wrpl/ground.json` | 184 条地面静态点位（115 坦克 + 69 工事，ptype=6 ECS transform） |
| `build/wrpl/side_final.json` | 阵营终判：44 个 uid 全部 ally/enemy（击杀关系二色 + 出生位置聚类，纯回放） |
| `build/wrpl/projectile_launches.json` | 1734 条发射点（ptype=6 ECS 弹药构造消息；只有创建时刻的点） |
| `build/wrpl/vehicle_display_names.json` | 37 条内部名 → 显示名（25 飞机 + 12 地面/工事；**显示约定**） |
| `build/wrpl/runways.json` | 跑道结论（`no_runway_geometry`）+ 4 个机场地标点（雷达/高炮聚类） |

缺哪一份都能跑：缺 `tracks.json` 时几何留空，缺 `own_props.json` 时飞参留 null —— 绝不用 8111 回填。

### 1.2 时间轴

回放 raw tick（256 kHz）：`sec = tick / 256000`。**原点与 `tracks.json` 的 `tick0` 相同（raw tick 7 962 368）**，
因为验收工具的时间映射（4a）就是在这条轴上拟合的，两边同轴才能逐样本对照。
`window = {t0: 0, t1: 558.846, duration: 558.846}`：这是**解码覆盖的完整跨度**（不是单条航迹）。
回放时钟自己的 0 点在本轴的 −31.103 s（本文件不含那一段，那里没有任何解码航迹）；回放自身总长 589.949 s（EndMarker）。
`own.t0 = 0.0`，所有通道（`own`/`units`/`events`）都在这一条轴上。事件另带整数 `replayTick`。

### 1.3 本机 `own`（10 Hz，n = 5589）

- 位置：`tracks.json` 的 `devValidation.ownSegmentIds` —— 本机在源数据里是**两段**
  （`uid-368` 31.1–430.8 s + `uid-368#2` 431.1–589.8 s，中间是机场重生/换位），按顺序拼接、
  **接缝处不插值**（`own.x` 有 18 个 `null`：接缝 + 契约 §5.2 包线滤除）；覆盖率 99.7%。
- 飞参：`own_props.json` 的 10 列（高度/表速/Mach/过载/航向/俯仰/滚转/升降率/油量/油门）插值到同一网格，
  覆盖率 100%（`throttle` 97%）。航向按 sin/cos 插值，避免 0/360 跳变。
- **回放确实没有的通道留 null**：迎角 `aoa`、弹药 `ammo`（不拿 8111 补）。
- 回放内部互校：属性表高度 vs 本机航迹 z → **rms 0.62 m / max 2.40 m**（两条都由回放独立解出）。

### 1.4 他机 `units[]`（45 条，全部空中，`hasAlt=true`、真实 z）

- 源数据已按"相邻采样隐含速度 >600 m/s"断轨（54 段，丢 6 段碎片），去掉本机的 2 段 → 交付 **52 条**。
  导出侧的 `--` 断轨逻辑保留但已经成为 **no-op**（`counts.unitSeamsSplit = 0`），不叠加第二次切分。
  每段继承原 uid 的 `side`/`vehicle`（`units[].segment` 给出段号，key 加 `#n` 后缀）。
- 身份来自 `tracks.json`/`uid_map.json`（05:48 终版）：`side` 有值 **13 条**（ally 7 / enemy 6），`unknown` 32 条；
  `vehicle` 有值 **39 条**（uid_map 31 + 击杀/伤害包反推 8，只在结论唯一时采用）；`player` 有值 1 条（本机）；
  逐条来源记在 `units[].vehicleSource`
  （ECS 解析只在无歧义时填 player，有歧义给 `playerCandidates`，不把候选当结论）；其余保持 `unknown`/null。
- 空中统一 **10 Hz**、地面 1 Hz。空中用 **Catmull-Rom 样条**插值（不是最近邻、也不只是线性），
  渲染端不必再管源数据约 4 Hz 的卡顿；`hdg` 由**平滑后的**航迹逐点推（等价于最短弧平滑）。
  跨段与 >1.5 s 空档一律 `null`，不凭空连线。交付前再按契约 §5.2 包线滤除：
  与上一个有效采样之间推算速度 >600 m/s 的点置 `null`（不做平滑、不编造中间位置），
  本局 **units 5 点 + own 15 点 = 20 点**（见 `counts.envelopeFiltered`）；
  **上采样后重新自算包线：52 段空中航迹 124 512 点，>600 m/s 的步 0、最大 591 m/s。**
- **y 轴**：脚本内置**数据驱动的自检**（本机位移方向 vs `own_props` 真罗盘，1601 样本），按结果决定是否取反。
  历史与现状：tracks **v1** 的 `axisMap` 是 `y=−fm.PosZ`（朝南），当时判据给出「不翻转 123.64° / 翻转 1.51°」
  → 导出侧自动取反，得到世界北坐标；航迹解析采纳该结论、把 v2 改成 `y=+fm.PosZ` 后，
  判据变成「不翻转 **1.51°** / 翻转 123.64°」→ 导出侧**保持原样**。两次输入因此得到同一套坐标，**不会双翻**。
  这条自检留在脚本里，以后上游再改符号也不会静默出错。

### 1.4b 地面单位（184 条，来自 `ground.json`）

- `domain:"ground"`、`side:"unknown"`（推不出就不猜）、`vehicle` = 原始模板名（`tank` / `fortification`）、
  `vehicleName` = 可读名（显示约定表：Tank / Fortification）、`static:true`、**单采样**（`n:1`、`hz:1`、
  `t0` = 出生 tick 折算，个别早于轴原点的已夹到 0，原始 tick 在 `spawnTickRaw`）。
- 坐标口径与 tracks v2/v3 相同（x 东、y 北、z 上），**不做任何翻转**；`z` 取 up 分量（实测 -0.1…1325.9 m）。
- 它们是"点位"不是航迹：`tracks.json` 只含空中实体，地面层是独立输入，两者互不影响。
- 位置校验（透传 `ground.json.devValidation`，本脚本未独立复算）：到 8111 地面接触点**中位 0.03 m**、
  ≤100 m 占 **66.3%**；对照把北向取反后退化到 **3632 m** —— 说明轴口径正确、位置可信。

### 1.4c 阵营与发射点

- **阵营**：`side` 主来源是 `side_final.json`（44 个 uid 全部定为 ally/enemy）——
  ①击杀关系二色（32 条击杀边、36 个 uid、**0 冲突**，与已闭合的 ECS 映射一致 13/14）；
  ②剩余 11 架 AI 用**出生位置最近中心**推定（锚点=①已定的 33 个，自检 32/33）。
  分段后的 `uid-N#2` 会剥掉 `#n` 再查表，所以 **52 段空中单位全部有色（ally 24 / enemy 28，unknown 0）**；
  ECS 原始取值逐条留在 `units[].sideDecoded`。`meta.sideSource = "击杀关系二色 + 出生位置聚类（纯回放；AI 为推定）"`。
  **AI 侧别是推定**，若与观感冲突可用齿轮的「反转」开关整体校准；地面点位仍为 unknown（没有可用证据）。
- **发射点 `shots[]`（1734 条，顶层新字段）**：`{t, tickRaw, template, templateName, x, y, z, yaw, side, shooterUid, entityId}`。
  模板分布 rocket 1589 / bomb 95 / rocket+arh_shell 29 / payload 17 / jettisoned 4；`templateName` 是显示约定表（不确定的留 null）。
  坐标是世界 (东, 北, 上)，与 tracks v2/v3 同向。
  - **只有创建时刻的点、没有弹道**（弹药在 ptype=2 FlightModel 里没有 uid 组件）。
  - `shooterUid`/`side` = "同刻最近的飞机航迹（≤100 m）"的**推断**，命中 **1668/1734 = 96.2%**，其余 null。
  - **`yaw` 一律 null**：两种四元数分量顺序都试过，与同刻发射机航向对不上
    （较优一组去掉固定 123.5° 偏移后 MAD 仍 34.1°），口径无法验证，故不给含义不明的角度。

### 1.4d 显示名与跑道

- **每条 unit 都带原始字段**：`vehicle`（原始内部名）、`template`、`class`、`entityId`（另有 `vehicleName`）。
  约定：界面**不得**出现 `AIR`/`GND` 这类兜底字样 —— 有 `vehicleName` 用显示名，没有就原样打印 `vehicle`。
- **空中 45/52 段有显示名**（`JF-17` / `JA 37DI (F21)` / `F-15A` / `F/A-18A` / `Su-25` …）；
  另外 7 段连 `vehicle` 本身都还未知（uid_map/击杀包都没给出载具名）→ `vehicleName = null`。
- **地面显示名：184/184**（label = `NAMES[unitClassName] || unitClassName || template`）：`vehicle_display_names.json` 的键是
  `tankModels/ussr_t_55_am`、`structures/uk_bunker_light_fat` 这类 **unit__className**，
  而 `ground.json` 只给到 `template`（`tank`/`fortification`）—— 两者对不上。
  所以**没有**再拿 "Tank"/"Fortification" 去糊（上一版就是这么做的，可读性不好），
  现在 `vehicleName = null`、`vehicle = tank/fortification`（原始名），场景打印原始字段即可。
  要拿到 `T-55AM` / `轻型掩体` 这一级，需要 ECS 解析在 `ground.json` 里补 `unitClassName` 字段，
  **本脚本已按该字段名预留并直接消费**（`g.get("unitClassName") or g.get("className")`），一到就能用。
- **`runways[]` 当前为空数组**，字段约定已留好：`{name, x1,y1,z1, x2,y2,z2, source}`（世界米）。
  空的原因：`ground.json` 区分不出机场结构（同样只有 `tank`/`fortification`；`structures/unit_airfield*`
  名字只在显示名表里），也拿不到成对端点。等 ECS 解析解出机场实体、或给地面点带 `unitClassName` 后再填。

### 1.4e 机场地标点与跑道结论

- `airfields[]`：**4 个机场位置点**（`af-01`…`af-04`），由机场雷达/高炮实体**聚类**得到
  （`source:"ecs-cluster"`），带 `structures` 计数与 `classNames`。它们是**点**，不是跑道端点。
- `runways[]`：**4 条线段**（`rw-af-01`…`rw-af-04`，各 3000 m、`confidence:"high"`），
  由**回放自身的机场标记点**（`t1/t2_airfield_top[_reload]` 的位置 + 单位航向）与其引用模型名
  `dynaf_pg_1line_3000_universal` 给出的 3000 m 长度反推 —— **不是关卡几何**。
  自证：同一条跑道两端高度差 ≤ 0.0–7.7 m；两队机场航向成对相反（155.3°/323.6°、168.2°/347.8°）；
  主/备机场结构数 13/3。历史结论（仍然成立、已保留在 `limits`）：
  全量 4427 个实体块按 airfield/runway/airstrip/strip/apron/taxiway/airbase/hangar/farp 搜索，
  只命中 12 个**点状**结构，没有任何实体带跑道几何，也没有可自证的 1000–3000 m 长度字段。
  字段约定仍留着 `{name, x1,y1,z1, x2,y2,z2, source}`，将来有几何直接填。

### 1.5 事件 `events[]`（65 条，按 `t` 升序）

| kind | 条数 | 来源与说明 |
|---|---|---|
| `kill` | **33** | `kill_events.json` 位流字段 + `tracks.json.killPositions` 坐标；与 33 个击杀包一一对应 |
| `award` | 26 | `awards[]`，每类只记首次时刻，文本带本局总次数 |
| `lastblow` | 1 | `awards[]` 的 `final_blow` |
| `chat` | 5 | `chat[]` 原文与发送者（`composed:false`）；文本去掉零宽字符与 `<color>` 标签 |

击杀事件字段：
- `actor`/`actorVehicle` ← `killerUid` 经 uid_map（本局 30 条给了击杀者载具）；`target`/`targetVehicle` ← `victimUid`；
- `weapon` = 回放里的 `PlayerWeapon` 内部名（30/33 有值，例 `us_aim9l_sidewinder`）—— 是内部 id，界面要俗名需另配映射；
- `x/y/z` = **受击者在击杀 tick 的位置**（30 条，`victim_pos_at_tick`），没有受击者坐标时退用击杀者位置（3 条，`killer_pos_at_tick`）⇒ **33/33 都有落点**；
- `weaponName` = 可读名（`AIM-9L`/`AIM-7M`/`R-60`/`RVV-AE`…）：**显示约定**（人工映射表 + 机械转写），
  映射不到就原样显示内部名；30/30 条有武器的击杀都能给出可读名。
- `text` 一律 `composed:true`（回放里没有本地化原文，全流 9 种文案模式命中 0），形如
  「`f_15a 击落 f_20a（us_aim9l_sidewinder）`」；`killerUid = 2047`（回放用 0x7FF 表示「无」）的 1 条写作
  「…被击落（无击杀者：坠毁/环境）」，`kind` 仍是 `kill` 以保持与击杀包一一对应；
- `isPlayerActor` / `isPlayerTarget` 由 `killerUid/victimUid == 本机 uid(368)` 判定 —— **本机 2 条击杀被正确归属**
  （t = 170.198 / 534.317，即回放时钟 201.3 s / 565.4 s，与 8111 hudmsg 原文的 2 条一致）。

### 1.6 `meta` 关键字段

- `source="wrpl"`；`sideSource="uid_map(ECS missionName 推定；方向未独立闭合)"`；
  `replayFile="2026.10.18 05.45.06.wrpl"`（5 133 517 字节，zstd → 86 773 包）。
- `player`：`=LyRe= 八幡 海鈴`，载具 `f_15a`，clanTag `=LyRe=`，team 2，slot 6，kills **2**（内嵌结算）；`callsign` 由 clanTag 推导。
- `matchResults`：32 名玩家的 kills/humanKills/groundKills/navalKills + `raw`（内嵌结算原样）；
  `mode=base_dom`、`scoreLimit=5300`、`timeLimitMin=25`、`matchSeconds=589.94928`。
- `replayTicks`：`{tick0: 7962368, tickLast: 151026944, secPerTick: 3.90625e-06, packets: 86773, killPackets: 33}`。
- `counts`：`ownSamples 5589 / ownSegments 2 / trackCount 236 / airTracks 52 / groundTracks 184 /
  groundUnits 184 / shots 1734 / runways 0 / airfields 4 / unitsWithSide 52 / unitsWithDisplayName 45 / events 65 /
  kills 33 / ownKillsByEvents 2 / ownKillsFromResults 2 / unitSeamsSplit 0（源侧已断）/
  unitsWithIdentity 45（含击杀/伤害包反推 9）/ envelopeFiltered 15 / killsWithWeaponName 30`。
- `grid`：`{stepX: 6500, stepY: 6500, note: "显示约定，非实测数据"}`。
- `devValidation`：见 §3.2（只有数字，不含坐标）。
- `provenance` 11 条、`limits` 14 条（含击杀点缺口、接缝断开条数、y 轴判据、侧别方向未闭合等）。

### 1.7 已知局限

1. **`side` 的来源与可信度（已收敛，但仍只覆盖 13/45）**：ECS 解析最后查明
   「`t1_/t2_` 前缀是**出生组**不是队别」，改为**从机型推阵营**（机型在各队出现过的 team 集合唯一才定），
   并收回了此前基于错误假设的取值；用两条独立的 8111 证据复核，**有明确取值的 9 个 uid 上 9/9 一致**；
   交付数据里 `side` 有值 13/45，按 as-is 极性核对 4g 也 **92%（n=12）一致**。
   未解出的 32 条一律 `unknown`（按契约走统一色），**不猜**。早期几次独立核对的结论如下（供追溯）：
   - 机型集合证据 24 vs 21，无法定「任务 t1/t2 ↔ 结算 team」的方向，最终按
     「录像者必在自己一队 + 本机 uid=368 属 t2」定案；
   - 用 8111 证据交叉核对得「一致 6 / 不一致 23」，判 uid_map 的 side 不可信，故没有写进 `tracks.json`；
   - 两次自查：8111 颜色投票「不取反 一致 13 / 冲突 29，整体取反 一致 16 / 冲突 26」；
     结算表 team 对 uid_map 里的玩家名「一致 9 / 相反 8」；
   - 出生点 k-means 两簇（锚点=本机簇）「跨阵营 3 成立 / 26 违反」。
   旧版（出生组假设）四种判据全部在 50% 附近或明显失败 —— 结论是当时的 side 与配对都不可靠，
   而不是「取反就对了」（这也是 ECS 解析收回旧值的原因）（2 km 关联半径下同一位置附近常同时有好几种颜色的接触点）。
   本文件当前口径 = `--side-source uid_map`，同时保留 `sideDecoded` 原值；
   想要保守就 `--side-source unknown` 重跑（一条命令，几秒）。唯一**已确认**的侧别是录像者本人 = ally。
   界面齿轮面板提供「阵营：推定 / 反转 / 未知」三选一（默认推定、即时生效、存 localStorage），
   把这份不确定性交给使用者，而不是替使用者猜。
2. 12 条 AI / 无阵营实体保持 `unknown`；`vehicle` 缺 14 条（uid_map 没有机种名）。
3. `player` 只有 1 条唯一命中；绝大多数目标标签只能显示**机种**，没有玩家名；`names = {}`。
4. 武器名/机种名是回放内部 id，本地化俗名需要另配映射表；没配就按契约 §0 显示 `--`。
5. 地面单位位置未解出，`units` 全为空中目标。
6. 奖项只记首次时刻（回放只给首末与最多 6 个实例 tick）。
7. 他机航迹是 ptype=2 的到达率（约 4 Hz），重采样到 5 Hz 属插值。
8. `vehicle` 仍缺 6 条（44 个 uid 里 38 个可定、6 个从未在任何包或 ECS 表里出现过载具名）；
   回放**没有**解出可靠的发射/命中类消息（0xF058 击杀、0xF056/0xF157 伤害已用；
   `ff0781*`/`ff0f81*` 家族 20 余种疑为每帧状态更新，语义未解），故没有导弹发射事件。

---

## 2. 开发脚手架 `dev/data/mission-data.8111-dev.js`（不交付）

由 `build_ace_mission_dev8111.py` 从 8111 抓取生成，结构同样对齐契约 §2，`meta.source="8111-dev"`，
供前端在几何未解码时开发。关键自检数字：窗口 84.52–646.30 s（561.78 s）；本机 10 Hz n=5618，x/y 覆盖 99.4%；
他机 331 条（空中 87 / 地面 244）；事件 153 条（kill 33 / damage 47 / disconnect 58 / award 14 / lastblow 1）；
体积 1.87 MB。坐标标定：航向约定 `atan2(dx,dy)`、平均误差 0.19°、**y 轴翻转**（位移方向误差 3.36° vs 不翻转 112.72°）。

两个真机陷阱（都写进脚本注释）：`indicators.altitude_hour` 是**英尺且按 10 000 ft 回绕**（与 `state` 的 `H, m` 中位差 0.33 m）；
`indicators.bank`（±8°）与 `indicators.aoa`（±180° 指针角）**不是**滚转角与迎角（滚转取 `aviahorizon_roll`，迎角取 `state` 的 `AoA, deg`）。

---

## 3. 开发期验证（数字，不带坐标；全部不进交付文件）

### 3.1 三条独立路径的一致性

| 项 | 结果 |
|---|---|
| 击杀包：独立分帧重数 `payload.bin`（86 773 条记录） vs `kill_events.json` | **33 = 33** |
| `events(kind=kill)` vs 击杀包 | **33 = 33**（按 tick 逐条对上） |
| 本机击杀：`ownKillsByEvents` = `ownKillsFromResults` = 结算 author 行 = 8111 hudmsg 原文 | **2 = 2 = 2 = 2** |
| 奖项 `events(award)+events(lastblow)` vs `len(awards[])` | **27 = 27**（不是 `sum(count)=103`，实例 tick 被截断） |
| 时间轴：`tick = tsMs × 256`、`sec = tsMs/1000` | 三者精确互换（2f PASS） |
| 击杀时刻受击者↔击杀者距离（纯回放内部） | n=29：p50 **1353 m** / p90 3537 m / max 4392 m，**86.2% < 3 km** —— 位置解错的概率很低 |
| 8111 hudmsg 游戏内秒 vs 回放 tick 轴 | `抓取 rel ≈ 游戏内秒 + 53.5`；首个 kill 62.191 s、先拔头筹 119.745 s、最后一击 583.609 s 三处逐条吻合 |

### 3.2 独立复算（`tools/check_ace_mission.py`）

| 检查 | 结果 |
|---|---|
| `1.own-truth` / `1b.own-series` | **PASS**：3 个随机时刻 z 偏差 ≤2.9 m、hdg ≤0.68°、g ≤0.01；整段 z vs 8111 `H, m`：max **6.1 m**、rms **1.0 m** |
| `2a.events-vs-kills` | **PASS** 33/33（tick 精确相等） |
| `2e.awards` / `2f.tick-unit` | **PASS** |
| `3.structure` / `3b.window` | **PASS** |
| `4a.time-map` | **PASS**：最佳实体 uid-368，rate 256000 ticks/s，offset 84.54 s，剖面相关 0.999（本机剖面单独拟合 84.52 s，差 0.03 s） |
| `4c.own-accuracy` | WARN：**`det = +1.000`（不再需要反射，证 y 轴已闭合）**，rms 6.3 m、p99 9.9 m、max 207.5 m（离群点） |
| `4d.speed-envelope` | **PASS**：交付 53 条航迹 67 804 步，超 600 m/s **0 步**、单步 >5000 m **0 处**，最大 587 m/s |
| `4d2.tracks-envelope` | **PASS**：源 52 014 步里 0 步超包线（`speedViolations600BeforeSplit = 16` 是断轨前的旧值） |
| `4g.side-vs-8111` | **PASS**：按 as-is 极性一致率 **92%（n=12）** |
| `2c.player-kills` | **PASS**：四源一致（事件归属 2 = 结算 2 = 8111 原文 2） |
| `4b.units-assoc` | **PASS**：他机与 8111 平面接触关联率 **68.4%** |
| `4c.own-accuracy` | WARN（tracks.json 侧）：平移+旋转+缩放拟合后 max 207.5 m / rms **6.3 m**（目标 max≤50、rms≤15）；解码线自报 130.3 / 3.98 m |
| `4d.speed-envelope` | FAIL（tracks.json 侧）：原始样本 16 步 >600 m/s、4 处单步 >5000 m（uid-348 有 255 km/s 的跳变）。**导出侧已按接缝断开处理**，见 §1.4 |
| `4e.devvalidation` | 2/4 一致：`unitAssocRate`、`speedViolations` 一致；`ownMaxErrM`/`ownRmsErrM` 与方法差异有关（见下） |
| `5a–5g`、`6a–6e` | 全部 **PASS**（含 6c：45 条 units 的 z 都非空、空中实体 z 都非常数） |
| `2b` | WARN（设计如此）：events 65 vs 窗口内去重 hudmsg 153 行 —— v4 起事件来自回放包，不与 hudmsg 行数对齐 |

`meta.devValidation` 里透传的是解码线的自检数字（本机平面 rms 3.98 m / max 130.3 m、高度 rms 0.45 m、
时间偏移 53.45 s、速度违规 16 步、他机关联率 58.9%），并注明「由解码线复算，本脚本未独立复算」；
另一份独立复算（Procrustes 拟合）得到 rms 6.32 m / max 207.5 m —— 两者方法不同（前者是去中位偏移后的逐点差，
后者是含旋转/缩放的相似变换），差异如实保留，不做修饰。

### 3.3 玩家击杀归属（口径更新）

回放击杀包的位流字段给了 `KillerUid`，配合 `uid_map.json` 可以**真正归属**：本机 uid=368，
`events` 里 `isPlayerActor` 的 kill 恰好 2 条，与内嵌结算 `kills=2` 一致。
也就是说契约 §2.6 说的「回放侧无法归属」在拿到位流字段后**不再成立**；
`counts.ownKillsByEvents` 因此是 2（而不是 0）。
归属链是 `UID → 实体航迹`：本机 uid=368 落在连续的 344…375 这一段（**32 个值 = 本局 32 名玩家**），
而击杀表出现的 uid 全集是 `[215,216,299,344…375,414,2047]` —— 全程只用回放的 uid 与航迹，
**不需要玩家名，也不依赖 8111**。早期版本的 2c 还写着「回放侧本不该能归属」，
该判据已确认过时。

---

## 4. 怎么复现

```powershell
# 交付文件（只读 build/wrpl/**，不读 8111 抓取）
python tools/build_ace_mission.py

# 开发脚手架（读 8111 抓取；不交付）
python tools/build_ace_mission_dev8111.py

# 独立复算 / 验收
python tools/check_ace_mission.py
```

`build_ace_mission.py` 是确定性的：同一份输入跑两次输出字节一致；关键数字（窗口、覆盖率、插值跨度、
事件分类、击杀坐标来源、接缝断开、y 轴判据）全部打印到 stdout；输出 ≤12 MB（超限按契约 §2.3 优先级降采样并记录）。

---

## 5. 第三方与署名

回放格式解析（容器布局、zstd 包流、ptype/签名、位打包实体、内嵌 BLK 结算）参考开源实现
**wrpl-inspector**：<https://github.com/maxsupermanhd/wrpl-inspector>（AGPL-3.0）。
它**只用于开发期**理解包格式、校验解码思路，**代码与数据都不进本产物**；
`mission-data.js` 的每个字节都来自本局 `.wrpl`，交付文件不依赖该仓库。

这段署名同时写进了 `meta.provenance`，页面 `?` 帮助会全文显示（帮助面板读取 `meta.provenance`）。

---

## 6. 已知的工具侧偏差（不影响交付数据）

`4a.time-map` 这一检查在**本机被切成两段**之后，把最佳实体选成了**较短的第二段** `uid-368#2`
（159 s、636 采样），并拟合出 `rate = 256472`（而 `tracks.secPerTick` 与 `header.clock` 都是 256000，
文件里也写着 `tickUnit = raw-256kHz-counter`）+ `offset = 85.54 s`（正确值是 84.54 s）。
用这条错的映射，"逐样本对照"类的检查会整体偏 —— 表现是 `1.own-truth` / `4c.own-accuracy` / `4e.devvalidation`
三项 FAIL（z 差到几百米、rms 18.5 m）。
**修正前**（同一份坐标、映射取到第一段 `uid-368`、rate 256000、offset 84.52 s）这三项是
PASS / WARN / PASS：z 偏差 ≤2.9 m、rms 6.3 m、`det = +1.000`。
建议工具侧：分段存在时把 4a 的候选锁到 `ownSegmentIds` 里**最长的那一段**（或把各段按 tick 轴并起来一起拟合），
并把 rate 固定为 `1 / secPerTick`（文件已声明 tick 单位）。**交付数据本身不需要改。**

---

## 7. 未做项（两条，都不阻塞，**当前字节已验证故未动**）

被验版本：`mission-data.js` mtime **06:09:31**、size 2 002 967、sha256 前 16 `315c8600ac97b873`，
独立复算结果 **PASS 31 / WARN 1 / FAIL 0**（VERIFY.md）。以下两条要改就会换掉这个哈希、
需要把整套检查重跑一遍，故留给下一次重建：

1. **`meta.devValidation` 补 `ownP99ErrM`**（独立复算 **4.99 m**；`tracks.json` 里的值是 **6.52 m**，
   本脚本可直接透传）。同时需要在 `note` 里写明采样口径差异：本脚本的 `ownSamplesCompared = 2236`
   是 `own_props.json` 的全部采样，另一口径的 2793 是「与 8111 真值时间重叠的样本数」。
   为什么值得补：`ownMaxErrM` 受极少数离群点支配（130.3 m，独立复算 35.0 m），
   **p99 才是可引用的精度数字**（复算 5.0 m / 上游 6.5 m）。
2. **（已更正）`header.json` 的 `clock` 并没有"残留旧键"**。2026-10-19 06:38 已证实：
   新版 `clock` 只有新键名（`rawTicksPerSecond` / `secondsPerRawTick` / `rawTicksPerMs` /
   `endMarkerRaw` …），**没有**旧的 `ticksPerSecond` / `secondsPerTick` / `tickQuantum` /
   `endMarkerTick` 独立键，也就不存在"值为 null"的键。
   那些 null 出自 `build/verify/report.md` L137 —— 那是**按旧键名 lookup 的诊断转储**
   （同一行的 `startMarkerSec` / `endMarkerSec` 反而是从新版读到的真值，正说明读的是新文件）；
   全仓库 grep `ticksPerSecond` 命中 `header.json`，是因为它是 `rawTicksPerSecond` 的**子串**。
   新版还显式提供 `clock.deprecatedKeyNames`（旧名 → 新名映射），按旧名查的脚本会直接读到映射而不是 null。
   本脚本的兜底读法（`secondsPerRawTick → secondsPerTick → 1/256000`、`endMarkerRaw → endMarkerTick`）
   无需改动，只是现在第一优先级就命中。**此条与交付数据无关，`mission-data.js` 未改。**

---

## 8. 类别层（`vehicleCategory`）的字节账：+7287 B 的构成，与「净差 247 B」的对账

被验版本：`mission-data.js` size **3 909 505**、sha256 前 16 **`63464e3185cf5001`**、mtime **10:50:03**；
上一版 size **3 902 218**、sha256 前 16 **`2149b0b8e3472e61`**、mtime 10:20:34。

独立估计按「移除新层重建上一版」算得 6400 + 616 + 114 + 404 = **7534 B**，与实际字节差 **+7287 B**
相差 **247 B**，因此怀疑有一处既有文本被改写。**核对结论：没有任何既有文本被改写**，247 B 全部来自估算口径，
可复跑脚本：`build/recon/cache-dataeng/explain_7287_byte_delta.py`（只读，不写文件）。

当前版本只在既有结构上**追加**五块，实测（文件里的 UTF-8 字节，含 JSON 逗号）：

| 块 | 内容 | 实测 | 独立估计 |
|---|---|---|---|
| ① | 236 个 `,"vehicleCategory":"X"` | 6400 B | 6400 B |
| ② | `,"vehicleCategories":{…}`（词表 + 来源 + 计数） | **345 B** | 616 B |
| ②b | `,"unitsWithCategory":236`（`counts` 新键） | **24 B** | 未列入 |
| ③ | hidden 那条 limit **追加**的子句 | 114 B | 114 B |
| ④ | 新增的「类别」limit 元素 | 404 B | 404 B |
| | **合计** | **7287 B** | 7534 B |

247 B 的对账：`616 − 345 = +271`（② 多算，主因）、`404 − 404 = 0`、`− 24`（②b 少算）→ **+247 ✓**。

②的 616 B 对不上任何一种实测口径（meta 块 UTF-8 345 B / `ensure_ascii=True` 形态 420 B /
builder 源码文本 766 字符），属估算值而非实测值。

**「既有字段一字未改」是字节级证明过的**：把上述五块从当前文件逆操作去掉（236 个字段 + ② + ②b + ③ + ④），
逐字节比对结果与上一版**完全相同**——脚本输出行
`重建的「上一版」：sha256[:16] 2149b0b8e3472e61 | size 3902218` 与
`与上一版逐字节相同：True`。因此 `limits` 是**追加一句 + 新增一个元素**，
不是任何句子的重写；`provenance`、`meta` 其它字段、`counts` 的键序均未动。

（本文档只改文档、未动数据：`mission-data.js` 仍是 `63464e3185cf5001` / 3 909 505 B。）

**独立复跑（2026-10-19 11:53 回执）**：用另一份自写的
`build/verify/recon_my_own.py`（只读）复算，五块逐项一致
（6400 + 345 + 24 + 114 + 404 = 7287 B = 实际字节差），逆操作重建后 hash 与上一版完全相同，
并做了"扰动 1 字节即失败"的对照，证明该比对能失败、不是走过场。
616 B 的来源也查清了：定位 `meta.vehicleCategories` 块时用了 `rfind("{")`，
匹配到**它前面那个嵌套对象**的 `{`，于是把 345 B 量成 616 B（**修正做法：从该键冒号后的第一个 `{` 起做括号配平**）；
再加漏列 `counts.unitsWithCategory` 的 24 B，271 − 24 = 247 正好等于该报告里的净差。
VERIFY.md 已把 §15q 判为 **PASS**（"既有字段一字未改"在字节层面成立），
并在 §3.13 留痕方法论：**"估算字节差"不能当判据，必须逐块实测或做逆操作重建比全量哈希**。
— 两份复算脚本各自独立、互不改写：`build/recon/cache-dataeng/` 与 `build/verify/`。
