# WTACRP — 战争雷霆回放 → Ace Combat 风格 3D 播放器

把《战争雷霆》的 `.wrpl` 回放变成一段可拖动的极简 3D 演出：航迹、击杀、地面单位、地形等高线与陆水掩膜、可切换机位与播放控制。解析全部在浏览器内完成，页面本身不发任何网络请求。

[English](#english) · [回放格式](docs/REPLAY-FORMAT.md) · [解码](docs/REPLAY-DECODE.md) · [数据说明](docs/DATA-NOTES.md)

## 两个入口

| 入口 | 用途 |
|---|---|
| `index.html` | 演示页：一局预生成的示例数据（`mission-data.js`），打开即看 |
| `web/index.html` | 工具页：拖入任意 `.wrpl`，浏览器内解析后播放 |

两页复用同一套渲染与播放（`three3d/scene.js`、`mission.js`、`ace.css`、`mission.css`）。工具页解析完成后通过 `window.ACEUI.mount()` 换数据，保留用户的显示设置。

## 特性

- 三维航迹（本机 / 小队 / 友军 / 敌军分色），按航向的机型图标与地面单位方块；
- 击杀落点常驻标记与扩散环，底部进度条带击杀刻度，支持 seek 与 1/2/4/8 倍速；
- 地形层：等高线与陆水掩膜按地图名加载，缺失时整层留空而不是编造；
- 地面单位会移动：`ptype=4` 的 MPI `0xF073`（服务器 `0xF074`）批量位置同步，1 Hz 阶梯保持；按构造块里的 uid 绑定型号，阵营取 ECS 的 `team` 组件（1=蓝/2=红）；
- 标签（型号 / 类别）、机位切换、网格、航迹开关、全屏、帮助；
- 呼号可自定义，队友按回放里实际小队绑定；玩家名/队伍从 slot 包精确解出（`unit__playerId`），队友 uid 直接绑定；
- 被击毁/销毁的单位按 ECS 销毁包（`0x26`）结束航迹，不再飞到回放结束；
- 机场跑道：地形素材没带机场时用机场目录（67 图 / 256 任务，由 wrpl-acmi 数据整理）兜底，按回放实际生成的 marker 选 rank 变体。
- 导弹弹道：`ptype=4` 的 WeaponSync（`0xF0DB` / `0xF11A`）真实同步位置，弹体不画，用白色虚线表示已飞过的航迹；设置面板可关。
- 本机与队友呼号下方可显示机体型号（设置面板“呼号下型号”开关）。

## 快速开始

仓库是纯静态站点，不需要构建就能在本地看两个页面：

```bash
python -m http.server 8000
# 打开 http://localhost:8000/          演示页（预生成数据）
# 打开 http://localhost:8000/web/      工具页（拖入 .wrpl）
```

部署到 GitHub Pages 的只有工具页；演示页与预生成数据留在仓库里供本地对照和回归。

完整的全地图地形（163 张等高线 + 掩膜，约 100 MB）来自 [WT-MAP-ANALYZE](https://github.com/ShioUmiusi/WT-MAP-ANALYZE) 的数据集，构建时打包：

```bash
python tools/pack_terrain.py --data ../WT-MAP-ANALYZE --out dist/terrain
```

仓库里只保留演示地图的样例。本地想看完整地形（163 图），把 WT-MAP-ANALYZE 的数据目录指给页面：

```js
window.ACE_TERRAIN_BASE = '/path/to/dataset/contours/';
window.ACE_MASK_BASE    = '/path/to/dataset/landmask/';
```

也可以用 `tools/pack_terrain.py` 打包到 `dist/terrain` 后从 `dist/` 起服务；其它地图缺素材时地形层留空并在 `limits` 里说明。

## 已知问题

- **阵营标识**：空中用击杀关系二色 + 出生位置聚类；地面用 ECS 的 `team` 组件（与上游 wrpl-acmi 对拍 6 份回放 715 个地面单位，0 分歧）。只有船/没有构造块的单位拿不到 team，保持 `unknown`，不按位置猜。

## 数据从哪来

位置与事件全部来自回放本身：`ptype=2` 的 FlightModel 三维航迹、`ptype=4` 的击杀包与地面同步、`ptype=6` 的 ECS 地面/机场标记点与 team，以及结算 BLK 的玩家表。仍旧读不出的字段（小队归属等）用回放内部证据推断，推断项在 `?` 帮助与 `docs/DATA-NOTES.md` 里写明。

示例数据已匿名化：玩家名、账号 ID、战队标签均为合成值，`ALPHA 01`…`ALPHA 32`。

解码与生成（Python，依赖见 `requirements.txt`）：

```bash
python tools/wrpl_header.py  --replay <replay.wrpl> --out-dir build/wrpl
python tools/wrpl_decode.py  --help      # 航迹
python tools/wrpl_decode_ecs.py --help   # ECS（地面、机场、发射点）
python tools/wrpl_kill_events.py --help  # 击杀
python tools/build_ace_mission.py --help # 汇总成 mission-data.js
python tools/check_ace_mission.py --help # 交叉校验
```

各脚本的参数与产物路径见 `docs/REPLAY-DECODE.md`、`docs/REPLAY-DECODE-ECS.md`、`docs/QUICK-REF.md`。

## 目录

```
index.html  mission.js  mission.css  ace.css  scene3d3-loader.js
mission-data.js  attitude-data.js        演示数据
three3d/scene.js                         渲染器（ES module）
web/                                     浏览器内解析（含 fzstd）
vendor/three/                            three.js r186
tables/                                  载具显示名与类别
terrain/                                 示例地图的等高线 / 掩膜
tools/                                   解码、生成、打包脚本
docs/                                    回放格式、解码、验证与数据说明
```

## 构建与部署

`tools/build_site.py` 把工具页与完整地形打包到 `dist/`（演示页不部署），`.github/workflows/pages.yml` 在 main 分支构建并部署到 GitHub Pages；`ci.yml` 做语法检查与示例数据解析检查。绑定自定义域名时在仓库 Pages 设置里填，并先配好 DNS。

## 许可与来源

- 代码：MIT，见 [LICENSE](LICENSE)；
- three.js（MIT）与 fzstd（MIT）见 `THIRD-PARTY-NOTICES.md`；
- 回放、地形与载具名数据均由玩家本机文件派生；《战争雷霆》及其素材版权归 Gaijin Entertainment，本项目与 Gaijin 无关；
- 仓库不包含游戏原始资源（`.wrpl`、关卡、贴图、模型）。

---

## English

Turn a War Thunder `.wrpl` replay into a minimal Ace Combat-style 3D scene: 3D tracks, kill markers, ground units, terrain contours and land/water masks, switchable cameras and playback controls. The server page parses the replay entirely in the browser and makes no network requests.

- `index.html` — demo with one pre-generated match; `web/index.html` — drop in any `.wrpl`.
- Rendering: three.js r186; parsing: `web/wrpl-parse.js` plus fzstd.
- Terrain comes from the [WT-MAP-ANALYZE](https://github.com/ShioUmiusi/WT-MAP-ANALYZE) dataset (`data-2026.10` tag) and is packed at build time by `tools/pack_terrain.py`; a sample map is included for local use.
- Python tools decode the replay container, tracks, kill events and ECS data into `mission-data.js`; see `docs/` for the format and the exact commands.
- Known issue: the replay stream does not expose a faction field yet, so ground units are assigned a side by position and can be misclassified; aircraft and the online player table still come from the match results.
- The bundled demo data is anonymized (player names, account ids and clan tags are synthetic). No original game assets are redistributed; War Thunder is property of Gaijin Entertainment and this project is not affiliated with Gaijin.

Code is MIT licensed.
