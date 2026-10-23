# `.wrpl` 回放格式情报（本局实测）

对象：`<game-dir>\Replays\2026.10.18 05.45.06.wrpl`
（5 133 517 字节，只读）。所有结论都在这一个文件上跑通；标「实测」的都可以用
`python tools/wrpl_header.py` 复现。

产物：
- `build/wrpl/header.json` —— 容器、文件头、时钟、内嵌结算 JSON、玩家表（32 人）
- `build/wrpl/entities.json` —— 单位名表、玩家↔载具、载具出生事件、击杀包、奖项、聊天
- `tools/wrpl_header.py` —— 生成上面两个文件的脚本

**可信度分级**（下文每张表都标）：

| 级别 | 含义 |
|---|---|
| **A** | 有一手源码/格式文档，且在本局文件上跑通，字段偏移可复现 |
| **B** | 本局实测标定（差值/吻合等硬证据），但无公开文档佐证 |
| **C** | 观察到的现象，含义未知或未证明 |
| **D** | 已排除（列了排除方法） |

---

## 1. 来源清单

| 来源 | 用途 | 许可 | 可信度 |
|---|---|---|---|
| [wrpl crate 0.10.0 源码](https://docs.rs/crate/wrpl/0.10.0/source/src/header.rs)（`header.rs` / `packet.rs` / `parser.rs` / `results.rs` / `README.md`） | 文件头字段宽度、包帧格式、MPI 签名、`rez_offset` 用途、结算 JSON 结构 | AGPL-3.0（README 明确要求先读再同意） | **A**：直接照抄其字段宽度即可解析本局头 |
| 同仓库 GitHub 镜像 [llama-for3ver/wt_replay_decoder](https://github.com/llama-for3ver/wt_replay_decoder) 的 `raw.githubusercontent.com/.../main/src/*.rs` | 拿完整未截断的源码（docs.rs 网页会截断） | 同上 | **A** |
| [wt_blk 0.3.1 `src/blk/mod.rs`](https://github.com/Warthunder-Open-Source-Foundation/wt_blk) | 二进制 BLK 格式的完整文字规格（名字表 / 字段表 / 嵌套表 / 12 种类型） | AGPL-3.0 | **A**：按文档实现的解包器在本局结算区一次跑通，且各段长度恰好用尽 |
| 同仓库 [`src/blk/binary_deserialize/parser.rs`](https://github.com/Warthunder-Open-Source-Foundation/wt_blk) | 解析顺序的权威实现（参数表先于字段定义；块名 1 基、字段名 0 基） | 同上 | **A** |
| [Sgambe33/WT-Plotter README](https://github.com/Sgambe33/WT-Plotter#readme) | 想找「回放里解出单位位置」的先例 | 未标注许可（只读其 README 做结论） | **D（否定）**：见 §8 |
| [nilshellerhoff/warthunder-replay-parser README](https://github.com/nilshellerhoff/warthunder-replay-parser#readme) | 另一份 .wrpl 解析尝试 | 未标注许可 | **D（部分）**：只解文件头 + 玩家载具列表，公开位置字段未解 |
| [PowerBroker2/Thunder_Viewer](https://github.com/PowerBroker2/Thunder_Viewer)、[PowerBroker2/WarThunder](https://github.com/PowerBroker2/WarThunder) | 想找先例 | 未标注许可 | **D（否定）**：这两个都是「stream and log War Thunder match data」/「access vehicle telemetry data while in War Thunder battles」——**读的是 8111 遥测，不解析 .wrpl** |
| WT 官方论坛「Replay parser for Warthunder replay files」帖 | 找格式讨论 | — | **D**：只找到讨论与工具推荐，没有包流字段布局 |
| [FlareFlo/wt_ext_cli](https://github.com/Warthunder-Open-Source-Foundation/wt_ext_cli)（WT-Plotter 自述的依赖） | 被 WT-Plotter 点名为其解析基础 | 未查（时间不够） | 未验证 —— 见 §9 |

**没有找到的东西（重要）**：社区**没有**公开 `ptype=2 AircraftSmall` 的字段布局，
也没有 `.wrpl` 实体位置解码的公开实现。上面三个「解析器」里，能解位置的都没有，
声称能画位置的那个（WT-Plotter）自己在 README 里写的是走 8111。
所以 §7 的实体位置布局必须自己反推。

---

## 2. 容器布局（可信度 A，本局实测数字在括注里）

```
偏移 0      magic u32 LE            = 0x1000ACE5
偏移 4      version u32 LE          = 101404
偏移 8      level        char[128]  "levels/air_skyscraper_city.bin"
偏移 136    levelSettings char[260] "gamedata/missions/cta/planes/historical/bfd/air_skyscraper_city_bfd_norespawn.blk"
偏移 396    battleType   char[128]  "air_skyscraper_city_BfD_norespawn"
偏移 524    environment  char[128]  "day"
偏移 652    visibility   char[32]   "cloudy"
偏移 684    rezOffset    u32 LE     = 5118758   ← 内嵌结算 BLK 的文件偏移
偏移 688    difficulty   u8         = 0x75（高 4 位 7，低 4 位 5）
偏移 689    padding      35 字节
偏移 724    sessionType  u32 LE     = 0
偏移 728    padding      4 字节
偏移 732    sessionId    u64 LE     = 0x4AD212E80006E54E
偏移 740    padding      4 字节
偏移 744    mSetSize     u32 LE     = 62323213
偏移 748    settingsSize u32 LE     = 632        ← settings BLK 的字节数
偏移 752    padding      28 字节
偏移 780    locName      char[128]  "missions/air_skyscraper_city_BfD_norespawn"
偏移 908    startTime    u32 LE     = 1792272908（2026-10-17 21:35:08 UTC）
偏移 912    timeLimit    u32 LE     = 25（分钟）
偏移 916    scoreLimit   u32 LE     = 5300
偏移 920    padding      48 字节
偏移 968    battleClass  char[128]  "base_dom"
偏移 1096   battleKillStreak char[128] ""（本局为空）
偏移 1224   —— 头部结束（定长 1224 字节）
偏移 1226   settings BLK 起，632 字节（= 1224 + 2 字节未知填充）
偏移 1858   包流区开始
偏移 1866   zstd 帧魔数 28 B5 2F FD  ← 真正的包流从这里开始
偏移 5118758 结算 BLK 起，14759 字节（到文件末尾）
```

字符串字段一律是定长 NUL 结尾（UTF-8，读到第一个 `\x00` 为止），这是
wrpl crate `parse_header` 的做法，本局逐字段核对通过。

**两个容易踩的坑（B）**：

1. `settingsSize` 之后还有 **8 个字节**（`02 01 0f 04 00 14 06 00`）才到 zstd 魔数。
   包流区起点 1858 与 zstd 帧起点 1866 差 8，别把 1858 当 zstd 起点。
2. 包流是 **zstd 单帧**，不是 zlib。wrpl crate 的 `parser.rs` 用的是
   `flate2::read::ZlibDecoder`，对现代回放会直接失败——它的注释里那段
   「Decompressed replay stream does not start with expected bytes」就是这件事。
   本局 `wrpl[1866:5118758]`（5 116 892 字节）解压出 14 180 794 字节。

**settings BLK 没解开（C）**：首字节 `0x49` 不在 wt_blk 的 `FileType` 枚举
（`0x00..0x05`）里，所以既不是 FAT 也不是 SLIM。键名以明文可见：
`mission / level / type / environment / weather / locName / locDesc / scoreLimit /
timeLimit / deathPenaltyMul / country_axis / country_allies / restoreType /
optionalTakeOff / allowedUnitTypes / isAirplanesAllowed / isTanksAllowed /
isShipsAllowed / isHelicoptersAllowed / stars / latitude / longitude / year / month …`
排除方法：对 `wrpl[1226:1858]` 依次试 FAT（`parse_fat_blk`）与 SLIM 会抛
`BLK 首字节 …，本函数只处理 FAT`，试 zstd/zlib 解压也无魔数。

---

## 3. 回放时钟（可信度 A）

**真实字段（推荐口径）**：

```
tsMs = 包体 body[2:6] 的 u32 LE，单位毫秒
秒   = tsMs / 1000
```

**历史口径（兼容用，不是文件里的字段）**：

```
raw  = tsMs * 256          # 等价 tsMs << 8
秒   = raw / 256000
```

**证据链**：

1. EndMarker 包 `body[2:6]` = **589 949 ms** = **589.949 秒**；
   内嵌结算 JSON 的 `timePlayed = 589.94928` 秒。**逐位吻合**。
2. StartMarker 包 `body[2:6]` = **7 822 ms** = 7.822 s。
3. 首个包（ptype=8）`body[2:6]` = **4 272 ms** = 4.272 s。
4. `raw` 与 `tsMs` 精确互换：`raw = tsMs * 256`，`tsMs = raw / 256`（整除，33 个击杀包逐个验证无余数）。

本局的回放自身时间轴：

| 事件 | tsMs | raw（= tsMs×256） | 秒 |
|---|---|---|---|
| 包流开始 | 4 272 | 1 093 632 | 4.272 |
| StartMarker（正式开局） | 7 822 | 2 002 432 | 7.822 |
| EndMarker（= 结算 timePlayed） | 589 949 | 151 026 944 | 589.949 |

> **为什么会有两个口径**：早期版本的包帧解析把包头当成 1 字节，
> 于是时间戳读的是 `body[1:5]` = `0x00` 加上真实毫秒的低 3 字节，正好等于 `ms << 8`。
> 早期报告里「时间戳差值最大公约数 = 256」「EndMarker tick 151 026 944」「tick / 256 = 毫秒」
> 全是这个错位读法的产物，**不是文件字段的性质**。真实字段就是毫秒（见 §13.2）。
> `raw` 之所以还在用：`mission-data.js` 的 `replayTick` 与 `tracks.json` 的 `tick` 都是它。

> 注意：这是**回放自身的时钟**。8111 抓取的时间轴零点不同，不要把它直接当 8111 的秒用。

### 3.1 时间单位口径（三者约定，2026-10-19 定）

**`tick` = `raw` = `tsMs × 256`**，与 `mission-data.js` 的 `replayTick`、`tracks.json` 的 `tick`
同一个编号空间；毫秒值一律叫 **`tsMs`**。三个产物都遵守这条，升级到秒一律 `raw / 256000` 或 `tsMs / 1000`。

| 产物 | 字段 | 口径 |
|---|---|---|
| `header.json` | `clock.startMarkerRaw` / `endMarkerRaw` / `firstPacketRaw` | raw |
| `header.json` | `clock.startMarkerMs` / `endMarkerMs` / `firstPacketMs` | 毫秒（真实字段） |
| `header.json` | `clock.unitConvention` | 这条约定的原文 |
| `entities.json` | 事件里的 `tick` | raw（与 mission-data 一致） |
| `entities.json` | 事件里的 `tsMs` / `sec` | 毫秒 / 秒 |
| `mission-data.js` | `events[].replayTick`、`meta.replayTicks.tickLast` | raw |
| `tracks.json`（契约 §1.0） | `tick0` / `samples[][0]` | raw（沿用同一口径） |

复算判据：`endMarkerRaw / 256 = 589949 = round(timePlayed × 1000)`。

---

## 4. 包帧格式（可信度 A）

包流是一串「长度前缀 + 包体」，无对齐、无分隔符：

**长度前缀（VLQ）**，按首字节的位模式：

| 首字节位模式 | 前缀长度 | 长度值 |
|---|---|---|
| `10xxxxxx` | 1 | `b0 & 0x7F` |
| `01xxxxxx` | 2 | `((b0<<8)\|b1) ^ 0x4000` |
| `001xxxxx` | 3 | `((b0<<16)\|(b1<<8)\|b2) ^ 0x200000` |
| `0001xxxx` | 4 | `((b0<<24)\|…\|b3) ^ 0x10000000` |
| `0000xxxx` | 5 | 后 4 字节按 **LE u32** 读，**不 XOR** |
| `11xxxxxx` | — | 非法，解析到此为止 |

**包体头部**：

```
读 1 字节 t
if (t & 0x10) != 0:            # 时间戳与上一包相同
    ptype = t ^ 0x10; tick = 上一包的 tick; 头长 1
else:
    ptype = t; tick = 后 4 字节 LE u32（毫秒刻度）; 头长 5
```

包体剩余部分就是该类型的载荷。

**包类型**：`0 EndMarker / 1 StartMarker / 2 AircraftSmall / 3 Chat / 4 MPI /
5 NextSegment / 6 ECS / 7 Snapshot / 8 ReplayHeaderInfo`。

本局 86 773 个包**全部**按上述规则解出，且恰好用尽 14 180 794 字节（无剩余、无截断），
这是帧格式正确的强证据。

### 4.1 本局包类型分布（实测）

| ptype | 名称 | 个数 |
|---|---|---|
| 0 | EndMarker | 1 |
| 1 | StartMarker | 1 |
| 2 | AircraftSmall | 2 330 |
| 3 | Chat | 5 |
| 4 | MPI | 73 189 |
| 6 | ECS | 11 246 |
| 8 | ReplayHeaderInfo | 1 |
| 5 / 7 | NextSegment / Snapshot | 0 |

`ptype=2` 的时间戳间隔众数 ~64 000 tick ≈ **250 ms**（4 Hz），覆盖 7.822 s 到 589.842 s，
即整场。

---

## 5. MPI 包（ptype=4）签名表

载荷前 4 字节是「签名」（消息 id）。本局全部计数（实测）：

| 签名 | 个数 | 含义 | 可信度 |
|---|---|---|---|
| `00FF0781` | 32 716 | 未知（最高频，看着像位置/状态增量） | C |
| `00035843` | 13 412 | turret angles（wrpl crate 注释） | A（注释）／C（含义） |
| `00FF0F81` | 9 448 | 未知 | C |
| `000258DB` | 3 783 | 未知 | C |
| `0002582D` | 3 627 | 未知 | C |
| `000258AA` | 3 253 | 未知 | C |
| `0003584A` | 2 236 | 未知 | C |
| `00025839` | **1 111** | **unit/template 定义**，载荷内含 1 字节长度前缀的单位名 | **B** |
| `00025828` | 1 088 | 未知 | C |
| `00025873` | 788 | **地面单位批量同步 GroundModelPositions**（msg id `0xF073`，服务器变体 `0xF074`），增量 + RLE 0ki | **A** |
| `0002588E` | 783 | 未知 | C |
| `00FF0785` | 442 | 未知 | C |
| `00FF8781` | 203 | 未知 | C |
| `00025878` | **103** | **award 奖项**，载荷内含 1 字节长度前缀的奖项内部名 | A（crate）／B |
| `00FF8F81` | 60 | 未知 | C |
| `00025858` | **33** | **kill 击杀**，载荷内含击杀者载具名 | A（crate） |
| `00025856` | **32** | **单位载具装载/出生**，载荷内含载具名 | **B** |
| `00025893` | 20 | 未知 | C |
| `00025857` | **15** | 同 `00025856` 的另一变体 | **B** |
| `00035844` | 11 | 未知 | C |
| `000058DF` | 8 | 任务/环境变量：明文键 `serverTime / cloudsOffsetX / cloudsOffsetZ / strataCloudsOffsetX / sunDir / moonDir / wind / sunColor / skyColor / isMoon` | B |
| `00025837` / `000258C3` / `0002587A` / `00025818` / `0002587C` / `0003581B` / `00035845` / `0002585D` | 7 / 2 / 2 / 2 / 1 / 1 / 1 / 1 | 未知；载荷 5~83 字节，看图像位打包的状态或触发器 | C |

### 5.1 `00025858` 击杀包（33 个，可信度 A/B 混合）

wrpl crate 的 `parse_kill_packet` 给出的偏移在本局**对不上**（它期望 `+6 = 00 FE 3F`，
本局是 `00 FE FF`，且载具名字符串不在它算的 `+13`）。本局实测的布局是：

```
+0   sig 4 字节          00 02 58 58
+4   1 字节              恒 0xF0
+5   1 字节              control（crate 记 damageType = control & 0xF0）
+6   3 字节              恒 00 FE FF
+9   1 字节              恒 07（本局 33 个包全部为 0x07）
+10  u32 LE              control / 单位 id 候选；本局取值 3..31
+14  1 字节              载具名长度 L
+15  L 字节 ASCII 载具名   例：su_25、f_15a、fa_18c_early、mig_29smt_9_19
     …（其后还有字节串，疑似坐标/受击者，语义未标定）
```

`+14` 与载具名起点在 33 个包里 **32 个一致**；剩下 1 个（tick 37 565 184，载荷 46 字节）的
`+10` 是 `0xFFFFFFFF`，整包为
`00025858 f0 23 00 fe ff 07 ffffffff 00 71 01 ffffffffffffff 00 00 07 80 05 …`，
**没有载具名串**——看图像非载具击杀（坠毁/环境）。脚本把它的 `vehicle` 记为 `null`，
下游统计时不要当成解析失败。

本局 33 个击杀包的载具分布：`f_15a`×8、`mig_29smt_9_19`×5、`su_25`×3、
`fa_18c_early`×3、`su_30mk2v_venezuela`×3、`f_16a_block_10`×2、`fa_18a`×2、`jf_17`×2、
`tornado_f3`、`f_20a`、`fa_18c_switzerland`、`saab_ja37di_f21` 各 1，另有 1 个未取到名字。

**注意：这个包里没有坐标字段被标定。** 契约 §2.4 说「用当事实体航迹在 tick 时刻的位置」
取击杀点，是稳妥做法。

### 5.2 `00025856` / `00025857` 载具装载事件（47 条，可信度 B）

```
+0   sig 4 字节     00 02 58 56（或 58 57）
+4   1 字节         恒 0xF0（5856）/ 0xF1（5857）
+5   2 字节         长度类字段（本局 0x13 00 / 0x12 00）
+7   1 字节         0x7E（5856）/ 0x3E（5857）
+8   1 字节         随事件变化
+9   1 字节         0x01
+10  u32 LE         本局取值 3..31，共 16 个互不相同的值
+14  1 字节         载具名长度 L
+15  L 字节 ASCII   载具名
```

本局这两类共 47 条（32 + 15），出现 11 种载具：`su_25 / f_15a / fa_18c_early /
tornado_f3 / su_30mk2v_venezuela / f_16a_block_10 / f_20a / fa_18a / jf_17 /
mig_29smt_9_19 / saab_ja37di_f21`。

**`+10` 的 u32 不是玩家槽位**（可信度 B，否定结论）：结算 JSON 的
`playersInfo.slot` 本局只取 `0..6`，而该 u32 取 `3..31`，两个区间只在 3..6 上偶然重合。
把两者当成同一编号空间会导致错误关联。该 u32 的真实含义仍未证明。

### 5.3 `00025839` 单位/模板定义（1 111 个，可信度 B）

载荷里是一串「1 字节长度前缀 + ASCII 名」。（脚本按「长度≥4、形如标识符、且含 `_`
或 `/`」过滤后剩下 403 个不同的名字。）

跑出来的名字可以清楚看出这是**任务模板里的单位与假人位**，不是玩家：

- `t1_player01` … `t1_player16`、`t2_player01` … `t2_player16` —— 每边 16 个玩家位
- `t1_aaa_squad` / `t1_trucks_squad` / `t2_acs_squad` / `bdt_t1_aaa_squad` —— 地面/AI 小队
- `at_t1_assault` / `at_t1_fighter` / `at_t2_gs_objective` —— 目标与任务单位
- `bdt_t1_bomb_zone_01_bomb_target` … `_10_bomb_target`、`bdt_t1_bomb_object_01`… —— 轰炸目标
- `dynTrench_small_bunker_axis`、`dynBase_oil_storage_small_square`、`dynBase_field_camp_square` … —— 动态地面建筑
- `missions/air_spawn`、`missions/airfield_spawn`、`type_bomber`

**这个包里没有玩家名，也没有数值化的 entityId。**

### 5.4 Chat 包（ptype=3，5 个，可信度 A）

```
+0  1 字节          flag/subtype
+1  1 字节          sender 长度
+2  N 字节 UTF-8    sender
    1 字节          message 长度
    M 字节 UTF-8    message
    其后若干字节     频道/阵营等（未标定）
```

本局原文（含玩家自己输入的文字，以及 `\u200b` 零宽字符与 `<color=#...>` 标签）：

| 发送者 | 秒（回放时钟） | 内容 |
|---|---|---|
| `RX_7_f15mtd@psn` | 118.209 | `注\u200b意\u200b战\u200b术\u200b地\u200b图！<color=#FF96966E> [E4]</color>` |
| `MAVERICK_HAWK` | 360.273 | `正\u200b在\u200b返\u200b回\u200b基\u200b地。` |
| `Mo Amaris` | 383.466 | `1` |
| `MAVERICK_HAWK` | 454.362 | `its f-18` |
| `Mo Amaris` | 587.738 | `不赖` |

**这是整条包流里唯一的人类可读文本**（见 §8）。

---

### 5.2 `00025873` 地面同步包（788 个，可信度 A）

载荷前 4 字节是 `02 58 73 F0`，其中 `73 F0` 按 u16 LE 读就是消息 id `0xF073`
（服务器回放用 `0xF074`）。这是上游 wrpl-acmi **dev 分支**新解的批量地面位置通道
（`wrpl/packet/parser/movement/ground.go`），本局实测格式如下：

```
+0   4 字节签名        02 58 73 F0
+4   ULEB 位长         包体按位计的长度；随后字节对齐
     u16 LE            uid 下界
     u16 LE            uid 上界（不含）
     u32 LE            同步时刻
     u16 LE            可靠性序号
     每 uid：
       1 bit            该 uid 是否出现在本包
       1 bit            未解标志
       1 bit            hasExtra；为 1 再跳 4 bit
       1 bit / 1 bit    两个跳过标志；同时为 1 时跳过该 uid
       1 bit            未解标志
       1 bit            hasDirectionFloats（决定位置字段是否带 4 个 float + 21×float 历史）
       8 bit            turret 数
       1 bit            compressed（本局恒 1；为 0 的上游直接报「不支持」）
       1 bit            fullDiff（1 = 完整体，0 = 增量）
       5 bit            增量基线距离（低位在前）
      13 bit            本次包序号（分两段读：8 位 + 5 位，段内 MSB 在前）
       ULEB            增量/完整体字节数；字节对齐
       n 字节          体数据：完整体；或 RLE delta（与历史体逐字节 XOR）
       ……              可选 point4（4×32 bit）、turret、sensor 分支，均按上面的标志位跳
```

体数据里位置字段（`parseVehiclePosition`）：

```
1 字节 记录头；1 bit hasPosition
hasDirectionFloats ? (4×float32 [+ 21×float32 历史]) : (3×u16)
1 bit 未解；1 bit full
full=1: 3×float32（x=世界 X，y=高度，z=世界 Z）
full=0: 2×u32 量化 —— x = clamp((v1>>10)/2^21-1)×36000
                          y = ((v1&0x3FF)<<10 | v2>>22)×0.007629402 - 100
                          z = clamp((v2&0x3FFFFF)/2^21-1)×36000
```

增量历史：32 槽，槽位 = 包序号 % 32，标签 = 包序号 & 0x1FFF；RLE 末字节必须是
`0x6F`(111) 或 `0xDE`(222) 表示最后一段是 1 个还是 2 个 2-bit 码；解出的 delta
与基线逐字节 XOR，长度不同时按长的补齐。

本局实测（`web/wrpl-parse.js` 与独立 Python 实现逐采样对比，最大差 0.0013 m）：

- 788 个包，`uid` 0–343，**118 个 uid 有 ≥3 个采样，共 7275 个位置**，0 解析失败；
- 采样间隔中位 5.5 s（p10 0.98 s）；高度 13–246 m，是真实地形高度，不是常量；
- 水平速度 p90 4.5 m/s、max 9.4 m/s（符合地面载具）；
- 118 个 uid 里有 **38 个**与 `ptype=6 ECS` 构造点的首采样水平距离 ≤0.3 m，
  型号/阵营可以直接沿用 ECS；其余 80 个移动过但绑不上构造点，输出为无身份
  「GROUND UNIT」（不按距离猜型号）。

### 5.3 `0002582D` 玩家槽位包（可信度 A）

载荷 `02 58 2D F0`（msg id `0xF02D`）。本局 3627 个包，是一个压缩/重复发送的玩家表：

```
+0   4 字节签名        02 58 2D F0
+4   1 字节            是否压缩（>0）
      ULEB             压缩后长度
      ULEB             解压后长度
      zstd 帧          解压后为消息流
     u16 LE            消息数
     每条消息：
       u16 LE          消息长度（含 oid）
       u16 LE          oid（低 11 位为槽位号；低 11 位 = 0x7FF 时再读 ULEB ext uid）
       n 字节          消息体；oid>>11 == 0xE 时是玩家槽位
玩家槽位消息体用 IdFieldSerializer255（u16 offset、u16 count=字段数|每字段位宽<<12、
字段尺寸表、字段索引表、字段数据）。本实现只取：
  字段 2 = 玩家 uid（u64 LE + 65 字节名字）、5 = 氏族、6 = 称号、9 = team、41 = 真实昵称
```

本局解出 **32 名玩家，0 解析失败**；配合 ECS 构造成员里的 `unit__playerId`（组件值起点 +119 的
int32，实测 32/32 与槽位表对上）可以给每架玩家飞机直接定位名字与队伍。

### 5.4 `000258DB` / `02581AF1` 弹药同步（可信度 A）

载荷 `02 58` + msgId u16：`0xF0DB`（shell/rocket 同步）或 `0xF11A`（rocket 变体）。
本局只有 `0xF0DB`，3783 个包：

```
+0   4 字节          02 58 DB F0
+4   ULEB            位长；随后字节对齐
     位流            3 组 class：0=火箭/导弹、1=炸弹、2=鱼雷
       每组：1 字节计数；每条记录：
         hasGuidance(1) noExtraTail(1) entity(u32 LE) isRaw(1)
         isRaw ? 3×float32 : 2×u32 量化（x/y/z ∈ ±15000 m）
         !noExtraTail 时还有尾部：ULEB、3×u32、6×u16、flags、seeker…
```

本局 class 0 解出 **112 个实体 / 5589 个位置样本**，0 解析失败；class 1 只有 2 条
（上游的 95 个炸弹大多是“只有构造点”的释放标记，本来就没有航迹）。
播放器只画白色虚线航迹（dash 180 m / gap 120 m，按出现时刻逐段放出），弹体不画，
设置面板“导弹弹道”可关。

## 6. 内嵌结算 JSON（可信度 A）

`rezOffset` 指向的不是压缩 JSON，而是一个**标准二进制 FAT BLK**（首字节 `0x01`）。
本局各段长度（实测，且各段恰好用尽 14 759 字节）：

| 段 | 本局值 |
|---|---|
| 文件类型字节 | `0x01` = FAT |
| 名字表条数（ULEB） | 88 |
| 名字表字节数（ULEB） | 955 |
| 名字表 | 88 个 NUL 分隔字符串 |
| 块数（ULEB） | 164 |
| 字段数（ULEB） | 1 476 |
| 字段数据区字节数（ULEB） | 1 393 |
| 字段数据区 | 1 393 字节 |
| 字段定义表 | 1 476 × 8 字节 |
| 嵌套表 | 593 字节 |

**字段定义** 8 字节：`name_id u24 LE | type_id u8 | data u32 LE`。
`name_id` 是**0 基**索引名字表。

**类型表**：`0x01 String`（NUL 结尾）／`0x02 Int(i32)`／`0x03 Float(f32)`／
`0x04 Float2`／`0x05 Float3`／`0x06 Float4`／`0x07 Int2`／`0x08 Int3`／
`0x09 Bool`／`0x0A Color(u8;4)`／`0x0B Float12`／`0x0C Long(i64)`。
其中 Int/Float/Bool/Color 是 **inline**：`data` 字段本身就是值；
其余类型的 `data` 是字段数据区内的偏移。

**嵌套表** 每条：`ULEB name_id | ULEB 字段数 | ULEB 子块数 | (子块数>0 时) ULEB 首个子块下标`。
块名是**1 基**索引名字表，`name_id == 0` 表示根。子块 = 扁平块
`[首个子块下标, 首个子块下标 + 子块数)`。字段按块顺序从字段定义表里连续取。

按这个规格解出来，本局结算 JSON 的字段全在：

```
status = "success"
timePlayed = 589.94928                     // 秒，对局时长
authorUserId = "31056911"
author = "=LyRe= 八幡 海鈴"
matchingInfo = {}                          // 本局为空对象
player[] = 32 条 { name, clanTag, kills, groundKills, humanKills, navalKills,
                   teamKills, aiKills, aiGroundKills, aiNavalKills, assists, deaths,
                   captureZone, damageZone, score, awardDamage, missileEvades,
                   shellIntgerceptions, userId(str), squadId, autoSquad, team }
uiScriptsData.playersInfo = 32 条，键是 "__int_<userId>"：
                   { name, id, clanTag, clanId, platform, team, slot, squad, country,
                     tier, rank, mrank, elo, scores, auto_squad, wait_time,
                     crafts: {"4": "fa_18a"}, 
                     crafts_info: { __array, array0: { name, type, slot, rank, mrank,
                                                        rankUnused } } }
```

### 6.1 玩家表（32 人，`header.json` 的 `players[]`）

每人 30 个字段，包含契约要的 `userId / name / clanTag / kills / humanKills /
groundKills / navalKills`，全部非空。汇总（实测）：

| 指标 | 合计 |
|---|---|
| `kills` | 27 |
| `humanKills` | **0** |
| `groundKills` | 0 |
| `navalKills` | 0 |
| `teamKills` | 0 |
| `aiKills` | 1 |
| `deaths` | 29 |
| `score` | 18 867 |

**要注意的一点**：本局是纯空战（`battleClass = base_dom`，飞机图），
`groundKills / navalKills / humanKills / teamKills` 在文件里**确实都是 0**，
有击杀的玩家体现在 `kills` 上（例如 `MAVERICK_HAWK` kills=4、`八幡 海鈴` kills=2）。
不要把 `humanKills=0` 当成解析失败——同一个块里的 `kills`、`deaths`、`score`
都是有值的，说明这些字段本来就填 0。合计 `kills`=27 与 33 个击杀包同量级，
互相对得上。

有击杀的玩家 15 名：`MAVERICK_HAWK`(4)、`xxxmayonaise@live`(2)、`RX_7_f15mtd@psn`(2)、
`Mo Amaris`(2)、`Gibby Tibbles@live`(2)、`D3lt48574@live`(2)、`Smart_money_con@psn`(2)、
`Spicy Sukhoi`(2)、`八幡 海鈴`(2)、`EenBeen`(2)、`Renamed#3469`(1)、`BasidialCash`(1)、
`Maikel_el_wbon1`(1)、`Prokill4`(1)、`TheBigMac112`(1)。

**每名玩家的 `timePlayed` 不存在**：结算 JSON 只有对局级的 `timePlayed`；
`playersInfo.wait_time` 是等待时长，不是游戏时长（D：已排除这条误读）。

---

## 7. 实体 / 载具名：有什么、没有什么

### 7.1 有的：**玩家 ↔ 载具** 真值（可信度 A，来自结算 BLK）

`uiScriptsData.playersInfo[__int_<userId>].crafts` + `crafts_info` 给出每名玩家的载具，
这是**完全离线、不依赖 8111** 的真值。本局 32 人全部有载具，共 20 种：
`cf_188a_canada / f_14a_early / f_15a / f_16a_block_10 / f_16a_block_10_netz_mod / f_16xl /
f_20a / fa_18a / fa_18c_early / fa_18c_switzerland / jf_17 / m_346fa /
mig_29_9_12_germany / mig_29smt_9_19 / mirage_4000 / saab_ja37di_f21 / su_25 /
su_30mk2v_venezuela / su_33 / tornado_f3`。

写在 `entities.json` 的 `playerVehicles[]`（含 `primaryCraft` / `primaryCraftType`：
`fighter` 之类），`header.json` 的 `players[].vehicles` 里也有一份名字列表。

### 7.2 有的：**包流里的单位名表**（可信度 B）

见 §5.3（`00025839`）：403 个模板单位名，主要是 AI 位与地面目标。

### 7.3 有的：**包流里的「单位装载载具」事件**（可信度 B）

见 §5.2：47 条 `(tick, 单位 id 候选, 载具名)`，写在 `entities.json`
的 `spawnVehicleEvents[]` 与 `spawnUnitIdToPlayers`。

### 7.4 **没有**：entityId ↔ 玩家名 的映射表（可信度 B，否定结论）

把 32 名玩家的名字逐个在**全部 86 773 个包**里做字节搜索，命中只有 5 处，
而且**全部**是 ptype=3 聊天包：

```
MAVERICK_HAWK     ×2   （聊天）
Mo Amaris         ×2   （聊天）
RX_7_f15mtd@psn   ×1   （聊天）
```

其余 86 768 个包一个玩家名都没有。**结论：玩家名只存在于两个地方——
文件末尾的内嵌结算 BLK，以及聊天包。**

因此，「哪个实体是哪名玩家」这件事在回放里**没有直接答案**。
可用的替代路径（按可靠度排序）：

1. 结算 `player[].kills` / `deaths` 与 `00025858` 击杀包的时序做约束求解（弱）。
2. `00025858` / `00025856` 载具名 + 结算 `crafts` 的载具集合做交集（可缩小子集，不能唯一确定）。
3. `00025839` 只有模板单位名，对玩家实体没用。

**给下游的建议**：不要为了给实体贴玩家名去猜。契约 §2.5 已经允许「对不上的目标不造呼号」，
按侧别+机种标（`ENY FTR`）即可；载具名可以用 §7.1 的玩家↔载具表去反查
「这架 f_15a 可能是这 3 个人之一」。界面上标"机种"是可靠的，标"玩家名"不可靠。

---

## 8. 击杀/伤害事件有没有本地化原文？——**没有**

这是一个必须明确回答的问题。**答：没有，回放里不存在「X 击落了 Y」这类原文。**

排除方法（可复现）：把 14 180 794 字节的解压包流整体做字节搜索，结果：

| 模式 | 命中次数 |
|---|---|
| `destroyed` | 0 |
| `shot down` | 0 |
| `has been` | 0 |
| `rammed` | 0 |
| `critically` | 0 |
| `hudmsg` | 0 |
| `battleLog` | 0 |
| UTF-8 `击落` | 0 |
| UTF-8 `摧毁` | 0 |
| `kill` | 36（**全部**在 `00025878` 的奖项内部名里） |

整条流里唯一的人类可读文本是 §5.4 的 5 条聊天。

与击杀相关的载荷只有两类：

- `00025858`（33 个）—— 结构化字段，见 §5.1，**没有文案**；
- `00025878`（103 个）—— 奖项，载荷里是**内部名**，共 27 种，例如
  `hidden_kill_streak`(15)、`hidden_kill1_on_fighter`(14)、`hidden_marks_1_aircraft`(15)、
  `hidden_win_streak`(16)、`multi_kill_air`(4)、`first_blood`(1)、`final_blow`(1)、
  `defender_fighter`(4)、`global_avenge_friendly`(4)、`trophy_near_bomber`(1)、
  `fr_fighter_cross_streak`(1) 等。这些是内部键，**不是**可显示的本地化文案
  （要文案得自己配一张 id→文案表，而回放里没有这张表）。

**结论**：事件文本只能从字段拼合，请按 `composed=true` 处理。

---

## 9. 还没解出来 / 已排除的部分

| 事项 | 状态 | 排除或差距 |
|---|---|---|
| `ptype=2 AircraftSmall` 的字段布局（→ 实体三维位置） | **未解** | 见下 |
| `ptype=6 ECS` 的字段布局 | **未解** | 11 246 个包，载荷 15 字节上下，没找到文档 |
| `00FF0781` / `00035843` / `00FF0F81` 等高频 MPI 的字段语义 | **未解** | 载荷是位打包字节流，没有明文锚点 |
| settings BLK 的解析 | **未解** | 首字节 `0x49` 不在 wt_blk 的 `FileType` 枚举内；键名可见，值区未解 |
| `00025858` 的坐标/受击者字节串 | **未标定** | 载荷尾部有可疑字节串，但没有独立真值可校验 |
| 玩家名出现在包流里 | **已排除** | 86 773 个包逐个搜 32 个名字，只命中 5 处聊天包 |
| 击杀事件本地化原文 | **已排除** | 9 种文案模式全流搜索均为 0 |
| 每名玩家的独立 `timePlayed` | **已排除** | 结算只有对局级值；`wait_time` 语义不同 |
| entityId ↔ 玩家 映射 | **已排除** | 见 §7.4；另已证明 `00025856` 的 u32 ≠ `playersInfo.slot` |

### 9.1 关于 `ptype=2 AircraftSmall`（给航迹解析的接力棒）

本局实测到的事实（可信度 B）：

- 共 2 330 个，从 7.822 s 到 589.842 s，间隔众数 ~250 ms（4 Hz）。
- 包头有**两种**形式：**1 581 个自带时间戳**（首字节 `0x02`，5 字节包头，tick 各不相同），
  **749 个继承上一包的时间戳**（首字节 `0x12`，1 字节包头）。逐包统计 `hl` 分布 = `{5: 1581, 1: 749}`。
- **两种形式的载荷都固定以 `00` 开头**（把包头剥掉之后）。
  开局那几个包的载荷头是 `00 F8 01 7B 40 5F 70 15 F8 81 57 CC 05 55 57 B8 05 …`。
  （本报告第一版写成「全部 2 330 个首字节都是 0x12、从不自带时间戳」，那是把 `body[1]`
  误当成 `body[0]` 读出来的；上面的数字是脚本重算并核过的。）
- 长度不固定，分组后前几名：1 646(65)、1 650(50)、1 871(46)、1 642(43)、1 810(34)、
  1 657(33)、1 943(31)、1 939(30)、89(28)；最短 45、最长 5 173。
- 载荷里有大量 `0x55`（`0101 0101`）位模式，是位打包的量化状态。
- 对同长度分组做「逐字节取值的种类数」方差分析，变化区与稳定区交替出现（不是整齐的定长记录表），
  进一步支持「位打包 / 差分编码」的判断。

**为什么没有公开结论**：`wrpl` crate 只定义了 `AircraftSmall` 这个枚举名，
没有任何字段解析代码（它的 `parser.rs` 对 ptype=2 什么都不做）；
WT-Plotter 自述位置取自 8111；Thunder_Viewer / WarThunder 都是 8111 工具；
`nilshellerhoff/warthunder-replay-parser` 只解文件头与玩家载具列表。
所以这条只能自己反推，或改用「契约 §1 允许的 8111 兜底」路径。

---

## 10. 复现步骤

```bash
# 需要 Python 3.8+ 与 zstandard（pip install zstandard）
python tools/wrpl_header.py
# 默认读 <game-dir>\Replays\2026.10.18 05.45.06.wrpl
# 默认写 build/wrpl/{header.json,entities.json}
```

脚本只读回放，不写回放。`--replay` 换文件，`--out-dir` 换输出目录，`--quiet` 关摘要。

**沙箱注意**：本会话里子进程（python）对**既有**目录（`tools/`、`data/_scratch/`）只读，
写会报 `PermissionError`／`[sandbox: file access denied under workspace-write mode]`；
只对工作区根目录与根目录下**新建**的目录可写。所以产物放在 `build/wrpl/`。
另外，`tools/` 下的文件被文件工具覆盖时会报 `ReplaceFileW EIO (Win32 1175)`，
**先 `read` 一次再写**就正常；`**`、`data/_scratch/**`、`web/**` 无此问题。

---

## 11. 本局关键数字速查

| 项 | 值 |
|---|---|
| 回放文件 | `2026.10.18 05.45.06.wrpl`，5 133 517 B |
| magic / version | `0x1000ACE5` / 101404 |
| 地图 / 模式 | `air_skyscraper_city` / `base_dom`（`day` + `cloudy`） |
| sessionId | `0x4AD212E80006E54E` |
| 对局开始（UTC） | 2026-10-17 21:35:08 |
| 头部 / settings / zstd 帧 / 结算 偏移 | 1226 / 1226(632 B) / 1866 / 5 118 758（头部定长 1226 B） |
| 解压后包流 | 14 180 794 B，86 773 个包，恰好用尽 |
| 时钟 | 真实字段 = 毫秒（`body[2:6]` u32 LE）；`raw = tsMs×256` 为历史口径；开局 7.822 s，结束 589.949 s |
| 对局时长（结算 timePlayed） | 589.94928 s |
| 玩家 | 32 人，`kills` 合计 27，`deaths` 合计 29 |
| 击杀包 / 奖项 / 聊天 / 单位名 | 33 / 103（27 种）/ 5 / 403 |
| 本地化击杀原文 | 无 |

---

## 12. `ptype=2` AircraftSmall 结构分析

（可信度 B：全部是本局实测标定，没有公开文档佐证。回答提出的四个问题。）

前置：下面说的「载荷」都指**剥掉包头之后**的部分（2330 个包；其中 1 581 个是 5 字节包头，
749 个是 1 字节包头，见 §9.1）。

### 12.1 四个问题的结论

| 问题 | 结论 | 依据 |
|---|---|---|
| 是定长记录网格吗？ | **不是**。是变长流；但内部数据按 **4 字节**对齐组织 | §12.3 |
| 有 id 类字段吗？ | **字节级和浅位级都没找到** | §12.6 |
| 开头那几个固定字节是记录计数/类型/标志吗？ | **不是计数**；是固定前缀，前 3 字节把包分成 3 类 | §12.2 / §12.5 |
| 长度对应实体数不同吗？ | 与 **8111 的空中目标数**相关 `corr≈0.69`；与总接触数只有 0.25；比值不恒定 | §12.7 |

### 12.2 三个前缀类（实测）

按载荷前几个字节分类，2330 个包严格分成三类，没有中间态：

| 类 | 载荷前缀 | 个数 | 长度范围 | 出现时段（回放秒） |
|---|---|---|---|---|
| **A** | `00 F8 01 7B 40 5F` | 52 | 44 – 86 | 7.8 – 20.3 |
| **B** | `00 F0 81 7B 40 5F 90 17 CE 05 …` | 1 691 | 81 – 3 899 | 20.6 – 589.8 |
| **C** | `00 F0 81 00 00 00 00 00 29 08 12 00 …` | 587 | 2 792 – 5 168 | 31.1 – 177.6 |

类 A 的前 6 字节在 52 个包里完全相同；类 B 的前 3 字节相同；类 C 的前 24 字节相同。

**三类的共同开头是 `00`**；B 与 C 还共享 `F0 81`。把 `F8 01` 与 `F0 81` 按 LSB 优先拆位，
两者只在两个位上有差别（第 3 位、第 15 位）——这更像**位打包流的分叉**，不像三个独立的字节字段。

### 12.3 不是定长记录网格（否定结论，四条证据）

1. **长度不落在等差数列上。** 全体的不同长度有 399 种，从 44 到 5 168；
   相邻长度差的最大公约数 = **1**。`[头][N × stride]` 的网格必然给出
   `base + N × stride` 的等差长度集合。分类别看也一样：A / B / C 三类的长度在
   **mod 2、mod 4、mod 8 下都取遍所有余数**。
2. **包内字节自相关没有台阶。** 对最长的 40 个包做「偏移 i 与 i+lag 的字节相等率」，
   lag 从 4 增到 112，相等率从 0.278 **单调衰减**到 0.14 附近（随机基线约 1/256），
   **任何 lag 上都没有峰**。定长记录会在 stride 处出现一个明显的峰。
3. **同长度包只有很短的前缀相同。** 例如 len=1645（71 个）相同前缀只有 10 字节、相同后缀 2 字节，
   中间 1 633 字节全在变；len=1866（58 个）是 10 / 7 字节。
4. **但数据是 4 字节取向的。** 类 C 的「逐字节取值种类数」向量在自己的
   自相关里，**lag 4 的相关系数最高（r = +0.720，lag 1 是 +0.622）**；
   包内 lag-4 字节相等率 0.285，是所有 lag 里最高的。这符合「大量小整数按 u32 LE 存放」
   ——高字节常常相等——而不是记录边界。

**合起来的判断**：载荷 = `[固定前缀][变长的、按 4 字节取向的编码体]`。
长度既然能翻倍，就不是「实体数 × 固定记录」，更像**按实体逐个写入的变长编码**。

### 12.4 类 C 的固定前缀长 24 字节（不是计数）

类 C 的 587 个包，**offset 0..23 全部逐字节相同**：

```
00 F0 81 00 00 00 00 00 29 08 12 00 04 00 04 00 08 00 04 00 08 00 04 00
```

而这 587 个包的长度从 2 792 到 5 168（近 2 倍差）。**任何记录计数字段都必须随长度变化，
所以这 24 字节里没有计数**。它只能是固定的魔数/表头/位流起始。

紧接着是：

- offset 24：18 种取值（120 占 485 个）；offset 25/26/27：各只有 1 种取值（`08 3C 3C`）
- offset 28：18 种取值（与 offset 24 同分布）；offset 29/30/31：各只有 1 种（`08 3C 3C`）
- offset 32：1 种；offset 33：2 种
- **offset 34 起**逐字节种类数跳到 223 / 164 / 112 / 238 / 235 …，变长编码体从这里开始

所以类 C 的布局是 `[24 B 固定前缀][2 × 4 B 小表项][2 B][变长体]`，
两个 4 字节表项的**首字节**是同分布的（同一个值重复两次），后 3 字节固定。
这个「表项」是什么还没标定。

### 12.5 开头的固定字节不是类型/计数（否定结论）

- **offset 0 在所有 2330 个包里都是 `0x00`** —— 一个恒定的首字节。
- 分类只用到 offset 1..4；**offset 1 只有 2 种取值（240 / 248），offset 2 只有 2 种（129 / 1）**。
  两个字节联合起来只有 2 种组合（`F0 81` / `F8 01`），不是「类型码 + 计数」的自由组合。
- 逐字节查「谁像计数」：offset 1..23 里没有任何一个字节与长度同向变化
  （类 B 的 offset 1..2 恒为 `F0 81`，而类 B 的长度跨 81..3 899）。
- 类 C 的 offset 0..23 恒定（见 §12.4），进一步排除计数。

### 12.6 找不到 id 类字段（否定结论 + 方法）

给定的判据是「包内取值互不相同、量级小、跨包缓慢变化、与出生事件 `unitId32` 集合对得上」。
两条路都走不通：

1. **「缓慢变化」这条判据在本族数据上失效。** 对全部 2330 个包按时间顺序，统计
   offset 0..32 每个字节「与上一包取值相同的比例」：**全部 ≥ 0.99**（前 23 个字节里
   大多数是 1.000）。也就是说**相邻两个包几乎逐字节相同**，任何字节都"看起来稳定"，
   这个方法无法把 id 字段和常量、和慢变字段区分开。根本原因是**变长编码里字节偏移不是稳定的字段边界**。
2. **浅位级搜索也没命中。** 对全部包，在 bit 偏移 0..400 上按 6 / 8 / 10 位 LSB 优先取值，
   筛「取值个数 ≥10、范围 1..64」，再与出生事件的 `unitId32` 集合
   `{3,4,5,6,10,13,15,19,21,22,23,24,25,26,28,31}` 求交集：
   - 8 位、10 位：**没有任何偏移满足条件**；
   - 6 位：最好的偏移取值集合与 id 集合重合 0.40（12 个值里中 3~4 个），
     **这正是随机期待水平**，不构成证据。

**结论**：在没解出变长体的编码规则之前，id 字段无法从字节/浅位层面找到。
如果 id 存在，它在编码体内部，需要先把编码体解出来（见 §9.1 的建议：先做同长度分组的
逐字节方差，再按 4 字节取向找 `u32` 字段）。

### 12.7 长度与实体数的关系（开发期验证，仅用于判断）

方法（**只用于判定，不进任何交付数据**）：把 8111 `map_obj.jsonl` 每行的
`type=="aircraft"` 计数按 `rel` 建索引，再对每个 `ptype=2` 包按
`8111_rel = 回放秒 + offset` 取最近样本，扫 offset 0.1 s 步长找最大相关。

结果：

| 对照量 | 最佳 offset | Pearson r |
|---|---|---|
| 8111 **空中目标数** | 53.6 – 54.1 s | **0.687** |
| 8111 全部非机场接触数 | 54.1 s | 0.250 |

- 最佳 offset ≈ **53.8 s**，与航迹解析用 `0003584a` 属性表独立标定出的
  `replay_sec + 53.8` **一致**——两边互相印证了时间基。
- 长度随时间**先升后单调降**（44 → 84 → 30 s 后跳到 ~4 700 → 75 s 峰值 ~5 140 →
  之后一路降到 450 s 后的 ~1 640），形状与「场上实体数」曲线一致。
- **但比值不恒定**：早期约 300 B / 架，450 s 后约 130 B / 架。
  所以长度**不是**空中目标数的线性函数；只能说「长度跟活动实体集合同向变化」。

这个数字只能作为判断依据。契约 §0 要求交付数据只用回放本身，本节不产生任何交付数据。

### 12.8 顺带发现的 MPI 线索（转给航迹解析）

在找「多实体位置」时扫了出现最多的几个 MPI 签名（载荷含 4 字节签名本身，所以
offset 0..3 恒定是对的）：

| 签名 | 个数 | 载荷长度众数 | offset 4 的取值 | 备注 |
|---|---|---|---|---|
| `00FF0781` | 32 716 | 29(15 562) / 14(7 291) / 178(1 345) | **恰好 16 个：`128+8k`, k=0..15** | 4 位字段打头 |
| `00FF0F81` | 9 448 | 16(6 117) / 13(996) / 14(812) | **同上，完全相同** | 与上一行共享同一个字段 |
| `000258AA` | 3 253 | 71 / 24 / 58 | 恒定 | offset 7 有 31 种取值、每个包只变一处，像「某一项更新」 |
| `000258DB` | 3 783 | 100(897) / 101(684) / 120(393) | 恒定 | offset 7 有 0..10 共 11 种 |
| `00025828` | 1 088 | 256(381) / 226(268) / 240(222) | 恒定（240） | offset 5 有 16 种取值但不规则 |
| `0002582D` | 3 627 | 31 / 30 / 45 | 恒定（240） | — |

`00FF0781` 与 `00FF0F81` 的 offset 4 都是 `{128,136,…,248}`，即二进制 `1xxx x000`，
是**同一个 4 位字段**（值 0..15）；它**不等于**出生事件的 `unitId32` 集合（`{3..31}` 中 16 个值）。
这个 16 值字段更像「通道/子类型编号」，不是实体 id。
`00FF0781`（32 716 个 ≈ 37 Hz）和 `00FF0F81`（9 448 个）比 `ptype=2` 更可能是逐实体的更新通道，
建议优先在它们身上找记录边界。

### 12.9 复现方法

本章所有数字都能用 `tools/wrpl_header.py` 里的
`parse_replay_header / locate_packet_stream / decompress_stream / parse_packets`
四个函数取到原始包，再用普通 Python 统计得到。关键统计量：

- 长度集合与 GCD：对所有 `ptype==2` 的 `len(payload)` 求 `gcd(b-a)`；
- 包内自相关：对同一包 `sum(p[i]==p[i+lag]) / count`，再对若干包平均；
- 逐字节取值种类数：`len({p[o] for p in group})`；
- 与 8111 的相关：`rel = replay_sec + offset` 取最近样本后算 Pearson。

### 12.10 附带产物

`build/wrpl/unit-names.txt` —— 从 §5.3 的 403 个模板单位名导出的纯文本列表
（按出现次数降序，带表头注释），可直接用于 AI / 地面单位的标签。

---

## 13. 参考实现对照（wrpl-inspector）

**来源与许可**

- 仓库：[maxsupermanhd/wrpl-inspector](https://github.com/maxsupermanhd/wrpl-inspector)，默认分支 `v3`
- 许可：**AGPL-3.0**（各文件头载明 `Copyright (C) 2025 flexcoral`，AGPL 第 3 版或更新）
- 本次只**移植知识**（字段布局、消息号 ↔ 名字对应关系、算法步骤）。
  `tools/*.py` 与本文档的全部实现都是自写的 Python，**没有复制参考实现的代码**。
- 名字表的二次出处（参考实现自己在 `names.go` 里注明）：
  [moleium/wrpl](https://github.com/moleium/wrpl) 的 `modules/packets.hpp`。

### 13.1 容器：`wrpl/header.go` + `wrpl/wrplRead.go`

| 项 | 参考实现 | 早先结论 | 处理 |
|---|---|---|---|
| 头部总长 | **1226 字节**（末尾有 `Raw_Unknown5 [2]byte`） | 1224（把末尾 2 字节当成「settings 前的填充」） | 参考实现更干净：1226 就是头部，settings 紧跟其后 |
| settings 尺寸字段 | **`SettingsBLKSize uint16`**（偏移 748） | 读成 u32（偏移 748，值 632） | 本局后 2 字节恰好为 0，两种读法同值；**按 u16 才对** |
| 偏移 740 | `ReplayPartNumber byte` + `Raw_Unknown2 [3]byte` | 当成 4 字节 padding | 补上；`Raw_Unknown2[1] == 0x5a` 表示**服务端分片回放** |
| 包流起点 | `Seek(SettingsBLKSize + 1234)` | 实测 zstd 帧在 `1226 + settingsSize + 8 = settingsSize + 1234` | **完全一致**（本局 632 + 1234 = 1866）✅ |
| 包流压缩 | `zlib.NewReader` | 本局是 **zstd 单帧**（魔数 `28 B5 2F FD`） | 参考实现这段对本局会失败；`rezOffset`、settings 偏移、起点公式都对得上 |

也就是说：**§2 里发现的「settings 之后还有 8 个字节」并不是神秘填充**——
它是 `1226 + 8 = 1234` 这个固定常量的一部分，参考实现直接把它写成了魔数。

### 13.2 包帧：`wrpl/packet/packetReader.go`（**本实现已按此修正**）

```go
psr.Header = make([]byte, 2)          // 包头最少 2 字节
io.ReadFull(psr.R, psr.Header)
if psr.Header[0]&0b00010000 != 0 {    // 时间戳与上一包相同
    pk.PacketType = psr.Header[0] ^ 0b00010000
    payloadSize = packetSize - 2
} else {
    pk.PacketType = psr.Header[0]
    binary.Read(psr.R, binary.LittleEndian, &psr.T)   // u32 LE，单位**毫秒**
    payloadSize = packetSize - 6
}
```

**这是一处实质修正**（早先的实现把包头当成 1 / 5 字节）：

| 项 | 早先的做法 | 参考实现 / 实测 |
|---|---|---|
| 最小包头 | 1 字节 | **2 字节**：`[类型 u8][0x00 u8]` |
| 带时间戳的包头 | 5 字节 | **6 字节**：`[类型 u8][0x00 u8][ts u32 LE]` |
| 时间戳单位 | 曾以为要除以 256（`tick/256000 = 秒`） | **就是毫秒**，直接 `ts/1000` |
| 载荷起点 | `body[1:]` | `body[2:]`（**往后一位**） |

实测核对：**全部 86 773 个包的 `body[1]` 都等于 `0x00`**；
用 `body[2:6]` 读毫秒时 EndMarker = **589949 ms**，与内嵌 `timePlayed = 589.94928 s` 吻合。

之前"时间戳总是 256 的倍数、除以 256 得到毫秒"这个现象，正是**错位一位**造成的：
实际读到的是 `body[1:5]` = `0x00 + ts 的低 3 字节` = `ts << 8`。结论（秒数）没错，成因说错了。
`wrpl_header.py` 已改正：包头 2/6 字节、时间戳为毫秒、载荷从 `body[2:]`/`body[6:]` 起。
**下游如果按老偏移解包，所有载荷内偏移都要 +1。**

包类型名与本报告 §4 完全一致（参考实现 `packet/packet.go` 的 `TypeString()`）。

### 13.3 MPI 消息号与名字表

参考实现读 `PacketPayload[0:4]` 当签名，布局是：

```
[u8 kind][u8 0x58][u16 LE msgId][字段数据…]
```

`kind` 本局取 `0x00 / 0x02 / 0x03`；**消息号 = `PacketPayload[2] | PacketPayload[3] << 8`**。
wrpl crate 把 `payload[0:4]` 当签名，实际错开了一位；早先用
`payload[0:4].hex()`（如 `00025858`）做标签，等价但偏移不同。

用参考实现的 `names.go` 表（161 条）对照本局 41 个消息号：**19 个有名、22 个表里没有**。
完整对应见 `build/wrpl/mpi_names.json`。要点：

| 旧标签 | msgId | 参考实现名字 | 本局个数 | 说明 |
|---|---|---|---|---|
| `00035843` | `0xD043` | **ReplayCameraParams** | 13 412 | 与航迹解析独立解出的「四元数 + 9 个 float」吻合 |
| `00025839` | `0xD039` | **DeferredReplicatedObjectCreate** | 1 111 | 与 §5.3「载荷里是单位名」吻合（创建被延迟的对象/单位） |
| `000058df` | `0xF0DF` | **CustomWeatherParams** | 8 | 与载荷里有 `sunDir/wind/skyColor` 吻合 |
| `00025858` | `0xF058` | **TextKillReport** | 33 | 击杀报告（见 §13.4） |
| `00025856` | `0xF056` | **TextCriticalHitReport** | 32 | 致命伤报告 |
| `00025857` | `0xF157` | **SevereDamage**（表里无，据 `damage/severe.go` 的签名 `02 58 57 f1`） | 15 | 重伤报告 |
| `00025878` | `0xF078` | **TextStreak** | 103 | 连杀/奖项文本（载荷里是 `hidden_kill_streak` 这类内部键） |
| `0002582d` | `0xF02D` | **ReflectionData** | 3 627 | — |
| `000258aa` | `0xF0AA` | **RedundancyReflectionData** | 3 253 | — |
| `000258db` | `0xF0DB` | **ShellsData** | 3 783 | — |
| `00025828` | `0xF028` | **RunTriggerAction** | 1 088 | — |
| `00025873` | `0xF073` | **GroundModelPositions** | 788 | **地面单位位置**，`7xx` CV 家族 |
| `0002588e` | `0xF08E`? | 表里无（`0xF08D`/`0xF08F` 之间） | 783 | — |
| `00ff0781` 一族 | `0x??81`（见下） | 表里无 | 约 3 万 | 见下 |

**还没对上的 22 个消息号**里，最大的一族是旧记为 `00ff0781` 的那批：
按新偏移它是 `PacketPayload = [0xff][0x07][0x81][XX]`，其中 `XX ∈ {0x80,0x88,…,0xF8}`（16 个值）。
它**不满足**参考实现的 `[kind][0x58][idLo][idHi]` 布局，所以那 16 个值被当成了 16 个不同消息号
（`0x8081 / 0x8881 / … / 0xF881`），表里都没有。
`PacketPayload[1] = 0x07 ≠ 0x58` 说明这是**另一类消息**，不是 MPI 名字表覆盖的范围。
（早先把 `XX` 当成"某个字段"、把整族当成一个签名，也是错的。）

### 13.4 MPI 载荷的通用编码：`IdFieldSerializer32`

这是本次最有价值的移植。参考实现用
`wrpl/idfieldserializer/IdFieldSerializer32.go` + `util.go` + `wrpl/danet/bitStream.go`
解码（击杀/伤害等包都用它）：

```
（要求字节对齐）
u16 LE  offsetNum      —— 尺寸表的字节偏移，相对本字段起点
varint  fields         —— 位掩码，第 i 位置位表示第 i 号字段存在
（记 startBody = 当前位偏移）
跳到 start + offsetNum*8，对 fields 里置位的每一位（按序）读一个"字段宽度"：
     3 位头：1→1, 2→8, 3→16, 4→32, 5→64, 6→96, 7→128 （单位：位）
     头 = 0 时，再读一个 varint 作为位宽
跳回 startBody
按 fields 从低位到高位遍历字段：读该字段（宽度取自尺寸表）；
    读到不认识的字段就按它的位宽跳过
```

也就是说：**MPI 载荷不是定长记录表，而是"位掩码 + 位宽表 + 按序排列的字段"**。
这解释了 §12 里 `ptype=2` 那种「变长、按 4 字节取向、找不到记录步长」的现象——
同一族消息用的是位流编码，字节偏移本来就不是稳定边界。
（`ptype=2` 是否也用这个序列化器**尚未确认**；`ptype=2` 的 `PacketPayload[0:2]` 当 u16 偏移会超出长度，
所以它多半是另一种编码。）

### 13.5 击杀/伤害包的完整字段（`wrpl/packet/parser/{kill,damage}`）

参考实现按 `PacketPayload[0:4]` 精确匹配签名，再对 `PacketPayload[4:]` 跑上面的序列化器：

**`kill2`** —— 签名 `02 58 58 f0`（= 旧记法的 `00025858`，本局 33 个）：

| 字段号 | 类型 | 含义 |
|---|---|---|
| 1 | u32 LE | `KillerPid` 击杀者玩家 PID |
| 2 | 长度前缀字符串 | `PlayerVehicle` 击杀者载具（本文件解出的 `su_25` / `f_15a` 就是这个） |
| 3 | u16 LE & `0x7FF` | `VictimUid` 受击者单位 UID |
| 4 | u16 LE & `0x7FF` | `KillerUid` 击杀者单位 UID |
| 0xA | 长度前缀字符串 | `PlayerWeapon` 所用武器 |
| 0xB | u32 LE | `VictimPid` 受击者玩家 PID |
| 0xC | 长度前缀字符串 | `DestroyedWeapon` 被摧毁的武器 |

另一个字段（`KillEntry.VictimPid`）拿到的是**玩家编号**，配合
`ECS.GetEntityByUid(uid)` 就能落到实体上。

**本项目已经把这个序列化器实现出来了，33/33 零失败**（`tools/wrpl_kill_events.py`
→ `build/wrpl/kill_events.json`）。位序要点（照 `danet/bitStream.go` 推出）：
一个字节内**从最高位开始**取，先取到的位是返回值的高位；`ReadCompressed` = LEB128
（每字节低 7 位有效、最高位是继续标志、低位字节在前）；`binary.Read(LE, uN)` = 顺序取 N 字节再按小端解释。

实测结果与五条新结论（可信度 A，双重自检）：

| 结论 | 证据 |
|---|---|
| **30/33 条击杀有武器名**，且与载具族严格对应 | `su_25`→`su_r_60`/`su_kh_25ml`、`fa_18c_early`→`us_aim7p_sparrow_fa_18`、`f_15a`→`us_aim7m_sparrow_f_15_default`、`tornado_f3`→`us_aim9l_sidewinder` |
| 33 个载具名与 §5.1「扫 1 字节长度前缀」得到的**逐元素完全一致** | 两种独立方法互证（唯一差别：坠毁那条位流给空串、扫描给 null） |
| **UID 344…375 正好连续 32 个值**，与本局 32 名玩家数相同；`2047 = 0x7FF` 表示"无" | 击杀表 UID 全集 = `[215,216,299,344…375,414,2047]`；航迹解析用回放高度认领的本机 `uid=368` 落在其中 |
| **Pid 是另一个编号空间**，取值 `[0,1,2,3,4,5,6,9,10,11,13,14,15,19,21,22,23,24,25,26,28,31]`，`0xFFFFFFFF` = 无 | 这正是早期错记成 `unitId32` 的那个 u32（旧偏移 `pl[10:14]`）——**它其实是 `PlayerPID`**，例：62.191 s 那条 = 28 |
| 致命伤与击杀能串成因果链 | 62.191 s 的致命伤 `{OffendedUID:361, PlayerPID:28, Vehicle:"su_25", PlayerUID:372}` 与同 tick 击杀包 `victimUid=361 / killerUid=372` 完全对上 |

2 条 `killerUid=2047` 且载具/武器为空的记录是**坠毁或环境击杀**，下游不要当正常击杀处理。
`DestroyedWeapon`（字段 0xC）本局 33 条全为空串。

**给航迹解析的击杀点算法**：受击者 UID = `victimUid`，取该 uid 在 `tsMs` 时刻
（或最近的 4 Hz 采样）的 fm 航迹位置即可。`kill_events.json` 里每条都带
`tick`(raw) / `tsMs` / `sec` / 原始 `payloadHex`。

**致命伤 / 重伤包**（`0xF056` 32 条 / `0xF157` 15 条）也已解出，0 失败：
字段 1 `OffendedUID` u16、2 `PlayerPID` u32、3 `Vehicle` 字符串、4 `PlayerUID` u16；
致命伤多 5 `Fire` bool + 6 `Unk0` byte，重伤的 5 是 `Unk0`。
注意 `critical.go` 存进结构体时**没有**把 UID `& 0x7FF`（查 ECS 时才掩码），本文件按原样输出。

**`critical`** —— 签名 `02 58 56 f0`（旧记法 `00025856`，本局 32 个）：
字段 1 `OffendedUID` u16、2 `PlayerPID` u32、3 `Vehicle` 字符串、4 `PlayerUID` u16、
5 `Fire` bool、6 `Unk0` byte。

**`severe`** —— 签名 `02 58 57 f1`（旧记法 `00025857`，本局 15 个）：
字段 1 `OffendedUID` u16、2 `PlayerPID` u32、3 `Vehicle` 字符串、4 `PlayerUID` u16、5 `Unk0` byte。

> **重要语义更正**：早先把 `00025856` / `00025857` 记成「载具出生事件」并写进
> `entities.json` 的 `spawnVehicleEvents`（还猜过一个 `unitId32` 是玩家槽位）。
> 按参考实现，它们是 **TextCriticalHitReport / SevereDamage 伤害报告**，
> 载荷里的载具名是**伤害施加者的载具**，玩家编号是 PID(u32)/UID(u16)。
> 那 47 条数据本身没错（时间、载具名都是实测），但**含义和字段编号要按上表重读**。
> `entities.json` 现在用 `damageVehicleToPlayers` 表达「该载具名在结算表里对应哪些玩家」，
> 只是一个交叉核对，不是唯一映射。

### 13.6 entityId ↔ 玩家：参考实现怎么做的

参考实现**不靠名字**，靠 ECS：

- `wrpl/packet/parser/ecs2/EntityManager.go` 维护 `Uid_lookup`（UID → `*Entity`）与 `Entities`；
- UID 取 u16 的 **低 11 位**（`& 0x7FF`，`0x7FF` 表示"无"）；
- 击杀/伤害包给出 UID → 查到 `Entity` → 再关联玩家；
- 坐标来自 `packet/parser/movement`（地面）与 `packet/parser/fm`（空中）两条航迹。

所以「实体 ↔ 玩家/载具」在社区实现里的正确路径是 **ECS 的 UID 表**，
而不是早先想找的「实体表里带名字」。这也解释了为什么包流里搜不到玩家名。

### 13.7 尚未核对的部分（如实列出）

以下文件本次**没有读**，所以没有对照结论：

- `wrpl/carve/{kill,damage,player,entity}.go`（离线"雕刻"工具，可能给出导出格式）
- `inspector/tabs/{playersui/players.go,results/results.go,kills/kills.go}`
  （界面侧的玩家表/结算/击杀展示，可能含字段语义补充）
- `wrpl/packet/parser/{movement,fm,ecs2}/*`（实体位置与 ECS，是航迹解析的移植范围）
- `wrpl/blk.go`（参考实现自己的 BLK 处理，可与本实现的 FAT 解包互校）

### 13.8 本次对照带来的实际改动

| 产物 | 改动 |
|---|---|
| `tools/wrpl_header.py` | 包头 1/5 → **2/6 字节**；时间戳按**毫秒**（不再 `/256`）；载荷起点后移一位；MPI 分支改用 **msgId**；伤害包按 `0xF056`/`0xF157` 匹配；`spawnVehicleEvents` 的 `unitId32` 换成 `msgId`/`legacySignature` |
| `tools/wrpl_mpi_names.py`（新增） | 解析 `names.go` 的 161 条名字并与本局 41 个消息号对照 |
| `build/wrpl/mpi_names.json`（新增） | 19/41 有名；含来源与许可 |
| `build/wrpl/unit-names.txt`（新增） | 403 个模板单位名 |
| `build/wrpl/{header,entities}.json` | 按新偏移重新生成；`clock.formula` 改为毫秒口径 |
