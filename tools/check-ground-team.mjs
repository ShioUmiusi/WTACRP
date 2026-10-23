#!/usr/bin/env node
/* 地面阵营自检：直接解析 .wrpl，与上游 wrpl-acmi 的 -list CSV 对比。
 *
 * 用法：
 *   node tools/check-ground-team.mjs "<replay.wrpl>" "<wrpl-acmi -list 生成的.csv>"
 *
 * 检查：
 *   - 每个能对上 CSV 的地面单位，`groundTeam` 必须等于 1（Blue）/ 2（Red）；
 *   - 如果回放结算里有作者 team，`side` 必须等于 (team === authorTeam ? ally : enemy)。
 * 退出码非 0 表示解析规则与上游不一致。
 */
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const here = dirname(fileURLToPath(import.meta.url));
const fz = require(join(here, '..', 'web', 'vendor', 'fzstd.min.js'));
globalThis.fzstd = fz;
require(join(here, '..', 'web', 'wrpl-parse.js'));

const [replayPath, csvPath] = process.argv.slice(2);
if (!replayPath || !csvPath) {
  console.error('用法: node tools/check-ground-team.mjs <replay.wrpl> <list.csv>');
  process.exit(2);
}

// CSV: object_id = ground:(1<<63 | uid)，team = Blue/Red
const truth = {};
for (const line of readFileSync(resolve(csvPath), 'utf8').split(/\r?\n/)) {
  if (!line.startsWith('ground:')) continue;
  const cols = line.split(',');
  if (cols[1] !== 'ground') continue;
  // object_id 是 2^63+uid 的十进制串；JS number 在 2^63 处精度只剩 1024，用 BigInt 减。
  const uid = Number(BigInt(cols[0].split(':')[1]) - (1n << 63n));
  truth[uid] = cols[3] === 'Blue' ? 1 : (cols[3] === 'Red' ? 2 : 0);
}

const buf = new Uint8Array(readFileSync(resolve(replayPath)));
const m = await globalThis.WRPLParse._internal.parseMain(buf, { fileName: replayPath });
const authorTeam = m.meta.player ? m.meta.player.team : null;
const grounds = m.units.filter(u => u.domain === 'ground');

let checked = 0, teamOk = 0, sideOk = 0, sideChecked = 0, unknown = 0, missing = 0;
const bad = [];
for (const u of grounds) {
  if (u.groundUid == null || truth[u.groundUid] == null) { unknown++; continue; }
  checked++;
  const want = truth[u.groundUid];
  if (u.groundTeam == null) { missing++; }
  else if (u.groundTeam === want) teamOk++;
  else bad.push({ key: u.key, uid: u.groundUid, got: u.groundTeam, want });
  if (u.groundTeam != null && (authorTeam === 1 || authorTeam === 2)) {
    sideChecked++;
    const wantSide = want === authorTeam ? 'ally' : 'enemy';
    if (u.side === wantSide) sideOk++;
    else bad.push({ key: u.key, side: u.side, wantSide, team: want, authorTeam });
  }
}

console.log(`ground units ${grounds.length}（无 CSV 对应 ${unknown}）`);
console.log(`team: ${teamOk}/${checked} 一致`);
console.log(authorTeam === 1 || authorTeam === 2
  ? `side: ${sideOk}/${sideChecked} 一致（authorTeam=${authorTeam}）`
  : 'side: 跳过（结算里没有作者 team）');
if (bad.length) {
  console.error('不一致示例:', bad.slice(0, 8));
  process.exit(1);
}
if (checked === 0) { console.error('没有任何可比对的地面单位'); process.exit(1); }
console.log('地面阵营自检通过');
