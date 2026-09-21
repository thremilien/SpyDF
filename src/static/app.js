const $ = id => document.getElementById(id);

// zones[page] = [{type:'rect'|'polygon'|'freehand', points:[[x,y],...], mode:'delete'|'pixelate'}]
// Points are in PDF coordinates.
let sid = null, pages = [], zones = {}, docName = '';
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

// ---------- signed out mid-session ----------
// The login cookie can expire while the tab stays open. Every call that meets a
// 401 hands the user back to the login page: the alternative is an error string
// in the status bar for something no retry can fix.
function signedOut(status) {
  if (status !== 401) return false;
  location.href = '/login';
  return true;
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
      if (signedOut(xhr.status)) return;
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

  sid = d.sid; pages = d.pages; docName = d.name || f.name;
  // redoStack used to be left out of this reset, so Ctrl+Y pasted zones from
  // the previous document onto the new one.
  zones = {}; history = []; redoStack = [];
  activePage = 0; selected = null; deletedPages = new Set();
  cancelPending();
  resetZoom();   // a new document starts at 100%
  showDocument();
  setStatus(`Rendering page 1 of ${pages.length}…`, 'busy-text');
  buildPages();
  awaitFirstPage();
}

// The first image can take seconds: hand control back (and free the status
// bar) only once the page is actually on screen.
function awaitFirstPage() {
  const pe = pageEls[0];
  if (!pe) { setBusy(false); updateStatus(); return; }
  // Ask for the first page rather than waiting to be offered it. Every other
  // request comes from the load observer, and a tab that is not in the
  // foreground receives no observer callbacks at all — switching away while the
  // upload runs is enough. The document then sits blank under "Rendering page
  // 1" until the safety net below gives up, fifteen seconds later.
  loadPage(0);
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

// From the drop zone to the document: the stage centres what it holds while
// there is nothing to scroll, and must stop once a page is taller than it, or
// the top of that page is centred out of reach.
function showDocument() {
  $('drop').hidden = true;
  pagesEl.hidden = false;
  $('workspace').classList.remove('no-doc');
}

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

// The stage resizes without the window doing so — a document opening, for
// instance — so this watches it directly: the pages are re-laid out, then
// re-requested sharper.
const paneResize = new ResizeObserver(() => {
  syncPageWidth();
  if (sid) scheduleSharpen();
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
  // negative: SVG turns clockwise (y grows downwards), and the stamp runs
  // bottom-left to top-right. With +angle the preview leaned the other way.
  t.setAttribute('transform', `rotate(${-angleDeg} ${cx} ${cy})`);
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
    // a delete zone shows the cover it will paint; a pixelate one keeps its mosaic
    if (z.mode !== 'pixelate') poly.style.fill = rgbCss(zoneColor(z));
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
        dropSelection();
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
  // the pipette reads the page under the zone, so a click on one still samples
  if (picking) { ev.preventDefault(); ev.stopPropagation(); pickAt(i, pageEls[i].svg, ev); return; }
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
// A selection made despite the guards (an extension forcing one, a drag that
// started outside the page) is dropped rather than left highlighted.
function dropSelection() {
  const sel = window.getSelection();
  if (sel && !sel.isCollapsed) sel.removeAllRanges();
}

function wireLayer(i, svg) {
  svg.addEventListener('pointerdown', e => {
    if (!e.isPrimary || e.button !== 0) return;
    if (picking) { e.preventDefault(); e.stopPropagation(); pickAt(i, svg, e); return; }
    if (deletedPages.has(i)) return;
    activePage = i;
    if (selected) { selected = null; closeMenu(); renderZones(i); }
    if (tool === 'rect') startRect(i, svg, e);
    else if (tool === 'freehand') startFreehand(i, svg, e);
  });
  // The second click of a double-click is a word selection for the browser:
  // left alone it selects the nearest text on the page — which a translation
  // extension then picks up. The gesture belongs to the polygon tool, so it is
  // taken before the browser acts on it.
  svg.addEventListener('mousedown', e => { if (e.detail > 1) e.preventDefault(); });
  svg.addEventListener('selectstart', e => e.preventDefault());
  svg.addEventListener('click', e => {
    if (tool === 'polygon' && !deletedPages.has(i)) polyClick(i, svg, e);
  });
  svg.addEventListener('dblclick', e => {
    e.preventDefault();
    e.stopPropagation();
    dropSelection();
    if (tool !== 'polygon' || !pending || pending.page !== i) return;
    // the second click of the double-click already added a duplicate vertex
    if (pending.pts.length > 1) pending.pts.pop();
    finishPolygon();
  });
}

// ---------- the cover colour ----------
// A white cover on a coloured scan is itself a mark: it says "something was
// here". The default is the page's paper colour, read by the server when the
// document was opened (`pages[i].bg`, see src/background.py).
const WHITE_RGB = [255, 255, 255];
let pageCanvas = { src: null, ctx: null, w: 0, h: 0 };

function rgbCss(c) { return `rgb(${c[0]}, ${c[1]}, ${c[2]})`; }
function zoneColor(z) { return z.color || WHITE_RGB; }

// The rendered page, on a canvas we can read. Same origin, so no tainting.
function pageContext(i) {
  const pe = pageEls[i];
  if (!pe || !pe.img.complete || !pe.img.naturalWidth) return null;
  if (pageCanvas.src !== pe.img.src) {
    const cv = document.createElement('canvas');
    cv.width = pe.img.naturalWidth;
    cv.height = pe.img.naturalHeight;
    const ctx = cv.getContext('2d', { willReadFrequently: true });
    try { ctx.drawImage(pe.img, 0, 0); } catch { return null; }
    pageCanvas = { src: pe.img.src, ctx, w: cv.width, h: cv.height };
  }
  return pageCanvas;
}

// PDF point -> pixel of the rendered page
function toPixel(i, x, y, cv) {
  const p = pages[i];
  return [
    Math.min(cv.w - 1, Math.max(0, Math.round((x - p.x0) / p.w * cv.w))),
    Math.min(cv.h - 1, Math.max(0, Math.round((y - p.y0) / p.h * cv.h))),
  ];
}

function pixelAt(i, x, y) {
  const cv = pageContext(i);
  if (!cv) return null;
  const [px, py] = toPixel(i, x, y, cv);
  try {
    const d = cv.ctx.getImageData(px, py, 1, 1).data;
    return [d[0], d[1], d[2]];
  } catch { return null; }
}

// The paper of page i: the default fill of every delete zone drawn on it.
function pageBackground(i) {
  const bg = pages[i] && pages[i].bg;
  return Array.isArray(bg) && bg.length === 3 ? [...bg] : [...WHITE_RGB];
}

function addZone(i, zone) {
  if (!zone.color) zone.color = pageBackground(i);
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
  syncColorFields(z);
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

function menuItems() { return [...menu.querySelectorAll('.zm-item, .zm-num')]; }

// ---------- the colour of the selected zone ----------
const RGB_FIELDS = ['zoneR', 'zoneG', 'zoneB'];

function syncColorFields(z) {
  const c = zoneColor(z);
  RGB_FIELDS.forEach((id, k) => { $(id).value = c[k]; });
  $('zoneSwatch').style.background = rgbCss(c);
  const pixelate = z.mode === 'pixelate';
  $('zoneColorRow').classList.toggle('is-off', pixelate);
  $('zoneColorRow').title = pixelate
    ? 'A pixelated zone is covered by its own mosaic, not by a colour.'
    : 'Colour of the cover painted over this zone.';
}

// One history entry per editing burst: typing three channels, or dragging
// through the pipette, is one change to undo.
let colorHistory = null;
function beginColorEdit() {
  if (!colorHistory) colorHistory = onceHistory();
  colorHistory();
}
function endColorEdit() { colorHistory = null; }

function setSelectedColor(c) {
  if (!selected) return;
  beginColorEdit();
  const z = zones[selected.page][selected.index];
  z.color = c;
  syncColorFields(z);
  renderZones(selected.page);
  updateStatus();
}

function readColorFields() {
  return RGB_FIELDS.map(id => {
    const v = Math.round(Number($(id).value));
    return Number.isFinite(v) ? Math.min(255, Math.max(0, v)) : 0;
  });
}

RGB_FIELDS.forEach(id => {
  $(id).addEventListener('input', () => setSelectedColor(readColorFields()));
  $(id).addEventListener('change', () => { setSelectedColor(readColorFields()); endColorEdit(); });
  // the menu closes on Escape/Tab; a number field must keep its own keys
  $(id).addEventListener('keydown', e => e.stopPropagation());
});

// ---------- pipette ----------
// Reads the rendered page, not the screen: clicking over a zone still samples
// the paper underneath rather than the cover drawn on top.
let picking = false;

function setPicking(on) {
  picking = on;
  document.body.classList.toggle('picking', on);
  $('zonePick').classList.toggle('active', on);
  $('zonePick').setAttribute('aria-pressed', on);
  if (on) setStatus('Pipette: click the page to take its colour (Esc to cancel).');
  else updateStatus();
}

function pickAt(i, svg, e) {
  const [x, y] = toSvgPoint(svg, e.clientX, e.clientY);
  const c = pixelAt(i, x, y);
  setPicking(false);
  if (!c) { setStatus('This page is not rendered yet: no colour to take.', 'warn'); return; }
  setSelectedColor(c);
  endColorEdit();
}

$('zonePick').onclick = () => {
  if (!selected) return;
  setPicking(!picking);
  closeMenu();
};

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

// ---------- zoom ----------
// One factor for the whole stage: pages size themselves from --page-w and
// --zoom, so scroll position and page size never drift apart.
const PAGE_MAX_W = 880;      // the width of a page at 100%, when the stage is wide enough
const PAGE_MIN_W = 120;
const ZOOM_MIN = 0.25, ZOOM_MAX = 5;
const ZOOM_STEPS = [0.25, 0.33, 0.5, 0.67, 0.8, 1, 1.25, 1.5, 2, 2.5, 3, 4, 5];
const ZOOM_WHEEL = 1.0015;   // per wheel pixel: one notch ≈ 16%
const PAN_STEP = 60;         // arrow keys, in screen pixels
const LINE_PX = 16, PAGE_PX = 400;   // wheel deltas reported in lines or pages

let zoom = 1;

// The base width of a page: as wide as the stage allows, capped. Set from
// here rather than in CSS because a percentage would resolve against a
// container that is itself sized by its pages once zoomed in.
function syncPageWidth() {
  const cs = getComputedStyle(stageEl);
  const inner = stageEl.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
  const w = Math.min(PAGE_MAX_W, Math.max(PAGE_MIN_W, inner));
  document.documentElement.style.setProperty('--page-w', `${Math.round(w)}px`);
}

// Zooming keeps one point still. That point is read as "page i, at fraction
// (fx, fy) of it" — the same terms as the scroll synchronisation — then put
// back under the same screen position once the pages have been resized.
function anchorAt(cx, cy) {
  const els = pageEls.map(pe => pe.container);
  for (let i = 0; i < els.length; i++) {
    if (!els[i]) continue;
    const r = els[i].getBoundingClientRect();
    if (cy < r.bottom || i === els.length - 1) {
      return {
        i, cx, cy,
        fx: r.width ? (cx - r.left) / r.width : 0,
        fy: r.height ? (cy - r.top) / r.height : 0,
      };
    }
  }
  return null;
}
function centerAnchor() {
  const r = stageEl.getBoundingClientRect();
  return anchorAt(r.left + r.width / 2, r.top + r.height / 2);
}
function restoreAnchor(a) {
  const el = a && pageEls[a.i] && pageEls[a.i].container;
  if (!el) return;
  const r = el.getBoundingClientRect();   // forces the new layout: read after the zoom
  stageEl.scrollLeft += r.left + a.fx * r.width - a.cx;
  stageEl.scrollTop += r.top + a.fy * r.height - a.cy;
}

function setZoom(z, anchor) {
  z = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z));
  if (Math.abs(z - zoom) < 0.001) return;
  zoom = z;
  document.documentElement.style.setProperty('--zoom', zoom);
  updateZoomUI();
  if (selected) renderZones(selected.page);   // handles keep a constant on-screen size
  if (anchor) restoreAnchor(anchor);
  scheduleSharpen();
}

function resetZoom() {
  zoom = 1;
  document.documentElement.style.setProperty('--zoom', 1);
  syncPageWidth();
  updateZoomUI();
}

function stepZoom(dir, anchor) {
  const next = dir > 0
    ? ZOOM_STEPS.find(s => s > zoom + 0.001)
    : ZOOM_STEPS.filter(s => s < zoom - 0.001).pop();
  setZoom(next === undefined ? (dir > 0 ? ZOOM_MAX : ZOOM_MIN) : next, anchor);
}

function zoomFromKeyboard(dir) {
  if (dir === 0) setZoom(1, centerAnchor());
  else stepZoom(dir, centerAnchor());
}

function updateZoomUI() {
  $('zoomLevel').textContent = `${Math.round(zoom * 100)}%`;
  const off = !sid;
  $('zoomIn').disabled = off || zoom >= ZOOM_MAX - 0.001;
  $('zoomOut').disabled = off || zoom <= ZOOM_MIN + 0.001;
  $('zoomLevel').disabled = off;
}

// Rendering a page is CPU-bound server-side: wait for the zoom to settle before
// asking for sharper images.
let sharpenTimer = null;
function scheduleSharpen() {
  clearTimeout(sharpenTimer);
  sharpenTimer = setTimeout(sharpenVisible, 250);
}
function sharpenVisible() {
  if (!sid) return;
  pageEls.forEach((pe, i) => {
    const r = pe.container.getBoundingClientRect();
    if (r.bottom > -window.innerHeight && r.top < window.innerHeight * 2) loadPage(i);
  });
}

$('zoomIn').onclick = () => zoomFromKeyboard(1);
$('zoomOut').onclick = () => zoomFromKeyboard(-1);
$('zoomLevel').onclick = () => zoomFromKeyboard(0);

// ---------- panning ----------
// Middle-button drag, or Space held with the left button: the two habitual ways
// to drag a zoomed page around. While Space is held the drawing layer lets the
// pointer through (CSS .pan-ready), so a drag scrolls instead of drawing.
let spaceDown = false;

function setPanReady(on) {
  if (spaceDown === on) return;
  spaceDown = on;
  document.body.classList.toggle('pan-ready', on);
}

function wirePane(pane) {
  pane.addEventListener('wheel', e => {
    if (!sid || !(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();   // this gesture is ours, not the browser's page zoom
    const d = e.deltaMode === 1 ? e.deltaY * LINE_PX
      : e.deltaMode === 2 ? e.deltaY * PAGE_PX : e.deltaY;
    setZoom(zoom * Math.pow(ZOOM_WHEEL, -d), anchorAt(e.clientX, e.clientY));
  }, { passive: false });

  pane.addEventListener('pointerdown', e => {
    if (e.button !== 1 && !(spaceDown && e.button === 0)) return;
    e.preventDefault();
    const sx = pane.scrollLeft, sy = pane.scrollTop;
    const x0 = e.clientX, y0 = e.clientY;
    document.body.classList.add('panning');
    startDrag(e, ev => {
      pane.scrollLeft = sx - (ev.clientX - x0);
      pane.scrollTop = sy - (ev.clientY - y0);
    }, () => document.body.classList.remove('panning'));
  });
  // without this the middle button opens the browser's auto-scroll widget
  pane.addEventListener('auxclick', e => { if (e.button === 1) e.preventDefault(); });
}

// Arrows and page keys scroll the stage, as in any viewer. Ignored as soon as
// something is focused: a zone, the watermark field or the zone menu answers
// them itself.
const PAN_KEYS = {
  ArrowUp: [0, -PAN_STEP], ArrowDown: [0, PAN_STEP],
  ArrowLeft: [-PAN_STEP, 0], ArrowRight: [PAN_STEP, 0],
};
function panKey(e) {
  const step = PAN_KEYS[e.key];
  if (step) { stageEl.scrollBy(step[0], step[1]); return true; }
  const h = stageEl.clientHeight * 0.9;
  if (e.key === 'PageDown') { stageEl.scrollBy(0, h); return true; }
  if (e.key === 'PageUp') { stageEl.scrollBy(0, -h); return true; }
  if (e.key === 'Home') { stageEl.scrollTo({ top: 0 }); return true; }
  if (e.key === 'End') { stageEl.scrollTo({ top: stageEl.scrollHeight }); return true; }
  return false;
}

wirePane(stageEl);
paneResize.observe(stageEl);

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
  // Ctrl +/-/0, as everywhere else — taken from the browser, which would
  // otherwise scale the whole interface instead of the pages.
  if (mod && (k === '+' || k === '=' || k === 'add')) { e.preventDefault(); zoomFromKeyboard(1); return; }
  if (mod && (k === '-' || k === '_' || k === 'subtract')) { e.preventDefault(); zoomFromKeyboard(-1); return; }
  if (mod && k === '0') { e.preventDefault(); zoomFromKeyboard(0); return; }
  if (mod) return;
  const idle = !document.activeElement || document.activeElement === document.body;
  if (e.code === 'Space' && idle) { e.preventDefault(); setPanReady(true); return; }
  if (idle && menu.hidden && panKey(e)) { e.preventDefault(); return; }
  if (e.key === 'Escape') {
    if (picking) { setPicking(false); return; }
    cancelPending(); selected = null; closeMenu(); renderAll();
  }
  if (e.key === 'Enter' && pending) finishPolygon();
  if (e.key === 'ContextMenu' && selected && menu.hidden) {
    e.preventDefault();
    keyboardNav = true;
    openMenuOnZone(zones[selected.page][selected.index]);
  }
  if ((e.key === 'Delete' || e.key === 'Backspace') && selected) { e.preventDefault(); deleteSelected(); }
});
// the key can be released over another window, or the window lose the focus
// mid-pan: either way the hand cursor must not stay on
window.addEventListener('keyup', e => { if (e.code === 'Space') setPanReady(false); });
window.addEventListener('blur', () => {
  setPanReady(false);
  document.body.classList.remove('panning');
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
  const n = (zones[activePage] || []).length;
  $('undo').disabled = busy || history.length === 0;
  $('redo').disabled = busy || redoStack.length === 0;
  $('clear').disabled = busy || !n;
  // an untouched document exports too: it is still flattened and re-read
  $('export').disabled = busy || !sid;
  updateZoomUI();
}

function updateStatus() {
  const total = Object.values(zones).reduce((a, b) => a + b.length, 0);
  const n = (zones[activePage] || []).length;
  $('pnum').textContent = pages.length ? `${activePage + 1} / ${pages.length}` : '— / —';
  syncButtons();
  // every mutation of the zones or the pages ends here, so this is the one
  // place the reload snapshot has to be written from
  saveState();
  if (busy) return;   // do not overwrite a progress message
  const delTxt = deletedPages.size ? `, ${deletedPages.size} page(s) deleted` : '';
  setStatus(pages.length
    ? `${n} zone(s) on the active page, ${total} in total${delTxt}.`
    : 'No document.');
}

// ---------- export progress ----------
// The export is one long POST; a second endpoint says how far it has got, and
// the overlay shows it. A failed poll is ignored: the POST alone decides the end.
const xp = {
  el: $('exportOverlay'), bar: $('xpBar'), fill: $('xpFill'),
  label: $('xpLabel'), title: $('xpTitle'), count: $('xpCount'), pct: $('xpPct'),
  sheet: $('xpSheet'), timer: null, hideTimer: null, lastFocus: null, lastDone: -1,
};

// restart the sheet's slide-in: one finished page, the next one comes in
function xpTurn() {
  xp.sheet.classList.remove('turn');
  void xp.sheet.offsetWidth;
  xp.sheet.classList.add('turn');
}

function xpRender(p) {
  const known = p && p.total > 0;
  const tail = p && (p.phase === 'starting' || p.phase === 'finishing');
  const indeterminate = !known || tail || !p;
  // the sheet only sweeps while pages are actually being read
  xp.el.dataset.phase = p && p.phase === 'pages' && known ? 'pages'
    : p && p.phase === 'finishing' ? 'finishing' : 'starting';
  if (known && p.phase === 'pages' && p.done !== xp.lastDone) {
    if (xp.lastDone >= 0) xpTurn();
    xp.lastDone = p.done;
  }
  xp.bar.classList.toggle('is-indeterminate', indeterminate);
  if (p && p.phase === 'finishing') {
    xp.label.textContent = 'Finishing the document…';
  } else if (known && p.phase === 'pages') {
    const page = Math.min(p.done + 1, p.total);
    xp.label.textContent = `Flattening and reading page ${page} of ${p.total}`;
  } else {
    xp.label.textContent = 'Preparing…';
  }
  if (indeterminate) {
    xp.bar.removeAttribute('aria-valuenow');
    xp.pct.textContent = '';
  } else {
    const v = Math.round(100 * p.done / p.total);
    xp.fill.style.width = v + '%';
    xp.bar.setAttribute('aria-valuenow', v);
    xp.pct.textContent = v + '%';
  }
  xp.count.textContent = known ? `${p.done} of ${p.total} pages done` : '';
}

async function xpPoll() {
  try {
    const r = await fetch(`/api/export/progress/${sid}`);
    if (signedOut(r.status)) return;
    if (r.ok && xp.timer) xpRender(await r.json());
  } catch { /* the POST reports the real failure */ }
}

function xpOpen() {
  clearTimeout(xp.hideTimer);
  xp.lastFocus = document.activeElement;
  xp.lastDone = -1;
  xp.sheet.classList.remove('turn');
  xp.title.textContent = 'Exporting';
  xp.fill.style.width = '0%';
  xpRender(null);
  xp.el.hidden = false;
  requestAnimationFrame(() => xp.el.classList.add('is-open'));
  xp.el.querySelector('.xp-card').tabIndex = -1;
  xp.el.querySelector('.xp-card').focus();
  xp.timer = setInterval(xpPoll, 450);
  xpPoll();
}

// ok: hold a full, checked bar for a moment before fading out
function xpClose(ok, pages) {
  clearInterval(xp.timer); xp.timer = null;
  const fade = () => {
    xp.el.classList.remove('is-open');
    xp.hideTimer = setTimeout(() => {
      xp.el.hidden = true;
      if (xp.lastFocus && xp.lastFocus.focus) xp.lastFocus.focus();
    }, 220);
  };
  if (!ok) { fade(); return; }
  xp.bar.classList.remove('is-indeterminate');
  xp.fill.style.width = '100%';
  xp.bar.setAttribute('aria-valuenow', 100);
  xp.pct.textContent = '100%';
  // the last page can finish between two polls: the count comes from the answer
  if (pages) xp.count.textContent = `${pages} of ${pages} pages done`;
  xp.title.textContent = 'Exported';
  xp.label.textContent = 'The download is starting.';
  xp.el.dataset.phase = 'done';
  xp.hideTimer = setTimeout(fade, 900);
}

// ---------- export ----------
$('export').onclick = async () => {
  if (busy) return;   // the button stayed live: a double-click exported twice
  setBusy(true, 'Processing…');
  xpOpen();
  let ok = false, pages = 0;
  try {
    const r = await fetch('/api/export', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        sid, zones, deleted_pages: [...deletedPages],
        watermark: $('wm').value,
      })
    });
    if (signedOut(r.status)) return;
    if (!r.ok) {
      const msg = await r.text().catch(() => '');
      setBusy(false);
      setStatus('Error: ' + (msg || `response ${r.status}`), 'warn');
      return;
    }
    const d = await r.json();
    const a = document.createElement('a'); a.href = d.download; a.download = d.filename;
    document.body.appendChild(a); a.click(); a.remove();
    ok = true; pages = d.pages;
    setBusy(false);
    if (d.ocr === 'ok') {
      setStatus(`Export done: ${d.pages} page(s) flattened and reindexed (${d.fragments} text fragments).`, 'ok');
    } else if (d.ocr === 'partial') {
      setStatus(`Export done: only ${d.indexed} of ${d.pages} page(s) could be reindexed.`, 'warn');
    } else {
      setStatus(`Export done: ${d.pages} page(s) flattened. No text layer — OCR is unavailable on this install.`, 'warn');
    }
  } catch (err) {
    setBusy(false);
    setStatus('Error: ' + (err.message || 'server unreachable'), 'warn');
  } finally {
    xpClose(ok, pages);
  }
};

