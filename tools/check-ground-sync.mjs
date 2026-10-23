#!/usr/bin/env node
/* 地面同步解析自检（`ptype=4` MPI 0xF073/0xF074）。
 *
 * 用法：
 *   node tools/check-ground-sync.mjs <解压后的包流.bin> [python参考.json]
 *
 * 只检查回放内部可验证的性质，不依赖 8111 / 网络：
 *   - 包数、uid 数、采样数 > 0，且解析失败数为 0；
 *   - 所有位置有限且在 ±40000 m 内，高度在 -500..9000 m；
 *   - 每个 uid 的采样时刻不递减；
 *   - 可选：与独立实现的 JSON（uid -> [[tick,x,alt,z],…]）逐采样比对，≤0.01 m。
 *
 * 失败时以非 0 退出，便于 CI / 改完解析后回归。
 */
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const here = dirname(fileURLToPath(import.meta.url));
require(join(here, '..', 'web', 'wrpl-parse.js'));

const payloadPath = process.argv[2];
if (!payloadPath) {
  console.error('用法: node tools/check-ground-sync.mjs <payload.bin> [ref.json]');
  process.exit(2);
}

const buf = new Uint8Array(readFileSync(resolve(payloadPath)));
const P = globalThis.WRPLParse;
let fail = 0;
const check = (ok, msg) => { if (!ok) { fail++; console.error('FAIL', msg); } else { console.log('ok  ', msg); } };

const pk = P._internal.parsePackets(buf);
check(pk.length > 0, `分帧 ${pk.length} 个包，用尽 ${pk.reduce((a, p) => a + (p.end - p.start), 0)} 字节`);

const gs = P._internal.parseGroundSync(buf, pk);
check(gs.stats.packets > 0, `地面同步包 ${gs.stats.packets}`);
check(gs.stats.uids > 0, `有轨迹的 uid ${gs.stats.uids}`);
check(gs.stats.samples > 0, `位置采样 ${gs.stats.samples}`);
check(gs.stats.errors === 0, `解析失败 ${gs.stats.errors}（应 0）`);

let rangeBad = 0, monotonicBad = 0;
const uids = Object.keys(gs.samples);
for (const uid of uids) {
  const s = gs.samples[uid];
  for (let i = 0; i < s.length; i++) {
    const [t, x, n, alt] = s[i];
    if (![t, x, n, alt].every(Number.isFinite) || Math.abs(x) > 40000 || Math.abs(n) > 40000 ||
        alt < -500 || alt > 9000) rangeBad++;
    if (i && s[i][0] < s[i - 1][0]) monotonicBad++;
  }
}
check(rangeBad === 0, `位置/高度范围异常 ${rangeBad}`);
check(monotonicBad === 0, `时刻回退 ${monotonicBad}`);

const refPath = process.argv[3];
if (refPath) {
  const ref = JSON.parse(readFileSync(resolve(refPath), 'utf8'));
  let maxDiff = 0, compared = 0, missing = 0;
  for (const uid of uids) {
    const a = gs.samples[uid], b = ref[uid];
    if (!b) { missing++; continue; }
    if (a.length !== b.length) { missing++; continue; }
    for (let i = 0; i < a.length; i++) {
      maxDiff = Math.max(maxDiff, Math.abs(a[i][0] - b[i][0]), Math.abs(a[i][1] - b[i][1]),
                         Math.abs(a[i][2] - b[i][3]), Math.abs(a[i][3] - b[i][2]));
      compared++;
    }
  }
  check(missing === 0, `与参考实现 uid 一致（缺 ${missing} 个）`);
  check(compared > 0 && maxDiff <= 0.01, `与参考实现逐采样比对 ${compared} 个，最大差 ${maxDiff.toFixed(5)} m`);
}

if (fail) { console.error(`\n${fail} 项失败`); process.exit(1); }
console.log('\n地面同步解析自检通过');
