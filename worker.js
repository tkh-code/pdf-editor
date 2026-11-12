// PDF処理用のワーカー：ブラウザ内の Python（Pyodide）で PyMuPDF を動かす。
// 画面（index.html）とは postMessage でやりとりし、PDFは外部に送らない。
'use strict';
const PYODIDE = 'https://cdn.jsdelivr.net/pyodide/v0.28.3/full/';
importScripts(PYODIDE + 'pyodide.js');

const base = new URL('./', self.location.href).href;
const loadedFonts = new Set();
const srcFiles = {};   // 読み込んだファイルの仮想パス（閉じるときに消す）
let core, nFile = 0;

const ready = (async () => {
  const py = await loadPyodide({indexURL: PYODIDE});
  await py.loadPackage('pymupdf');
  py.FS.mkdirTree('/fonts'); py.FS.mkdirTree('/in'); py.FS.mkdirTree('/out');
  const src = await (await fetch(base + 'pdfcore.py?' + Date.now(), {cache: 'no-store'})).text();
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
  // ファイルは仮想ファイル（ブラウザ側のメモリ）に置いて開く。Python 側に丸ごと複製しないので
  // 1GB を超える計算書でも開いて保存できる。
  async open({file, data, name}){
    if (file) data = new Uint8Array(await file.arrayBuffer());
    const ext = (name.match(/\.[A-Za-z0-9]+$/) || ['.pdf'])[0].toLowerCase();
    const path = `/in/f${++nFile}${ext}`;
    core.py.FS.writeFile(path, data, {canOwn: true});
    try {
      const info = JSON.parse(core.api_open_path(path, name));
      srcFiles[info.id] = path;
      return info;
    } catch (err){ try { core.py.FS.unlink(path); } catch(e){} throw err; }
  },
  async image({data}){ return JSON.parse(core.api_image(data)); },
  async render(q){ return toBytes(core.api_render(JSON.stringify(q))); },
  async spans(q){ return JSON.parse(core.api_spans(JSON.stringify(q))); },
  async search(q){ return JSON.parse(core.api_search(JSON.stringify(q))); },
  async textin(q){ return core.api_textin(JSON.stringify(q)); },
  async hastext(q){ return JSON.parse(core.api_hastext(JSON.stringify(q))); },
  async export(spec){
    const s = JSON.stringify(spec);
    await ensureFonts(JSON.parse(core.api_fonts(s)));
    const path = `/out/o${Date.now()}.pdf`;
    core.api_export_file(s, path);
    const data = core.py.FS.readFile(path);
    core.py.FS.unlink(path);
    return data;
  },
  async raster(q){ return toBytes(core.api_raster(JSON.stringify(q))); },
  async text({src}){ return core.api_text(src); },
  async close({src}){
    core.api_close(src);
    if (srcFiles[src]){ try { core.py.FS.unlink(srcFiles[src]); } catch(e){} delete srcFiles[src]; }
    return true;
  },
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
    if (/memory|OOM|bad_alloc|Aborted/i.test(msg)) msg = 'メモリが足りません（ファイルが大きすぎます）。ページを分けて保存してください。';
    self.postMessage({id, error: msg});
  }
};
ready.then(() => self.postMessage({ready: true}), err => self.postMessage({fatal: String(err)}));
