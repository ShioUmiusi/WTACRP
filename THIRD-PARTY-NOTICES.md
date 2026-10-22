# 第三方组件与数据

本项目代码以 MIT 发布。下面是用到的第三方组件与数据源，各自保留原许可。

## 前端运行时

| 组件 | 用途 | 许可 |
|---|---|---|
| [three.js](https://threejs.org/) r186（`vendor/three/`） | WebGL 渲染 | MIT，Copyright 2010-2026 Three.js Authors |
| [fzstd](https://github.com/101arrowz/fzstd) 0.1.1（`web/vendor/`） | 浏览器内 zstd 解压 | MIT |

## Python 工具

| 组件 | 用途 | 许可 |
|---|---|---|
| NumPy | 数值与轨迹处理 | BSD-3-Clause |
| lz4 | ECS 控制码解压 | BSD-3-Clause |
| zstandard | 回放包解压 | BSD-3-Clause |

## 数据

- **回放数据**：`mission-data.js` 是示例对局（已匿名化：玩家名、账号 ID、战队标签均为合成值），由 `tools/build_ace_mission.py` 从 `.wrpl` 解码产物生成。
- **地形与掩膜**：由 [WT-MAP-ANALYZE](https://github.com/ShioUmiusi/WT-MAP-ANALYZE) 从玩家本机安装的游戏文件解析得到；仓库只保留少量样例，完整数据在该仓库的数据集里。
- **载具表**：`tables/vehicle_names.json`、`tables/vehicle_categories.json` 由社区数据整理（War-Thunder-Datamine 等），仅保留显示名与类别。
- **格式参考**：[wrpl-inspector](https://github.com/maxsupermanhd/wrpl-inspector)（AGPL-3.0）用于理解回放容器与包结构；只作参考，没有复制其代码。

## 游戏素材

《战争雷霆》（War Thunder）及其素材版权归 Gaijin Entertainment 所有，本项目与 Gaijin 无关。仓库不包含游戏原始文件（`.wrpl`、关卡、贴图、模型）；示例数据是由玩家自己的回放与地形派生的结果。
