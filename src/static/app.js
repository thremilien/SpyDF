const $ = id => document.getElementById(id);

// zones[page] = [{type:'rect'|'polygon'|'freehand', points:[[x,y],...], mode:'delete'|'pixelate'}]
// Les points sont en coordonnées PDF.
let sid = null, pages = [], zones = {};
let tool = 'rect';
let defaultMode = 'delete';
let activePage = 0;
let selected = null;      // {page, index}
let pending = null;       // polygone en cours de tracé
let deletedPages = new Set();
let history = [];         // instantanés JSON pour l'annulation
let redoStack = [];
const pageEls = [];

const pagesEl = $('pages'), menu = $('zoneMenu');

const ICON_TRASH = '<svg viewBox="0 0 18 18" fill="none"><path d="M4 5.5h10M7.5 5.5V4a1 1 0 011-1h1a1 1 0 011 1v1.5M5.5 5.5l.6 8a1 1 0 001 .9h3.8a1 1 0 001-.9l.6-8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const ICON_RESTORE = '<svg viewBox="0 0 18 18" fill="none"><path d="M4 8h7a3.5 3.5 0 010 7H8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M6.5 5L4 8l2.5 3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';

// poignées de redimensionnement: [nom, ratio x, ratio y, curseur]
const BOX_HANDLES = [
  ['nw', 0, 0, 'nwse-resize'], ['n', .5, 0, 'ns-resize'], ['ne', 1, 0, 'nesw-resize'],
  ['e', 1, .5, 'ew-resize'], ['se', 1, 1, 'nwse-resize'], ['s', .5, 1, 'ns-resize'],
  ['sw', 0, 1, 'nesw-resize'], ['w', 0, .5, 'ew-resize'],
];
const CORNER_HANDLES = BOX_HANDLES.filter(h => h[0].length === 2);

// ---------- historique ----------
// Instantanés complets: tout passe par pushHistory() AVANT mutation, donc
// tracé, déplacement, redimensionnement, mode, suppression de zone ou de page
// sont tous annulables de la même façon.
function snapshot() { return JSON.stringify({ z: zones, d: [...deletedPages] }); }
function restore(s) {
  const d = JSON.parse(s);
  zones = d.z; deletedPages = new Set(d.d);
  selected = null; menu.hidden = true;
  renderAll(); syncDeletedUI(); updateStatus();
}
function pushHistory() {
  history.push(snapshot());
  if (history.length > 200) history.shift();
  redoStack.length = 0;   // une nouvelle action invalide le redo
}
// Renvoie une fonction qui n'enregistre l'état qu'au premier appel réel:
// évite de polluer l'historique quand un clic ne déplace rien.
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

// ---------- ouverture ----------
async function openFile(f) {
  if (!f) return;
  if (f.type !== 'application/pdf' && !f.name.toLowerCase().endsWith('.pdf')) {
    alert("Ce fichier n'est pas un PDF.");
    return;
  }
  const fd = new FormData();
  fd.append('file', f);
  const r = await fetch('/api/open', { method: 'POST', body: fd });
  if (!r.ok) { alert('Erreur : ' + await r.text()); return; }
  const d = await r.json();
  sid = d.sid; pages = d.pages;
  zones = {}; history = []; activePage = 0; selected = null; deletedPages = new Set();
  $('drop').hidden = true; pagesEl.hidden = false;
  buildPages();
  updateStatus();
}

$('file').onchange = e => openFile(e.target.files[0]);

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

// ---------- construction des pages ----------
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

    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'page-layer');
    svg.setAttribute('viewBox', `${p.x0} ${p.y0} ${p.w} ${p.h}`);
    svg.setAttribute('preserveAspectRatio', 'none');

    const badge = document.createElement('div');
    badge.className = 'page-deleted-badge';
    badge.innerHTML = '<span>Page supprimée</span>';

    const delBtn = document.createElement('button');
    delBtn.className = 'page-del-btn';
    delBtn.type = 'button';
    delBtn.innerHTML = ICON_TRASH;
    delBtn.title = 'Supprimer cette page';
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
  if (selected && selected.page === i) { selected = null; menu.hidden = true; }
  syncDeletedUI(); renderZones(i); updateStatus();
}

