const $ = id => document.getElementById(id);

// zones[page] = [{type:'rect'|'polygon'|'freehand', points:[[x,y],...], mode:'delete'|'pixelate'}]
// Points are in PDF coordinates.
let sid = null, pages = [], zones = {};
let tool = 'rect';
let defaultMode = 'delete';
let activePage = 0;
let selected = null;      // {page, index}
let pending = null;       // polygon currently being drawn
let deletedPages = new Set();
let history = [];         // JSON snapshots, for undo
let redoStack = [];
let busy = false;         // a network request is in flight
let keyboardNav = false;  // selection came from the keyboard: give it the focus back
const pageEls = [];

const pagesEl = $('pages'), menu = $('zoneMenu');

// Hooks for the inspector pane (inspector.js, loaded after this file). Defined
// here as no-ops so the app still works on its own without it.
let onDocumentOpened = () => {};
let onZonesChanged = () => {};
let onActivePageChanged = () => {};

const ICON_TRASH = '<svg viewBox="0 0 18 18" fill="none"><path d="M4 5.5h10M7.5 5.5V4a1 1 0 011-1h1a1 1 0 011 1v1.5M5.5 5.5l.6 8a1 1 0 001 .9h3.8a1 1 0 001-.9l.6-8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const ICON_RESTORE = '<svg viewBox="0 0 18 18" fill="none"><path d="M4 8h7a3.5 3.5 0 010 7H8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M6.5 5L4 8l2.5 3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';

// resize handles: [name, x ratio, y ratio, cursor]
const BOX_HANDLES = [
  ['nw', 0, 0, 'nwse-resize'], ['n', .5, 0, 'ns-resize'], ['ne', 1, 0, 'nesw-resize'],
  ['e', 1, .5, 'ew-resize'], ['se', 1, 1, 'nwse-resize'], ['s', .5, 1, 'ns-resize'],
  ['sw', 0, 1, 'nesw-resize'], ['w', 0, .5, 'ew-resize'],
];
const CORNER_HANDLES = BOX_HANDLES.filter(h => h[0].length === 2);

// ---------- status bar ----------
// A single entry point: transient messages (progress, error, export result)
// must not be overwritten by the zone summary.
function setStatus(text, cls) {
  const el = $('status');
  el.textContent = '';
  const span = document.createElement('span');
  if (cls) span.className = cls;
  span.textContent = text;
  el.appendChild(span);
}

function setBusy(on, text) {
  busy = on;
  $('busybar').hidden = !on;
  document.body.classList.toggle('busy', on);
  $('openBtn').disabled = on;
  if (text) setStatus(text, 'busy-text');
  syncButtons();
}

// ---------- history ----------
// Full snapshots: everything calls pushHistory() BEFORE mutating, so drawing,
// moving, resizing, mode changes and zone or page deletion are all undone the
// same way.
function snapshot() { return JSON.stringify({ z: zones, d: [...deletedPages] }); }
function restore(s) {
  const d = JSON.parse(s);
  zones = d.z; deletedPages = new Set(d.d);
  selected = null; closeMenu();
  renderAll(); syncDeletedUI(); updateStatus();
}
function pushHistory() {
  history.push(snapshot());
  if (history.length > 200) history.shift();
  redoStack.length = 0;   // a new action invalidates the redo stack
}
// Returns a function that snapshots only on its first real call: keeps a click
// that moves nothing out of the history.
function onceHistory() {
  let done = false;
  return () => { if (!done) { done = true; pushHistory(); } };
}
function undo() {
  if (!history.length) return;
  cancelPending();
  redoStack.push(snapshot());
  restore(history.pop());
}
function redo() {
  if (!redoStack.length) return;
  cancelPending();
  history.push(snapshot());
  restore(redoStack.pop());
}

// ---------- opening ----------
// fetch() cannot report upload progress: on a PDF of several dozen MB the UI
// would stay silent for the whole upload.
function uploadPdf(f, onProgress) {
  return new Promise((resolve, reject) => {
    const fd = new FormData();
    fd.append('file', f);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/open');
    xhr.responseType = 'text';
    xhr.upload.onprogress = e => {
      if (e.lengthComputable) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try { resolve(JSON.parse(xhr.responseText)); }
        catch { reject(new Error('unreadable response from the server')); }
      } else {
        reject(new Error(xhr.responseText || `error ${xhr.status}`));
      }
    };
    xhr.onerror = () => reject(new Error('server unreachable'));
    xhr.onabort = () => reject(new Error('upload interrupted'));
    xhr.send(fd);
  });
}

