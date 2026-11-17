// ==UserScript==
// @name         Amazon 商品图下载
// @namespace    https://github.com/GZhonghui/Draft
// @version      0.1.1
// @description  在 Amazon 商品页一键下载主图、各颜色主图和 A+ 详情图（原图分辨率），打包成一个 ZIP。无依赖、不联网到第三方。
// @author       GZhonghui
// @match        https://www.amazon.com/*
// @match        https://www.amazon.co.jp/*
// @match        https://www.amazon.co.uk/*
// @match        https://www.amazon.de/*
// @match        https://www.amazon.fr/*
// @match        https://www.amazon.it/*
// @match        https://www.amazon.es/*
// @match        https://www.amazon.nl/*
// @match        https://www.amazon.se/*
// @match        https://www.amazon.pl/*
// @match        https://www.amazon.com.be/*
// @match        https://www.amazon.com.tr/*
// @match        https://www.amazon.ca/*
// @match        https://www.amazon.com.mx/*
// @match        https://www.amazon.com.br/*
// @match        https://www.amazon.com.au/*
// @match        https://www.amazon.in/*
// @match        https://www.amazon.sg/*
// @match        https://www.amazon.ae/*
// @match        https://www.amazon.sa/*
// @match        https://www.amazon.eg/*
// @grant        none
// @run-at       document-idle
// @noframes
// ==/UserScript==

