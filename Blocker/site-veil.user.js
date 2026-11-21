// ==UserScript==
// @name         看完通知就收尾
// @namespace    local.site-veil
// @version      1.0.0
// @description  在 X、Instagram、Bilibili、小红书和 Threads 留 10 秒看通知，再随机用一种效果慢慢遮住网页。
// @match        *://*.x.com/*
// @match        *://*.twitter.com/*
// @match        *://*.instagram.com/*
// @match        *://*.bilibili.com/*
// @match        *://*.bilibili.tv/*
// @match        *://*.b23.tv/*
// @match        *://*.xiaohongshu.com/*
// @match        *://*.rednote.com/*
// @match        *://*.xhslink.com/*
// @match        *://*.threads.com/*
// @match        *://*.threads.net/*
// @run-at       document-body
// @noframes
// @sandbox      DOM
// ==/UserScript==

(() => {
  'use strict';

  const GRACE_MS = 10_000;
  const TRANSITION_MS = 6_000;
  const INK = '#172126';

  function fade(ctx, width, height, progress) {
    ctx.globalAlpha = progress;
    ctx.fillRect(0, 0, width, height);
  }

  function expandingCircle(ctx, width, height, progress) {
    const radius = Math.hypot(width, height) / 2 * progress;
    ctx.beginPath();
    ctx.arc(width / 2, height / 2, radius, 0, 2 * Math.PI);
    ctx.fill();
  }

  function closingIris(ctx, width, height, progress) {
    const radius = Math.hypot(width, height) / 2 * (1 - progress);
    ctx.beginPath();
    ctx.rect(0, 0, width, height);
    ctx.moveTo(width / 2 + radius, height / 2);
    ctx.arc(width / 2, height / 2, radius, 0, 2 * Math.PI);
    ctx.fill('evenodd');
  }

  function curtains(ctx, width, height, progress) {
    const panelWidth = width / 2 * progress;
    ctx.fillRect(0, 0, panelWidth, height);
    ctx.fillRect(width - panelWidth, 0, panelWidth, height);
  }

  function risingShade(ctx, width, height, progress) {
    const covered = height * progress;
    ctx.fillRect(0, height - covered, width, covered);
  }

  function blinds(ctx, width, height, progress) {
    const slats = 8;
    const spacing = height / slats;
    for (let row = 0; row < slats; row++) {
      ctx.fillRect(0, row * spacing, width, spacing * progress);
    }
  }

  function mosaic(ctx, width, height, progress) {
    const columns = 7;
    const rows = 5;
    const cellWidth = width / columns;
    const cellHeight = height / rows;
    for (let row = 0; row < rows; row++) {
      for (let col = 0; col < columns; col++) {
        const offset = (row + col) / (rows + columns - 2);
        const scale = Math.max(0, Math.min(1, 2 * progress - offset));
        ctx.fillRect((col + (1 - scale) / 2) * cellWidth, (row + (1 - scale) / 2) * cellHeight,
          cellWidth * scale, cellHeight * scale);
      }
    }
  }

  const effects = [expandingCircle, closingIris, curtains, risingShade, blinds, mosaic, fade];
  const draw = matchMedia('(prefers-reduced-motion: reduce)').matches
    ? fade : effects[Math.floor(Math.random() * effects.length)];

  function createVeil() {
    const host = document.createElement('div');
    const shadow = host.attachShadow({ mode: 'open' });
    const styles = new CSSStyleSheet();
    styles.replaceSync(`
      :host { all: initial !important; }
      dialog[open] {
        position: fixed;
        inset: 0;
        box-sizing: border-box;
        width: 100%;
        height: 100%;
        max-width: none;
        max-height: none;
        margin: 0;
        border: 0;
        padding: 0;
        display: grid;
        place-items: center;
        overflow: hidden;
        touch-action: none;
        overscroll-behavior: none;
        background: transparent;
        color: #edf3f4;
        font: 500 clamp(18px, 3vw, 28px)/1.6 "Source Sans 3", system-ui, sans-serif;
        text-align: center;
      }
      dialog::backdrop { background: transparent; }
      canvas { position: absolute; inset: 0; width: 100%; height: 100%; }
      p { position: relative; margin: 0; padding: 1em; opacity: 0; }
    `);
    shadow.adoptedStyleSheets = [styles];

    const dialog = document.createElement('dialog');
    const canvas = document.createElement('canvas');
    const reminder = document.createElement('p');
    reminder.id = 'reminder';
    reminder.textContent = '看完通知，就到这里吧。';
    dialog.setAttribute('aria-labelledby', reminder.id);
    dialog.setAttribute('closedby', 'none');
    canvas.setAttribute('aria-hidden', 'true');
    dialog.addEventListener('cancel', event => event.preventDefault());
    dialog.append(canvas, reminder);
    shadow.append(dialog);
    document.documentElement.append(host);
    dialog.showModal();
    document.documentElement.style.setProperty('overflow', 'hidden', 'important');
    const ctx = canvas.getContext('2d');

    return progress => {
      if (progress >= 1) {
        dialog.style.backgroundColor = INK;
        reminder.style.opacity = '1';
        canvas.remove();
        return;
      }

      // 动画中也按当前窗口尺寸绘制，转屏或调整窗口后仍能覆盖四角。
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      if (canvas.width !== width) canvas.width = width;
      if (canvas.height !== height) canvas.height = height;
      const eased = progress * progress * (3 - 2 * progress);
      if (ctx) {
        ctx.clearRect(0, 0, width, height);
        ctx.fillStyle = INK;
        draw(ctx, width, height, eased);
      } else {
        canvas.style.backgroundColor = INK;
        canvas.style.opacity = String(eased);
      }
      reminder.style.opacity = String(Math.max(0, (progress - 0.65) / 0.35));
    };
  }

  // 累计前台时间，后台打开的标签页会在切过去时才开始倒计时。
  let visibleSince = null;
  let visibleMs = 0;
  let timer;
  let frame;
  let paint;

  function tick() {
    if (visibleSince === null) return;
    const elapsed = visibleMs + performance.now() - visibleSince;
    if (elapsed < GRACE_MS) {
      timer = setTimeout(tick, GRACE_MS - elapsed);
      return;
    }
    if (!paint) paint = createVeil();
    const progress = Math.min(1, (elapsed - GRACE_MS) / TRANSITION_MS);
    paint(progress);
    if (progress < 1) frame = requestAnimationFrame(tick);
    else document.removeEventListener('visibilitychange', onVisibilityChange);
  }

  function onVisibilityChange() {
    if (document.hidden) {
      if (visibleSince === null) return;
      visibleMs += performance.now() - visibleSince;
      visibleSince = null;
      clearTimeout(timer);
      cancelAnimationFrame(frame);
    } else if (visibleSince === null) {
      visibleSince = performance.now();
      tick();
    }
  }

  document.addEventListener('visibilitychange', onVisibilityChange);
  onVisibilityChange();
})();
