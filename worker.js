// PDF処理用のワーカー：ブラウザ内の Python（Pyodide）で PyMuPDF を動かす。
// 画面（index.html）とは postMessage でやりとりし、PDFは外部に送らない。
'use strict';
const PYODIDE = 'https://cdn.jsdelivr.net/pyodide/v0.28.3/full/';
importScripts(PYODIDE + 'pyodide.js');

const base = new URL('./', self.location.href).href;
const loadedFonts = new Set();
let core;

const ready = (async () => {
  const py = await loadPyodide({indexURL: PYODIDE});
  await py.loadPackage('pymupdf');
  py.FS.mkdirTree('/fonts');
  const src = await (await fetch(base + 'pdfcore.py', {cache: 'no-cache'})).text();
  py.FS.writeFile('/home/pyodide/pdfcore.py', src);
  core = py.pyimport('pdfcore');
  core.py = py;
})();

async function ensureFonts(names){
  for (const n of names){
    if (loadedFonts.has(n)) continue;
    const r = await fetch(base + 'fonts/' + n);
    if (!r.ok) throw new Error('フォントを読み込めません: ' + n);
    core.py.FS.writeFile('/fonts/' + n, new Uint8Array(await r.arrayBuffer()));
    loadedFonts.add(n);
  }
}

// PyProxy の bytes → Uint8Array（コピーして Python 側を解放）
function toBytes(b){ const u = b.toJs(); b.destroy(); return u; }

const handlers = {
  async open({data, name}){ return JSON.parse(core.api_open(data, name)); },
  async image({data}){ return JSON.parse(core.api_image(data)); },
  async render(q){ return toBytes(core.api_render(JSON.stringify(q))); },
  async spans(q){ return JSON.parse(core.api_spans(JSON.stringify(q))); },
  async export(spec){
    const s = JSON.stringify(spec);
    await ensureFonts(JSON.parse(core.api_fonts(s)));
    return toBytes(core.api_export(s));
  },
  async raster(q){ return toBytes(core.api_raster(JSON.stringify(q))); },
  async text({src}){ return core.api_text(src); },
  async close({src}){ core.api_close(src); return true; },
};

self.onmessage = async e => {
  const {id, cmd, args} = e.data;
  try {
    await ready;
    const result = await handlers[cmd](args);
    const transfer = result instanceof Uint8Array ? [result.buffer] : [];
    self.postMessage({id, result}, transfer);
  } catch (err){
    let msg = String(err && err.message || err);
    // Python の例外は最後の行が本文
    const lines = msg.trim().split('\n'); msg = lines[lines.length - 1];
    self.postMessage({id, error: msg});
  }
};
ready.then(() => self.postMessage({ready: true}), err => self.postMessage({fatal: String(err)}));
