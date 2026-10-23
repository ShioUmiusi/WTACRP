/* 航迹 / 弹道“结束后缩短”的窗口计算（纯函数，Node 可直接测）。 */

export const TRAIL_FADE_DEAD = 6;   /* 阵亡后飘带继续缩短到消失的秒数 */
export const MISSILE_FADE = 5;      /* 导弹命中后弹道缩短到消失的秒数 */

/** 飘带回看窗口（秒）。post 为死亡后的秒数，<= 0 表示仍存活。 */
export function ribbonWindow(baseWin, post) {
  if (!(post > 0)) { return baseWin; }
  return baseWin * Math.max(0, 1 - post / TRAIL_FADE_DEAD);
}

/** 导弹命中后要跳过的尾部 dash 数。count 为总段数，post 为命中后秒数。 */
export function missileVisibleStart(count, post) {
  if (!(post > 0)) { return 0; }
  const keep = 1 - post / MISSILE_FADE;
  if (keep <= 0) { return count; }
  return Math.min(count, Math.floor((1 - keep) * count));
}
