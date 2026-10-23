import assert from 'node:assert';
import { ribbonWindow, missileVisibleStart, TRAIL_FADE_DEAD } from '../three3d/trail-window.js';

assert.equal(ribbonWindow(22, -1), 22, 'alive keeps the window');
assert.equal(ribbonWindow(60, 0), 60, 'death instant keeps the window');
assert.equal(ribbonWindow(22, TRAIL_FADE_DEAD / 2), 11, 'halfway halves the window');
assert.equal(ribbonWindow(22, TRAIL_FADE_DEAD), 0, 'gone after the fade');
assert.equal(ribbonWindow(22, 99), 0, 'stays gone');

assert.equal(missileVisibleStart(100, -1), 0, 'before impact: nothing trimmed');
assert.equal(missileVisibleStart(100, 0), 0, 'at impact: nothing trimmed');
assert.equal(missileVisibleStart(100, 2.5), 50, 'halfway: half the tail dropped');
assert.equal(missileVisibleStart(100, 5), 100, 'after the fade: all dropped');
assert.equal(missileVisibleStart(100, 9), 100, 'stays dropped');

console.log('trail window ok');