async function openFile(f) {
  if (!f) return;
  if (busy) return;
  stopPrefetch();
  if (f.type !== 'application/pdf' && !f.name.toLowerCase().endsWith('.pdf')) {
    setStatus('This file is not a PDF.', 'warn');
    return;
  }
  setBusy(true, `Uploading "${f.name}"…`);
  let d;
  try {
    d = await uploadPdf(f, ratio => {
      setStatus(ratio < 1
        ? `Uploading "${f.name}" — ${Math.round(ratio * 100)}%`
        : 'Analysing the document…', 'busy-text');
    });
  } catch (err) {
    setBusy(false);
    setStatus('Error: ' + err.message, 'warn');
    updateStatus();
    return;
  }

  sid = d.sid; pages = d.pages;
  // redoStack used to be left out of this reset, so Ctrl+Y pasted zones from
  // the previous document onto the new one.
  zones = {}; history = []; redoStack = [];
  activePage = 0; selected = null; deletedPages = new Set();
  cancelPending();
  $('drop').hidden = true; pagesEl.hidden = false;
  setStatus(`Rendering page 1 of ${pages.length}…`, 'busy-text');
  buildPages();
  onDocumentOpened(sid);
  awaitFirstPage();
}

// The first image can take seconds: hand control back (and free the status
// bar) only once the page is actually on screen.
function awaitFirstPage() {
  const pe = pageEls[0];
  if (!pe) { setBusy(false); updateStatus(); return; }
  let done = false;
  const finish = () => {
    if (done) return;
    done = true;
    clearTimeout(timer);
    setBusy(false);
    updateStatus();
    prefetchPages();
  };
  const timer = setTimeout(finish, 15000);   // safety net
  pe.img.addEventListener('load', finish, { once: true });
  pe.img.addEventListener('error', finish, { once: true });
  if (pe.img.complete && pe.img.naturalWidth) finish();
}

// ---------- background prefetch ----------
// Page 1 is rendered on demand, the rest are fetched quietly while the user
// works, so scrolling ahead is instant instead of waiting on a render.
//
// Strictly one at a time: rendering is CPU-bound in a single-process server, so
// a burst of prefetches would queue in front of the page the user actually
// scrolled to. Sequential means that page waits for one render at worst.
let prefetchToken = 0;

function stopPrefetch() { prefetchToken++; }

function prefetchPages() {
  const token = ++prefetchToken;
  const next = () => {
    if (token !== prefetchToken || !sid) return;   // cancelled, or another document
    const i = pageEls.findIndex(pe => !pe.loaded);
    if (i === -1) return;
    const pe = pageEls[i];
    let done = false;
    const step = () => {
      if (done) return;
      done = true;
      clearTimeout(guard);
      setTimeout(next, 50);   // breathe: leave room for an interactive request
    };
    const guard = setTimeout(step, 20000);   // a page that never loads must not stall the rest
    pe.img.addEventListener('load', step, { once: true });
    pe.img.addEventListener('error', step, { once: true });
    loadPage(i);
  };
  next();
}

$('file').onchange = e => { openFile(e.target.files[0]); e.target.value = ''; };

const stageEl = $('stage');
let dragDepth = 0;
['dragenter', 'dragover', 'dragleave', 'drop'].forEach(evt => {
  stageEl.addEventListener(evt, e => { e.preventDefault(); e.stopPropagation(); });
});
stageEl.addEventListener('dragenter', () => {
  dragDepth++;
  stageEl.classList.add('drag-over');
  $('drop').classList.add('drag-over');
});
stageEl.addEventListener('dragleave', () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) { stageEl.classList.remove('drag-over'); $('drop').classList.remove('drag-over'); }
});
stageEl.addEventListener('drop', e => {
  dragDepth = 0;
  stageEl.classList.remove('drag-over');
  $('drop').classList.remove('drag-over');
  const f = e.dataTransfer.files && e.dataTransfer.files[0];
  if (f) openFile(f);
});

// The drop zone opens the file picker too: it is the first thing you see, and
// nothing on it pointed to the button in the top bar. It is hidden as soon as a
// document is open.
$('drop').addEventListener('click', () => { if (!busy) $('file').click(); });
$('drop').addEventListener('keydown', e => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('drop').click(); }
});

