# 独立解码线：`ptype=6 ECS`（Danet ECS）—— 结果与穷尽记录

对象：`2026.10.18 05.45.06.wrpl`（version 101404），解压包流 `data/_scratch/payload.bin`
（14 180 794 B，86 773 个包）。

产物：

| 文件 | 内容 |
|---|---|
| `tools/wrpl_decode_ecs.py` | 本线全部代码（自包含，不依赖 `wrpl_decode.py`） |
| `build/wrpl/uid_map.json` | **`uid ↔ entityId ↔ 载具/阵营/玩家` 映射（44 个 uid，全覆盖）** |
| `build/wrpl/tracks_ecs.json` | 契约 §1.0 同构的部分产物：81 个飞机实体的出生点 |
| `build/wrpl/ecs_entities.json` | 全部 ECS 实体（4427 条构造记录，见 §3.6） |
| `build/wrpl/vehicle_display_names.json` | 内部名 → 显示名（显示约定，见 §5.5） |
| `build/wrpl/projectile_launches.json` | 1734 条弹药创建点（含三维位置与朝向，见 §3.6） |
| `build/wrpl/runways.json` | 机场/跑道判定：**无跑道几何** + 4 个机场点（见 §3.7） |
| `build/wrpl/squad.json` | 真实小队（结算 `squadId`）+ 队友 uid 假设（见 §5.8） |

复现：

```bash
python tools/wrpl_decode_ecs.py --all        # explore+spawn+verify+uidmap+build
python tools/wrpl_decode_ecs.py --uidmap     # 只出 uid_map.json
```

