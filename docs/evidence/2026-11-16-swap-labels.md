# 证据：换机后机型标签跟“段”走（浏览器实测，2026-11-16）

## 复现

```
cd D:\8111tool && python -m http.server 18099 --bind 127.0.0.1
# 浏览器打开 http://127.0.0.1:18099/ace-combat/web/index.html
# 拖入 scratch/ground-dom.wrpl（= C:\Users\i\Downloads\2026.11.03 05.04.17.wrpl 的副本）
# 或用页面 API：ACEUI.seek(秒) 定位，ACEUI.setLabelMode('category'|'model') 切标签模式
```

## 实测（该回放，本机玩家 `=LyRe= hoshimati_renko`）

| 播放时刻 | 本机标签 | 说明 |
|---|---|---|
| T+30 s | `ALPHA 01 / ST-A3` | 地面段（坦克 `jp_st_a3`） |
| T+280 s | `ALPHA 01 / J7W1` | 空中段（`j7w1`，267 s 起） |
| T+300 s | `ALPHA 01 / J7W1` | 同上，坦克仍在场上但已不是当前载具 |
| T+560 s | `ALPHA 01 / J7W1` | 空中段到 588 s 才结束 |

地面单位在 `model` 模式下带玩家名（`M18 GMC · Mantis4445661@live` 等），`category` 模式仍是类别 + 数量聚合（`TANK ×30`）。

截图：`img/2026-11-16-swap-labels-category.png`（类别模式，干净）、`img/2026-11-16-swap-labels-t300.png`（型号模式，玩家名齐全但拥挤）、`img/2026-11-16-swap-labels.png`（早期一版）。

## 规则（已写进代码）

- 同一玩家多段航迹时，每段标签显示**当段载具**，第二行显示玩家名；只有“该玩家在本回放有 ≥2 条不同 uid 航迹”才标名字（`multiPlayers`）。
- 本机副行取“当前时刻覆盖、且最近开始”的那一段；都结束了取最后一段。
- 这只是**显示约定**，不新增数据字段；数据来自 `web/wrpl-parse.js` 的 `player` / `vehicleName`。

## 不能证明

- 拥挤程度是主观判断，脚本给不出结论；`model` 模式下 100+ 地面单位同时可见时会重叠，需要人看一眼决定是否收敛。