// ---------- building the pages ----------
function buildPages() {
  pagesEl.innerHTML = '';
  pageEls.length = 0;
  pages.forEach((p, i) => {
    const cont = document.createElement('div');
    cont.className = 'page-container';
    cont.style.aspectRatio = `${p.w} / ${p.h}`;
    cont.dataset.page = i;

    const tab = document.createElement('div');
    tab.className = 'page-num-tab';
    tab.textContent = `${i + 1} / ${pages.length}`;

    const img = document.createElement('img');
    img.className = 'page-img';
    img.alt = `Page ${i + 1}`;

    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'page-layer');
    svg.setAttribute('viewBox', `${p.x0} ${p.y0} ${p.w} ${p.h}`);
    svg.setAttribute('preserveAspectRatio', 'none');

    const badge = document.createElement('div');
    badge.className = 'page-deleted-badge';
    badge.innerHTML = '<span>Page deleted</span>';

    const delBtn = document.createElement('button');
    delBtn.className = 'page-del-btn';
    delBtn.type = 'button';
    delBtn.innerHTML = ICON_TRASH;
    delBtn.title = 'Delete this page';
    delBtn.onclick = ev => { ev.stopPropagation(); togglePageDeleted(i); };

    cont.append(img, svg, badge, tab, delBtn);
    pagesEl.appendChild(cont);
    pageEls.push({ container: cont, img, svg, delBtn, loaded: false });
    wireLayer(i, svg);
  });

  loadObserver.disconnect();
  activeObserver.disconnect();
  pageEls.forEach(pe => { loadObserver.observe(pe.container); activeObserver.observe(pe.container); });
}

function togglePageDeleted(i) {
  pushHistory();
  if (deletedPages.has(i)) deletedPages.delete(i); else deletedPages.add(i);
  if (selected && selected.page === i) { selected = null; closeMenu(); }
  syncDeletedUI(); renderZones(i); updateStatus();
  onZonesChanged(i);
}

function syncDeletedUI() {
  pageEls.forEach((pe, i) => {
    const del = deletedPages.has(i);
    pe.container.classList.toggle('deleted', del);
    pe.delBtn.classList.toggle('is-deleted', del);
    pe.delBtn.innerHTML = del ? ICON_RESTORE : ICON_TRASH;
    pe.delBtn.title = del ? 'Restore this page' : 'Delete this page';
  });
}

const loadObserver = new IntersectionObserver(entries => {
  entries.forEach(en => { if (en.isIntersecting) loadPage(+en.target.dataset.page); });
}, { rootMargin: '900px 0px', threshold: 0 });

const activeObserver = new IntersectionObserver(entries => {
  entries.forEach(en => {
    if (en.isIntersecting && en.intersectionRatio >= 0.5) {
      activePage = +en.target.dataset.page;
      updateStatus();
      onActivePageChanged(activePage);
    }
  });
}, { threshold: [0.5] });

// Render width = the screen pixels the page actually occupies.
function wantedWidth(pe) {
  const css = pe.container.clientWidth || 880;
  return Math.round(css * (window.devicePixelRatio || 1));
}

function loadPage(i) {
  const pe = pageEls[i];
  if (!pe) return;
  const w = wantedWidth(pe);
  // only reload when it actually buys sharpness
  if (pe.loaded && w <= pe.renderedAt * 1.15) return;
  pe.loaded = true;
  pe.renderedAt = w;
  pe.img.src = `/api/page/${sid}/${i}?w=${w}`;
}

// when the window widens, re-request the visible pages at a higher resolution
let reflowTimer = null;
window.addEventListener('resize', () => {
  if (!sid) return;
  clearTimeout(reflowTimer);
  reflowTimer = setTimeout(() => {
    pageEls.forEach((pe, i) => {
      if (pe.loaded && pe.container.getBoundingClientRect().top < window.innerHeight * 2) loadPage(i);
    });
  }, 250);
});

// ---------- geometry ----------
function toSvgPoint(svg, clientX, clientY) {
  const pt = svg.createSVGPoint();
  pt.x = clientX; pt.y = clientY;
  const p = pt.matrixTransform(svg.getScreenCTM().inverse());
  return [p.x, p.y];
}
// The viewBox is stretched (preserveAspectRatio=none): x and y scales differ.
function unitScale(svg) {
  const m = svg.getScreenCTM();
  return { ux: 1 / (m.a || 1), uy: 1 / (m.d || 1) };
}
function bbox(pts) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const [x, y] of pts) {
    if (x < x0) x0 = x; if (y < y0) y0 = y;
    if (x > x1) x1 = x; if (y > y1) y1 = y;
  }
  return [x0, y0, x1, y1];
}
function edgesFrom(name, bb, p) {
  let [x0, y0, x1, y1] = bb;
  if (name.includes('w')) x0 = p[0];
  if (name.includes('e')) x1 = p[0];
  if (name.includes('n')) y0 = p[1];
  if (name.includes('s')) y1 = p[1];
  return [Math.min(x0, x1), Math.min(y0, y1), Math.max(x0, x1), Math.max(y0, y1)];
}

