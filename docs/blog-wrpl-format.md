# `.wrpl` 文件结构：一个 5 MB 回放的完整解析路径

一场十分钟的空战回放，磁盘上是 5 MB 左右的一个文件。解完之后能拿到 86 773 个包、44 架飞机的三维航迹、
33 次击杀、112 枚导弹的位置序列，以及 32 名玩家的结算数据。这些内容全部来自同一个文件，没有外部索引，
也没有第二个数据源可以对照。

这篇文章按「字节 → 包 → 实体 → 语义」四层，把 `.wrpl` 的结构拆开写清楚，并在关键处给出可以直接照抄的
代码片段。文末附一条从字节到画面的最短路径，以及目前仍然空着的几格。

---

## 1. 整体形状：三段拼接

文件没有索引表、没有分块目录，只有三段首尾相接：

```
[ 1224 字节定长头 ][ 一个 zstd 帧 ][ 内嵌结算 BLK ]
        0                   1224              文件尾 - rezOffset
```

| 偏移 | 宽度 | 内容 | 示例值 |
|---|---|---|---|
| 0 | u32 | magic | `0x1000ACE5` |
| 4 | u32 | version | `101404` |
| 8 | 128 | level（NUL 结尾） | `air_skyscraper_city` |
| 136 | 260 | levelSettings（NUL 结尾） | `gamedata/missions/.../air_skyscraper_city_bfd_norespawn.blk` |
| 396 | 128 | battleType（NUL 结尾） | `air_skyscraper_city_BfD_norespawn` |
| 684 | u32 | `rezOffset`：结算 BLK 起点相对文件尾的位置 | `5118758` |
| 688 | u32 | `randomSeed` | — |
| 692 | u32 | `sessionId` 低 32 位 | — |
| 748 | u32 | `settingsSize` | `632` |
| 908 | u32 | `startTime`（Unix 秒） | `1792272908` |
| 1224 | — | 头部结束，包流开始 | — |

字段宽度按「定长头」读取，不需要变长解析：

```c
struct WRPLHeader {           // 小端，定长 1224 字节
  uint32_t magic;             // 0x1000ACE5
  uint32_t version;
  char     level[128];
  char     level_settings[260];
  char     battle_type[128];
  uint32_t rez_offset;        // 从文件尾倒数
  uint32_t random_seed;
  uint32_t session_id_lo;
  /* … 中间的服务器 / 会话字段 … */
  uint32_t settings_size;
  /* … */
  uint32_t start_time;
};
```

两个位置容易读错：

- `settings_size` 之后还有 8 个字节才出现压缩帧的魔数，直接按 `settings_size` 往后跳会错位。
- `rez_offset` 是**从文件尾倒数**的偏移，不是从文件头累加。

结算区是一段独立的二进制块（第 7 节），和包流互不重叠。

---

## 2. 包流：单帧压缩 + 长度前缀

头部之后是一整段压缩数据。现代回放用的是 **zstd 单帧**——不是 zlib，也不是多帧拼接。上游 Go 解析器
早期用 `ZlibDecoder` 读这段，遇到新版回放会直接解压失败，报的错是「解压后的流不以预期字节开头」。

解压之后的容器是「长度前缀帧」的连续流：

```
[ VLQ 长度 ][ 该帧内容 ][ VLQ 长度 ][ 该帧内容 ] …
```

长度是变长整数（每字节 7 位，最高位表示继续），帧内容自描述，不需要外部帧表：

```python
def read_vlq(rd):
    v, shift = 0, 0
    while True:
        b = rd.read(1)[0]
        v += (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            return v

while rd.remaining():
    n = read_vlq(rd)
    frame = rd.read(n)
    handle_frame(frame)          # 帧内再按两字节类型切分
```

### 帧内的包

每个帧是若干条包，每条包以 `u16 type` 开头，随后是载荷。类型编号直接决定谁来解释载荷：

| `type` | 含义 | 载荷要点 |
|---|---|---|
| `2` | 飞机位置（`AirCraftSmall`） | 紧凑位打包，4 Hz |
| `4` | 槽位 / 同步类 | `0xF02D` 玩家表、`0xF0DB` 弹药、`0xF073` 地面单位 |
| `6` | 实体系统（ECS） | 模板定义、构造、销毁、状态 |

