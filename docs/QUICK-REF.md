# `.wrpl` 解码速查（一页）

> 供最终报告直接引用。所有数字都是本局 `2026.10.18 05.45.06.wrpl` 实测，可复现。
> 详细推导、来源 URL、可信度分级见同目录 [`REPLAY-FORMAT.md`](./REPLAY-FORMAT.md)。
> 最后核对：2026-10-19。

## A. 时间口径（三边定案，2026-10-19）

**真实字段**：`tsMs` = 包体 `body[2:6]` 的 **u32 LE，单位毫秒**；`秒 = tsMs / 1000`。
**历史口径**：`raw = tsMs × 256`；`秒 = raw / 256000`。

> **约定：所有产物里 `tick` 一律 = `raw`（= `tsMs × 256`）；毫秒值一律叫 `tsMs`。**
> 三个产物（`header.json` / `entities.json` / `mission-data.js`）都遵守这条。
> 两者精确互换、整除无余数：`tick = tsMs × 256`，`tsMs = tick / 256`。

| 本局锚点 | tsMs | raw | 秒 |
|---|---|---|---|
| 包流开始 | 4 272 | 1 093 632 | 4.272 |
| StartMarker（正式开局） | 7 822 | 2 002 432 | 7.822 |
| EndMarker（= 结算 `timePlayed`） | **589 949** | **151 026 944** | **589.949** |

**复算判据（已逐条自证通过）**

```
header.clock.endMarkerRaw / 256 == 589949 == round(results.timePlayed * 1000)
header.clock.endMarkerRaw       == mission-data 的 meta.replayTicks.tickLast == 151026944
entities.killPackets[i].tick    == mission-data 的 events[i].replayTick            # 33/33
entities.<任意事件>.tick / 256  == entities.<同一事件>.tsMs                          # 整除
```

**旧键名不要当 null**：`clock` 里已移除 `ticksPerSecond` / `secondsPerTick` / `tickQuantum` /
`firstPacketTick` / `startMarkerTick` / `endMarkerTick`，换成上表的 `*Raw` / `*Ms`。
`clock.deprecatedKeyNames` 里显式写了旧名 → 新名的对应表，避免被误读成"值为 null"。
**判断产物新旧只看顶层 `generatedAtUtc`，不要看 `clock` 里某个键是否存在。**
（曾两次出现"读到旧版本 → 误认为字段缺失或自检为 false"的误判。）

**包帧**（`wrpl-inspector` 的 `packetReader.go`，本实现已按它改正）：

```
VLQ size = S
读 2 字节包头：Header[0]=类型，Header[1] 恒 0x00（本局 86773 个包全部成立）
  Header[0] & 0x10 置位 → 类型 = Header[0]^0x10；包头 2 字节；载荷 = body[2:]
  否则                  → 类型 = Header[0]；再读 u32 LE 毫秒；包头 6 字节；载荷 = body[6:]
```

**`raw` 的来历（不要当成文件字段）**：早期把包头误当 1 字节，读到的是
`body[1:5]` = `0x00` + 真实毫秒的低 3 字节 = `ms << 8`。
所以早期报告里「时间戳差值最大公约数 = 256」「EndMarker tick = 151026944」都是这个错位读法的产物。

## B. 击杀包 `TextKillReport`（msgId `0xF058`）

**匹配**：ptype=4，`PacketPayload[0:4] == 02 58 58 F0`。
**编码**：`IdFieldSerializer32` —— `u16 LE 尺寸表字节偏移` + `LEB128 位掩码` + 按位掩码顺序排列的字段，
每字段位宽取自尺寸表（3 位头：1/8/16/32/64/96/128 位，头 0 时再读一个 LEB128 当位宽）。
位序：**字节内 MSB 优先**；`binary.Read(LE, uN)` = 顺序取 N 字节再按小端解释。

| 字段号 | 类型 | 含义 |
|---|---|---|
| 1 | u32 LE | `KillerPid` 击杀者玩家编号 |
| 2 | 长度前缀字符串 | `PlayerVehicle` 击杀者载具 |
| 3 | u16 LE & `0x7FF` | **`VictimUid` 受击者单位 UID** |
| 4 | u16 LE & `0x7FF` | **`KillerUid` 击杀者单位 UID** |
| 0xA | 长度前缀字符串 | `PlayerWeapon` 武器 |
| 0xB | u32 LE | `VictimPid` 受击者玩家编号 |
| 0xC | 长度前缀字符串 | `DestroyedWeapon`（本局 33 条全为空串） |