// `pointercancel` fires when the browser takes the gesture over: the drawing
// has to be dropped rather than half-committed.
function startDrag(ev, onMove, onEnd) {
  const id = ev.pointerId;
  const move = e => { if (e.pointerId === id) onMove(e); };
  const stop = cancelled => e => {
    if (e.pointerId !== id) return;
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', up);
    window.removeEventListener('pointercancel', cancel);
    if (onEnd) onEnd(e, cancelled);
  };
  const up = stop(false), cancel = stop(true);
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up);
  window.addEventListener('pointercancel', cancel);
}

// ---------- rendering the zones ----------
function renderAll() { pageEls.forEach((_, i) => renderZones(i)); }

function modeLabel(mode) { return mode === 'pixelate' ? 'pixelate' : 'delete'; }

function zoneLabel(z, i, idx) {
  return `Zone ${idx + 1}, page ${i + 1}, ${modeLabel(z.mode)}`;
}

// Watermark preview: same diagonal (bottom-left -> top-right) and same relative
// scale as the stamp _apply_watermark lays down server-side. Not pixel-exact —
// it just must not lie about the result.
const WM_DIAGONAL_RATIO = 0.78;
const WM_MIN_SIZE = 8;
// line height in multiples of the font size (Helvetica metrics: ascender 0.718,
// descender 0.207). No maximum size in PDF units: it would make the preview
// depend on the scale of the page.
const WM_LINE_HEIGHT = 0.925;
const WM_PROBE_SIZE = 40;

function watermarkValue() { return $('wm').value.trim(); }

function drawWatermarkPreview(svg, i) {
  const text = watermarkValue();
  if (!text) return;
  const p = pages[i];
  if (!p) return;

  const cx = p.x0 + p.w / 2, cy = p.y0 + p.h / 2;
  const diag = Math.hypot(p.w, p.h);
  const angleDeg = Math.atan2(p.h, p.w) * 180 / Math.PI;

  const t = document.createElementNS(svg.namespaceURI, 'text');
  t.setAttribute('class', 'wm-preview');
  t.setAttribute('x', cx);
  t.setAttribute('y', cy);
  t.setAttribute('text-anchor', 'middle');
  t.setAttribute('dominant-baseline', 'middle');
  t.setAttribute('font-size', WM_PROBE_SIZE);
  t.textContent = text;   // never innerHTML: the watermark comes from the user
  svg.appendChild(t);

  let probe = 0;
  try { probe = t.getComputedTextLength(); } catch { /* mesure indisponible */ }
  if (!probe) probe = text.length * WM_PROBE_SIZE * 0.55;   // rough fallback
  const w0 = probe / WM_PROBE_SIZE;   // text width at size 1

  // same geometric guard as _watermark_fit_size server-side: the text box,
  // rotated by the diagonal's angle, has to fit inside the page.
  const fit = diag / Math.max(w0 + WM_LINE_HEIGHT * p.h / p.w,
                              w0 + WM_LINE_HEIGHT * p.w / p.h) * 0.97;
  const fontSize = Math.min(Math.max(Math.min(diag * WM_DIAGONAL_RATIO / w0, fit), WM_MIN_SIZE), fit);
  t.setAttribute('font-size', fontSize);
  t.setAttribute('transform', `rotate(${angleDeg} ${cx} ${cy})`);
}

function renderZones(i) {
  const pe = pageEls[i];
  if (!pe) return;
  const svg = pe.svg;
  svg.textContent = '';
  const list = zones[i] || [];
  const locked = deletedPages.has(i);
  let selEl = null;

  list.forEach((z, idx) => {
    const isSel = !locked && selected && selected.page === i && selected.index === idx;
    const poly = document.createElementNS(svg.namespaceURI, 'polygon');
    poly.setAttribute('points', z.points.map(p => p.join(',')).join(' '));
    poly.setAttribute('class', `zone zone-${z.mode}` + (isSel ? ' selected' : ''));
    poly.dataset.idx = idx;
    if (!locked) {
      poly.setAttribute('tabindex', '0');
      poly.setAttribute('role', 'button');
      poly.setAttribute('aria-label', zoneLabel(z, i, idx));
      poly.addEventListener('pointerdown', ev => beginZoneDrag(ev, i, idx));
      poly.addEventListener('contextmenu', ev => {
        ev.preventDefault(); ev.stopPropagation();
        keyboardNav = false;
        select(i, idx);
        showMenu(ev.clientX, ev.clientY, z, false);
      });
      poly.addEventListener('focus', () => {
        if (!selected || selected.page !== i || selected.index !== idx) {
          keyboardNav = true;
          select(i, idx);
        }
      });
      poly.addEventListener('keydown', ev => zoneKeydown(ev, i, idx, z));
      if (isSel) selEl = poly;
    }
    svg.appendChild(poly);
    if (isSel) renderHandles(svg, i, idx, z);
  });

  drawWatermarkPreview(svg, i);

  // re-rendering destroys the focused element: give it the focus back
  if (selEl && keyboardNav && !menu.contains(document.activeElement)) {
    selEl.focus({ preventScroll: true });
  }
  onZonesChanged(i);
}

