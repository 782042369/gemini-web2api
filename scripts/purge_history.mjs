#!/usr/bin/env node
/* 单轮删除：连 llq CDP 桥，用 UI 路径删掉侧栏第一个会话项。
 * 输出一行 JSON：{"status":"deleted|skip-notebook|exhausted|no-at|...","title":"...","remain":N}
 * 退出码 0（exhausted/正常）或 1（链路/会话异常）。
 */
import { readFileSync } from 'fs';

const BRIDGE = 'http://127.0.0.1:9229';

const PAGE_JS = `(async function () {
  try {
    var links = Array.from(document.querySelectorAll('gem-nav-list-item a[aria-label]'));
    var titles = links.map(function (x) { return x.getAttribute('aria-label'); });
    for (var li = 0; li < links.length; li++) {
      var item = links[li].closest('gem-nav-list-item');
      ['pointerover', 'mouseover', 'mouseenter'].forEach(function (t) {
        item.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true }));
      });
      await new Promise(function (rs) { setTimeout(rs, 700); });
      var icon = item.querySelector('mat-icon[fonticon="more_vert"]');
      var btn = icon ? icon.closest('button') : null;
      if (!btn) continue;
      btn.click();
      await new Promise(function (rs) { setTimeout(rs, 1200); });
      var menu = document.querySelector('mat-menu-content') || document.querySelector('.mat-mdc-menu-panel');
      if (!menu) continue;
      var menuTexts = Array.from(menu.querySelectorAll('button')).map(function (b) { return (b.textContent || '').trim(); });
      var isChat = menuTexts.indexOf('分享对话内容') >= 0;
      var delBtn = menuTexts.indexOf('删除') >= 0 ? Array.from(menu.querySelectorAll('button')).find(function (b) { return (b.textContent || '').trim() === '删除'; }) : null;
      if (!isChat || !delBtn) {
        document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', keyCode: 27, bubbles: true }));
        await new Promise(function (rs) { setTimeout(rs, 400); });
        return JSON.stringify({ status: 'skip-notebook', title: (titles[li] || '').slice(0, 25) });
      }
      delBtn.click();
      await new Promise(function (rs) { setTimeout(rs, 1200); });
      var dlg = document.querySelector('mat-dialog-container, [role=dialog]');
      if (!dlg) continue;
      var confirmBtn = Array.from(dlg.querySelectorAll('button')).find(function (b) {
        return (b.textContent || '').trim() === '删除';
      });
      if (!confirmBtn) continue;
      confirmBtn.click();
      await new Promise(function (rs) { setTimeout(rs, 1600); });
      return JSON.stringify({ status: 'deleted', title: (titles[li] || '').slice(0, 25), remain: document.querySelectorAll('gem-nav-list-item a[aria-label]').length });
    }
    return JSON.stringify({ status: 'exhausted', remain: document.querySelectorAll('gem-nav-list-item a[aria-label]').length });
  } catch (e) {
    return JSON.stringify({ status: 'exc: ' + String(e).slice(0, 120) });
  }
})()`;

async function main() {
  let targets;
  try {
    targets = await (await fetch(BRIDGE + '/json/list', { signal: AbortSignal.timeout(8000) })).json();
  } catch (e) {
    console.log(JSON.stringify({ status: 'bridge-unreachable' }));
    process.exit(1);
  }
  const page = targets.find(t => t.type === 'page' && (t.url || '').includes('gemini.google.com'));
  if (!page) {
    console.log(JSON.stringify({ status: 'no-gemini-tab' }));
    process.exit(1);
  }
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  const done = new Promise((res, rej) => {
    const to = setTimeout(() => rej(new Error('ws timeout')), 90000);
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id === 1) {
        clearTimeout(to);
        res(m);
      }
    };
    ws.onerror = () => { clearTimeout(to); rej(new Error('ws error')); };
  });
  await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
  ws.send(JSON.stringify({ id: 1, method: 'Runtime.evaluate', params: { expression: PAGE_JS, awaitPromise: true, returnByValue: true } }));
  const m = await done;
  try { ws.close(); } catch (e) {}
  const value = m.result && m.result.result ? m.result.result.value : null;
  if (!value) {
    console.log(JSON.stringify({ status: 'eval-err' }));
    process.exit(1);
  }
  console.log(value);
  process.exit(0);
}
main().catch(e => { console.log(JSON.stringify({ status: 'fatal: ' + String(e).slice(0, 100) })); process.exit(1); });