方法来源：**结构移植自 [wrpl-inspector v3](https://github.com/maxsupermanhd/wrpl-inspector)（Go，AGPL-3.0）**
`wrpl/packet/parser/ecs2/{ecs2,Parsers,compdefs,EntityManager}.go` 与 `data/ecshashes.json`
（组件哈希→名字，780 KB）。本脚本是独立的 Python 重写，并按本局实测改了两处（见 §2）。
本脚本**不读** `data/raw/rb-session/**`，8111 只出现在 `--verify` 的开发期校验里。

---

## 1. 结论速览

| 项 | 结果 | 可信度 |
|---|---|---|
| ptype=6 是 Danet ECS 消息流 | **成立**：控制码 0x20/0x22/0x24/0x25/0x26 与参考实现完全对应 | A |
| 实体构造（control 0x24/0x25）可完整解码 | **成立**：2524 条构造消息，0 个包解析失败；模板 17 个、组件定义 137 个 | A |
| `uid` ↔ 实体 | **成立**：44 个 uid，与航迹解析用 ptype=2/fm 独立解出的 44 个 uid **数量一致** | A |
| 飞机出生三维位置 | **成立**：81 个飞机实体，位置落在机场/空中出生区，量级与编队几何自洽 | B |
| 逐帧（每 tick）三维航迹 | **不成立**：ECS 构造消息只在实体创建时发一次；本局 `bfd_norespawn` 无重生 | — |
| `uid ↔ 玩家名` | **只解出 1 个唯一命中（另一种阵营定向下 9 个）**，其余是 2–3 个候选 | B |
| 坐标系与 8111 的刚体对齐 | **未闭合**，只做了平移/旋转免疫的距离指纹 | C |

---

## 2. 与参考实现不同的两处（本局实测）

### 2.1 本实现的载荷比参考实现多 1 字节前缀

参考实现的 `PacketStreamReader` 每次读 **2 字节**包头（`Header[0]`=类型，`Header[1]` 丢弃），
本实现的包框架只读 1 字节；有 tick 时它读 6 字节、这里读 5 字节。
所以 **参考实现的 payload = 本实现的 payload[1:]**。

交叉验证（这条一开始是用来排除误读的判据，结果是决定性的）：

| 参考实现里的匹配条件 | 本局对应签名 | 核对 |
|---|---|---|
| `02 58 58 f0`（kill） | `00025858` → pl = `02 58 58 f0 23 00 fe ff` | ✓ ✓ ✓ ✓ |
| `ff 0f …`（movement） | `00FF0F81` → pl = `ff 0f 81 …` | ✓ ✓ |
| ECS 控制码 0x20/0x24/0x25 | 本实现载荷偏移 **1** 处 | ✓ |

因此 `ptype=6` 载荷的**第 0 字节是本实现框架里的填充**，控制码在偏移 1。

### 2.2 `compCount == 0` 是「全部组件」，不是「没有组件」

参考实现 `deserializeConstruction` 把模板定义之后的那个字节当作**组件下标增量表**的长度。
本局实测：该字节恒为 `0x00`，而块里还有大量剩余字节（例如 `aircraft+player_unit` 的块 399 B、
按参考逻辑只消费 2 B）。把它解释成「按模板顺序把**全部**组件值依次写下」之后，
`t_aircraft+player_unit` 的块能被**恰好用尽**，且解出的字符串/位置全部合理。

反证：若按参考逻辑，则 81 个飞机块里应出现大量「值起点在块尾之后」的情况，实际没有。

> 注意：`0 = 全部` 这条**只对 `compCount == 0` 成立**；本局没有观测到非 0 的 compCount，
> 所以「非 0 时按增量表」这一分支**未被验证**（参考实现的分支保留在代码里，未触发）。

---

## 3. 关键字段定位（可复现的判据）

### 3.1 `uid`

在飞机（`aircraft+player_unit` / `aircraft`）构造块里，**组件值起点 + 29** 处的 u16 LE，
取低 11 位（0x7FF = 无效）：

- 44 个互异值，范围 **344…422**；
- 与航迹解析用完全不同的通道（ptype=2 FlightModel）解出的 uid 集合**数量一致（44 个）**，
  且本机 uid=368 落在其列出的区间内 → 互证。
- 定位方法：对 81 个块的每个相对偏移，统计 u16 LE 取值，条件是「80% 落在 1..2046 且 70% 互异」；
  只有 `rel=29` 命中（65/81 落在 1..2046，59 个互异），其余偏移命中 0。

#### 3.1.1 大块必须走 `spawnSlotName` 兜底（2026-10-19 增补）

`uid` 在「组件值起点 + 29」这条规则**只对小块成立**。本局飞机构造块有两个量级：

| 块长 | 内容 | 值起点+29 读 uid |
|---|---|---|
| 399 B（32 个） | 占位实体，`unit__className` = `dummy_plane` | ✅ 有效 |
| ~1023–2100 B（33 个） | 成熟实体，真实机型 | ✅ 有效 |
| **~2183–3300 B（16 个）** | 成熟实体，真实机型 | ❌ **读到无效值** |

那 16 个块读不到 uid，于是对应 uid **只剩占位块**（`dummy_plane`），机型就丢了 ——
这正是「7 个 uid 没有机型」的真正原因（**不是回放里没有数据**）。

**兜底链**：占位块与成熟块共用同一个 `spawnSlotName`（`tN_playerMM_K`），
而本局实测同一 uid 的所有块 `spawnSlotName` **100% 一致**（§3.2）。所以：

```
spawnSlotName → uid（从能读到 uid 的块建立）→ 该 missionName 下所有块的机型
```

修后：**44 / 44 个 uid 都有机型**，且**没有任何一个 uid 解出两个不同机型**（0 冲突）。
连带把 `team`/`side` 覆盖从 14 提到 **22**，`player` 唯一命中从 9 提到 **14**。

顺带更正：早先曾说「missionName 前缀不是队别」仍然成立；这里用的是它作为
**同一实体的连接键**（不是队别），两者不矛盾。

### 3.2 `spawnSlotName`（`tN_playerMM_K`）——**它不是队别**（重要更正）

块内长度前缀 ASCII 串，形如 `t1_player03_0` / `t2_player16_0`，与
`REPLAY-FORMAT.md` §5.3 的 `t1_player01…t1_player16` 模板名对得上。
44 个 uid 里 32 个有它，且**每个 uid 的多个块给出完全相同的值（0/44 不一致）**，
所以「uid ↔ 这个串」的配对是可靠的。

**但这个前缀不是队别，是出生组。** 证据全部回放内部（用机型去查结算玩家表的 `team`）：

| uid | spawnSlotName | 机型 | 该机型在结算表里的 team | 结论 |
|---|---|---|---|---|
| 346 | `t1_player03_0` | `su_33` | 只出现在 team 2 | `t1_` 却是 team 2 |
| 348 | `t1_player05_0` | `f_20a` | 只出现在 team 1 | `t1_` 却是 team 1 |
| 364 | `t2_player05_0` | `f_16xl` | 只出现在 team 2 | `t2_` 却是 team 2 |
| 369 | `t2_player10_0` | `tornado_f3` | 只出现在 team 1 | `t2_` 却是 team 1 |

`t1_` 与 `t2_` **各自都同时包含两队**。所以 `spawnGroup` 只当原始信息保留，
**不要**拿它推 `team`/`side`。

（更正记录：本文档第一版与 `uid_map.json` 第一版把 `t1_/t2_` 当队别，给出了 32/44 的
`team`/`side` —— **那一版是错的**。航迹解析用 8111 的颜色/可见性独立复核后指出
`side` 不一致 23/29，并给出锚点「本机两次击杀的受击者 uid 348、374 都应是敌方」；
按该锚点复查，348 对、374 错 → 定位到本 bug。现已改成从机型推。）

### 3.3 `unit__className`（→ 机型）

块内的长度前缀 ASCII 串。占位实体是 `dummy_plane`，真正的机型出现在同一 uid 的**后一个**
（块更大的）实例上：`f_15a`、`su_33`、`fa_18c_switzerland`、`f_16a_block_10_netz_mod`、
`cf_188a_canada`、`f_16xl`、`su_30mk2v_venezuela`、`tornado_f3`、`su_25`、`fa_18a`、`jf_17`、
`f_14a_early`、`f_20a`、`mig_29smt_9_19`。AI 实体给出 `mig-15bis_ish`、`a_4b`。
取值按 `^[a-z0-9][a-z0-9_.-]{2,48}$` 过滤（不过滤会混进 `k;DC`、`d?**` 这类误读）。

### 3.4 出生位置

组件值起点 **+10**（相对偏移）处三个连续 float32 = `(x, 高度, z)`：

- 81/81 个飞机块都命中；
- 相对偏移直方图**只有 +10 一个峰（81 个块），其它偏移 ≥3 的一个都没有**；
- x ≈ 28 300…29 700 m、高度 ≈ 1 090…1 560 m、z ≈ 28 900…29 620 m，正好是两个出生区的两小簇；
- `dummy_plane` 占位实体与真机实体（同一 uid）位置一致。

> 这是**经验标定**，不是从格式文档推出来的。参考实现的 `TransformParser` 自己也没解内容
> （它只是 `IgnoreBits`）。用 **+10（相对组件值起点）**而不是绝对偏移，是因为首次出现的模板
> 会把定义表写在前面，绝对偏移会整体后移。

### 3.5 开发期校验数字（8111 只用于验证）

`--build` 会自动挑「出生样本最多的 1 秒桶中、8111 同时刻空中接触点 ≥4 个」的那一桶，
做**平移/旋转免疫**的两两距离分位曲线比对：

| 项 | 值 |
|---|---|
| 分位曲线相关 | **0.9252**（9 个分位点） |
| 该切片 | 回放秒 267.54，±0.4 s，ECS 12 个出生点 vs 8111 56 个空中接触点 |
| ECS 两两距离中位 | 15 948 m |
| 8111 两两距离中位 | 13 136 m |

**怎么读这个数**：两组不是同一批目标（ECS 只有 12 个刚出生的实体，8111 是该时刻全部
56 个空中接触），所以只能比**分布形状**，不能逐点配对。0.93 说明「量级与离散程度一致」，
是**弱到中等**的正向证据；**没有**做到刚体对齐，也没有逐点残差（没有残差数字）。
这一项不影响 `tracks_ecs.json` 的内容——它只出现在 `devValidation` 里。

---

### 3.6 ECS 实体全清单与「弹药类实体」（2026-10-19 增补）

`ecs_entities.json` 由 `--entity-list` 生成，逐条列出全部实体构造记录。

**规模（实测）**：构造消息 **4 427** 条，互异 `entityIndex` **3 738** 个，模板 **27** 个，
组件定义 **206** 个。

> 更正一个之前的说法：一度报过「110 个实体」。那个数字是 **control 0x20（ID_ENTITY_MSG）
> 通道里互异的 EID 数**，不是实体总数。实体总数见上。

| 类别 | 条数 | 有位置 | 判据（都能在代码里复核） |
|---|---|---|---|
| `projectile` **弹药类** | **1 734** | **0** | 模板组件恰好 `[<xxx>_component, active, proj__traceHandle]` |
| `scenery` 布景/触发体 | 1 457 | 697 | 灯柱、机场警示灯、grid_holder、destroy_ri_box_area |
| `mod` 载具模块 | 928 | 0 | 模板含 `mod__*` 组件（工具包/灭火器/医疗包/烟雾） |
| `ground` **地面单位** | 116 | **115** | 模板 `tank`（`unit_storage__tank` + `uid`，className `tankModels/...`） |
| `aircraft` 飞机 | 81 | **81** | 模板 `aircraft` / `aircraft+player_unit` |
| `structure` 工事 | 69 | **69** | 模板 `fortification`（`structures/...`） |
| `player_slot` 玩家位 | 32 | 0 | 模板 `sync_player`（`possessed/connid/is_local/team/base_player_id`） |
| `explosion` 爆炸区 | 8 | 0 | 模板 `damage_area`（含 `damage_area__pos1/pos2/vel`） |
| `misc` | 2 | 0 | `msg_sink` / `server_load_stat` |

**弹药类细分（共 1 734 条，全部是「实体创建」事件）**：

| 模板 | 条数 | 说明 |
|---|---|---|
| `rocket` | 1 589 | 火箭/导弹弹体 |
| `rocket+arh_shell` | 29 | **主动雷达制导空空弹**（ARH = active radar homing） |
| `bomb` | 95 | 炸弹 |
| `payload` | 17 | 挂载/副油箱 |
| `jettisoned` | 4 | 抛弃物 |

#### 弹药类的三维位置：**已解出**（2026-10-19 更正，此前写"未解"是错的）

**先认错**：早先报过"弹药位置未解"，那是**假否定**，两个原因都在本侧：

1. **混扫**：同一模板的 block 有多个长度变体（`rocket` 有 132/133/134/189/197/…），
   按模板整体扫一个偏移，不同变体互相污染，命中率只有 12%–62%。
2. **轴向映射写错**：判"出生点是否紧邻发射机"时，把 ECS 的 `(x, 上, z)` 直接当成
   世界 `(东, 北, 上)` 用了，少了一次换位（正确是 `(x, z, 上)`）。这一步足以把米级
   距离放大到公里级，于是把正确的位置当成了错的。地面单位那次的校验用的是**正确**映射，
   所以没受影响——同一个坑在两个地方只踩了一次。

**正确规则（不写死偏移）**：

```
块内找唯一一个 float4，其模长 |q| ∈ [0.98, 1.02]（四元数）
位置 = 紧邻它**前面的 12 字节** = 3×float32 = (x, 上, z)
```

四元数相对组件值起点的偏移**随 block 长度变**（本局实测 28 / 29 / 30），所以按
**四元数指纹**锚定，而不是按固定偏移。按长度分档后的命中率：

| 模板 | block 长度 | 条数 | 四元数 | 位置 |
|---|---|---|---|---|
| `rocket` | 133 | 955 | rel=29 **100%** | rel=17 **100%** |
| `rocket` | 132 | 531 | rel=28 **100%** | rel=16 **100%** |
| `rocket` | 134 / 190 / 198 / 236 | 20 / 14 / 6 / 4 | rel=30 / 29 / 29 / 29 **100%** | −12 |
| `rocket` | 189 / 197 / 235 / 171 / 201 / 237 | 24 / 20 / 9 / 2 / 1 / 1 | rel=28 / 28 / 28 / 29 / 28 / 29 **100%** | −12 |
| `rocket+arh_shell` | 236 / 235 | 26 / 2 | rel=29 / 28 **100%** | −12 |
| `bomb` | 136 / 137 | 85 / 7 | rel=28 / 29 **100%** | −12 |
| `payload` | 132 / 133 | 8 / 7 | rel=28 / 29 **100%** | −12 |
| `jettisoned` | 133 | 3 | rel=29 **100%** | −12 |

→ 先前那条"`bomb` 85/95 vs `rocket` 193/1589 的不对称"**完全由混扫造成**，分组后两边都是 100%。

**独立复算**（判据是语义的：弹体出生点应当紧邻同刻的发射机；参照 `tracks.json`
的飞机航迹，同属回放自身数据）：

| 量 | 值 |
|---|---|
| 解出位置的弹类实体 | **1 734 / 1 734（100 %）** |
| 到最近飞机航迹：中位 | **15.1 m** |
| p90 / p99 / 最大 | 34.2 m / 45.6 m / **74 m** |
| ≤100 m 比例 | **100 %** |
| 对照：y/z 互换 | 中位 12 370 m、≤100 m 占 0.6 % |
| 对照：北向取反 | 中位 6 132 m、≤100 m 占 0.3 % |

偏移选错时这一项会退化到公里级（早先就是这么误判的），所以米级的结果是硬证据。
两个对照由航迹解析独立复核给出，与 15.08 m 逐位吻合。

**一个必须记下的守卫**：有 2 个块的「第一个满足 |q|≈1 的 float4」前面是**全 0 填充**，
位置恰好解成 `(0, -0, -0)`，把 ≤100 m 比例从 100 % 拉到 99.9 %。
加一条守卫（位置落在原点 100 m 内就跳过、继续找下一个四元数候选）之后，
这 2 条都找到了正确候选，指标变成 **100 %、最大 74 m、零离群**。
教训：`|x| ≤ 45000` 这种**量级合法性**检查挡不住全 0 —— 全 0 永远"合法"。


**产物**：`build/wrpl/projectile_launches.json`
（1 734 条 `{entityId, entityIndex, template, spawnTickRaw, x, y, z, quaternion}`，
坐标已是世界 `(东, 北, 上)`，只给 raw tick）。`ecs_entities.json` 里弹类的
`position` 也已一并填上（此前是 `null`）。

**仍然没有的**：弹药的**逐帧**航迹。弹药模板没有 `uid` 组件，无法关联到 `ptype=2`
FlightModel；每条只有一个采样点（创建时刻）。

#### 顺带得到的、**已校验**的东西：地面单位位置

同一套「transform 打头 → 组件值起点 +10 = (x, 上, z)」规则，对地面单位也成立，而且
**它们彼此独立地互相验证**（这是本线最强的位置证据）：

| 模板 | 命中 | y（上轴）绝对值中位 | 应有形态 |
|---|---|---|---|
| `aircraft+player_unit` / `aircraft` | 81/81 | 1 223 m | 空中 |
| `tank` | 115/116 | **29 m** | 贴地 |
| `fortification` | 69/69 | **61 m** | 贴地 |

地面单位在同一时刻是静止的，所以可以拿**全部** 8111 地面接触点做一次平面最近邻：

| 量 | 值 |
|---|---|
| 被测地面/工事位置 | 184 条 |
| 到最近 8111 地面接触点：中位 | **0.03 m** |
| p75 | 601.8 m |
| ≤100 m 比例 | **66.3 %** |
| 对照组（把 north 轴向取反）中位 | 3 632 m |
| 对照组 ≤300 m 比例 | 8.7 % |

坐标对应：ECS `(x, 上, z)` ↔ 世界 `(east, 上, north)`；8111 `map_obj` 的归一化 y 是**屏幕轴**
（向下=南），所以 `north = -(y-0.5)×65536`。中位 0.03 m 说明这套对应是正确的，
**不是**自证：轴向取反后立刻退化到 3.6 km。

这些数字只写进 `ecs_entities.json` 的 `devValidation`（纯数字，不含任何外部坐标）。

> **地面层的正式产物不在这里**：兄弟线航迹解析已从 `ecs_entities.json` 产出
> `build/wrpl/ground.json`（184 条静态点位，含同一套校验数字）。
> 本线**不再产出第二份地面文件**，避免两份竞争。本脚本的 `--display-names` 只负责
> 显示名，不写任何坐标。

### 3.7 `className` 字段与「跑道」结论（2026-10-19 增补）

#### `ecs_entities.json` 的 `className`

**这是一个补上的列，之前没有。** 早期版本只给 `strings`（块里前 6 个长度前缀串），
**不能拿来取车型名**：实测地面单位的第 1 个串是模板名 `tank`，其后是二进制误读出的乱码
（样例 `["tank","I=D","hlA","PC^","Y%7","Us{"]`），真正的 `unit__className` 不在前 6 个里。

正确取法：逐偏移扫长度前缀串，前缀必须是 `tankModels/` 或 `structures/`（地面/工事）；
飞机则要求串落在「结算玩家载具词表 ∪ 观测到的 AI 机型」里。

| 类 | 有 `className` |
|---|---|
| ground | 115 / 116 |
| structure | 69 / 69 |
| aircraft | 81 / 81 |

#### 跑道：**回放里没有跑道几何**（结论，不是解析失败）

四条独立查证，结论一致：

1. **全量 4427 个实体块**按 `airfield|runway|airstrip|strip|apron|taxiway|airbase|hangar|farp`
   逐块扫全部字符串 → 命中的**全是点状**结构：机场主雷达 ×4、飞行雷达 ×8、机场高炮 ×22。
2. **尺寸判据**：块里有一个恒定的 `2048.0` f32，落在 1000–3000 m 区间 —— 但**所有块都一样**。
   一度只按「值域」筛，把它当成跑道长度候选，得出 `candidate`；收严到「同一概类里要有 ≥3 个
   不同值」后判为**无**。被否掉的候选留在 `runways.json` 的
   `searched.rejectedConstantSizeCandidates` 里，可复核。
   **教训与 §3.6 的全 0 那条同源：值域检查不是有效性检查，常量永远落在值域里。**
3. **朝向判据**：少数块有 |q|≈1 的四元数，但**没有可配对的长度** → 定义不出线段。
4. **回放自带单位名表**（403 个名字）含机场关键词 59 个，**全是高炮小队、雷达、出生点**
   （`ft_t*_airfield_*_aaa_squad`、`t1_airfield_top`、`missions/airfield_spawn`），
   `runway` / `airstrip` / `strip` / `apron` / `taxiway` 一个都没有。

**产物** `build/wrpl/runways.json`：

```jsonc
{ "answer": "no_runway_geometry",
  "answerZh": "回放里没有跑道几何，无法画跑道直线",
  "runways": [],                    // 空是结论
  "airfields": [4 个点],             // 由机场雷达/高炮结构**聚类**得到，不是跑道端点
  "airfieldStructures": [32 条],     // 带 entityId / 位置 / blockSize
  "searched": {...}, "limits": [...] }
```

**4 个机场点**（世界 `(东, 北, 上)`，米）：

| key | 位置 | 结构数 | 依据 |
|---|---|---|---|
| af-01 | (5746, −27038, 120) | 13 | 同时有主雷达 + 飞行雷达 + 高炮 |
| af-02 | (−26483, 25878, 64) | 13 | 同上 |
| af-03 | (−14245, −21584, 173) | 3 | 主雷达 + 飞行雷达 |
| af-04 | (−17777, 24439, 246) | 3 | 同上 |

**不要**把雷达点到雷达点连一条线充作跑道 —— 那是本线明确排除的编造。

（**注**：本小节写于第一次查证之后；随后用机场标记点反推出了 4 条跑道，见 §3.7.1。这里保留「不要连雷达点」的告诫 —— 反推用的是**标记点**，不是雷达点。）

**跑道的可视几何来自游戏关卡 `levels/air_skyscraper_city.bin`，该文件不在回放里。**

### 3.7.1 更正：跑道**反推出来了**（2026-10-19 二次增补）

上面「没有跑道几何」的结论**仍然成立**（回放里确实没有跑道多边形），但**线段可以反推**：
`00025839`（单位/模板定义）包里出现了四个机场标记点——

| 标记名 | 内容 |
|---|---|
| `t1_airfield_top` | 位置 3×f32（同点重复采样 8 次）+ **航向 3×f32 单位向量** |
| `t2_airfield_top` | 同上（14 次） |
| `t1_airfield_top_reload` | 同上（14 次） |
| `t2_airfield_top_reload` | 同上（14 次） |

每个标记点后面紧跟一个长度前缀的模型名 **`dynaf_pg_1line_3000_universal`** ——
「1 条线，3000」，这就是**长度 3000 m** 的线索。于是
`两端点 = 中心 ± 单位航向 × 1500 m`。

**自证（三条独立）**：

| 机场 | 标记 | 航向 | 位置采样散布 | 与机场结构高度差 | 距机场簇 |
|---|---|---|---|---|---|
| af-01 | `t1_airfield_top` | 155.3° | 14.2 m | 7.7 m | 1869 m |
| af-02 | `t2_airfield_top` | 323.6° | 25.7 m | 7.7 m | 1827 m |
| af-03 | `t1_airfield_top_reload` | 168.2° | 25.7 m | **0.0 m** | 2418 m |
| af-04 | `t2_airfield_top_reload` | 347.8° | 25.7 m | **0.0 m** | 2418 m |

1. **高度吻合**：标记点高度与该机场雷达/高炮结构高度差 0.0–7.7 m（af-01 112.0 vs 119.7；
   af-02 55.9 vs 63.6；af-03 172.8 vs 172.8；af-04 245.6 vs 245.6）。
2. **航向成对相反**：155.3°/323.6°（差 168.3°）、168.2°/347.8°（差 179.6°）——
   两队机场朝向相对，符合「主机场 + 备用跑道」的布局。
3. **结构数与主/备一致**：`top` 对应的 af-01/af-02 各有 **13** 个结构（含 10 门高炮，
   主机场防御）；`top_reload` 对应的 af-03/af-04 只有 **3** 个结构（备用跑道）。

**这一条不是从出生点猜的**：位置与航向都来自回放自身的机场标记点，
长度线索来自它引用的模型名。「地面出生点即跑道头」这一思路方向一致，
但**实际可用的锚点是机场标记点，比出生点更直接**（本局的 81 个飞机出生点都是
**空中出生**，高度 114–1558 m，不在跑道上）。

**这段实现里有三个 bug，都留痕**：
1. **长度前缀子串**：`t1_airfield_top` 是 `t1_airfield_top_reload` 的前缀，直接 `find`
   会把两者混起来 —— 两个标记点解出同一个位置。改成按「长度前缀」匹配。
2. **位置聚类半径过大**（300 m）：单链式合并把无关样本串成一簇，`spread` 失真到 275 m，
   把 confidence 压到 low。收到 30 m 后为 14–26 m。
3. **重叠扫描窗口**：同一个 f32 三连会在连续偏移上被重复计数，采样数虚高到 38。
   要求窗口至少前进 12 字节后为 8–14 次。

**教训同源**：这三条都属于「判据看起来在测某样东西，其实没有」。与 §3.6 的
「全 0 永远合法」、§3.7 的「常量永远落在值域里」是一类问题。

除非允许引入关卡文件或外部数据源的 airfield 线段（契约不允许），否则画不出跑道直线。

## 4. 排除了什么（穷尽记录）

按任务要求的「先试完再说话」逐条列，**含失败的**：

| 试过的 | 结果 | 判据 |
|---|---|---|
| `ptype=6` token 分组 | 7 类：0x20(7351) / 0x22(634) / 0x23(2) / 0x24(1066) / 0x25(542) / 0x26(1650) / 0x21(1) | 载荷偏移 1 处取值统计 |
| ECS 0024 大包（139/140/141 B）里的 f32 位置 | **命中但不是飞机**：那是 `rocket` 模板（tid=20）的弹体状态：位置 3×f32 + 四元数(|q|=1) + 速度 + 单位向量 | 模板名与组件序列化解出 |
| `00035843`（71 B，13412 包，24 Hz） | `4B 签名 + 5B 常量 + 14×f32 + 6B 常量`；偏移 9 的四元数在 **13412/13412** 个包里 |q|=1（完美指纹）；**载荷内没有 id 字段**（偏移 0..8、65..70 全部逐字节常量）→ **不是「按实体多路复用」的通道**；相邻包四元数连续（|dq| 中位 7.3e-4）→ 单实体流；但它导出 yaw 与 8111 本机 `compass` 中位差 −146°、MAD 20° → **它是谁未解出** | 逐偏移取值种类 + 四元数模长 + 与前导/后继包签名统计（前导/后继 78% 是 `00FF0781`/`00FF0F81`，游程几乎恒为 1） |
| `0002588e`（42 B 定长，783 包） | 结构清楚：3×f32（X, 高度≥0, Z）+ 1.0f + 单位方向向量(|v|≈1)；但相邻样本隐含速度中位 13 km/s → **相邻包不是同一实体**，且没有 id → 无法成轨 | 逐偏移 float32 扫描 + 单位向量模长 |
| `00FF0781` / `00FF0F81` 等 MPI 里的 float64 位置三连 | **全流逐包扫描，零命中**。条件收紧到「X/Y ∈ ±45000、Z ∈ [-1500,16000]」后，所有 (签名,长度,偏移) 组合的命中率都 <0.5 且是常量垃圾 | 见下 |
| 参考实现 movement 的字节条件（`ff 0f … a3 f0 … 00 00 … 13`） | **本局版本对不上**（`00FF0F81` 的对应字节是 `… a0 f0 … 02 13 …`）→ 参考实现锁的是更早的版本 | 逐包比对条件字节 |
| 按 (签名, 长度) 分组的「两两位移」相关扫描 | **唯一高点 0.836 是伪相关**：它落在 ECS 0024 偏移 81/82，那里的 float 就是**回放时间（秒）**，与位移通过「飞得快」相关；改用窄 Δt 窗后该峰消失/退化 | 见下 |
| 位级 10/12/14/16/18/20/24 bit 有符号定点扫描（所有偏移、所有分组） | 无命中（窄 Δt 窗内 corr 全部 < 0.55） | 同上 |
| ECS 控制码 0x20 是否按 EID 寻址 | **是**：把首个字段按 `ReadEID` 解，得 110 个 `0x400xxx` 形态的实体 id，且出现频次高度倾斜（0x400001 ×1950）。但 0x20 的组件值布局参考实现没实现，**未解** | 取值分布 |
| ECS 控制码 0x22 | 634 个包、607 个 L=18、1.03 Hz；首字段不像 EID（634 个互异）；前 6 个包里 `a9→aa→ab→ac→ad→ae` 单调增 → 更像时钟/序号。**未解** | 取值分布 + 样本比对 |
| ECS 控制码 0x26（销毁） | 1650 个包，L=4/5/8；首字段 1001 个互异，不是 EID。**未解** | 取值分布 |
| `00025839` / `000258db` 等短 MPI | 前者是单位/模板名表（REPLAY-FORMAT §5.3 已定）；后者无位置指纹 | 同上 |

### 4.1 「两两位移」扫描为什么先给假峰

统计量是：同一 (签名,长度) 分组内、时间相近的包对 `(i,j)`，
比较 `X = |f_i − f_j|` 与 `Y = |P_i − P_j|`（8111 本机三维位移）的 Pearson 相关。
它对未知平移免疫，但**对「时间本身」不免疫**——飞机在 dt 内飞得远，`Y` 就大；
任何随时间单调的字段（时间戳、里程计）都会拿到高相关。

第一版扫出 `ECS 0024 L=141 偏移 81/82 → corr 0.836`、`0003584a → 0.82`，全是这类。
第二版把包对限制在**窄 Δt 窗** `[0.7·median_dt, 1.5·median_dt]` 之后，这些峰消失。
**这条经验对后来者重要**：任何「单字段 vs 位置位移」的相关，必须先把 Δt 的效应消掉。

### 4.2 位置三连全扫的边界

穷尽的搜索空间（都跑过，全零命中）：

- ptype ∈ {4, 6} 的全部 (签名/控制码, 载荷长度) 分组（ptype=4 有 40+ 个签名 × 数十种长度）；
- 每个分组的每个字节偏移：`f32 / f64 / u8 / i8 / u16 / i16 / u32 / i32`；
- 每个分组的每个位偏移 0..7 × 位宽 {10,12,14,16,18,20,24,32} 的有符号定点；
- 候选位置的量级约束：`|X|,|Z| ≤ 45000`、`Z(高度) ∈ [-1500, 16000]`、`max(|X|,|Z|) > 200`；
- 命中率阈值 ≥ 0.5（组内 ≥50% 的包）。

结论：**本局的 MPI 里没有 float32/float64 的绝对世界坐标三连**（`0002588e` 是唯一的
「位置+方向」定长记录，但它是多实体混合且无 id）。这也解释了为什么参考实现那套
`movement` 条件在本局全不命中——版本变了。

---

## 5. `uid_map.json`

44 个 uid（与航迹解析的 44 个一致）：

| 字段 | 覆盖 |
|---|---|
| `entityId` / `entityIndex` | 44 / 44 |
| `spawnSlotName`（`tN_playerMM_K`，**不是队别**） | 32 / 44（另外 12 个是 AI：`mig-15bis_ish`、`a_4b`） |
| `vehicle`（`unit__className`） | 31 / 44 |
| `team` / `side`（**只从机型推**） | 14 / 44（8 ally + 6 enemy；其余机型两队都有人 → 留 null） |
| `player` 唯一命中 | 8 / 44 |
| `playerCandidates` | 有候选的都给出来，**有歧义一律不填 `player`** |

### 5.1 `team`/`side` 怎么来的、为什么只有 14 个

**不从 `spawnSlotName` 推**（理由见 §3.2：那个前缀是出生组，t1/t2 各自都含两队）。

从**机型**推：结算 BLK 给出 `player → vehicles[]`（本局每人 1 架机）。于是
`机型 → 该机型出现过的 team 集合`：

| 机型 | team 集合 |
|---|---|
| `f_14a_early` `f_20a` `f_16a_block_10` `mirage_4000` `saab_ja37di_f21` `su_25` `tornado_f3` `cf_188a_canada` | {1} |
| `su_33` `mig_29smt_9_19` `mig_29_9_12_germany` `jf_17` `f_16xl` `m_346fa` `su_30mk2v_venezuela` `fa_18c_switzerland` `f_16a_block_10_netz_mod` | {2} |
| `f_15a` `fa_18a` `fa_18c_early` | {1,2} → **留 null** |
| AI `mig-15bis_ish` / `a_4b` | 不在玩家表里 → null |

集合只有 1 个元素 → 定死 `team`；2 个元素 → `team=null`（宁缺勿错）。
本机 team=2（结算 `results.author`），所以 team 1 → `enemy`、team 2 → `ally`。

**对航迹解析两个锚点的结果**：uid 348（`f_20a`）→ enemy ✓；
uid 374（`fa_18a`）→ **null**（不判 ally，也不判 enemy）——这正是期望的「不确定就不说」。

### 5.2 `player` 为什么大多只有一个候选

`(team, vehicle)` 能反查玩家，但同队同机型有多人时（team 2 的 `f_15a` 有 3 人）就是
2–5 个候选，全部列在 `playerCandidates` 里；`team` 没定住时候选退化成「所有用该机型的玩家」。
回放里**没有** entityId ↔ 玩家名的直接映射（`REPLAY-FORMAT.md` §7.4 已排除），
出生位编号 `tN_playerMM` 与结算 `playersInfo.slot`(0..6) **也不是同一编号空间**。
所以：**宁可不填**。这是刻意留空，不是解析失败。

### 5.3 本机（可选锚点）

`--own-uid 368` 会把本机机型从**本局结算 BLK 的 author 行**补上（`f_15a`，team 2），
并在 `vehicleSource` 里标明来源。`uid 368 = 本机` 是**兄弟线用开发期真值钉住的**结论
（不是本线独立得出的），但补进去的机型名本身来自回放自己的结算 BLK，不含外部数据。
不带这个参数时该字段留 null。

顺带一个自检：本机 `uid 368` 的 `playerCandidates` = `[Mo Amaris, WhatsConsent, 八幡 海鈴]`，
**录像者自己就在候选里** —— 说明「机型 → 玩家」这条链是自洽的。

### 5.4 独立复算结果（航迹解析复核，开发期）

航迹解析用两条互相独立、且彼此一致的 8111 证据复核了修正版的 `side`：
① 色板 `#174DFF` 友 / `#39D921` 小队 / `#FA0C00` 敌；② RB 里友军全程显示、敌军仅被点亮时显示，
于是「最近 8111 接触覆盖率≈100%」=友军、「覆盖率低」=敌军。

结果：**本线给出明确 `side` 的 9 个 uid，与他们的判定 9/9 全一致**
（349/352/358 = ally；344/345/348/361/369/372 = enemy）。其余 uid 本线给 `null`，
他们也多数判不出（覆盖率落在 0.5–0.95 的中间地带）→ 「不确定就不取值」的处理被确认是对的。

下游已落地：`tracks.json.units[].side` = ally 8 / enemy 6 / unknown 30（本线的 `null` 映射成 `unknown`）；
`vehicle` 覆盖 31/44；本机 `uid-368` = `ally` + `vehicle:"f_15a"`。

> 本次复核同时纠正了本文档上一版 §5.1 的错误（把 `tN` 前缀当队别）。留个记录：
> 错在「用一个看起来像队别的字段去推队别」，而没有先用**独立锚点**（本机两次击杀的
> 受击者 uid 348、374 都必须是敌方）去查。**先找锚点，再信统计。**

---

### 5.5 `vehicle_display_names.json`（显示约定，不是回放数据）

`--display-names` 产出。结构就是一张**扁平映射表**，可一行 join：

```js
const label = NAMES[vehicle] || vehicle;
```

| 项 | 值 |
|---|---|
| 覆盖 | 地面/工事内部名 **12** 个 + 飞机内部名 **25** 个 = **37** 条 |
| 已映射（curated） | 37；原样保留（raw）0 |
| 机型粒度 | **只保留基础型号**：飞机去掉出口型/批次/子型号/中文注记（`F/A-18C 早期型→F/A-18C`、`MiG-29SMT (9-19)→MiG-29SMT`、`F-16A Block 10 Netz→F-16A`、`Su-30MK2V 委内瑞拉→Su-30MK2V`、`JA 37DI (F21)→JA 37DI`、`Tornado F.3→TORNADO F.3`）；**地面/工事保守不动**（那些名是「是什么东西」而不是出口型） |
| 文件大小 | 约 2.6 KB |

- 键是回放里的原始 `unit__className`（如 `tankModels/ussr_t_55_am`），值是显示名（如 `T-55AM`）。
- **唯一的保留键是 `__meta`**（来源、计数、未映射清单、内部名列表）。遍历时跳过以 `__` 开头的键。
- 文件里写明了：**显示名是本项目的显示约定，不是回放数据，也不是经考据的历史型号名**。
  拆不出来的名字**原样返回内部名**，不编造。
- 地面/工事的判据是块内形如 `tankModels/…` / `structures/…` 的长度前缀串；
  飞机的是 `aircraft` / `aircraft+player_unit` 模板块里出现在「结算玩家载具词表」中的串
  （外加观测到的 AI 机型 `mig-15bis_ish` / `a_4b` / `dummy_plane` 等）。

## 5.7 显示名：改由数据挖掘库生成（2026-10-19 重做）

**来源**：`gszabi99/War-Thunder-Datamine` 本地只读副本（外部参考仓库 `War-Thunder-Datamine`（本地只读副本），版本
**2.59.0.73**）的 `lang.vromfs.bin_u/lang/units.csv`。载具显示名的键是
**`<内部名>_shop`**（例：`f_15a_shop` = `F-15A`、`ussr_t_55_am_shop` = `T-55AM-1`）。
该副本**只读**，本线只读取、未修改，也未分发其任何文件。

**处理流水线**：官方英文名 → 去 UI 标记（`◌` `▄` 等）→ 去括号备注 → 去批次/出口型后缀
（`Block 10` `Early` `Netz` `(9-19)` `F21` `AMV` `ISh`）→ 全大写 → 去非 ASCII。

**目标与结果**：不出现中文、全大写、只留基础型号。

| 项 | 值 |
|---|---|
| 总计 | 37 条（地面/工事 12 + 飞机 25） |
| 来自 datamine | **22** |
| 覆盖表（override） | **13** |
| 机械兜底（fallback） | **0** |
| 隐藏（hidden） | **2** |
| 整个 JSON 的非 ASCII 字符 | **无（纯 ASCII）** |
| 文件名 | `build/wrpl/vehicle_display_names.json`（5.6 KB） |

**为什么有 13 条 override**：datamine 在本局这几条上给的官方名与实际机型不符或不合适，
按明确给出的目标名固定（这是**显示约定**，`DISPLAY_OVERRIDE` 里逐条写了理由）：

| 内部名 | datamine 官方名 | 采用 | 理由 |
|---|---|---|---|
| `fr_crotale_ng` | `ItO 90M` | **CROTALE NG** | datamine 指向另一国型号 |
| `us_truck_us6_studebaker_tent` | （无此键） | **US6** | 内部名只取底盘代号 |
| `uk_rooikat_za_35` | `ZA-35` | **ROOIKAT ZA-35** | datamine 缺底盘名 |
| `f_16a_block_10_netz_mod` | `Netz (Mod)` | **F-16A** | 那条是改装项不是机型 |
| `su_30mk2v_venezuela` | `Su-30MK2 AMV` | **SU-30MK2V** | 出口型后缀 |
| `fa_18c_switzerland` | `◌F/A-18C` | **F/A-18C** | 带去 UI 标记前缀 |
| `mig_29_9_12_germany` | `MiG-29 (9-12A)` | **MIG-29** | 批次号 |
| `saab_ja37di_f21` | `JA37DI F21` | **JA 37DI** | datamine 无空格且带 F21 |
| `mig_15bis_ish_250_bombs` | `MiG-15bis ISh` | **MIG-15BIS** | 挂弹变体继承基型 |
| `uk_bunker_light_fat` | `Light Pillbox` | **LIGHT BUNKER** | 指定 |
| `ussr_quest_large_art_noground` | `Howitzer` | **ARTILLERY** | 要求：名字要短 |
| `unit_airfield_flight_radar_char` | （无本地化名） | **AIRFIELD FLIGHT RADAR** | 由内部名生成 |
| `unit_airfield_main_radar_char` | （无本地化名） | **AIRFIELD MAIN RADAR** | 由内部名生成 |

**隐藏项**（`hidden: true` 语义，值为空串；`__meta.hiddenTemplates` 里写明"占位物，界面不显示"）：

- `structures/dummy_immortal`
- `dummy_plane`

**`__meta`** 里注明了：数据来源（Datamine + 版本 + 署名/许可说明）、
「显示名是显示约定、不是回放数据」、原始内部名始终在 `vehicle` 字段、
以及 `usage`（`displayName = map[vehicle] ?? vehicle`；空串则跳过；`__` 开头的键是元数据）。

> `__meta` 的说明文字**全部用英文**，为的是让整个 JSON **不含任何非 ASCII 字符** ——
> 这样"不出现中文"这条可以用 `grep` 直接验证，不用靠人读。
> 中文解释放在本文档里（就是本节）。

## 5.8 真实小队 `squad.json` 与「玩家本人识别」三步法（2026-10-19）

### 5.8.1 小队来自结算表，不是「航迹长度前 N」

`header.json` 的结算表里每个玩家都有 **`squadId`**。本局：

| squadId | 成员 | clanTag | team | slot | vehicle | uid |
|---|---|---|---|---|---|---|
| **2002** | `八幡 海鈴`（作者） | `=LyRe=` | 2 | 6 | `f_15a` | **368** |
| **2002** | `Mo Amaris` | `┾PL15E┿` | 2 | 6 | `f_15a` | **null** |

→ 本局是**双人小队，只有 1 名队友**。产物 `build/wrpl/squad.json`。
**这是回放内的权威信息**，请用它决定"谁是队友"，不要再用航迹长度之类的外部启发式。

### 5.8.2 `Mo Amaris` 的 uid：**解出 = 357**（纯回放判据，不是启发式）

**判据是「击杀/阵亡指纹」的唯一性** —— 全部来自回放，没有用任何启发式：

| 步 | 规则 | 本局结果 |
|---|---|---|
| ① | 机型候选集（排除作者 uid） | `f_15a` → {351, 357, 359, 370} |
| ② | `kills` == 结算 `kills`(2) | → {357, 359, 370} |
| ③ | **`deaths` == 结算 `deaths`(0)** | → **{357} 唯一** |

候选的 (kills, deaths) 实测：

| uid | kills | deaths | |
|---|---|---|---|
| 351 | 0 | 1 | kills 不符 |
| **357** | **2** | **0** | **✓ 唯一命中** |
| 359 | 2 | 1 | deaths 不符 |
| 370 | 2 | 1 | deaths 不符 |

`kills`/`deaths` 都是**用回放自己的击杀包**统计的（`tracks.json.killPositions`，33 个击杀包），
与结算表的 `kills`/`deaths` 字段比对。放之任何回放都成立：
**换别的回放时按 ①机型 ②kills ③deaths 三步；不能收敛到唯一候选就退回 `null`。**

**判据里明确排除的两样东西**：

1. **编队距离**（最初用的那个）—— 属**启发式，已弃用**。本局它恰好也指向 357，但
   纯属巧合吻合，不作为判据。`uidHypothesis` / `uidHypothesisEvidence` / `formationDistM`
   在 `squad.json` 里**保留作留痕**，并标注 "NOT used as a criterion"。
2. **`humanKills` / `aiKills` / `groundKills` / `navalKills`** —— 在本局结算表里**全场为 0**
   （`humanKills` 合计 0、`aiKills` 合计 1），**不可用作判据**。本局只有 `kills`/`deaths` 带信息。

**开发期旁证（不参与结论）**：uid 357 的两次击杀，受击者是 uid 370、359，**机型都是 `f_15a`**，
与击杀流水里 Mo Amaris 打掉的两架 F-15A 一致；击杀时刻 180.88 / 536.70 s ↔ 流水 gameT 181 / 537。
这些来自击杀流水（开发期数据），**未进入交付文件**。

### 5.8.3 通用「玩家本人识别」三步法（对任何回放都成立）

| 步 | 做法 | 本局是否可用 | 复现 |
|---|---|---|---|
| ① | 结算表 `results.authorUserId` / `results.author` → 玩家身份（**权威、离线、必可用**） | ✅ | `header.json` 的 `results` 段 |
| ② | **`0003584a` ReplayCockpitParams（本机属性表）与各实体 FM 航迹匹配 → 认领 uid**。这是**换别人回放时的兜底**：本机属性表只描述"这一架"，把它与 44 条 FM 航迹逐条比残差，最小的那条就是本机 | ✅ | `python tools/wrpl_decode.py --own`（航迹解析的工具）→ `build/wrpl/own_props.json`；残差见 `tracks.json.devValidation.ownSelfAltitudeRmsM = 0.6435`、`ownRmsErrM`（平面） |
| ③ | ECS `spawnSlotName → 玩家槽位 → 结算表` | ❌ **本局不成立** | `spawnSlotName` 是**出生组**（§3.2），与 `playersInfo.slot`(0..6) 不是同一编号空间（`REPLAY-FORMAT.md` §5.2 已排除该 u32 与 slot 同源） |

**注意 ② 的归属**：`own_props.json` 与 `tracks.json` 的残差数字是**兄弟线航迹解析产出的**，
本线只是引用；本线独立做的是 ①（`uid_map.json` 里 `uid-368 → 八幡 海鈴`）与 ③ 的**证伪**。

**`squad.json` 的字节是纯 ASCII**：用 `ensure_ascii=True` 写出，非 ASCII 的回放值
（玩家名、战队标签）以 JSON unicode 转义（反斜杠-u 形式）存放 —— **解码后原样还原，没有做任何转写**。
这样"零非 ASCII"可以用 `grep` 直接验证，同时不篡改回放数据。

**换别人回放时的推荐顺序**：① 拿身份 → ② 认领 uid（唯一可靠兜底）→ ③ 若 ② 失败再考虑
`spawnSlotName`，但必须先用本局这类样本验证它到底是不是槽位（本局就不是）。

### 5.7.1 隐藏集合的职责划分（一次误报的更正）

曾报告「`AIRFIELD FLIGHT RADAR` / `AIRFIELD MAIN RADAR` 没有被隐藏、会带长名字上画面」——
**这个报警是错的**。只查了这一层的 `hiddenTemplates`（里面只有占位物），**没有去查交付物**。

实测冻结版 `mission-data.js`（`2149b0b8e3472e61`）：

| 项 | 值 |
|---|---|
| `hidden:true` 单位 | **21** |
| 内部名分布 | `dummy_immortal` 9 + `unit_airfield_flight_radar_char` 8 + `unit_airfield_main_radar_char` 4 |
| 这 21 个的 `vehicleName` | **全是 `""`** |
| 非 hidden 里缺名字的 | **0**（`unitsWithDisplayName 215 = 236 − 21`） |

原因在交付侧 `build_ace_mission.py`：隐藏集合是**三者并集**
（它的 `HIDDEN_TEMPLATES` ∪ 本文件的 `__meta.hiddenTemplates` 键 ∪ 显示名为 `""` 的键），
机场雷达由**交付侧**负责隐藏。

**所以「机场雷达不显示」这个需求当前版本已满足，不欠账**；本文件的 `hiddenTemplates`
**只登记占位物**，**不要**把雷达加进来（加了只会改本文件的哈希，交付物逐字节不变）。

**教训（与 §3.1.1 / §3.6 / §3.7 同源）**：判「需求是否满足」要**在交付物上验**，
不能只看自己那一层的中间件。当时正确的第一动作是读 `mission-data.js` 里 `hidden` 与 `vehicleName`，
一条命令就能自证并避免一次误报。

## 5.9 类别表 / 显示名表：**已扩到 datamine 全量**（2026-10-22）

换用新录制的 `air_falklands` 回放（`mig_23mld`、`su_22m4`、`f_4s`、`mirage_2000c_s5` 等）后，
暴露出「友机型号显示不对/不全」—— 因为此前的两张表只覆盖 skyscraper 那一局的 37 个内部名。
现已按 datamine 全量重建：

| 产物 | 键数 | 说明 |
|---|---|---|
| `build/wrpl/vehicle_display_names.json` | **6127** | 含 `__meta`；空串 = 不显示 |
| `build/wrpl/vehicle_categories.json` | **6110** | 含 `__meta` |
| `tables/vehicle_names.json` | **6127** | **纯 map、无 `__meta`**，页面 fetch |
| `tables/vehicle_categories.json` | **6110** | 同上 |

类别分布（6110 个键的源集合 3617 个内部名）：

| 类别 | 数 | | 类别 | 数 |
|---|---|---|---|---|
| FIGHTER | 1495 | | FORTIFICATION | 58 |
| TANK | 1090 | | UNKNOWN | **54** |
| SHIP | 604 | | ATTACKER | 36 |
| SPAA | 159 | | ARTILLERY | 20 |
| BOMBER | 75 | | BUNKER / RADAR | 13 / 10 |
| | | | HELI / TRANSPORT | 2 / 1 |

- **`SHIP` / `TRANSPORT` 是新增词条**（超出最初约定的词表）：全量 datamine 有 600+ 艘舰船，
  不映射会被误判成 UNKNOWN。
- **54 个 UNKNOWN 的成因**（`__meta.unresolvedByReason`）：47 个"units.csv 有显示名但 datamine
  没有模型文件"（未实装/参考条目，回放里不可能出现）、7 个"有 blkx 但没有 type 字段"。
- **键的形式**：同时给裸名与回放里的带前缀形式（地面 `tankModels/X`、工事 `structures/X`），
  并把 `-` 归一化成 `_` 的别名也补上 —— datamine 的键是 `f-4s_shop`，而回放可能给 `f_4s`。

### 5.9.1 falklands 清单的实测覆盖

> **`F-14A` 三合一（已定）**：`f_14a` / `f_14a_early` / `f_14a_iriaf` 统一显示成
> **`F-14A`** —— 与 `F-16A` / `F/A-18C` 同一个口径：**只保留基础型号，不猜变体**。
> 其中裸名 `f_14a` 在 datamine v2.59 里并不存在，但回放/上报可能直接给裸名，所以显式补一条。
> 这三条已折进生成器的 `DISPLAY_OVERRIDE`（override 合计 16 条），
> 避免下次重跑把手工改动覆盖回去。


18 个机型里 **17 个命中**；只差 `f_14a` —— datamine 里**没有** `f_14a` 这个键，
只有 `f_14a_early`（`F-14A EARLY`）与 `f_14a_iriaf`（`F-14A IRIAF`）两个变体，
**两者都可能**，所以不做猜测（留空 → 页面回退成原始内部名）。
若确认该局用的是哪一个，加一条 override 即可。

### 5.9.2 原 37 条版本的定位

此前那版（37 条）是**按单局回放裁剪**的，只够 skyscraper 一局；现在两张表都是全量，
旧版仅作为"页面 fetch 失败时的内嵌兜底"保留（由页面侧决定）。

## 5.10 类别表 `vehicle_categories.json`（型号 ⇄ 类别 开关）

供画面做「具体型号 ⇄ 类别」切换。扁平表 `内部名 -> 类别`，**全大写 ASCII、零中文**。

| 类别 | 条数 |
|---|---|
| FIGHTER | 22 |
| SPAA | 4 |
| ATTACKER | 3 |
| RADAR | 2 |
| TANK | 2 |
| FORTIFICATION / BUNKER / ARTILLERY / TRUCK | 各 1 |
| **合计** | **37（未解析 0）** |

本局出现过的类别只有这些；`IFV` / `APC` / `BOMBER` / `HELI` 在词表里保留但**本局没有实例**。

### 来源与优先级（都来自本地只读 datamine）

1. **飞机**：结算 BLK 的 `primaryCraftType`（`fighter` → FIGHTER、`assault` → ATTACKER）
   → 21 条；AI 机型（`a_4b` / `mig-15bis_ish`）退回 `flightmodels/*.blkx` 的 `type`。
2. **地面载具**：`gamedata/units/**/*.blkx` 的 **`type`** 字段
   （`typeMediumTank`/`typeLightTank` → TANK、`typeSPAA` → SPAA …）。
   查不到时退回 `units.csv` 的通用类别词（如 `germ_flakpz_I_Gepard_2` = `SPAA`）。
3. **工事/结构**：名字含 `radar` → RADAR；否则 `blkx` 的 `onRadarAs`
   （`pillbox` → BUNKER、`artillery` → ARTILLERY）；再否则 `subclass=fortification` → FORTIFICATION。
4. **挂载/子型变体**（`a_4b_bomb_mk83`、`mig_15bis_ish_250_bombs`）继承基型；
   注意内部名里**连字符与下划线混用**（基型是 `mig-15bis_ish`），所以要用归一化索引找基型。

### 两处需要下游注意的判断

| 内部名 | datamine 给 | 本表取 | 说明 |
|---|---|---|---|
| `tankModels/uk_rooikat_za_35` | `type` = **`typeSPAA`** | **SPAA** | 示例写法是 `IFV`，但 datamine 明确是 SPAA（该车本就是自行高炮）。按「以 datamine 为准」取 SPAA。**已确认取 SPAA**（权威口径优先于其示例中的 IFV）。 |
| `tankModels/us_truck_us6_studebaker_tent` | `type` = `typeLightTank` | **TRUCK** | 那个 type 说的是**底盘类**；内部名 `us_truck_`/`us6` 明示是卡车，故覆盖，理由记在 `__meta.reasons`。 |

### `__meta`

注明：来源 = gszabi99/War-Thunder-Datamine（版本 **2.59.0.73**，本地只读副本，未分发）、
「类别是**显示约定**、不是回放数据」、`type2category` 完整映射表、词表、
逐条 `sources`（每条用的是哪一步推出来的）、`overrides` 与 `reasons`、`unresolved`。
用法：`category = CATS[vehicle] ?? 'UNKNOWN'`，遍历时跳过 `__` 开头的键。

### 5.10.1 本表是交付依赖，保持定版

`vehicle_categories.json` 已被并入交付链路（`mission-data.js` 的 `units[].vehicleCategory`，
供画面「型号 ⇄ 类别」开关），因此：

- **定版值**：sha256（磁盘字节口径）`a3ccafaef47f077366ef4cade52afd5b922b4fb66afeb75ad6727426fc0131ae`、
  **5326 B**、37 条 / 未解析 0 / 纯 ASCII；
- 生成是**确定性**的（同输入两次跑得到相同字节），所以即便 `--all` 重跑，哈希不变；
- `uk_rooikat_za_35` = **SPAA**（已确认，权威口径优先于示例）；`us_truck_us6_studebaker_tent` = **TRUCK**（已确认）；
- 取哈希一律用 `build/ecs/hash_report.py`（只走 `open(fp,"rb")`）。

## 6. 给下游怎么用

1. `tracks.json` 的每个 `units[].uid` → 查 `uid_map.json` 的 `"<uid>"`：
   - `vehicle` 直接当机型显示（`F-15A` 之类），`side` 当颜色；
   - `player` 有值就用真名，为 null 时用 `playerCandidates` 的**人数**决定是否显示
     （1 个候选可直接显示；2–3 个候选建议只显示机种）。
2. `tracks_ecs.json` 只含**出生点**（每实体 1 个采样），不要当航迹用；它适合做
   出生位置标记或与 fm 航迹的第一帧交叉校验。
3. **数据不含任何 8111 内容**：`tracks_ecs.json` 与 `uid_map.json` 里的每个数字都来自
   `.wrpl`。8111 只出现在本文档与 `--verify` 的输出里。

### 5.5.1 哈希口径：报哈希一律用**二进制（磁盘字节）**

本线的 JSON 产物是 **CRLF** 换行 —— 脚本用 Python 文本模式写文件，Windows 会把 `\n` 翻译成 `\r\n`。
后果：用 `open(fp, encoding="utf-8").read()`（文本模式）取哈希，会先做 universal-newlines 规范化，
**字节数也一起变**，于是同一个文件得到两个不同的 sha256。

实测（`vehicle_display_names.json`）：磁盘 **5825** 字节、`\r\n` **156** 处、裸 `\n` 0；
文本模式读出 **5669** 字符；`5825 − 156 = 5669`。

**约定**：本线对外一律报 `sha256(open(fp,"rb").read())`，并注明「磁盘字节口径」。

**这条约定只写进文档是不够的 —— 又踩了一次，所以把它做成了脚本**：

```bash
python build/ecs/hash_report.py      # 只走 open(fp,"rb")，打印 sha256 + bytes + CRLF/LF
```

脚本里写死「只允许二进制读」，并顺带打印「二进制 vs 文本模式」的差量与 CRLF 处数做自证，
这样**手滑也滑不回去**。第二次踩坑的留痕：把 `vehicle_categories.json` 报成
`4636cc92d182b1cf…` / 5185 B，磁盘实际是 `a3ccafaef47f0773…` / **5326 B**；
差 141 正是该文件 CRLF 处数。原因是临时的一行命令写成
`open(fp, encoding="utf-8").read()` 再 `.encode()` —— **标签写着"binary"、实际是文本模式**。
早期报过文本模式的值（`1b63d766…` vs 正确的 `e08faffe…`），已由导出脚本顶回并更正 —— 留痕。

**要不要改成 LF-only**（`newline=""`）？收益是跨平台字节稳定、文件小 156 字节；
代价是**全部产物哈希再变一次**、下游要重建一轮。本局决定**保持 CRLF**：JSON 读者对 CRLF 无感，
而此刻 `mission-data.js` 已进入最终验证。数据-eng 的 `mission-data.js` 是 LF-only，故没有这个问题。

### 5.6 时间戳口径（避免 256 倍误读）

本线所有产物**只给回放原始 tick**一种口径（`spawnTick` / `spawnTickRaw` / `samples[][0]`）：

```
秒 = tick / 256000        （等价：tick / 256 = 毫秒）
```

不另给「秒」字段，也不给「毫秒×256」这种中间口径 —— 兄弟线踩过一次（把 raw tick 当毫秒×256，
大了 256 倍）。`tracks_ecs.json` 的 `secPerTick = 3.90625e-06` 就是 1/256000。

## 7. 复现与自检

```bash
python tools/wrpl_decode_ecs.py --explore   # 控制码×长度分布
python tools/wrpl_decode_ecs.py --ecs       # 模板/组件/构造消息统计
python tools/wrpl_decode_ecs.py --spawn     # 位置偏移直方图 + 81 个出生点
python tools/wrpl_decode_ecs.py --verify    # 8111 距离指纹（开发期）
python tools/wrpl_decode_ecs.py --uidmap --own-uid 368   # uid_map.json（--own-uid 可选）
python tools/wrpl_decode_ecs.py --entity-list      # ecs_entities.json（全部 ECS 实体）
python tools/wrpl_decode_ecs.py --display-names # 显示名（读 .ref 的 datamine lang/units.csv）
python tools/wrpl_decode_ecs.py --launches      # projectile_launches.json（弹药创建点）
python tools/wrpl_decode_ecs.py --runways      # runways.json（机场/跑道判定）
python tools/wrpl_decode_ecs.py --squad        # squad.json（真实小队）
python tools/wrpl_decode_ecs.py --categories   # vehicle_categories.json（型号⇄类别）
python tools/wrpl_decode_ecs.py --build     # tracks_ecs.json
```

`--spawn` 打印的**相对偏移直方图**是本线最重要的自检：只有一个峰（+10，81 个块）才说明
位置字段定位不是碰巧。

## 8. 没解出来的（留给后来者）

| 项 | 状态 |
|---|---|
| `ptype=6` 控制码 0x20（7351 包）的组件值布局 | 未解（已证明它按 EID 寻址，110 个实体） |
| `ptype=6` 控制码 0x22（634 包）、0x26（1650 包） | 未解 |
| `compCount != 0` 的分支 | 本局没出现，未验证 |
| `unit__playerId` 字段位置 | 未解（相对偏移 0..63 上找不到「取值 ≤40 且 20+ 互异」的 u8） |
| 任务名 `tN_playerMM` 与结算 `playersInfo.slot` 的对应关系 | 未解（编号空间不同） |
| `00035843` 是谁的流 | 未解（结构已完全确定，身份未定） |
| 弹药类实体的创建位置 | **已解出**（见 §3.6，1734/1734；到发射机中位 15.1 m） |
| 弹药的逐帧航迹 | 仍未解且结构性不可得（弹药模板没有 `uid` 组件，关联不到 ptype=2） |
| `0002588e` 的实体 id | 未解（结构确定，无 id 字段） |
| ECS 坐标系与 8111 世界坐标的刚体变换 | 未闭合 |