(function () {
  'use strict';

  // ---------- 从页面里收集图片 ----------

  // 读取 parseJSON('...') 里的单引号字符串字面量，返回反转义后的 JSON 文本。
  function readParseJsonLiteral(text, fromIndex) {
    const marker = "parseJSON('";
    const start = text.indexOf(marker, fromIndex);
    if (start < 0) return null;
    let i = start + marker.length;
    const begin = i;
    while (i < text.length) {
      const c = text[i];
      if (c === '\\') { i += 2; continue; }
      if (c === "'") return unescapeJsString(text.slice(begin, i));
      i++;
    }
    return null;
  }

  // 按 JS 字符串字面量的规则反转义（页面里的 JSON 会带 \' 和 \\）。
  function unescapeJsString(s) {
    return s.replace(/\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|[\s\S])/g, (m, e) => {
      if ((e[0] === 'u' || e[0] === 'x') && e.length > 1) return String.fromCharCode(parseInt(e.slice(1), 16));
      switch (e) {
        case 'n': return '\n';
        case 'r': return '\r';
        case 't': return '\t';
        case 'b': return '\b';
        case 'f': return '\f';
        case 'v': return '\v';
        case '0': return '\0';
        default: return e;
      }
    });
  }

  function parseJsonSafe(text) {
    if (!text) return null;
    try { return JSON.parse(text); } catch (e) { return null; }
  }

  function toItems(list) {
    return (list || [])
      .map((img, i) => ({ url: img.hiRes || img.large || '', label: img.variant || '' }))
      .filter((it) => it.url);
  }

  // 当前款式的图集：'colorImages': { 'initial': A.$.parseJSON('[...]') }
  // 切换款式后 Amazon 可能追加新的脚本块，所以取最后一个。
  function collectCurrentGallery(scripts) {
    let items = [];
    for (const text of scripts) {
      const at = text.indexOf("'colorImages'");
      if (at < 0 || text.indexOf("'initial'", at) < 0) continue;
      const list = parseJsonSafe(readParseJsonLiteral(text, at));
      if (Array.isArray(list) && list.length) items = toItems(list);
    }
    return items;
  }

  // 各颜色主图：ImageBlockBTF 里的 "colorImages":{"颜色名":[...]}（服装类页面常见）。
  // 这里每个颜色只有 MAIN 一张，其余图要等用户切换颜色后才由 AJAX 加载。
  function collectAllColors(scripts) {
    let colors = [];
    for (const text of scripts) {
      if (text.indexOf('"colorImages":{') < 0) continue;
      const obj = parseJsonSafe(readParseJsonLiteral(text, 0));
      const map = obj && obj.colorImages;
      if (!map || typeof map !== 'object' || Array.isArray(map)) continue;
      const found = Object.keys(map)
        .map((name) => ({ name, items: toItems(map[name]) }))
        .filter((c) => c.items.length);
      if (found.length) colors = found;
    }
    return colors;
  }

  // 没有图集 JSON 时的兜底：主图上的 data-old-hires / data-a-dynamic-image。
  function collectFallbackGallery(doc) {
    const items = [];
    doc.querySelectorAll('#imageBlock img, #main-image-container img, #imgTagWrapperId img').forEach((img) => {
      let url = img.getAttribute('data-old-hires') || '';
      if (!url) {
        const dyn = parseJsonSafe(img.getAttribute('data-a-dynamic-image'));
        if (dyn) {
          url = Object.keys(dyn).sort((a, b) => area(dyn[b]) - area(dyn[a]))[0] || '';
        }
      }
      if (url && /\/images\/I\//.test(url)) items.push({ url, label: '' });
    });
    return items;
  }

  function area(size) {
    return Array.isArray(size) ? Number(size[0]) * Number(size[1]) : 0;
  }

  // A+ 详情图（含品牌故事），懒加载的真实地址在 data-src 里。
  function collectAplus(doc) {
    const items = [];
    const seen = new Set();
    doc.querySelectorAll('img').forEach((img) => {
      const url = img.getAttribute('data-src') || img.getAttribute('src') || '';
      if (!/\/images\/S\/aplus-media/.test(url) || seen.has(url)) return;
      seen.add(url);
      items.push({ url, label: '' });
    });
    return items;
  }

  function collect(doc) {
    const scripts = Array.from(doc.querySelectorAll('script:not([src])'), (s) => s.textContent || '');
    let main = collectCurrentGallery(scripts);
    if (!main.length) main = collectFallbackGallery(doc);
    const asinInput = doc.querySelector('#ASIN, input[name="ASIN"]');
    const urlAsin = (location.pathname.match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/) || [])[1];
    const titleEl = doc.querySelector('#productTitle');
    return {
      asin: (asinInput && asinInput.value) || urlAsin || 'amazon',
      title: titleEl ? titleEl.textContent.replace(/\s+/g, ' ').trim() : '',
      main,
      colors: collectAllColors(scripts),
      aplus: collectAplus(doc),
    };
  }

  // ---------- 图片地址换算 ----------

  // 商品图：.../images/I/<id>.<尺寸参数>.jpg → 去掉参数即卖家上传的原图（最大，但可能带白边）。
  // 'amazon' 模式保留 hiRes 原样（通常是 _SL1500_，Amazon 已自动裁掉白边）。
  // A+ 图：只保留裁剪框 CRx,y,w,h，去掉缩放，得到与页面显示一致的全分辨率图。
  function resolveUrl(url, mode) {
    url = url.split('?')[0];
    const product = url.match(/^(https?:\/\/[^/]+\/images\/I\/[^./]+)\.(?:[^/]*\.)?(jpe?g|png|gif|webp)$/i);
    if (product) return mode === 'amazon' ? url : `${product[1]}.${product[2]}`;
    const aplus = url.match(/^(https?:\/\/[^/]+\/images\/S\/.+\/[^./]+)\.__(.+?)___\.(\w+)$/);
    if (aplus) {
      const crop = aplus[2].match(/CR\d+,\d+,\d+,\d+/);
      return crop ? `${aplus[1]}.__${crop[0]}___.${aplus[3]}` : `${aplus[1]}.${aplus[3]}`;
    }
    return url;
  }

  function extOf(url) {
    const m = url.match(/\.(jpe?g|png|gif|webp)$/i);
    return m ? m[1].toLowerCase().replace('jpeg', 'jpg') : 'jpg';
  }

  function safeName(s) {
    return s.replace(/[\\/:*?"<>|\x00-\x1f]/g, '_').replace(/\s+/g, ' ').trim().slice(0, 80) || 'untitled';
  }

  function pad(n) {
    return String(n).padStart(2, '0');
  }

  // 按勾选项生成 {name, url} 列表，并按最终地址去重（当前款式通常也出现在颜色列表里）。
  function buildTasks(data, opts) {
    const tasks = [];
    const seen = new Set();
    const add = (folder, items) => {
      let n = 0;
      for (const it of items) {
        const url = resolveUrl(it.url, opts.mode);
        if (seen.has(url)) continue;
        seen.add(url);
        n++;
        const base = it.label ? `${pad(n)}_${safeName(it.label)}` : pad(n);
        tasks.push({ name: `${folder}/${base}.${extOf(url)}`, url });
      }
    };
    if (opts.main) add('main', data.main);
    if (opts.colors) data.colors.forEach((c) => add(`colors/${safeName(c.name)}`, c.items));
    if (opts.aplus) add('aplus', data.aplus);
    return tasks;
  }

  // ---------- ZIP（仅存储，不压缩；JPEG 再压缩也几乎没收益） ----------

  const CRC_TABLE = (() => {
    const t = new Uint32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      t[n] = c >>> 0;
    }
    return t;
  })();

  function crc32(bytes) {
    let c = 0xffffffff;
    for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
  }

  function buildZip(files) {
    const enc = new TextEncoder();
    const now = new Date();
    const time = (now.getHours() << 11) | (now.getMinutes() << 5) | (now.getSeconds() >> 1);
    const date = ((now.getFullYear() - 1980) << 9) | ((now.getMonth() + 1) << 5) | now.getDate();
    const body = [];
    const central = [];
    let offset = 0;
    for (const f of files) {
      const name = enc.encode(f.name);
      const crc = crc32(f.data);
      const size = f.data.length;
      const local = new DataView(new ArrayBuffer(30));
      local.setUint32(0, 0x04034b50, true);
      local.setUint16(4, 20, true);
      local.setUint16(6, 0x0800, true); // 文件名为 UTF-8
      local.setUint16(10, time, true);
      local.setUint16(12, date, true);
      local.setUint32(14, crc, true);
      local.setUint32(18, size, true);
      local.setUint32(22, size, true);
      local.setUint16(26, name.length, true);
      body.push(local, name, f.data);

      const entry = new DataView(new ArrayBuffer(46));
      entry.setUint32(0, 0x02014b50, true);
      entry.setUint16(4, 20, true);
      entry.setUint16(6, 20, true);
      entry.setUint16(8, 0x0800, true);
      entry.setUint16(12, time, true);
      entry.setUint16(14, date, true);
      entry.setUint32(16, crc, true);
      entry.setUint32(20, size, true);
      entry.setUint32(24, size, true);
      entry.setUint16(28, name.length, true);
      entry.setUint32(42, offset, true);
      central.push(entry, name);
      offset += 30 + name.length + size;
    }
    const centralSize = central.reduce((n, p) => n + p.byteLength, 0);
    const end = new DataView(new ArrayBuffer(22));
    end.setUint32(0, 0x06054b50, true);
    end.setUint16(8, files.length, true);
    end.setUint16(10, files.length, true);
    end.setUint32(12, centralSize, true);
    end.setUint32(16, offset, true);
    return new Blob([...body, ...central, end], { type: 'application/zip' });
  }

  // ---------- 下载 ----------

  async function mapLimit(items, limit, fn) {
    const results = new Array(items.length);
    let next = 0;
    const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
      while (next < items.length) {
        const i = next++;
        results[i] = await fn(items[i], i);
      }
    });
    await Promise.all(workers);
    return results;
  }

  async function fetchBytes(url) {
    const res = await fetch(url, { credentials: 'omit' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return new Uint8Array(await res.arrayBuffer());
  }

  function saveBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }

  async function downloadZip(data, opts, onProgress) {
    const tasks = buildTasks(data, opts);
    if (!tasks.length) throw new Error('没有选中任何图片');
    let done = 0;
    const failed = [];
    const results = await mapLimit(tasks, 4, async (t) => {
      try {
        return { name: t.name, data: await fetchBytes(t.url) };
      } catch (e) {
        failed.push(`${t.url}\t${e.message}`);
        return null;
      } finally {
        onProgress(++done, tasks.length);
      }
    });
    const files = results.filter(Boolean);
    if (!files.length) throw new Error('全部下载失败');
    if (failed.length) files.push({ name: '_failed.txt', data: new TextEncoder().encode(failed.join('\n')) });
    // 商品名截到 60 个字符：单个文件名系统上限约 255（有的系统按 UTF-8 字节算，中日文每字 3 字节），
    // 而 Amazon 标题常有 100–200 字，不截可能保存失败。60 是兼顾可读性和余量的经验值，直接硬切、不加省略号。
    const zipName = `${data.asin}${data.title ? ' ' + safeName(data.title).slice(0, 60) : ''}.zip`;
    saveBlob(buildZip(files), zipName);
    return { ok: files.length - (failed.length ? 1 : 0), failed: failed.length };
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
    }
  }

  // ---------- 界面 ----------

  const SETTINGS_KEY = 'zh-amazon-image-downloader';
  const DEFAULTS = { main: true, colors: false, aplus: true, mode: 'original' };

  function loadSettings() {
    try {
      return Object.assign({}, DEFAULTS, JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}'));
    } catch (e) {
      return Object.assign({}, DEFAULTS);
    }
  }

  function saveSettings(s) {
    try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(s)); } catch (e) { /* 无痕模式等 */ }
  }

  const STYLE = `
    :host { all: initial; }
    * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans", sans-serif; }
    .fab { position: fixed; right: 16px; bottom: 96px; z-index: 2147483000; padding: 8px 12px; border: 0; border-radius: 18px;
      background: #232f3e; color: #fff; font-size: 13px; cursor: pointer; box-shadow: 0 2px 8px rgba(0,0,0,.25); }
    .fab:hover { background: #37475a; }
    .panel { position: fixed; right: 16px; bottom: 140px; z-index: 2147483000; width: 300px; padding: 12px 14px; border-radius: 8px;
      background: #fff; color: #0f1111; font-size: 13px; line-height: 1.5; box-shadow: 0 4px 16px rgba(0,0,0,.25); }
    .panel[hidden] { display: none; }
    .head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; font-weight: 600; }
    .close { border: 0; background: none; font-size: 16px; cursor: pointer; color: #565959; }
    label { display: flex; align-items: center; gap: 6px; margin: 4px 0; cursor: pointer; }
    label .n { margin-left: auto; color: #565959; }
    label.off { color: #999; cursor: default; }
    .tip { margin: 2px 0 0 22px; color: #767676; font-size: 11px; line-height: 1.4; }
    .sec { margin-top: 8px; padding-top: 8px; border-top: 1px solid #e7e7e7; }
    .btns { display: flex; gap: 8px; margin-top: 10px; }
    .btns button { flex: 1; padding: 6px 0; border: 1px solid #d5d9d9; border-radius: 6px; background: #fff; font-size: 13px; cursor: pointer; }
    .btns button.primary { background: #ffd814; border-color: #fcd200; }
    .btns button:disabled { opacity: .5; cursor: default; }
    .status { margin-top: 8px; min-height: 1.5em; color: #565959; font-size: 12px; }
  `;

  function mountUi() {
    const host = document.createElement('div');
    host.id = 'zh-amazon-image-downloader';
    const root = host.attachShadow({ mode: 'open' });
    root.innerHTML = `
      <style>${STYLE}</style>
      <button class="fab" type="button">⬇ 商品图</button>
      <div class="panel" hidden>
        <div class="head"><span>下载商品图</span><button class="close" type="button" title="关闭">✕</button></div>
        <label data-k="main"><input type="checkbox" name="main">当前款式主图<span class="n"></span></label>
        <label data-k="colors"><input type="checkbox" name="colors">各颜色主图<span class="n"></span></label>
        <label data-k="aplus"><input type="checkbox" name="aplus">A+ 详情图<span class="n"></span></label>
        <div class="tip">页面只内嵌每个颜色的 1 张主图；要某个颜色的完整图集，先在页面上选中该颜色并刷新，再下“当前款式主图”。</div>
        <div class="sec">
          <label><input type="radio" name="mode" value="original">原图（最大，可能带白边）</label>
          <label><input type="radio" name="mode" value="amazon">Amazon 展示图（≈1500px，已裁白边）</label>
        </div>
        <div class="btns">
          <button class="primary" type="button" data-act="zip">下载 ZIP</button>
          <button type="button" data-act="copy">复制链接</button>
        </div>
        <div class="status"></div>
      </div>`;
    document.body.appendChild(host);

    const $ = (sel) => root.querySelector(sel);
    const panel = $('.panel');
    const status = $('.status');
    const settings = loadSettings();
    let data = null;
    let busy = false;

    const readOpts = () => ({
      main: $('input[name="main"]').checked,
      colors: $('input[name="colors"]').checked,
      aplus: $('input[name="aplus"]').checked,
      mode: $('input[name="mode"]:checked').value,
    });

    function refresh() {
      data = collect(document);
      const mainUrls = new Set(data.main.map((it) => resolveUrl(it.url, 'original')));
      const colorExtra = data.colors.reduce(
        (n, c) => n + c.items.filter((it) => !mainUrls.has(resolveUrl(it.url, 'original'))).length, 0);
      const counts = {
        main: data.main.length ? `${data.main.length} 张` : '',
        colors: data.colors.length ? `${data.colors.length} 色 / 新增 ${colorExtra} 张` : '',
        aplus: data.aplus.length ? `${data.aplus.length} 张` : '',
      };
      for (const k of ['main', 'colors', 'aplus']) {
        const label = $(`label[data-k="${k}"]`);
        const box = label.querySelector('input');
        const has = k === 'colors' ? data.colors.length > 0 : data[k].length > 0;
        box.disabled = !has;
        box.checked = has && settings[k];
        label.classList.toggle('off', !has);
        label.querySelector('.n').textContent = has ? counts[k] : '无';
      }
      $(`input[name="mode"][value="${settings.mode}"]`).checked = true;
      status.textContent = '';
    }

    root.addEventListener('change', (e) => {
      if (!e.target.matches('input')) return;
      const o = readOpts();
      for (const k of ['main', 'colors', 'aplus']) {
        if (!$(`input[name="${k}"]`).disabled) settings[k] = o[k];
      }
      settings.mode = o.mode;
      saveSettings(settings);
    });

    $('.fab').addEventListener('click', () => {
      if (panel.hidden) refresh();
      panel.hidden = !panel.hidden;
    });
    $('.close').addEventListener('click', () => { panel.hidden = true; });

    $('[data-act="copy"]').addEventListener('click', async () => {
      const tasks = buildTasks(data, readOpts());
      if (!tasks.length) { status.textContent = '没有选中任何图片'; return; }
      await copyText(tasks.map((t) => t.url).join('\n'));
      status.textContent = `已复制 ${tasks.length} 个链接`;
    });

    $('[data-act="zip"]').addEventListener('click', async () => {
      if (busy) return;
      busy = true;
      root.querySelectorAll('.btns button').forEach((b) => { b.disabled = true; });
      try {
        const r = await downloadZip(data, readOpts(), (done, total) => {
          status.textContent = `下载中 ${done}/${total}…`;
        });
        status.textContent = r.failed ? `完成：${r.ok} 张，失败 ${r.failed} 张（见 _failed.txt）` : `完成：${r.ok} 张`;
      } catch (e) {
        status.textContent = `出错：${e.message}`;
      } finally {
        busy = false;
        root.querySelectorAll('.btns button').forEach((b) => { b.disabled = false; });
      }
    });
  }

  // 只在商品页（能找到图片的页面）显示按钮。
  const initial = collect(document);
  if (initial.main.length || initial.colors.length || initial.aplus.length) mountUi();
})();
