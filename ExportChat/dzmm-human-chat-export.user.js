// ==UserScript==
// @name         DZMM 真人聊天记录导出
// @namespace    https://www.dzmm.ai/
// @version      0.1.1
// @description  将当前账号有权查看的 DZMM 真人单聊或群聊完整导出为 HTML、Markdown 或 JSON。
// @author       local
// @match        https://*.dzmm.ai/*
// @include      /^https:\/\/(www\.)?dzmm\.ai\/chat(?:[/?#]|$)/
// @run-at       document-idle
// @grant        GM_registerMenuCommand
// ==/UserScript==

// Tampermonkey脚本

(function () {
  "use strict";

  const SCRIPT_ID = "dzmm-human-chat-exporter";
  const PAGE_SIZE = 50;
  const REQUEST_INTERVAL_MS = 180;
  const MAX_PAGES = 10000;
  const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

  let activeAbortController = null;

  function sleep(ms, signal) {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) {
        reject(new DOMException("操作已取消", "AbortError"));
        return;
      }

      const onAbort = () => {
        clearTimeout(timer);
        reject(new DOMException("操作已取消", "AbortError"));
      };
      const timer = setTimeout(() => {
        signal?.removeEventListener("abort", onAbort);
        resolve();
      }, ms);
      signal?.addEventListener("abort", onAbort, { once: true });
    });
  }

  function getErrorMessage(payload, fallback) {
    return (
      payload?.error?.json?.message ||
      payload?.error?.message ||
      payload?.message ||
      fallback
    );
  }

  function unwrapTrpcPayload(payload) {
    if (payload?.error) {
      throw new Error(getErrorMessage(payload, "DZMM 接口返回错误"));
    }

    const data = payload?.result?.data;
    if (data && Object.prototype.hasOwnProperty.call(data, "json")) {
      return data.json;
    }
    if (data !== undefined) {
      return data;
    }
    if (payload?.result !== undefined) {
      return payload.result;
    }
    throw new Error("无法识别 DZMM 接口返回的数据格式");
  }

  async function trpcQuery(path, input, options = {}) {
    const { signal, retries = 3 } = options;
    const url = new URL(`/api/trpc/${path}`, location.origin);

    // DZMM 当前使用 SuperJSON；普通对象会被序列化到 json 字段中。
    if (input !== undefined) {
      url.searchParams.set("input", JSON.stringify({ json: input }));
    }

    let lastError;
    for (let attempt = 0; attempt <= retries; attempt += 1) {
      try {
        const response = await fetch(url, {
          method: "GET",
          credentials: "include",
          headers: { Accept: "application/json" },
          signal,
        });

        const payload = await response.json().catch(() => null);
        if (!response.ok) {
          const error = new Error(
            getErrorMessage(payload, `请求失败（HTTP ${response.status}）`),
          );
          error.status = response.status;
          throw error;
        }
        return unwrapTrpcPayload(payload);
      } catch (error) {
        if (error?.name === "AbortError") {
          throw error;
        }
        lastError = error;

        const retryable =
          error?.status === 429 ||
          error?.status >= 500 ||
          error instanceof TypeError;
        if (!retryable || attempt === retries) {
          throw error;
        }
        await sleep(700 * 2 ** attempt, signal);
      }
    }
    throw lastError;
  }

  async function safeQuery(path, input, signal) {
    try {
      return await trpcQuery(path, input, { signal, retries: 1 });
    } catch (error) {
      if (error?.name === "AbortError") {
        throw error;
      }
      console.warn(`[${SCRIPT_ID}] 可选信息读取失败：${path}`, error);
      return null;
    }
  }

  function getCurrentChatroomId() {
    const chatroomId = new URL(location.href).searchParams.get("c") || "";
    if (!UUID_RE.test(chatroomId)) {
      throw new Error("请先在 DZMM 中打开一个真人单聊或群聊，再点击导出。");
    }
    return chatroomId;
  }

  function flattenChatList(value) {
    if (Array.isArray(value)) {
      return value.flatMap(flattenChatList);
    }
    if (!value || typeof value !== "object") {
      return [];
    }
    for (const key of ["items", "chats", "data", "pages"]) {
      if (Array.isArray(value[key])) {
        return value[key].flatMap(flattenChatList);
      }
    }
    return [value];
  }

  function getObjectChatroomId(value) {
    return String(
      value?.chatroomId ||
        value?.chatroom_id ||
        value?.data?.chatroomId ||
        value?.data?.chatroom_id ||
        value?.id ||
        "",
    );
  }

  async function getChatDescriptor(chatroomId, signal) {
    const list = await safeQuery("chat.listAll", undefined, signal);
    const descriptor = flattenChatList(list).find(
      (item) => getObjectChatroomId(item) === chatroomId,
    );

    if (descriptor?.type && descriptor.type !== "user") {
      throw new Error("当前选择的不是与真人的单聊或群聊。");
    }
    return descriptor || null;
  }

  async function fetchAllMessages(chatroomId, signal, onProgress) {
    const pages = [];
    const seenCursors = new Set();
    let before = "";
    let beforeMessageId = "";
    let rawCount = 0;

    for (let pageNumber = 1; pageNumber <= MAX_PAGES; pageNumber += 1) {
      const input = {
        chatroomId,
        limit: PAGE_SIZE,
        ...(before ? { before } : {}),
        ...(beforeMessageId ? { beforeMessageId } : {}),
      };
      const page = await trpcQuery("chatroom.getMessages", input, { signal });
      if (!Array.isArray(page?.messages)) {
        throw new Error("消息接口没有返回预期的 messages 数组，网页接口可能已更新。");
      }

      pages.push(page.messages);
      rawCount += page.messages.length;
      onProgress?.({ pageNumber, rawCount, pageSize: page.messages.length });

      const pagination = page.pagination || {};
      if (!pagination.hasMore) {
        break;
      }

      const nextCursor = pagination.nextCursor || "";
      const nextMessageId = pagination.nextMessageId || "";
      if (!nextCursor || !nextMessageId) {
        throw new Error("服务器表示还有旧消息，但没有返回下一页游标。");
      }

      const cursorKey = `${nextCursor}\n${nextMessageId}`;
      if (seenCursors.has(cursorKey)) {
        throw new Error("服务器重复返回同一个分页游标，已停止以避免无限请求。");
      }
      seenCursors.add(cursorKey);
      before = nextCursor;
      beforeMessageId = nextMessageId;
      await sleep(REQUEST_INTERVAL_MS, signal);

      if (pageNumber === MAX_PAGES) {
        throw new Error(`导出已达到安全上限（${MAX_PAGES * PAGE_SIZE} 条消息）。`);
      }
    }

    // 接口从最新一页向旧消息翻页；先倒转页序，再按时间稳定排序。
    const unique = new Map();
    for (const message of pages.slice().reverse().flat()) {
      const key =
        message?.message_id ||
        `${message?.sent_at || ""}|${message?.sent_by || ""}|${JSON.stringify(message?.content)}`;
      if (!unique.has(key)) {
        unique.set(key, message);
      }
    }

    return [...unique.values()]
      .map((message, order) => ({ message, order }))
      .sort((a, b) => {
        const aTime = Date.parse(a.message?.sent_at || "");
        const bTime = Date.parse(b.message?.sent_at || "");
        if (Number.isFinite(aTime) && Number.isFinite(bTime) && aTime !== bTime) {
          return aTime - bTime;
        }
        return a.order - b.order;
      })
      .map(({ message }) => message);
  }

  function getCandidateId(value) {
    return String(
      value?.userId ||
        value?.user_id ||
        value?.profileId ||
        value?.profile_id ||
        value?.id ||
        "",
    );
  }

  function getCandidateName(value) {
    return String(
      value?.fullName ||
        value?.full_name ||
        value?.displayName ||
        value?.display_name ||
        value?.username ||
        value?.name ||
        "",
    ).trim();
  }

  function collectIdentities(value, result, depth = 0, visited = new WeakSet()) {
    if (!value || typeof value !== "object" || depth > 6 || visited.has(value)) {
      return;
    }
    visited.add(value);

    if (Array.isArray(value)) {
      for (const item of value) {
        collectIdentities(item, result, depth + 1, visited);
      }
      return;
    }

    const id = getCandidateId(value);
    const name = getCandidateName(value);
    if (id && name) {
      result.set(id, name);
    }

    for (const child of Object.values(value)) {
      if (child && typeof child === "object") {
        collectIdentities(child, result, depth + 1, visited);
      }
    }
  }

  function resolveCurrentUserId(profile) {
    return getCandidateId(profile);
  }

  function resolveTitle(descriptor, preview, identities, chatroomId) {
    const candidates = [
      descriptor?.title,
      descriptor?.chatName,
      descriptor?.data?.title,
      descriptor?.data?.otherUserName,
      descriptor?.data?.groupTitle,
      preview?.title,
      preview?.chatName,
    ];
    const title = candidates.find(
      (candidate) => typeof candidate === "string" && candidate.trim(),
    );
    if (title) {
      return title.trim();
    }

    const visibleTitle = document.querySelector(
      "main .font-semibold.border-b span.text-base",
    )?.textContent;
    if (visibleTitle?.trim()) {
      return visibleTitle.trim();
    }

    const names = [...new Set(identities.values())].filter(Boolean).slice(0, 4);
    return names.length ? names.join("、") : `聊天_${chatroomId.slice(0, 8)}`;
  }

  function senderLabel(senderId, identities, currentUserId) {
    const name = identities.get(String(senderId)) || "";
    if (String(senderId) === String(currentUserId)) {
      return name ? `我（${name}）` : "我";
    }
    return name || `用户 ${String(senderId || "未知").slice(0, 8)}`;
  }

  function formatTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return String(value || "时间未知");
    }
    return date.toLocaleString("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  }

  function contentToDisplay(content) {
    if (!content || typeof content !== "object") {
      return { text: String(content || ""), media: [] };
    }

    const media = [];
    let text = "";
    switch (content.type) {
      case "text":
        text = content.text || "";
        break;
      case "image": {
        const url = content.url || content.imageUrl || "";
        text = `[图片]${content.alt ? ` ${content.alt}` : ""}`;
        if (url) media.push({ type: "image", url, label: content.alt || "聊天图片" });
        break;
      }
      case "video": {
        const url = content.videoUrl || content.url || "";
        text = `[视频]${content.duration ? ` ${content.duration} 秒` : ""}`;
        if (url) media.push({ type: "video", url, label: "聊天视频" });
        break;
      }
      case "voice": {
        const url = content.url || content.audioUrl || "";
        text = `[语音]${content.duration ? ` ${content.duration} 秒` : ""}`;
        if (content.transcript) text += `\n转写：${content.transcript}`;
        if (url) media.push({ type: "audio", url, label: "聊天语音" });
        break;
      }
      case "sticker": {
        const url = content.url || content.imageUrl || "";
        text = `[表情]${content.emoji ? ` ${content.emoji}` : ""}`;
        if (url) media.push({ type: "image", url, label: content.emoji || "聊天表情" });
        break;
      }
      case "share":
        text = `[分享${content.shareType ? `：${content.shareType}` : ""}]${
          content.resourceId ? ` ${content.resourceId}` : ""
        }`;
        break;
      case "deleted":
        text = `[消息已删除${content.original_type ? `，原类型：${content.original_type}` : ""}]`;
        break;
      case "system":
        text = `[系统消息] ${content.text || content.message || ""}`.trim();
        break;
      default:
        text = `[${content.type || "未知类型"}] ${JSON.stringify(content)}`;
    }

    if (content.reference) {
      const reference = content.reference;
      const referenceContent = reference.content;
      const referenceText =
        typeof referenceContent === "string"
          ? referenceContent
          : referenceContent?.text || "被引用的消息";
      text = `↪ 回复：${referenceText}\n\n${text}`;
    }
    return { text, media };
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function escapeAttribute(value) {
    return escapeHtml(value).replaceAll("`", "&#96;");
  }

  function renderMediaHtml(media) {
    return media
      .map((item) => {
        const url = escapeAttribute(item.url);
        const label = escapeAttribute(item.label);
        if (item.type === "image") {
          return `<a class="media-link" href="${url}" target="_blank" rel="noreferrer"><img loading="lazy" src="${url}" alt="${label}"></a>`;
        }
        if (item.type === "video") {
          return `<video controls preload="metadata" src="${url}">${label}</video>`;
        }
        if (item.type === "audio") {
          return `<audio controls preload="metadata" src="${url}">${label}</audio>`;
        }
        return `<a href="${url}" target="_blank" rel="noreferrer">${label}</a>`;
      })
      .join("");
  }

  function renderHtml(exportData) {
    const rows = exportData.messages
      .map((message) => {
        const display = contentToDisplay(message.content);
        const isSelf = String(message.sent_by) === String(exportData.currentUserId);
        const sender = senderLabel(
          message.sent_by,
          exportData.identityMap,
          exportData.currentUserId,
        );
        return `<article class="message ${isSelf ? "self" : "other"}" data-message-id="${escapeAttribute(
          message.message_id || "",
        )}">
  <div class="meta"><span class="sender">${escapeHtml(sender)}</span><time>${escapeHtml(
          formatTime(message.sent_at),
        )}</time></div>
  <div class="bubble"><div class="text">${escapeHtml(display.text).replaceAll(
          "\n",
          "<br>",
        )}</div>${renderMediaHtml(display.media)}</div>
</article>`;
      })
      .join("\n");

    return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${escapeHtml(exportData.title)} - DZMM 聊天记录</title>
  <style>
    :root { color-scheme: light dark; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
    body { margin: 0; background: #f5f5f7; color: #202124; }
    main { width: min(900px, calc(100% - 24px)); margin: 24px auto 80px; }
    header.page { position: sticky; top: 0; z-index: 2; padding: 18px 20px; border: 1px solid #ddd;
      border-radius: 16px; background: rgba(255,255,255,.94); backdrop-filter: blur(12px); }
    h1 { margin: 0 0 6px; font-size: 20px; }
    .summary { color: #666; font-size: 13px; }
    .message { display: flex; flex-direction: column; margin: 18px 0; }
    .message.self { align-items: flex-end; }
    .message.other { align-items: flex-start; }
    .meta { display: flex; gap: 9px; align-items: baseline; margin: 0 7px 5px; color: #777; font-size: 12px; }
    .sender { color: #444; font-weight: 650; }
    .bubble { max-width: min(76%, 680px); padding: 10px 13px; border-radius: 15px; background: #fff;
      box-shadow: 0 1px 3px rgba(0,0,0,.08); overflow-wrap: anywhere; }
    .self .bubble { background: #ec4899; color: #fff; border-top-right-radius: 4px; }
    .other .bubble { border-top-left-radius: 4px; }
    .text:empty { display: none; }
    img, video { display: block; max-width: 100%; max-height: 520px; margin-top: 9px; border-radius: 10px; }
    audio { display: block; max-width: 100%; margin-top: 9px; }
    .media-link { display: block; }
    @media (prefers-color-scheme: dark) {
      body { background: #111216; color: #eee; }
      header.page { border-color: #333; background: rgba(27,28,32,.94); }
      .summary, .meta { color: #aaa; }
      .sender { color: #ddd; }
      .bubble { background: #26272d; }
      .self .bubble { background: #be185d; }
    }
    @media print {
      body { background: #fff; }
      main { width: 100%; margin: 0; }
      header.page { position: static; box-shadow: none; }
      .bubble { box-shadow: none; border: 1px solid #ddd; }
    }
  </style>
</head>
<body>
  <main>
    <header class="page">
      <h1>${escapeHtml(exportData.title)}</h1>
      <div class="summary">DZMM 真人聊天记录 · ${exportData.messages.length} 条消息 · 导出于 ${escapeHtml(
        formatTime(exportData.exportedAt),
      )}</div>
    </header>
    ${rows}
  </main>
</body>
</html>`;
  }

  function renderMarkdown(exportData) {
    const lines = [
      `# ${exportData.title}`,
      "",
      `- 聊天室 ID：\`${exportData.chatroomId}\``,
      `- 消息数：${exportData.messages.length}`,
      `- 导出时间：${formatTime(exportData.exportedAt)}`,
      "",
      "---",
      "",
    ];

    for (const message of exportData.messages) {
      const sender = senderLabel(
        message.sent_by,
        exportData.identityMap,
        exportData.currentUserId,
      ).replaceAll("\n", " ");
      const display = contentToDisplay(message.content);
      lines.push(`### ${formatTime(message.sent_at)} · ${sender}`, "", display.text || "");
      for (const media of display.media) {
        lines.push("", `[${media.label}](${media.url})`);
      }
      lines.push("", `<!-- message_id: ${message.message_id || ""} -->`, "");
    }
    return `\uFEFF${lines.join("\n")}`;
  }

  function renderJson(exportData) {
    const participants = [...exportData.identityMap.entries()].map(([id, name]) => ({
      id,
      name,
      isCurrentUser: String(id) === String(exportData.currentUserId),
    }));
    return JSON.stringify(
      {
        schemaVersion: 1,
        source: location.origin,
        chatroomId: exportData.chatroomId,
        title: exportData.title,
        exportedAt: exportData.exportedAt,
        currentUserId: exportData.currentUserId || null,
        participants,
        messageCount: exportData.messages.length,
        messages: exportData.messages,
      },
      null,
      2,
    );
  }

  function safeFilename(value) {
    const normalized = String(value || "聊天记录")
      .normalize("NFKC")
      .replace(/[\\/:*?"<>|\u0000-\u001f]/g, "_")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, 80);
    return normalized || "聊天记录";
  }

  function downloadText(text, filename, mimeType) {
    const blob = new Blob([text], { type: `${mimeType};charset=utf-8` });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.style.display = "none";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  }

  async function buildExportData(chatroomId, signal, onProgress) {
    const descriptor = await getChatDescriptor(chatroomId, signal);
    onProgress("正在读取消息……");
    const messages = await fetchAllMessages(chatroomId, signal, ({ pageNumber, rawCount }) => {
      onProgress(`正在读取第 ${pageNumber} 页，已取得 ${rawCount} 条消息……`);
    });

    onProgress("正在补充参与者名称……");
    const [profile, members, preview] = await Promise.all([
      safeQuery("user.getMe", undefined, signal),
      safeQuery("chatroom.getMembers", { chatroomId }, signal),
      safeQuery("chatroom.getPreview", { chatroomId }, signal),
    ]);

    const identityMap = new Map();
    collectIdentities(profile, identityMap);
    collectIdentities(members, identityMap);
    collectIdentities(preview, identityMap);
    collectIdentities(descriptor, identityMap);

    const currentUserId = resolveCurrentUserId(profile);
    return {
      chatroomId,
      title: resolveTitle(descriptor, preview, identityMap, chatroomId),
      exportedAt: new Date().toISOString(),
      currentUserId,
      identityMap,
      messages,
    };
  }

  function injectStyles() {
    if (document.getElementById(`${SCRIPT_ID}-style`)) return;
    const style = document.createElement("style");
    style.id = `${SCRIPT_ID}-style`;
    style.textContent = `
      #${SCRIPT_ID}-button {
        position: fixed; right: 16px; bottom: 86px; z-index: 2147483645;
        border: 0; border-radius: 999px; padding: 10px 14px; cursor: pointer;
        background: #db2777; color: white; font: 600 13px/1.2 system-ui, sans-serif;
        box-shadow: 0 5px 18px rgba(0,0,0,.25);
      }
      #${SCRIPT_ID}-button:hover { background: #be185d; }
      #${SCRIPT_ID}-overlay {
        position: fixed; inset: 0; z-index: 2147483646; display: grid; place-items: center;
        padding: 18px; background: rgba(0,0,0,.55); font-family: system-ui, sans-serif;
      }
      #${SCRIPT_ID}-dialog {
        box-sizing: border-box; width: min(440px, 100%); padding: 20px; border-radius: 16px;
        background: #fff; color: #202124; box-shadow: 0 22px 60px rgba(0,0,0,.35);
      }
      #${SCRIPT_ID}-dialog h2 { margin: 0 0 8px; font-size: 19px; }
      #${SCRIPT_ID}-dialog p { margin: 8px 0; color: #666; font-size: 13px; line-height: 1.55; }
      #${SCRIPT_ID}-dialog label { display: block; margin: 16px 0 6px; font-size: 13px; font-weight: 650; }
      #${SCRIPT_ID}-dialog select {
        box-sizing: border-box; width: 100%; padding: 9px 10px; border: 1px solid #ccc;
        border-radius: 9px; background: #fff; color: #222;
      }
      #${SCRIPT_ID}-status {
        min-height: 20px; margin-top: 13px; padding: 9px 10px; border-radius: 9px;
        background: #f5f5f7; color: #555; font-size: 12px; overflow-wrap: anywhere;
      }
      #${SCRIPT_ID}-actions { display: flex; justify-content: flex-end; gap: 9px; margin-top: 16px; }
      #${SCRIPT_ID}-actions button {
        border: 0; border-radius: 9px; padding: 9px 14px; cursor: pointer; font-weight: 650;
      }
      #${SCRIPT_ID}-cancel { background: #eee; color: #333; }
      #${SCRIPT_ID}-run { background: #db2777; color: #fff; }
      #${SCRIPT_ID}-actions button:disabled { cursor: not-allowed; opacity: .55; }
      @media (prefers-color-scheme: dark) {
        #${SCRIPT_ID}-dialog { background: #202126; color: #f4f4f5; }
        #${SCRIPT_ID}-dialog p { color: #bbb; }
        #${SCRIPT_ID}-dialog select { border-color: #555; background: #292a30; color: #f4f4f5; }
        #${SCRIPT_ID}-status { background: #292a30; color: #ccc; }
        #${SCRIPT_ID}-cancel { background: #36373d; color: #eee; }
      }
    `;
    document.head.appendChild(style);
  }

  function openDialog() {
    injectStyles();
    document.getElementById(`${SCRIPT_ID}-overlay`)?.remove();

    const overlay = document.createElement("div");
    overlay.id = `${SCRIPT_ID}-overlay`;
    overlay.innerHTML = `
      <section id="${SCRIPT_ID}-dialog" role="dialog" aria-modal="true" aria-labelledby="${SCRIPT_ID}-title">
        <h2 id="${SCRIPT_ID}-title">导出当前真人聊天</h2>
        <p>脚本会使用当前登录状态，按页读取你有权查看的单聊或群聊。聊天数据只在本机浏览器中整理并下载。</p>
        <label for="${SCRIPT_ID}-format">导出格式</label>
        <select id="${SCRIPT_ID}-format">
          <option value="html">HTML（推荐，便于阅读和打印）</option>
          <option value="markdown">Markdown</option>
          <option value="json">JSON（保留原始结构）</option>
        </select>
        <div id="${SCRIPT_ID}-status" role="status">请确认已经打开需要导出的真人聊天。</div>
        <div id="${SCRIPT_ID}-actions">
          <button id="${SCRIPT_ID}-cancel" type="button">关闭</button>
          <button id="${SCRIPT_ID}-run" type="button">开始导出</button>
        </div>
      </section>`;
    document.body.appendChild(overlay);

    const formatSelect = overlay.querySelector(`#${SCRIPT_ID}-format`);
    const status = overlay.querySelector(`#${SCRIPT_ID}-status`);
    const cancelButton = overlay.querySelector(`#${SCRIPT_ID}-cancel`);
    const runButton = overlay.querySelector(`#${SCRIPT_ID}-run`);

    const close = () => {
      activeAbortController?.abort();
      activeAbortController = null;
      overlay.remove();
    };

    cancelButton.addEventListener("click", close);
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay && !activeAbortController) close();
    });

    runButton.addEventListener("click", async () => {
      runButton.disabled = true;
      formatSelect.disabled = true;
      cancelButton.textContent = "停止";
      activeAbortController = new AbortController();

      try {
        const chatroomId = getCurrentChatroomId();
        const exportData = await buildExportData(
          chatroomId,
          activeAbortController.signal,
          (text) => {
            status.textContent = text;
          },
        );

        const date = new Date().toISOString().slice(0, 10);
        const basename = safeFilename(`DZMM_${exportData.title}_${date}`);
        const format = formatSelect.value;
        if (format === "json") {
          downloadText(renderJson(exportData), `${basename}.json`, "application/json");
        } else if (format === "markdown") {
          downloadText(renderMarkdown(exportData), `${basename}.md`, "text/markdown");
        } else {
          downloadText(renderHtml(exportData), `${basename}.html`, "text/html");
        }

        status.textContent = `导出完成：${exportData.messages.length} 条消息。附件保留为原始链接。`;
        cancelButton.textContent = "关闭";
      } catch (error) {
        if (error?.name === "AbortError") {
          status.textContent = "导出已取消。";
        } else {
          console.error(`[${SCRIPT_ID}] 导出失败`, error);
          status.textContent = `导出失败：${error?.message || String(error)}`;
        }
      } finally {
        activeAbortController = null;
        runButton.disabled = false;
        formatSelect.disabled = false;
        cancelButton.textContent = "关闭";
      }
    });
  }

  function ensureFloatingButton() {
    if (!location.pathname.startsWith("/chat")) return;
    injectStyles();
    if (document.getElementById(`${SCRIPT_ID}-button`)) return;

    const button = document.createElement("button");
    button.id = `${SCRIPT_ID}-button`;
    button.type = "button";
    button.textContent = "导出聊天";
    button.title = "导出当前 DZMM 真人单聊或群聊";
    button.addEventListener("click", openDialog);
    document.body.appendChild(button);
  }

  if (typeof GM_registerMenuCommand === "function") {
    GM_registerMenuCommand("导出当前 DZMM 真人聊天", openDialog);
  }

  ensureFloatingButton();
  // DZMM 是单页应用；定时检查可覆盖站内跳转后按钮尚未创建的情况。
  setInterval(ensureFloatingButton, 1500);
})();