**本局 33 条全部解出，0 失败**（`build/wrpl/kill_events.json`）。
- 30/33 条有武器名，且与载具族严格对应（`su_25`→`su_r_60`、`fa_18c_early`→`us_aim7p_sparrow_fa_18` …）。
- **2 条 `killerUid = 2047` 且载具/武器为空 = 坠毁或环境击杀，必须单列，不能冒充正常击杀。**
- **双重自检**：位流解出的 33 个载具名与「扫 1 字节长度前缀」的独立结果**逐元素完全一致**
  （`selfCheck.vehicleMatchesPrefixScan = true`、`comparedByTickAndOrder = true`、`itemsCompared = 33`，
  按 tick 与按出现顺序都比过）。**每个产物都带 `generatedAtUtc`**：
  若看到 `false` 之类的可疑值，先比时间戳，确认不是读到了修复前的旧版本。

**致命伤 `0xF056`（32 条）/ 重伤 `0xF157`（15 条）**：字段 1 `OffendedUID` u16、2 `PlayerPID` u32、
3 `Vehicle` 串、4 `PlayerUID` u16；致命伤多 5 `Fire` bool + 6 `Unk0`，重伤 5 是 `Unk0`。
62.191 s 的致命伤 `{OffendedUID:361, PlayerPID:28, Vehicle:"su_25", PlayerUID:372}`
与同 tick 击杀包 `victimUid=361 / killerUid=372` 完全对上 → 可串因果链。

**击杀点算法**：受击者 UID = `victimUid`，取该 uid 在 `tsMs` 时刻（或最近的 4 Hz 采样）的
FM 航迹位置即可。

## C. 实体编号

| 编号 | 本局取值 | 含义 |
|---|---|---|
| **UID** | **344 … 375 连续 32 个值** | **正好对应 32 名玩家**；航迹解析用回放高度认领的**本机 = 368**（rms 0.64 m） |
| UID | 215 / 216 / 299 / 414 | AI 或其它非玩家单位 |
| UID | **2047 = `0x7FF`** | **"无"**（击杀包里没有击杀者/受击者时用） |
| Pid | `0,1,2,3,4,5,6,9,10,11,13,14,15,19,21,22,23,24,25,26,28,31` | **另一个编号空间**；`0xFFFFFFFF` = 无 |

**三向归属吻合（`UID → 实体航迹` 这条链不需要玩家名、不依赖 8111）**

| 来源 | 本机击杀数 | 原始编号（可直接复核） |
|---|---|---|
| 回放击杀包 `KillerUid == 368` | **2** 条，均为 `f_15a` | `tick` = **51 533 056** / **144 747 520** → 201.301 s / 565.420 s |
| 回放内嵌结算 `players[]` 里本机 `kills` | **2** | — |
| 8111 `hudmsg` 窗口内原文 | **2** | — |

三条独立来源一致 → 契约里"回放侧无法归属"的说法可以被推翻；
`isPlayerActor` 因此能完全由回放自身算出来。
（独立复算给出的 tick 与上表逐位相同：`51533056/256 = 201301`、`144747520/256 = 565420`。）

- UID 取 u16 的**低 11 位**（`& 0x7FF`）；参考实现靠 ECS 的 `Uid_lookup`（UID → Entity）关联实体。
- **更正**：早期错记成 `unitId32` 的字段（旧偏移 `pl[10:14]` 的 u32）**其实是 `PlayerPID`**
  （例：62.191 s 那条 = 28）。所以它不是槽位；与 `playersInfo.slot`（本局只取 0..6）无关。
- **玩家名只出现在两个地方**：文件末尾内嵌结算 BLK、以及 ptype=3 聊天包的 `sender` 字段。
  86773 个包里其余位置搜 32 个玩家名，命中 0。
- **击杀事件没有本地化原文**：全流 9 种文案模式（`destroyed` / `shot down` / `has been` / `击落` …）命中均为 0。
  事件文本只能拼合，`composed=true`。

## D. 复现命令

```bash
python tools/wrpl_header.py        # → build/wrpl/{header,entities}.json + unit-names.txt
python tools/wrpl_mpi_names.py     # → build/wrpl/mpi_names.json
python tools/wrpl_kill_events.py   # → build/wrpl/kill_events.json
```

## E. 来源与许可

| 来源 | 许可 | 用途 |
|---|---|---|
| [maxsupermanhd/wrpl-inspector](https://github.com/maxsupermanhd/wrpl-inspector)（分支 `v3`） | **AGPL-3.0** | 包帧、MPI 消息号名字表、`IdFieldSerializer32`、击杀/伤害字段 |
| [Warthunder-Open-Source-Foundation/wt_blk](https://github.com/Warthunder-Open-Source-Foundation/wt_blk) | **AGPL-3.0** | 二进制 BLK（FAT）格式规格 |
| [wrpl crate 0.10.0](https://docs.rs/crate/wrpl/0.10.0/source/) | **AGPL-3.0** | 文件头字段宽度、包类型名、`rez_offset` 用途 |

**只移植知识，实现自写**：`tools/*.py` 全部是自写的 Python，
没有复制上述项目的代码；名字表属事实性数据（消息号 ↔ 名字），已在 `mpi_names.json` 的
`source` 字段里注明出处与许可。
