// ==UserScript==
// @name         rclone WebDAV 导出 M3U8
// @namespace    local.rclone-webdav-m3u
// @version      1.1.2
// @description  从 rclone WebDAV 目录页面筛选音视频，递归扫描并导出可附带账号密码的 UTF-8 M3U8。
// @include      /^https:\/\/dav\.[a-z0-9.-]+\.com(?::\d+)?\/.*$/
// @grant        none
// @run-at       document-end
// @noframes
// ==/UserScript==

/*
 * 使用：安装到 Tampermonkey，登录 rclone WebDAV 目录后点击右下角“导出 M3U8”。
 * 勾选音视频后缀后下载；账号密码会自动从同源 JSON 文件读取，无需再次输入。
 * 文件不存在、无法读取或格式不正确时，提示后继续导出不含账号密码的普通 URL。
 * 默认 JSON 路径：/metainfo/rclone_passwd.json（按网站根目录解析，与当前目录无关）。
 * 文件内容示例：{"username":"你的账号","password":"你的密码"}
 * JSON 路径、递归深度与文件上限放在默认折叠的高级设置中，通常无需修改。
 * 深度：0 = 仅当前目录，1 = 再包含一层子目录，2 = 再包含两层，以此类推。
 * 数量：默认最多导出 1000 个符合所选后缀的文件，可修改；达到上限立即停止扫描。
 *
 * 限制：
 * - 仅面向 rclone serve webdav 的默认 HTML 目录页面，不是通用 WebDAV 客户端。
 *   不使用 PROPFIND；关闭 HTML 目录列表或改用其他页面模板时可能无法工作。
 * - 仅支持 rclone 自带的 HTTP Basic 用户名/密码认证，不支持表单 Cookie、OAuth、
 *   额外的反向代理登录或客户端证书。请使用服务器最终的 URL，扫描不跟随重定向。
 * - JSON 文件必须能沿用浏览器当前登录状态读取，且仍应受 DAV 认证保护。
 *   username 和 password 均须为字符串；密码可以为空，用户名非空且不能包含冒号。
 *   非 ASCII 凭据按 UTF-8 发送；如使用 --baseurl，需在高级设置的 JSON 路径中带上前缀。
 * - 导出的 .m3u8 使用 UTF-8（无 BOM），用扩展名明确编码，避免播放器按本地编码读取
 *   .m3u 时出现中文乱码。读取到有效凭据时，媒体 URL 包含可还原的明文账号密码，
 *   请私密保存。否则仅导出普通 URL，播放器如需认证须另行配置；扫描仍使用浏览器
 *   当前的同源登录状态。凭据不会写入浏览器存储，也不会上传第三方。
 * - IINA / foobar2000 仍须支持带凭据的 HTTP(S) URL 及对应媒体格式；播放器版本、
 *   特殊字符凭据和服务器配置可能影响兼容性，建议先用深度 0 验证播放。
 * - 仅扫描同源、当前目录下的文件，按后缀筛选；不读取时长、标签或转码。
 *   默认勾选常见音视频后缀，使用复选框调整。逐目录串行扫描，任一目录失败则停止，
 *   不会静默导出缺少文件的列表。重新加载页面后，输入和选择都会重置。
 * - 默认仅在 https://dav.*.com（可带端口）的目录页面显示按钮，使用正则 @include，
 *   因为 @match 不支持域名中间的通配符。其他域名需同时修改 @include 与下方域名判断。
 */