function select(i, idx) {
  selected = { page: i, index: idx };
  renderZones(i);
  updateStatus();
}

function selectedEl() {
  if (!selected) return null;
  const pe = pageEls[selected.page];
  return pe ? pe.svg.querySelector(`.zone[data-idx="${selected.index}"]`) : null;
}

// The context menu used to be the only way to change a zone's mode: without a
// mouse (or without a right button) the zone could not be edited at all.
function zoneKeydown(ev, i, idx, z) {
  const k = ev.key;
  if (k === 'Enter' || k === ' ' || k === 'ContextMenu' || (k === 'F10' && ev.shiftKey)) {
    ev.preventDefault(); ev.stopPropagation();
    keyboardNav = true;
    select(i, idx);
    openMenuOnZone(z);
    return;
  }
  if (k === 'Delete' || k === 'Backspace') {
    ev.preventDefault(); ev.stopPropagation();
    deleteSelected();
  }
}

function renderHandles(svg, i, idx, z) {
  const { ux, uy } = unitScale(svg);
  const hw = 9 * ux, hh = 9 * uy;   // handles keep a constant on-screen size

  if (z.type === 'polygon') {
    z.points.forEach((pt, vi) => {
      const c = document.createElementNS(svg.namespaceURI, 'ellipse');
      c.setAttribute('cx', pt[0]); c.setAttribute('cy', pt[1]);
      c.setAttribute('rx', hw / 2); c.setAttribute('ry', hh / 2);
      c.setAttribute('class', 'handle handle-vertex');
      c.style.cursor = 'grab';
      c.addEventListener('pointerdown', ev => beginVertexDrag(ev, i, idx, vi));
      c.addEventListener('dblclick', ev => {
        ev.preventDefault(); ev.stopPropagation();
        if (z.points.length <= 3) return;   // a polygon keeps at least 3 vertices
        pushHistory();
        z.points.splice(vi, 1);
        renderZones(i); updateStatus();
      });
      svg.appendChild(c);
    });
    return;
  }

  // rectangle: 8 handles; freehand: the 4 corners only
  const set = z.type === 'rect' ? BOX_HANDLES : CORNER_HANDLES;
  const bb = bbox(z.points);
  set.forEach(([name, rx, ry, cursor]) => {
    const cx = bb[0] + (bb[2] - bb[0]) * rx;
    const cy = bb[1] + (bb[3] - bb[1]) * ry;
    const r = document.createElementNS(svg.namespaceURI, 'rect');
    r.setAttribute('x', cx - hw / 2); r.setAttribute('y', cy - hh / 2);
    r.setAttribute('width', hw); r.setAttribute('height', hh);
    r.setAttribute('class', 'handle');
    r.style.cursor = cursor;
    r.addEventListener('pointerdown', ev => beginResize(ev, i, idx, name));
    svg.appendChild(r);
  });
}

// ---------- editing: move / resize ----------
function beginZoneDrag(ev, i, idx) {
  if (!ev.isPrimary || ev.button !== 0) return;
  ev.stopPropagation();
  keyboardNav = false;
  selected = { page: i, index: idx };
  renderZones(i);
  closeMenu();

  const svg = pageEls[i].svg;
  const z = zones[i][idx];
  const start = toSvgPoint(svg, ev.clientX, ev.clientY);
  const orig = z.points.map(p => p.slice());
  const remember = onceHistory();
  let moved = false;

  startDrag(ev, e => {
    const p = toSvgPoint(svg, e.clientX, e.clientY);
    const dx = p[0] - start[0], dy = p[1] - start[1];
    if (!moved && Math.abs(dx) + Math.abs(dy) < 0.4) return;
    moved = true; remember();
    z.points = orig.map(([x, y]) => [x + dx, y + dy]);
    renderZones(i);
  }, (e, cancelled) => {
    if (cancelled && moved) { z.points = orig; renderZones(i); }
  });
}

function beginVertexDrag(ev, i, idx, vi) {
  if (!ev.isPrimary || ev.button !== 0) return;
  ev.stopPropagation();
  const svg = pageEls[i].svg;
  const z = zones[i][idx];
  const orig = z.points.map(p => p.slice());
  const remember = onceHistory();
  let moved = false;

  startDrag(ev, e => {
    if (!moved) { moved = true; remember(); }
    z.points[vi] = toSvgPoint(svg, e.clientX, e.clientY);
    renderZones(i);
  }, (e, cancelled) => {
    if (cancelled && moved) { z.points = orig; renderZones(i); }
  });
}

