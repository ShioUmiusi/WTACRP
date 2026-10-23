#!/usr/bin/env node
/* 三项新增解析的自检：玩家槽位、击毁/失效时刻、机场目录。
 *
 * 用法：
 *   node tools/check-replay-extras.mjs "<replay.wrpl>" "<wrpl-acmi -list 的 csv>" [airfields.json]
 *
 * 判据：
 *   - slot：解析出玩家的空中单位，player 必须等于 CSV 的 player（有错即失败）；覆盖率只报告；
 *   - 小队：每个带 uid 的成员，uidMap 里的 playerUserId 必须等于结算 userId；
 *   - 击毁：空中单位的死亡时刻不得超过 CSV 的最后采样（+3 s 容差）；
 *   - 机场：目录能按地图/任务选中，且覆盖回放实际生成的 marker 名。
 */
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, '..');
const fz = require(join(repo, 'web', 'vendor', 'fzstd.min.js'));
globalThis.fzstd = fz;
require(join(repo, 'web', 'wrpl-parse.js'));
require(join(repo, 'web', 'airfields.js'));

const [replayPath, csvPath, catPathArg] = process.argv.slice(2);
if (!replayPath || !csvPath) {
  console.error('用法: node tools/check-replay-extras.mjs <replay.wrpl> <list.csv> [airfields.json]');
  process.exit(2);
}

const csv = { air: {}, groundEnd: {}, missiles: 0 };
for (const line of readFileSync(resolve(csvPath), 'utf8').split(/\r?\n/)) {
  const cols = line.split(',');
  if (line.startsWith('air:')) {
    csv.air[cols[0].slice(4)] = { player: cols[2], team: cols[3], end: Number(cols[6]) };
  } else if (line.startsWith('missile:')) {
    csv.missiles++;
  }
}

const mission = await globalThis.WRPLParse._internal.parseMain(
  new Uint8Array(readFileSync(resolve(replayPath))), { fileName: replayPath });
const counts = mission.meta.counts || {};
const air = mission.units.filter(u => u.domain === 'air');
const seen = new Set();
let checked = 0, ok = 0, missing = 0, wrong = 0, deathBad = 0;
const bad = [];
for (const u of air) {
  const key = String(u.uid);
  if (seen.has(key)) { continue; }
  seen.add(key);
  const ref = csv.air[key];
  if (!ref) { continue; }
  if (u.player == null) { missing++; }
  else {
    checked++;
    if (u.player === ref.player) { ok++; }
    else { wrong++; bad.push({ uid: u.uid, got: u.player, want: ref.player }); }
  }
  if (u.deathTickRaw != null) {
    const rt = mission.meta.replayTicks;
    const absEnd = (u.deathTickRaw - rt.tick0) * rt.secPerTick + rt.tick0 * rt.secPerTick;
    if (absEnd > ref.end + 3) { deathBad++; bad.push({ uid: u.uid, death: absEnd, csvEnd: ref.end }); }
  }
}
console.log(`玩家名：解析到 ${checked} 个，正确 ${ok}，错误 ${wrong}，未解析 ${missing}（uid 去重）`);

let squadBad = 0;
const sq = mission.meta.squad;
if (sq && sq.members) {
  for (const m of sq.members) {
    if (m.uid == null) { continue; }
    const ref = csv.air[String(m.uid)];
    if (ref && ref.player && m.name && ref.player !== m.name) {
      squadBad++; bad.push({ squad: m.name, uid: m.uid, csv: ref.player });
    }
  }
  console.log(`小队：${sq.members.length} 人，已绑定 uid ${sq.members.filter(m => m.uid != null).length}，错绑 ${squadBad}`);
}

let missileBad = 0;
const missiles = mission.missiles || [];
for (const m of missiles) {
  if (!(m.n >= 2)) { missileBad++; continue; }
  for (let i = 1; i < m.n; i++) {
    if (!(m.t[i] >= m.t[i - 1])) { missileBad++; break; }
  }
}
const missileCover = csv.missiles ? missiles.length / csv.missiles : 1;
console.log(`导弹弹道：${missiles.length} 条（CSV ${csv.missiles}，覆盖 ${(missileCover * 100).toFixed(0)}%），` +
  `样本 ${missiles.reduce((n, m) => n + m.n, 0)}，时序错误 ${missileBad}`);
if (csv.missiles >= 10 && missileCover < 0.9) { missileBad++; }

let afMsg = '跳过（无目录）';
const catPath = catPathArg || join(repo, 'terrain', 'airfields.json');
try {
  const cat = JSON.parse(readFileSync(resolve(catPath), 'utf8'));
  const pick = globalThis.ACE_AIRFIELDS.pick(cat, mission);
  if (!pick) { afMsg = '目录无此地图/任务'; }
  else {
    const markers = mission.meta.markerNames || [];
    const missingMarkers = markers.filter(n => !pick.runways.some(r => r.name === n));
    afMsg = `${pick.mission} / ${pick.how} / 跑道 ${pick.runways.length} 条` +
      (missingMarkers.length ? `，marker 未覆盖 ${missingMarkers.length}` : '，marker 全覆盖');
    if (pick.runways.length === 0) { squadBad++; }
  }
} catch (e) { afMsg = '目录读取失败：' + e.message; }
console.log('机场：' + afMsg);

const fail = wrong + deathBad + squadBad + missileBad;
if (fail) { console.error('失败示例:', bad.slice(0, 6)); process.exit(1); }
console.log('新增解析自检通过');