(() => {
    'use strict';

    const AUDIO = 'mp3 flac m4a aac ogg opus wav aif aiff alac ape wv wma mka ac3 dts'.split(' ');
    const VIDEO = 'mp4 mkv avi mov webm m4v mpg mpeg ts m2ts mts flv wmv ogv vob 3gp rm rmvb'.split(' ');
    const ALLOWED_HOST = /^dav\.[a-z0-9.-]+\.com$/;

    // 新旧 rclone 默认模板都包含 .listing 表格与 #filter，空目录也能识别。
    function isListing(doc) {
        return Boolean(doc.querySelector('.listing table[aria-describedby="summary"]') && doc.querySelector('#filter'));
    }

    function decodeName(value) {
        try { return decodeURIComponent(value); } catch { return value; }
    }

    function* readEntries(doc, directory) {
        if (!isListing(doc)) throw new Error('响应不是支持的 rclone 目录页面。');
        // 旧版将 .name 放在 span，新版放在 td。
        for (const anchor of doc.querySelectorAll('.listing table .name a[href]')) {
            const href = anchor.getAttribute('href');
            if (!href || href.startsWith('#') || href.startsWith('?')) continue;
            const url = new URL(href, directory);
            if (url.origin !== directory.origin || url.username || url.password || url.search || url.hash) continue;
            if (!url.pathname.startsWith(directory.pathname)) continue;
            const relative = url.pathname.slice(directory.pathname.length);
            const isDir = relative.endsWith('/');
            const leaf = isDir ? relative.slice(0, -1) : relative;
            // 只接收当前目录的直接成员，排除自身、上级、面包屑及 ZIP 下载链接。
            if (!leaf || leaf.includes('/')) continue;
            yield { url, isDir, name: decodeName(leaf) };
        }
    }

    function basicAuth(user, password) {
        const bytes = new TextEncoder().encode(`${user}:${password}`);
        return `Basic ${btoa(Array.from(bytes, byte => String.fromCharCode(byte)).join(''))}`;
    }

    async function loadCredentials(path, root, signal) {
        const url = new URL(path, root.origin);
        if (!path.startsWith('/') || path.startsWith('//') || url.origin !== root.origin ||
            url.username || url.password || url.search || url.hash) {
            throw new Error('JSON 路径必须是以 / 开头的同源文件路径，不含查询参数或锚点。');
        }
        const response = await fetch(url.href, {
            headers: { Accept: 'application/json' },
            credentials: 'same-origin',
            cache: 'no-store',
            redirect: 'error',
            signal,
        });
        if (!response.ok) throw new Error(`读取账号密码文件失败（HTTP ${response.status}），请检查高级设置中的路径和浏览器登录状态。`);
        let data;
        try {
            data = await response.json();
        } catch (error) {
            if (signal.aborted) throw error;
            // JSON 解析错误有时包含原文片段，避免将密码显示在错误提示中。
            throw new Error('账号密码文件不是有效的 JSON。');
        }
        if (!data || typeof data.username !== 'string' || !data.username || data.username.includes(':') ||
            typeof data.password !== 'string') {
            throw new Error('JSON 需包含字符串 username 和 password；用户名非空且不能包含冒号，密码可以为空。');
        }
        return data;
    }

    function makePlaylist(files, credentials = null) {
        const lines = ['#EXTM3U'];
        const userinfo = credentials ? `${encodeURIComponent(credentials.username)}:${encodeURIComponent(credentials.password)}@` : '';
        for (const file of files) {
            const url = new URL(file.url);
            // 分别编码，保留字面 % 与 @、#、: 等字符，路径保持服务器原有编码。
            // 手工保留空密码后的冒号；URL 序列化会将 user:@ 变成 user@。
            const mediaUrl = `${url.protocol}//${userinfo}${url.host}${url.pathname}`;
            lines.push(`#EXTINF:-1,${file.name.replace(/[\r\n\u0000]/g, ' ')}`, mediaUrl);
        }
        return `${lines.join('\n')}\n`;
    }

    async function scan(root, extensions, maxDepth, maxFiles, authorization, signal, onProgress) {
        const queue = [{ url: root, depth: 0 }];
        const visited = new Set([root.href]);
        const files = new Map();
        let scanned = 0;
        const result = limited => ({
            files: [...files.values()].sort((a, b) => decodeName(a.url.pathname).localeCompare(decodeName(b.url.pathname), undefined, { numeric: true })),
            limited,
        });
        for (let index = 0; index < queue.length; index++) {
            signal.throwIfAborted();
            const current = queue[index];
            const response = await fetch(current.url.href, {
                headers: authorization ? { Authorization: authorization } : {},
                credentials: 'same-origin',
                redirect: 'error',
                signal,
            });
            if (!response.ok) throw new Error(`目录请求失败（HTTP ${response.status}）：${decodeName(current.url.pathname)}`);
            const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
            for (const entry of readEntries(doc, current.url)) {
                if (entry.isDir) {
                    if (current.depth < maxDepth && !visited.has(entry.url.href)) {
                        visited.add(entry.url.href);
                        queue.push({ url: entry.url, depth: current.depth + 1 });
                    }
                } else if (extensions.has(entry.name.split('.').pop().toLowerCase())) {
                    files.set(entry.url.href, entry);
                    if (files.size >= maxFiles) return result(true);
                }
            }
            scanned++;
            onProgress(`已扫描 ${scanned} 个目录，找到 ${files.size} 个文件。`);
        }
        return result(false);
    }

    function download(text, name) {
        const url = URL.createObjectURL(new Blob([text], { type: 'audio/x-mpegurl;charset=utf-8' }));
        const link = document.createElement('a');
        link.href = url;
        link.download = name;
        document.body.append(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 60_000);
    }

    function mountPanel() {
        const root = new URL(location.href);
        root.search = '';
        root.hash = '';
        root.username = '';
        root.password = '';
        if (root.protocol !== 'https:' || !ALLOWED_HOST.test(root.hostname) ||
            !root.pathname.endsWith('/') || !isListing(document)) return;

        // Shadow DOM 防止服务器页面样式与工具界面相互影响。
        const host = document.createElement('div');
        host.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:2147483647;';
        const shadow = host.attachShadow({ mode: 'open' });
        shadow.innerHTML = `
            <style>
                :host { font: 14px/1.5 system-ui, sans-serif; color: #222; }
                * { box-sizing: border-box; }
                [hidden] { display: none !important; }
                button, input { font: inherit; }
                button { cursor: pointer; padding: 6px 12px; }
                button:disabled { cursor: wait; }
                form { background: #fff; border: 1px solid #aaa; border-radius: 8px;
                       padding: 16px; width: min(380px, calc(100vw - 32px));
                       max-height: 85vh; overflow: auto; box-shadow: 0 3px 18px #0003; }
                header, footer { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
                h3 { margin: 0; font-size: 16px; }
                .field { display: block; margin-top: 10px; }
                .field input { display: block; width: 100%; padding: 5px; }
                fieldset { border: 1px solid #ccc; margin: 12px 0; padding: 8px; }
                .types { display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; }
                .types label { white-space: nowrap; }
                .all { font-weight: 600; }
                details { margin: 12px 0; }
                summary { cursor: pointer; color: #555; }
                small { display: block; color: #555; margin: 6px 0; }
                #status { white-space: pre-wrap; overflow-wrap: anywhere; margin: 10px 0 0; }
                #status[data-warning="true"] { color: #934500; }
            </style>
            <button id="open" type="button">导出 M3U8</button>
            <form hidden autocomplete="off">
                <header><h3>rclone 导出 M3U8</h3><button id="close" type="button">收起</button></header>
                <div id="extensions"></div>
                <details id="advanced">
                    <summary>高级设置（通常无需修改）</summary>
                    <label class="field">账号密码 JSON 路径<input id="credentials-path" type="text" value="/metainfo/rclone_passwd.json" required></label>
                    <small>相对网站根目录；JSON 包含 username 和 password 字符串。</small>
                    <label class="field">递归深度<input id="depth" type="number" min="0" step="1" value="0" required></label>
                    <small>0＝仅当前目录；1＝包含一层子目录；2＝包含两层。</small>
                    <label class="field">文件数量上限<input id="limit" type="number" min="1" step="1" value="1000" required></label>
                    <small>按符合所选后缀的文件累计；达到上限即停止并导出。</small>
                </details>
                <footer><button id="generate" type="submit">下载 M3U8</button><button id="cancel" type="button" disabled>取消</button></footer>
                <small>读取到有效凭据时，下载的 M3U8 会包含账号密码，请私密保存。</small>
                <p id="status" role="status" aria-live="polite"></p>
            </form>`;
        const get = id => shadow.getElementById(id);
        const form = shadow.querySelector('form');
        const status = get('status');
        let controller = null;

        for (const [title, extensions] of [['音频', AUDIO], ['视频', VIDEO]]) {
            const group = document.createElement('fieldset');
            group.innerHTML = `<legend><label class="all"><input type="checkbox" checked> ${title}全选</label></legend><div class="types"></div>`;
            const toggle = group.querySelector('legend input');
            for (const extension of extensions) {
                const label = document.createElement('label');
                label.innerHTML = `<input type="checkbox" data-extension="${extension}" checked> ${extension}`;
                group.querySelector('.types').append(label);
            }
            const boxes = [...group.querySelectorAll('[data-extension]')];
            toggle.addEventListener('change', () => {
                for (const box of boxes) box.checked = toggle.checked;
                toggle.indeterminate = false;
            });
            group.addEventListener('change', () => {
                const count = boxes.filter(box => box.checked).length;
                toggle.checked = count === boxes.length;
                toggle.indeterminate = count > 0 && count < boxes.length;
            });
            get('extensions').append(group);
        }

        get('open').addEventListener('click', () => {
            form.hidden = false;
            get('open').hidden = true;
            get('generate').focus();
        });
        get('close').addEventListener('click', () => {
            form.hidden = true;
            get('open').hidden = false;
        });
        get('cancel').addEventListener('click', () => controller?.abort());
        form.addEventListener('submit', async event => {
            event.preventDefault();
            if (controller) return;
            const credentialsPath = get('credentials-path').value.trim();
            const depth = Number(get('depth').value);
            const limit = Number(get('limit').value);
            const extensions = new Set([...shadow.querySelectorAll('[data-extension]:checked')].map(box => box.dataset.extension));
            if (!Number.isSafeInteger(depth) || depth < 0) {
                status.textContent = '递归深度必须是大于等于 0 的整数。';
                return;
            }
            if (!extensions.size) {
                status.textContent = '请至少选择一个文件后缀。';
                return;
            }
            if (!Number.isSafeInteger(limit) || limit < 1) {
                status.textContent = '文件数量上限必须是大于等于 1 的整数。';
                return;
            }
            controller = new AbortController();
            get('generate').disabled = true;
            get('cancel').disabled = false;
            status.dataset.warning = 'false';
            status.textContent = '正在读取账号密码……';
            let credentialWarning = '';
            const showStatus = message => { status.textContent = credentialWarning + message; };
            try {
                let credentials = null;
                try {
                    credentials = await loadCredentials(credentialsPath, root, controller.signal);
                } catch (error) {
                    controller.signal.throwIfAborted();
                    credentialWarning = `提醒：${error.message}\n本次导出普通 URL（不含账号密码），播放器可能需要另行认证。\n`;
                    status.dataset.warning = 'true';
                }
                controller.signal.throwIfAborted();
                showStatus('正在扫描……');
                const authorization = credentials ? basicAuth(credentials.username, credentials.password) : null;
                const { files, limited } = await scan(root, extensions, depth, limit, authorization, controller.signal, showStatus);
                controller.signal.throwIfAborted();
                if (!files.length) {
                    showStatus('没有找到符合所选后缀的文件。');
                    return;
                }
                const folder = decodeName(root.pathname.split('/').filter(Boolean).pop() || 'playlist');
                const filename = `${folder.replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_')}.m3u8`;
                download(makePlaylist(files, credentials), filename);
                showStatus(`${limited ? `已达到 ${limit} 个文件上限，停止扫描。` : ''}已导出 ${files.length} 个文件。${credentials ? '播放列表包含账号密码，请私密保存。' : ''}`);
            } catch (error) {
                showStatus(controller.signal.aborted ? '已取消，未导出播放列表。' : `未导出：${error.message}`);
            } finally {
                controller = null;
                get('generate').disabled = false;
                get('cancel').disabled = true;
            }
        });
        document.body.append(host);
    }

    mountPanel();
})();