function beginResize(ev, i, idx, name) {
  if (!ev.isPrimary || ev.button !== 0) return;
  ev.stopPropagation();
  const svg = pageEls[i].svg;
  const z = zones[i][idx];
  const orig = z.points.map(p => p.slice());
  const bb = bbox(orig);
  const ow = (bb[2] - bb[0]) || 1, oh = (bb[3] - bb[1]) || 1;
  const remember = onceHistory();
  let moved = false;

  startDrag(ev, e => {
    if (!moved) { moved = true; remember(); }
    const p = toSvgPoint(svg, e.clientX, e.clientY);
    const [x0, y0, x1, y1] = edgesFrom(name, bb, p);
    if (z.type === 'rect') {
      z.points = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]];
    } else {
      // freehand: remap every point into the new box
      const nw = x1 - x0, nh = y1 - y0;
      z.points = orig.map(([x, y]) => [
        x0 + ((x - bb[0]) / ow) * nw,
        y0 + ((y - bb[1]) / oh) * nh,
      ]);
    }
    renderZones(i);
  }, (e, cancelled) => {
    if (cancelled && moved) { z.points = orig; renderZones(i); }
  });
}

// ---------- drawing ----------
function wireLayer(i, svg) {
  svg.addEventListener('pointerdown', e => {
    if (!e.isPrimary || e.button !== 0) return;
    if (deletedPages.has(i)) return;
    activePage = i;
    if (selected) { selected = null; closeMenu(); renderZones(i); }
    if (tool === 'rect') startRect(i, svg, e);
    else if (tool === 'freehand') startFreehand(i, svg, e);
  });
  svg.addEventListener('click', e => {
    if (tool === 'polygon' && !deletedPages.has(i)) polyClick(i, svg, e);
  });
  svg.addEventListener('dblclick', e => {
    if (tool !== 'polygon' || !pending || pending.page !== i) return;
    e.preventDefault();
    // the second click of the double-click already added a duplicate vertex
    if (pending.pts.length > 1) pending.pts.pop();
    finishPolygon();
  });
}

function addZone(i, zone) {
  pushHistory();
  (zones[i] = zones[i] || []).push(zone);
  activePage = i;
  renderZones(i);
  updateStatus();
}

function startRect(i, svg, e) {
  const p0 = toSvgPoint(svg, e.clientX, e.clientY);
  const ghost = document.createElementNS(svg.namespaceURI, 'rect');
  ghost.setAttribute('class', 'zone-ghost zone-ghost-fill');
  svg.appendChild(ghost);
  let last = p0;
  startDrag(e, ev => {
    last = toSvgPoint(svg, ev.clientX, ev.clientY);
    ghost.setAttribute('x', Math.min(p0[0], last[0]));
    ghost.setAttribute('y', Math.min(p0[1], last[1]));
    ghost.setAttribute('width', Math.abs(last[0] - p0[0]));
    ghost.setAttribute('height', Math.abs(last[1] - p0[1]));
  }, (ev, cancelled) => {
    ghost.remove();
    if (cancelled) return;
    const x0 = Math.min(p0[0], last[0]), y0 = Math.min(p0[1], last[1]);
    const x1 = Math.max(p0[0], last[0]), y1 = Math.max(p0[1], last[1]);
    if (x1 - x0 < 3 || y1 - y0 < 3) return;
    addZone(i, { type: 'rect', points: [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], mode: defaultMode });
  });
}

function startFreehand(i, svg, e) {
  const pts = [toSvgPoint(svg, e.clientX, e.clientY)];
  const line = document.createElementNS(svg.namespaceURI, 'polyline');
  line.setAttribute('class', 'zone-ghost');
  // shows what will actually be erased, updated as the stroke is drawn
  const hull = document.createElementNS(svg.namespaceURI, 'rect');
  hull.setAttribute('class', 'zone-ghost zone-ghost-hull');
  svg.append(hull, line);
  const syncHull = () => {
    const [x0, y0, x1, y1] = bbox(pts);
    hull.setAttribute('x', x0); hull.setAttribute('y', y0);
    hull.setAttribute('width', x1 - x0); hull.setAttribute('height', y1 - y0);
  };
  startDrag(e, ev => {
    const p = toSvgPoint(svg, ev.clientX, ev.clientY);
    const l = pts[pts.length - 1];
    if (Math.hypot(p[0] - l[0], p[1] - l[1]) < 2) return;
    pts.push(p);
    line.setAttribute('points', pts.map(x => x.join(',')).join(' '));
    syncHull();
  }, (ev, cancelled) => {
    line.remove(); hull.remove();
    if (cancelled) return;
    if (pts.length >= 3) addZone(i, { type: 'freehand', points: pts, mode: defaultMode });
  });
}

