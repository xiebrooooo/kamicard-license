/* 跨语言一致性验证：手机版签发器(纯 JS) ↔ Python 版 license.py
 *
 * 两边必须是**字节级一致**的 —— 手机签出来的卡，软件必须认。
 * 一旦有人改了其中一边的密钥或字段布局，这个脚本会立刻报错。
 *
 * 跑法（需要 node，Python 部分由 test_parity.py 调用）：
 *     node tests/test_js_parity.js
 * 或直接：
 *     python tests/test_parity.py        ← 自动调 node，推荐
 *
 * 输出一行 JSON：{ok, machine, card}
 */
'use strict';

const fs = require('fs');
const path = require('path');

const PKG = path.resolve(__dirname, '..');
const HTML = path.join(PKG, 'issuer', 'ui', 'card_tool_mobile.html');

// ---- 把 html 里那段 <script> 抠出来 ----
const html = fs.readFileSync(HTML, 'utf8');
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) {
  console.error('没找到 <script> 块');
  process.exit(2);
}
let code = m[1];

// ---- 喂一套最小 DOM 桩：脚本顶层会绑定按钮事件、读写 localStorage ----
// 用 Proxy 吞掉所有属性访问/赋值/调用，避免为了跑一个纯函数而拖进整个 DOM。
function stub(name) {
  const f = function () { return stub(name + '()'); };
  return new Proxy(f, {
    get(t, k) {
      if (k === 'toString' || k === Symbol.toPrimitive) return () => '';
      if (k === 'length') return 0;
      if (k === Symbol.iterator) return function* () {};
      return stub(name + '.' + String(k));
    },
    set() { return true; },
    apply() { return stub(name + '()'); },
    has() { return true; },
  });
}

const sandbox = {
  document: stub('document'),
  window: {},
  navigator: stub('navigator'),
  localStorage: {
    _d: {},
    getItem(k) { return this._d[k] === undefined ? null : this._d[k]; },
    setItem(k, v) { this._d[k] = String(v); },
    removeItem(k) { delete this._d[k]; },
  },
  crypto: globalThis.crypto,           // Node 22 自带 webcrypto
  setTimeout: () => 0,
  clearTimeout: () => {},
  alert: () => {},
  console,
};

// 把代码包成函数，注入桩对象
const runner = new Function(
  'document', 'window', 'navigator', 'localStorage', 'crypto',
  'setTimeout', 'clearTimeout', 'alert', 'console',
  code + '\n;return { makeCard, parseCard, machineFp, KEY };'
);

let api;
try {
  api = runner(sandbox.document, sandbox.window, sandbox.navigator,
               sandbox.localStorage, sandbox.crypto, sandbox.setTimeout,
               sandbox.clearTimeout, sandbox.alert, sandbox.console);
} catch (e) {
  console.error('执行脚本失败:', e.message);
  process.exit(3);
}

// ---- 用**固定 nonce 和固定时间**签发，保证可以和 Python 逐字节对比 ----
const MACHINE = '89b6a98b26c5628f';
const HOURS = 24;
const NOW = 1800000000;                 // 固定时间戳
const NONCE = new Uint8Array([0x12, 0x34]);

const card = api.makeCard(HOURS, MACHINE, NONCE, NOW);

// 顺带自检：解析回来要能对上
const parsed = api.parseCard(card);
if (!parsed || parsed.status !== 'ok') {
  console.error('JS 自己解析不回来:', JSON.stringify(parsed));
  process.exit(4);
}

console.log(JSON.stringify({
  ok: true,
  machine: MACHINE,
  hours: HOURS,
  now: NOW,
  nonce: '1234',
  card,
  parsed_expires_at: parsed.expires_at,
}));