时间轴不写在包头的通用字段里：回放以「帧序号 × 每帧毫秒数」推时间，头部另有一个起始时刻；
两者合起来才能把包事件还原成绝对时间。

---

## 3. 一族「地址」：`02 58` 开头的 MPI

`type=4` 的载荷大多长这样：前两个字节固定 `02 58`，随后是 `u16` 的消息号。

```
02 58 DB F0   →  0xF0DB（弹药同步）
02 58 2D F0   →  0xF02D（槽位 / 玩家表）
02 58 73 F0   →  0xF073（地面单位同步）
```

这一族消息就是回放里各种「周期性状态」的载体。分派写法很直白：

```js
function dispatch(payload) {
  if (payload[0] === 0x02 && payload[1] === 0x58) {
    const msgId = payload[2] | (payload[3] << 8);
    switch (msgId) {
      case 0xf02d: return parseSlot(payload.subarray(4));    // 玩家名、队别、载具词表
      case 0xf0db: return parseWeaponSync(payload.subarray(4));// 弹药位置
      case 0xf073: return parseGroundSync(payload.subarray(4));// 地面单位位置
    }
  }
  return null;
}
```

`0xF02D` 给出的是「槽位 → 玩家名 / 队别 / 载具词表」，是整份文件里少见的**可直接读的语义**；
`0xF0DB` / `0xF073` 给的是实体位置，需要和 ECS 的构造块对上号才能变成有名字的对象。

---

## 4. 实体系统：模板 + 构造块 + 稀疏下标

`type=6` 是实体系统，三类消息构成：模板定义、构造（出生）、销毁。

### 4.1 模板

第一次出现的模板会带完整定义：模板 id、名字、组件数量、组件 id 列表；每个**新**组件再跟两个 `u32` 哈希
——一个是数据组件哈希，一个是组件类型哈希。同一个模板第二次出现时只给 id，名字与组件表都不重发。

```text
模板定义 = tid:varint  name:len-prefixed  ncomp:u16
           [ cid:varint (新 cid 追加 dataHash:u32 typeHash:u32) ] * ncomp
```

组件类型与哈希的对照表由外部数据文件提供（游戏内组件名 ↔ `u32` 哈希），这是把「第 21 个组件」变成
`unit__playerId` 的唯一途径。

### 4.2 构造块

构造块只写**一部分**组件，用「增量下标」指出是模板里的第几个：

```text
构造块 = 模板头  count:u8
         [ ofs:u8  value(按组件类型解析) ] * count
         其中 idx = (i == 0) ? ofs : idx + ofs + 1
```

这段稀疏写法解释了一个常见误解：**构造块不是模板组件的顺序铺开**。飞机模板恰好每次都写同样的组件集合、
同样的宽度，于是「固定偏移」读法能在飞机上长期成立；换成坦克模板（组件更多、模型数据块更长），
同一偏移落到的就是别的字段。逐条按类型走才是通用解：

```js
function walkConstruct(block, defs, tplComps, startByte) {
  const r = new BitReader(block);          // MSB-first 位读取器
  r.pos = startByte * 8;                   // 组件条数已由调用方读过
  let count = r.byte();
  let idx = 0, out = [];
  for (let i = 0; i < count; i++) {
    const ofs = r.byte();
    idx = (i === 0) ? ofs : idx + ofs + 1;
    const [dataHash, typeHash] = defs[tplComps[idx]];
    out.push({ index: idx, name: nameOf(dataHash), value: readValue(r, nameOfType(typeHash)) });
  }
  return out;                              // 剩下未消费的位属于下一个消息
}
```

### 4.3 组件类型与位宽

组件的读取只有十来种形态：定长标量、变长字符串、实体 id、列表、以及几个游戏自定义结构。

| 组件类型 | 位宽 / 形态 |
|---|---|
| `bool` | 1 位 |
| `uint8_t` / `uint16_t` | 8 / 16 位 |
| `int` / `uint32_t` / `float` | 32 位 |
| `Point2` / `Point3` / `Point4` | 64 / 96 / 128 位 |
| `TMatrix` | 384 位（4 个 `Point3`） |
| `ecs::string` | 以 `0x00` 结尾的字节串 |
| `ecs::EntityId` | 变长（首 2 字节带标志位，按标志决定再读 0 / 1 / 2 字节） |
| `ecs::*List` | 变长计数 + 逐元素 |
| `dm::PartIdList` | 变长计数 **× 8** 个元素，每元素 6 位 |
| `*ModelStorageComponent` | 变长字节长度 + 该长度的原始位块（内部结构未公开） |