function polyClick(i, svg, e) {
  if (!pending || pending.page !== i) {
    cancelPending();
    const poly = document.createElementNS(svg.namespaceURI, 'polyline');
    poly.setAttribute('class', 'zone-ghost');
    const hull = document.createElementNS(svg.namespaceURI, 'rect');
    hull.setAttribute('class', 'zone-ghost zone-ghost-hull');
    svg.append(hull, poly);
    pending = { page: i, svg, poly, hull, pts: [] };
  }
  pending.pts.push(toSvgPoint(svg, e.clientX, e.clientY));
  pending.poly.setAttribute('points', pending.pts.map(pt => pt.join(',')).join(' '));
  const [x0, y0, x1, y1] = bbox(pending.pts);
  pending.hull.setAttribute('x', x0); pending.hull.setAttribute('y', y0);
  pending.hull.setAttribute('width', x1 - x0); pending.hull.setAttribute('height', y1 - y0);
}
function finishPolygon() {
  if (pending && pending.pts.length >= 3) {
    addZone(pending.page, { type: 'polygon', points: pending.pts, mode: defaultMode });
  }
  cancelPending();
}
function cancelPending() {
  if (pending) { pending.poly.remove(); pending.hull.remove(); }
  pending = null;
}

// ---------- context menu ----------
function showMenu(x, y, z, focusFirst) {
  $('zoneModeDelete').classList.toggle('active', z.mode === 'delete');
  $('zoneModeDelete').setAttribute('aria-checked', z.mode === 'delete');
  $('zoneModePixelate').classList.toggle('active', z.mode === 'pixelate');
  $('zoneModePixelate').setAttribute('aria-checked', z.mode === 'pixelate');
  menu.hidden = false;
  const r = menu.getBoundingClientRect();
  menu.style.left = Math.max(6, Math.min(x, window.innerWidth - r.width - 10)) + 'px';
  menu.style.top = Math.max(6, Math.min(y, window.innerHeight - r.height - 10)) + 'px';
  if (focusFirst) menuItems()[0].focus();
}

// opened from the keyboard: the menu anchors under the zone, not the cursor
function openMenuOnZone(z) {
  const el = selectedEl();
  if (!el) return;
  const r = el.getBoundingClientRect();
  showMenu(r.left, r.bottom + 4, z, true);
}

function menuItems() { return [...menu.querySelectorAll('.zm-item')]; }

function closeMenu(refocus) {
  if (menu.hidden) return;
  menu.hidden = true;
  if (refocus) {
    const el = selectedEl();
    if (el) el.focus({ preventScroll: true });
  }
}

menu.addEventListener('keydown', e => {
  const items = menuItems();
  const i = items.indexOf(document.activeElement);
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault();
    const d = e.key === 'ArrowDown' ? 1 : -1;
    items[(i + d + items.length) % items.length].focus();
  } else if (e.key === 'Escape' || e.key === 'Tab') {
    e.preventDefault();
    keyboardNav = true;
    closeMenu(true);
  }
});

function setSelectedMode(mode) {
  if (!selected) return;
  pushHistory();
  zones[selected.page][selected.index].mode = mode;
  renderZones(selected.page);
  $('zoneModeDelete').classList.toggle('active', mode === 'delete');
  $('zoneModeDelete').setAttribute('aria-checked', mode === 'delete');
  $('zoneModePixelate').classList.toggle('active', mode === 'pixelate');
  $('zoneModePixelate').setAttribute('aria-checked', mode === 'pixelate');
  updateStatus();
}
function deleteSelected() {
  if (!selected) return;
  const { page, index } = selected;
  pushHistory();
  zones[page].splice(index, 1);
  selected = null;
  closeMenu();
  renderZones(page); updateStatus();
}
$('zoneModeDelete').onclick = () => setSelectedMode('delete');
$('zoneModePixelate').onclick = () => setSelectedMode('pixelate');
$('zoneDelete').onclick = deleteSelected;
document.addEventListener('pointerdown', e => {
  if (!menu.hidden && !menu.contains(e.target)) closeMenu();
});

// ---------- toolbar ----------
function setTool(t) {
  tool = t;
  if (t !== 'polygon') cancelPending();
  document.querySelectorAll('.tool-btn').forEach(b => {
    const on = b.dataset.tool === t;
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', on);
  });
}
$('tool-rect').onclick = () => setTool('rect');
$('tool-polygon').onclick = () => setTool('polygon');
$('tool-freehand').onclick = () => setTool('freehand');