function syncDeletedUI() {
  pageEls.forEach((pe, i) => {
    const del = deletedPages.has(i);
    pe.container.classList.toggle('deleted', del);
    pe.delBtn.classList.toggle('is-deleted', del);
    pe.delBtn.innerHTML = del ? ICON_RESTORE : ICON_TRASH;
    pe.delBtn.title = del ? 'Restaurer cette page' : 'Supprimer cette page';
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

// Largeur de rendu = pixels écran réellement occupés par la page.
function wantedWidth(pe) {
  const css = pe.container.clientWidth || 880;
  return Math.round(css * (window.devicePixelRatio || 1));
}

function loadPage(i) {
  const pe = pageEls[i];
  if (!pe) return;
  const w = wantedWidth(pe);
  // on ne recharge que si l'on gagne vraiment en netteté
  if (pe.loaded && w <= pe.renderedAt * 1.15) return;
  pe.loaded = true;
  pe.renderedAt = w;
  pe.img.src = `/api/page/${sid}/${i}?w=${w}`;
}

// si la fenêtre s'élargit, on redemande les pages visibles en plus net
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

// ---------- géométrie ----------
function toSvgPoint(svg, clientX, clientY) {
  const pt = svg.createSVGPoint();
  pt.x = clientX; pt.y = clientY;
  const p = pt.matrixTransform(svg.getScreenCTM().inverse());
  return [p.x, p.y];
}
// Le viewBox est étiré (preserveAspectRatio=none): l'échelle diffère en x et y.
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

function startDrag(onMove, onEnd) {
  const move = e => onMove(e);
  const up = e => {
    window.removeEventListener('mousemove', move);
    window.removeEventListener('mouseup', up);
    if (onEnd) onEnd(e);
  };
  window.addEventListener('mousemove', move);
  window.addEventListener('mouseup', up);
}

// ---------- rendu des zones ----------
function renderAll() { pageEls.forEach((_, i) => renderZones(i)); }

function renderZones(i) {
  const pe = pageEls[i];
  if (!pe) return;
  const svg = pe.svg;
  svg.textContent = '';
  const list = zones[i] || [];
  const locked = deletedPages.has(i);

  list.forEach((z, idx) => {
    const isSel = !locked && selected && selected.page === i && selected.index === idx;
    const poly = document.createElementNS(svg.namespaceURI, 'polygon');
    poly.setAttribute('points', z.points.map(p => p.join(',')).join(' '));
    poly.setAttribute('class', `zone zone-${z.mode}` + (isSel ? ' selected' : ''));
    if (!locked) {
      poly.addEventListener('mousedown', ev => beginZoneDrag(ev, i, idx));
      poly.addEventListener('contextmenu', ev => {
        ev.preventDefault(); ev.stopPropagation();
        selected = { page: i, index: idx };
        renderZones(i); openMenu(ev, z);
      });
    }
    svg.appendChild(poly);
    if (isSel) renderHandles(svg, i, idx, z);
  });
}

function renderHandles(svg, i, idx, z) {
  const { ux, uy } = unitScale(svg);
  const hw = 9 * ux, hh = 9 * uy;   // poignées à taille d'écran constante

  if (z.type === 'polygon') {
    z.points.forEach((pt, vi) => {
      const c = document.createElementNS(svg.namespaceURI, 'ellipse');
      c.setAttribute('cx', pt[0]); c.setAttribute('cy', pt[1]);
      c.setAttribute('rx', hw / 2); c.setAttribute('ry', hh / 2);
      c.setAttribute('class', 'handle handle-vertex');
      c.style.cursor = 'grab';
      c.addEventListener('mousedown', ev => beginVertexDrag(ev, i, idx, vi));
      c.addEventListener('dblclick', ev => {
        ev.preventDefault(); ev.stopPropagation();
        if (z.points.length <= 3) return;   // un polygone garde 3 sommets mini
        pushHistory();
        z.points.splice(vi, 1);
        renderZones(i); updateStatus();
      });
      svg.appendChild(c);
    });
    return;
  }

  // rectangle: 8 poignées; tracé libre: seulement les 4 coins
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
    r.addEventListener('mousedown', ev => beginResize(ev, i, idx, name));
    svg.appendChild(r);
  });
}

// ---------- édition: déplacer / redimensionner ----------
function beginZoneDrag(ev, i, idx) {
  if (ev.button !== 0) return;
  ev.stopPropagation();
  selected = { page: i, index: idx };
  renderZones(i);
  menu.hidden = true;

  const svg = pageEls[i].svg;
  const z = zones[i][idx];
  const start = toSvgPoint(svg, ev.clientX, ev.clientY);
  const orig = z.points.map(p => p.slice());
  const remember = onceHistory();
  let moved = false;

  startDrag(e => {
    const p = toSvgPoint(svg, e.clientX, e.clientY);
    const dx = p[0] - start[0], dy = p[1] - start[1];
    if (!moved && Math.abs(dx) + Math.abs(dy) < 0.4) return;
    moved = true; remember();
    z.points = orig.map(([x, y]) => [x + dx, y + dy]);
    renderZones(i);
  });
}

function beginVertexDrag(ev, i, idx, vi) {
  if (ev.button !== 0) return;
  ev.stopPropagation();
  const svg = pageEls[i].svg;
  const z = zones[i][idx];
  const remember = onceHistory();
  let moved = false;

  startDrag(e => {
    if (!moved) { moved = true; remember(); }
    z.points[vi] = toSvgPoint(svg, e.clientX, e.clientY);
    renderZones(i);
  });
}

function beginResize(ev, i, idx, name) {
  if (ev.button !== 0) return;
  ev.stopPropagation();
  const svg = pageEls[i].svg;
  const z = zones[i][idx];
  const orig = z.points.map(p => p.slice());
  const bb = bbox(orig);
  const ow = (bb[2] - bb[0]) || 1, oh = (bb[3] - bb[1]) || 1;
  const remember = onceHistory();
  let moved = false;

  startDrag(e => {
    if (!moved) { moved = true; remember(); }
    const p = toSvgPoint(svg, e.clientX, e.clientY);
    const [x0, y0, x1, y1] = edgesFrom(name, bb, p);
    if (z.type === 'rect') {
      z.points = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]];
    } else {
      // tracé libre: on remappe tous les points dans la nouvelle boîte
      const nw = x1 - x0, nh = y1 - y0;
      z.points = orig.map(([x, y]) => [
        x0 + ((x - bb[0]) / ow) * nw,
        y0 + ((y - bb[1]) / oh) * nh,
      ]);
    }
    renderZones(i);
  });
}