`dm::PartIdList` 的「计数 × 8」是这一层最容易踩的坑：计数字段声明的不是元素个数，而是 8 字节一组的组数，
真实元素数是其 8 倍。少乘 8 不会立刻报错，只会让后面每个组件整体错位，最终表现成「组件下标越界」。

---

## 5. 三类实体的具体读法

### 5.1 飞机

飞机有两条独立通道：`type=2` 的 4 Hz 紧凑位置包，和 ECS 里的构造/状态包。位置包解码成「实体 id + 三维坐标 +
航向」，量化为整数后按固定比例还原；ECS 侧给出机型名（`unit__className`）、uid、队别、玩家 id。

### 5.2 地面

地面单位的位置来自 `0xF073` 同步包（约 1 Hz），构造块给出静态点坐标与机型。两侧用同一套 `uid` 对齐：

```text
0xF073 包 → uid + (x, y, z, 朝向)
构造块   → uid + unit__className + unit__playerId + team
```

`uid` 只有 11 位，**会跨时段复用**：同一编号先属于一架飞机、后被另一辆坦克使用是正常现象。因此
「uid → 机型」的映射必须按域（空中 / 地面）分开存，否则地面单位会显示成同号的飞机。

### 5.3 弹药

弹药同步（`0xF0DB`）按三个 class 分组：0 = 火箭/导弹、1 = 炸弹、2 = 鱼雷。每组带计数，逐条记录：

```text
hasGuidance:1  noExtraTail:1  entity:u32  isRaw:1
isRaw ? 3×float32 : 2×u32 量化（各轴 ±15000 m 量程）
noExtraTail == 0 时追加尾部：ULEB、3×u32、6×u16、flags、seeker 计时…
```

弹药模板没有 `uid` 组件，所以只能通过 ECS 的构造位置与发射机的航迹做时间—空间关联，不能直接查表。

---

## 6. 坐标、单位与精度

- 位置在包内是**整数**，按量程线性还原（例如 ±15000 m 对 16 位无符号量化）。
- 空中位置 4 Hz、地面 1 Hz；插值会造出假运动，回放播放同样按「阶梯保持」处理。
- 出生与机场重生会造成瞬时大跳变（曾有 1024 m 的位移，前后速度都为 0），分段处理比强行平滑更诚实。
- `速度`、`马赫`、`高度` 这类量在不同包里的单位并不统一：有的带单位后缀（`IAS, km/h`），有的是裸 SI。

---

## 7. 文件尾的结算 BLK

结算区不是压缩 JSON，而是一段标准二进制 BLK：首字节 `0x01`，随后是名字表、块表、字段定义。
每个字段定义 8 字节：

```text
name_id : u24     字段名在名字表里的下标
type_id : u8      值类型
data    : u32     值（或指向值区的偏移 / 下标）
```

解出来的内容是本局所有玩家的 `kills / deaths / score / userId / squadId / team`，以及每人用过的载具名。
这是整份文件里唯一一处**离线可用的「玩家 ↔ 载具」真值**：包流里既没有玩家名，也没有直接的
「实体 ↔ 玩家名」映射，两边只能靠槽位号、队别与机型词表交叉推。

```text
BLK      = 0x01  name_count:u32  names[...]  block_count:u32  blocks[...]
name     = len:u8  bytes
fieldDef = 8 字节（见上）
```

---

## 8. 上游 Go 实现的分层

同一份格式，开源的 Go 解析器（`wrpl-inspector`，AGPL）按「读、切、解、拼」四层组织，值得单独看一遍，
因为层与层的边界正好对应格式里的边界：