function setDefaultMode(m) {
  defaultMode = m;
  document.querySelectorAll('.mode-btn').forEach(b => {
    const on = b.dataset.mode === m;
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', on);
  });
}
$('mode-delete').onclick = () => setDefaultMode('delete');
$('mode-pixelate').onclick = () => setDefaultMode('pixelate');

$('undo').onclick = undo;
$('redo').onclick = redo;
$('clear').onclick = () => {
  if (!(zones[activePage] || []).length) return;
  pushHistory();
  delete zones[activePage];
  if (selected && selected.page === activePage) { selected = null; closeMenu(); }
  renderZones(activePage); updateStatus();
};

window.addEventListener('keydown', e => {
  if (!sid) return;
  const mod = e.ctrlKey || e.metaKey;
  const k = (e.key || '').toLowerCase();
  // Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y — insensitive to Shift and Caps Lock
  if (mod && k === 'z') { e.preventDefault(); e.shiftKey ? redo() : undo(); return; }
  if (mod && k === 'y') { e.preventDefault(); redo(); return; }
  if (mod) return;
  if (e.key === 'Escape') { cancelPending(); selected = null; closeMenu(); renderAll(); }
  if (e.key === 'Enter' && pending) finishPolygon();
  if (e.key === 'ContextMenu' && selected && menu.hidden) {
    e.preventDefault();
    keyboardNav = true;
    openMenuOnZone(zones[selected.page][selected.index]);
  }
  if ((e.key === 'Delete' || e.key === 'Backspace') && selected) { e.preventDefault(); deleteSelected(); }
});
// handles have a fixed on-screen size: they must be redrawn on resize
window.addEventListener('resize', () => { if (selected) renderZones(selected.page); });

// ---------- help ----------
const help = $('helpPop');
$('helpBtn').onclick = e => {
  e.stopPropagation();
  help.hidden = !help.hidden;
  $('helpBtn').setAttribute('aria-expanded', !help.hidden);
};
document.addEventListener('pointerdown', e => {
  if (!help.hidden && !help.contains(e.target) && e.target !== $('helpBtn')) {
    help.hidden = true;
    $('helpBtn').setAttribute('aria-expanded', 'false');
  }
});

function syncButtons() {
  const total = Object.values(zones).reduce((a, b) => a + b.length, 0);
  const n = (zones[activePage] || []).length;
  $('undo').disabled = busy || history.length === 0;
  $('redo').disabled = busy || redoStack.length === 0;
  $('clear').disabled = busy || !n;
  $('export').disabled = busy || !(total || deletedPages.size || watermarkValue());
}

function updateStatus() {
  const total = Object.values(zones).reduce((a, b) => a + b.length, 0);
  const n = (zones[activePage] || []).length;
  $('pnum').textContent = pages.length ? `${activePage + 1} / ${pages.length}` : '— / —';
  syncButtons();
  if (busy) return;   // do not overwrite a progress message
  const delTxt = deletedPages.size ? `, ${deletedPages.size} page(s) deleted` : '';
  setStatus(pages.length
    ? `${n} zone(s) on the active page, ${total} in total${delTxt}.`
    : 'No document.');
}

// ---------- export ----------
$('export').onclick = async () => {
  if (busy) return;   // the button stayed live: a double-click exported twice
  setBusy(true, 'Processing…');
  try {
    const r = await fetch('/api/export', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        sid, zones, strip_meta: $('meta').checked, deleted_pages: [...deletedPages],
        watermark: $('wm').value,
      })
    });
    if (!r.ok) {
      const msg = await r.text().catch(() => '');
      setBusy(false);
      setStatus('Error: ' + (msg || `response ${r.status}`), 'warn');
      return;
    }
    const d = await r.json();
    const a = document.createElement('a'); a.href = d.download; a.download = d.filename;
    document.body.appendChild(a); a.click(); a.remove();
    setBusy(false);
    // leak content comes from the PDF: never innerHTML with it.
    if (d.leak_count) {
      const detail = d.leaks.map(l => `p${l.page} ${l.kind} "${l.text}"`).join(', ');
      setStatus(`Warning: ${d.leak_count} item(s) remain inside the zones (${detail}). Check the result.`, 'warn');
    } else {
      setStatus('Export done, no residue detected inside the zones.', 'ok');
    }
  } catch (err) {
    setBusy(false);
    setStatus('Error: ' + (err.message || 'server unreachable'), 'warn');
  }
};

// Preview and Export button follow the watermark field live. The button reacts
// on every keystroke, the preview waits for a pause: redrawing every page on
// each key makes typing stutter on a long document.
let wmTimer = null;
$('wm').addEventListener('input', () => {
  updateStatus();
  clearTimeout(wmTimer);
  wmTimer = setTimeout(renderAll, 120);
});

setTool('rect');
setDefaultMode('delete');