// ---------- tracé ----------
function wireLayer(i, svg) {
  svg.addEventListener('mousedown', e => {
    if (e.button !== 0 || deletedPages.has(i)) return;
    activePage = i;
    if (selected) { selected = null; menu.hidden = true; renderZones(i); }
    if (tool === 'rect') startRect(i, svg, e);
    else if (tool === 'freehand') startFreehand(i, svg, e);
  });
  svg.addEventListener('click', e => {
    if (tool === 'polygon' && !deletedPages.has(i)) polyClick(i, svg, e);
  });
  svg.addEventListener('dblclick', e => {
    if (tool !== 'polygon' || !pending || pending.page !== i) return;
    e.preventDefault();
    // le 2e clic du double-clic a déjà ajouté un sommet en double
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
  startDrag(ev => {
    last = toSvgPoint(svg, ev.clientX, ev.clientY);
    ghost.setAttribute('x', Math.min(p0[0], last[0]));
    ghost.setAttribute('y', Math.min(p0[1], last[1]));
    ghost.setAttribute('width', Math.abs(last[0] - p0[0]));
    ghost.setAttribute('height', Math.abs(last[1] - p0[1]));
  }, () => {
    ghost.remove();
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
  svg.appendChild(line);
  startDrag(ev => {
    const p = toSvgPoint(svg, ev.clientX, ev.clientY);
    const l = pts[pts.length - 1];
    if (Math.hypot(p[0] - l[0], p[1] - l[1]) < 2) return;
    pts.push(p);
    line.setAttribute('points', pts.map(x => x.join(',')).join(' '));
  }, () => {
    line.remove();
    if (pts.length >= 3) addZone(i, { type: 'freehand', points: pts, mode: defaultMode });
  });
}

function polyClick(i, svg, e) {
  if (!pending || pending.page !== i) {
    cancelPending();
    const poly = document.createElementNS(svg.namespaceURI, 'polyline');
    poly.setAttribute('class', 'zone-ghost');
    svg.appendChild(poly);
    pending = { page: i, svg, poly, pts: [] };
  }
  pending.pts.push(toSvgPoint(svg, e.clientX, e.clientY));
  pending.poly.setAttribute('points', pending.pts.map(pt => pt.join(',')).join(' '));
}
function finishPolygon() {
  if (pending && pending.pts.length >= 3) {
    addZone(pending.page, { type: 'polygon', points: pending.pts, mode: defaultMode });
  }
  cancelPending();
}
function cancelPending() {
  if (pending) pending.poly.remove();
  pending = null;
}

// ---------- menu contextuel ----------
function openMenu(ev, z) {
  $('zoneModeDelete').classList.toggle('active', z.mode === 'delete');
  $('zoneModePixelate').classList.toggle('active', z.mode === 'pixelate');
  menu.hidden = false;
  const r = menu.getBoundingClientRect();
  menu.style.left = Math.min(ev.clientX, window.innerWidth - r.width - 10) + 'px';
  menu.style.top = Math.min(ev.clientY, window.innerHeight - r.height - 10) + 'px';
}
function setSelectedMode(mode) {
  if (!selected) return;
  pushHistory();
  zones[selected.page][selected.index].mode = mode;
  renderZones(selected.page);
  $('zoneModeDelete').classList.toggle('active', mode === 'delete');
  $('zoneModePixelate').classList.toggle('active', mode === 'pixelate');
  updateStatus();
}
function deleteSelected() {
  if (!selected) return;
  pushHistory();
  const { page, index } = selected;
  zones[page].splice(index, 1);
  selected = null; menu.hidden = true;
  renderZones(page); updateStatus();
}
$('zoneModeDelete').onclick = () => setSelectedMode('delete');
$('zoneModePixelate').onclick = () => setSelectedMode('pixelate');
$('zoneDelete').onclick = deleteSelected;
document.addEventListener('mousedown', e => {
  if (!menu.hidden && !menu.contains(e.target)) menu.hidden = true;
});

// ---------- barre d'outils ----------
function setTool(t) {
  tool = t;
  if (t !== 'polygon') cancelPending();
  document.querySelectorAll('.tool-btn').forEach(b => b.classList.toggle('active', b.dataset.tool === t));
}
$('tool-rect').onclick = () => setTool('rect');
$('tool-polygon').onclick = () => setTool('polygon');
$('tool-freehand').onclick = () => setTool('freehand');

function setDefaultMode(m) {
  defaultMode = m;
  document.querySelectorAll('.mode-btn').forEach(b => b.classList.toggle('active', b.dataset.mode === m));
}
$('mode-delete').onclick = () => setDefaultMode('delete');
$('mode-pixelate').onclick = () => setDefaultMode('pixelate');

$('undo').onclick = undo;
$('clear').onclick = () => {
  if (!(zones[activePage] || []).length) return;
  pushHistory();
  delete zones[activePage];
  if (selected && selected.page === activePage) { selected = null; menu.hidden = true; }
  renderZones(activePage); updateStatus();
};

window.addEventListener('keydown', e => {
  if (!sid) return;
  const mod = e.ctrlKey || e.metaKey;
  const k = (e.key || '').toLowerCase();
  // Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y — insensible à Maj et Verr.Maj
  if (mod && k === 'z') { e.preventDefault(); e.shiftKey ? redo() : undo(); return; }
  if (mod && k === 'y') { e.preventDefault(); redo(); return; }
  if (mod) return;
  if (e.key === 'Escape') { cancelPending(); selected = null; menu.hidden = true; renderAll(); }
  if (e.key === 'Enter') finishPolygon();
  if ((e.key === 'Delete' || e.key === 'Backspace') && selected) { e.preventDefault(); deleteSelected(); }
});
// les poignées ont une taille écran fixe: il faut les redessiner au resize
window.addEventListener('resize', () => { if (selected) renderZones(selected.page); });

// ---------- aide ----------
const help = $('helpPop');
$('helpBtn').onclick = e => { e.stopPropagation(); help.hidden = !help.hidden; };
document.addEventListener('mousedown', e => {
  if (!help.hidden && !help.contains(e.target) && e.target !== $('helpBtn')) help.hidden = true;
});

function updateStatus() {
  const total = Object.values(zones).reduce((a, b) => a + b.length, 0);
  const n = (zones[activePage] || []).length;
  $('pnum').textContent = pages.length ? `${activePage + 1} / ${pages.length}` : '— / —';
  $('undo').disabled = history.length === 0;
  $('clear').disabled = !n;
  $('export').disabled = !(total || deletedPages.size);
  const delTxt = deletedPages.size ? `, ${deletedPages.size} page(s) supprimée(s)` : '';
  $('status').textContent = pages.length
    ? `${n} zone(s) sur la page active, ${total} au total${delTxt}.`
    : 'Aucun document.';
}

// ---------- export ----------
$('export').onclick = async () => {
  $('status').textContent = 'Traitement…';
  const r = await fetch('/api/export', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sid, zones, strip_meta: $('meta').checked, deleted_pages: [...deletedPages] })
  });
  if (!r.ok) { $('status').textContent = 'Erreur : ' + await r.text(); return; }
  const d = await r.json();
  const a = document.createElement('a'); a.href = d.download; a.download = d.filename;
  document.body.appendChild(a); a.click(); a.remove();
  $('status').innerHTML = d.leak_count
    ? `<span class="warn">Attention : ${d.leak_count} fragment(s) de texte touchent encore les zones (`
      + d.leaks.map(l => `p${l.page} : ${l.text}`).join(', ') + `). Vérifiez le résultat.</span>`
    : `<span class="ok">Export terminé, aucun texte résiduel détecté dans les zones.</span>`;
};