| 层 | 职责 | 关键对象 |
|---|---|---|
| 容器 | 打开文件、读定长头、解压、按长度切帧 | `ReplayReader`、`PacketStreamReader` |
| 位读取 | 提供按位读取的原语（变长整数、实体 id、字节对齐） | `danet.BitReader` |
| 分派 | 按包类型 / MPI 消息号选解析器，解析器各自注册 | `packet.PacketParser`、`catalog` 里的注册表 |
| 语义 | 把实体流拼成「飞机 / 地面 / 弹药 / 击杀 / 玩家」 | `wrpl/carve`、`cmd/acmi` 的导出器 |

分派层的写法是一张注册表：每个解析器实现同一个接口，声明自己处理哪些类型，主循环只负责「读帧 → 交给
能处理的那一个」：

```go
// 语义等价写法（注册表）
var parsers = map[packet.Type]PacketParser{
    packet.TypeFlightModel: fmParser{},
    packet.TypeECS:         ecsParser{},
    packet.TypeSlot:        slotParser{},
    packet.TypeKill:        killParser{},
}

for !eof {
    var pk packet.Packet
    eof, err = stream.ReadPacket(&pk)
    if p, ok := parsers[pk.Type]; ok {
        p.Parse(&pk)
    }
}
```

ECS 层再往下还有一层注册表：组件哈希 → 解析器。定长类型走通用读取（按位宽），变长类型走专用解析器，
未登记的数据组件按「组件类型是定长 POD 就按宽度跳过」降级处理——这条降级规则是让新版本回放仍能解析的
关键，否则一个陌生组件就会让整块构造失败。

`wrpl/carve` 这一层做的是「把时间轴摊平成事件」：实体、击杀、玩家各一份，输出给下游渲染或导出。
格式本身不规定这些语义，`carve` 只是把隐含约定（谁打死谁、谁在哪一刻出生）写成显式结构。

---

## 9. 一条从字节到画面的最短路径

```text
读 1224 字节头          → magic / 版本 / 地图 / 起始时刻 / 结算偏移
从 1224 读 zstd 帧解压   → 连续字节流
按 VLQ 长度切帧          → frame[]
每帧按 u16 type 分派      → 位置包 / 同步包 / ECS 包
├─ type=2  → 4 Hz 位置 → (entityId, x, y, z, hdg)
├─ type=4  → 02 58 族  → 玩家表 / 弹药 / 地面同步
└─ type=6  → 模板 + 构造块 + 销毁 → 实体、机型、uid、玩家 id、队别
对齐                    → uid 把位置与实体接上；playerId 把实体接上玩家
渲染                    → 航迹、标签、结算面板
```

---

## 10. 尚未闭合的部分

| 项 | 状态 | 差距 |
|---|---|---|
| `settings` BLK 的值区 | 未解 | 首字节不在已知 BLK 枚举内，键名明文可见 |
| 状态类包的组件值布局 | 未解 | 已证明按实体 id 寻址，但值字段的排布未定 |
| 构造块的 `compCount != 0` 分支 | 未验证 | 现有样本里没有出现 |
| 任务名与槽位号的对应 | 未解 | 两套编号空间不同，任务名看起来是出生组而非槽位 |
| 弹药记录的尾部标量 | 未定位 | 游戏内界面显示过载 / 攻角 / 导引头，说明文件里有，但字节位置未定 |
| 弹药逐帧航迹 | 结构上不可得 | 弹体模板没有 `uid` 组件，无法与位置包关联 |
| 回放坐标 ↔ 外部世界坐标的刚体变换 | 未闭合 | 缺少独立真值对齐 |
| 渲染实例组件的语义 | 未解 | 少数块无法走查，属表现层，与实体语义无关 |

---

## 11. 复现所需的最小工具集

- **解压**：任一 zstd 实现（单帧，`uncompress` 即可）。
- **位读取**：MSB-first 的位读取器 + 变长整数 + 变长实体 id，三者就够走完整个包流。
- **组件名表**：组件哈希 ↔ 名字的对照数据；没有这张表，构造块只能读出数字，读不出含义。
- **样本**：空战回放（`*_norespawn`）适合验证位置与击杀；陆战回放带地面与重生，适合验证同一玩家换载具的场景。

格式本身没有版本化的字段表，字段含义全靠「同一份文件内互相印证」：位置包与同步包的坐标要能对上，
结算的击杀数要与击杀事件条数一致，机型词表要与出现的机型吻合。三处对不上，就说明某一处的偏移读错了。
