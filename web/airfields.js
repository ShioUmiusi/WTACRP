/* 机场目录选择器：把上游 wrpl-acmi 的 airfields 目录折成渲染器要的 runways[]。
 * 纯函数（不 fetch），页面负责加载 terrain/airfields.json；Node 也能直接测。
 *
 * 选择顺序：
 *   1) 回放自己解析出的机场标记名（meta.markerNames）——实际生成的布局，最准；
 *   2) 没有标记名时退而用非条件项（不随 rank 变化的那批）；
 *   3) 仍然没有就按「同名取 rank 范围最宽的一条」去重，避免把多个 rank 变体全画出来。
 * 坐标已是回放世界米（x=东、y=北、z=上），与 units 同框架。
 */
(function (g) {
  'use strict';

  function normLevel(lvl) {
    return String(lvl || '').split('/').pop().replace(/\.bin$/i, '');
  }

  function headingDeg(x1, y1, x2, y2) {
    var d = Math.atan2(x2 - x1, y2 - y1) * 180 / Math.PI;
    return (d % 360 + 360) % 360;
  }

  function toRunway(r) {
    var length = Math.hypot(r.x2 - r.x1, r.y2 - r.y1);
    var cx = (r.x1 + r.x2) / 2, cy = (r.y1 + r.y2) / 2;
    var cz = (r.z1 + r.z2) / 2;
    return {
      key: String(r.name || 'runway'), name: String(r.name || 'RUNWAY'),
      kind: 'runway', source: 'airfields-catalogue',
      x1: r.x1, y1: r.y1, x2: r.x2, y2: r.y2, z1: r.z1, z2: r.z2,
      centerX: Math.round(cx * 10) / 10, centerY: Math.round(cy * 10) / 10,
      centerZ: Math.round(cz * 10) / 10,
      lengthM: Math.round(length * 10) / 10, widthM: r.width || null,
      headingDeg: Math.round(headingDeg(r.x1, r.y1, r.x2, r.y2) * 10) / 10,
      team: r.team == null ? null : r.team,
      altM: r.alt == null ? cz : r.alt
    };
  }

  function dedupe(list) {
    var out = [], seen = {};
    (list || []).forEach(function (r) {
      var k = [r.name, Math.round(r.x1), Math.round(r.y1),
               Math.round(r.x2), Math.round(r.y2)].join('|');
      if (seen[k]) return;
      seen[k] = 1;
      out.push(r);
    });
    return out;
  }

  function pick(catalog, mission) {
    if (!catalog || !mission || !mission.meta) return null;
    var map = normLevel(mission.meta.replayHeader && mission.meta.replayHeader.level);
    var entry = catalog.maps && catalog.maps[map];
    if (!entry || !(entry.missions || []).length) return null;

    var hdr = mission.meta.replayHeader || {};
    var loc = (String(hdr.locName || '') + ' ' + String(hdr.battleType || '')).toLowerCase();
    var best = null, bestLen = -1;
    entry.missions.forEach(function (m) {
      var name = String(m.mission || '').toLowerCase();
      if (!name || loc.indexOf(name) < 0) return;
      if (name.length > bestLen) { best = m; bestLen = name.length; }
    });
    if (!best) best = entry.missions[0];      /* 没有名字线索时用第一个任务，下面还有标记名兜底 */

    var all = (best.runways || []).slice();
    var markers = (mission.meta.markerNames || []).map(function (s) { return String(s); });
    var chosen, how;
    if (markers.length) {
      chosen = all.filter(function (r) { return markers.indexOf(String(r.name)) >= 0; });
      how = 'marker-names';
      if (!chosen.length) chosen = all.filter(function (r) { return !r.conditional; });
      if (!chosen.length) chosen = all;
    } else {
      chosen = all.filter(function (r) { return !r.conditional; });
      how = 'non-conditional';
      if (!chosen.length) {
        var byName = {};
        all.forEach(function (r) {
          var key = String(r.name);
          var span = r.rank ? (r.rank[1] - r.rank[0]) : -1;
          if (!byName[key] || span > byName[key].span) byName[key] = { span: span, r: r };
        });
        chosen = Object.keys(byName).map(function (k) { return byName[k].r; });
        how = 'widest-rank';
      }
    }
    chosen = dedupe(chosen);
    if (!chosen.length) return null;
    var runways = chosen.map(toRunway);
    return {
      map: map, mission: best.mission, how: how,
      markerNames: markers, sourceCount: all.length,
      runways: runways,
      airfields: runways.map(function (r) {
        return { key: r.key, name: r.name, x: r.centerX, y: r.centerY, z: r.altM };
      })
    };
  }

  g.ACE_AIRFIELDS = { pick: pick };
})(typeof window !== 'undefined' ? window : globalThis);