// ---------- surviving a reload ----------
// The document itself never left the server, but the page held the only copy of
// the session id, of the zones and of the page geometry — so a reload, wanted or
// not, threw away an afternoon of marking and asked for the file again.
//
// sessionStorage rather than localStorage: it survives a reload, including
// Ctrl+F5, and goes when the tab does. A marking is not content — it says where
// a name sits on the page, not what it says — but it has no reason to outlive
// the sitting either, and the document it belongs to expires server-side anyway.
const STATE_KEY = 'spydf-session';

function saveState() {
  if (!sid) return;
  try {
    sessionStorage.setItem(STATE_KEY, JSON.stringify({
      sid, zones,
      deleted: [...deletedPages],
      watermark: $('wm').value,
    }));
  } catch {
    // a full or disabled storage costs the reload safety net, nothing else
  }
}

function clearState() {
  try { sessionStorage.removeItem(STATE_KEY); } catch { /* nothing to clear */ }
}

// Only the marking is kept here; the document, its name and its page geometry
// are asked back from the server, which is the one that still has them.
async function restoreState() {
  let saved;
  try { saved = JSON.parse(sessionStorage.getItem(STATE_KEY) || 'null'); } catch { saved = null; }
  if (!saved || !saved.sid) return;

  const r = await fetch(`/api/session/${saved.sid}`).catch(() => null);
  // A 401 says the login expired, not the document: the marking is kept, and the
  // login page brings the tab back here with it.
  if (r && signedOut(r.status)) return;
  if (!r || !r.ok) { clearState(); return; }   // expired, or the server restarted
  const d = await r.json();

  sid = d.sid; pages = d.pages; docName = d.name;
  zones = saved.zones && typeof saved.zones === 'object' ? saved.zones : {};
  deletedPages = new Set(Array.isArray(saved.deleted) ? saved.deleted : []);
  history = []; redoStack = [];   // the undo stack belongs to the page that is gone
  activePage = 0; selected = null;
  if (typeof saved.watermark === 'string') $('wm').value = saved.watermark;

  setBusy(true, 'Picking the document back up…');
  showDocument();
  buildPages();
  syncDeletedUI();
  renderAll();   // buildPages lays out empty pages: the zones are drawn here
  awaitFirstPage();
}

restoreState();

// The preview follows the watermark field once typing pauses: redrawing every
// page on each key makes typing stutter on a long document.
let wmTimer = null;
$('wm').addEventListener('input', () => {
  updateStatus();
  clearTimeout(wmTimer);
  wmTimer = setTimeout(renderAll, 120);
});

setTool('rect');
setDefaultMode('delete');
syncPageWidth();
updateZoomUI();
