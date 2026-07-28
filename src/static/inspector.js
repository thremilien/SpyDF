// Inspector pane: the document's hidden layer, on the right.
//
// Opening a PDF in a reader shows only a picture of it. This pane shows what the
// file really carries — its indexed text layer, metadata, bookmarks,
// annotations, fields, attachments, layers, links and JavaScript — and says, for
// each item, whether it will disappear on export or stay in the file.
//
// Every page is redrawn at the exact size of the one on the left, each item at
// its real position, so the two views read against each other and scrolling
// stays in sync page for page. Anything belonging to no page (metadata,
// bookmarks, scripts…) has no position: it lives in the pane's left column.
//
// Read-only: nothing here modifies the document. The zones and the "Strip
// document traces" checkbox remain the only way to act.

const inspector = $('inspector');
const insRail = $('insRail');
const insPages = $('insPages');
const workspace = $('workspace');
const stage = $('stage');

const SVGNS = 'http://www.w3.org/2000/svg';

let inspectData = null;
// Items are grouped by page (key 'doc' for what belongs to none), so moving a
// zone only recolours the page it is on.
let inspectItems = {};      // page|'doc' -> [{rule, page, rect, hidden, el, chip, notable}]
let keptCount = {};         // page|'doc' -> number of identifying items kept
let ghosts = [];            // one per page: {el, zoneLayer}
let dirty = new Set();
let refreshFrame = 0;

const GONE = { cls: 'gone', label: 'erased' };
const KEPT = { cls: 'kept', label: 'kept' };

// ---------- views ----------
let view = 'split';

function applyView() {
  workspace.className = `view-${view}` + (inspectData ? '' : ' no-doc');
  syncPageWidth();   // a pane just changed width: the pages follow
  document.querySelectorAll('.view-btn').forEach(b => {
    const on = b.dataset.view === view;
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', on);
    b.disabled = !inspectData;   // nothing to show until a PDF is open
  });
}
// A hidden pane receives no scroll event: on returning to the split view, catch
// up the accumulated offset rather than waiting for the next gesture.
function setView(v) {
  view = v;
  applyView();
  if (v === 'split') requestAnimationFrame(() => syncFrom(stage, inspector));
}
document.querySelectorAll('.view-btn').forEach(b => {
  b.onclick = () => setView(b.dataset.view);
});
applyView();

// ---------- geometry ----------
// Redaction follows the drawn outline (the server cuts it into horizontal
// strips), not its bounding box, so page items must be tested against the
// polygon itself.
function pointInPoly(x, y, pts) {
  let inside = false;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const [xi, yi] = pts[i], [xj, yj] = pts[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

function segsCross(a, b, c, d) {
  const s = (p, q, r) => (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0]);
  const d1 = s(a, b, c), d2 = s(a, b, d), d3 = s(c, d, a), d4 = s(c, d, b);
  return ((d1 > 0) !== (d2 > 0)) && ((d3 > 0) !== (d4 > 0));
}

function rectHitsPoly(rect, pts) {
  const [x0, y0, x1, y1] = rect;
  for (const [x, y] of pts) {                       // a vertex inside the rectangle
    if (x >= x0 && x <= x1 && y >= y0 && y <= y1) return true;
  }
  const corners = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]];
  for (const [x, y] of corners) {                   // a corner inside the outline
    if (pointInPoly(x, y, pts)) return true;
  }
  for (let i = 0; i < pts.length; i++) {            // or two edges that cross
    const a = pts[i], b = pts[(i + 1) % pts.length];
    for (let j = 0; j < 4; j++) {
      if (segsCross(a, b, corners[j], corners[(j + 1) % 4])) return true;
    }
  }
  return false;
}

function coveredBy(page, rect) {
  if (!rect) return false;
  return (zones[page] || []).some(z => rectHitsPoly(rect, z.points));
}

function stripMeta() { return $('meta').checked; }

// ---------- status of an item ----------
// Mirrors exactly what /api/export does: purge annotations and fields touching a
// zone, redact the drawn outline, then scrub the document if the checkbox is on
// (metadata, XMP, bookmarks, attachments, links, JavaScript, invisible text,
// field values, layer names).
function statusOf(it) {
  const dead = it.page != null && deletedPages.has(it.page);
  const hit = dead || coveredBy(it.page, it.rect);

  switch (it.rule) {
    case 'meta':
      return stripMeta() ? GONE : KEPT;
    case 'layer':
      return stripMeta() ? { cls: 'gone', label: 'renamed' } : KEPT;
    case 'link':
    case 'struct':
      // not page content: no zone can reach it, only the scrubbing can
      return (dead || stripMeta()) ? GONE : KEPT;
    case 'annot':
    case 'image':
      return hit ? GONE : KEPT;
    case 'imagemeta':
      // Exif lives in the image's own stream, not on the page. Redacting over
      // the image does rewrite that stream, but only the scrubbing is promised
      // to reach it: claiming an erasure a zone might not deliver is the one
      // mistake this pane must not make.
      return stripMeta() ? GONE : KEPT;
    case 'cover':
      // the white box hides the pixels, it does not remove them: only a zone
      // over it destroys what is underneath
      return hit ? { cls: 'gone', label: 'erased' } : { cls: 'kept', label: 'still there' };
    case 'widget':
      if (hit) return GONE;
      return stripMeta() ? { cls: 'partial', label: 'value reset' } : KEPT;
    case 'text':
      if (hit) return GONE;
      if (it.hidden) return stripMeta() ? GONE : { cls: 'kept', label: 'invisible text kept' };
      return KEPT;
    default:
      return { cls: 'info', label: '' };
  }
}

// ---------- building ----------
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;   // never innerHTML: it all comes from the PDF
  return e;
}

function svgEl(tag, cls) {
  const e = document.createElementNS(SVGNS, tag);
  if (cls) e.setAttribute('class', cls);
  return e;
}

function section(parent, title, count) {
  const sec = el('div', 'ins-sec');
  const h = el('h3', null, title);
  if (count != null) h.append(el('span', 'ins-count', String(count)));
  sec.append(h);
  parent.append(sec);
  return sec;
}

// A "label / value / status" row. This is the unit recoloured when the zones or
// the checkbox change.
function row(parent, label, value, item) {
  const r = el('div', 'ins-row');
  r.append(el('span', 'ins-label', label));
  r.append(el('span', 'ins-value', value));
  const chip = el('span', 'ins-chip');
  r.append(chip);
  parent.append(r);
  item.el = r;
  item.chip = chip;
  addItem(item);
  return r;
}

function addItem(item) {
  const key = item.page == null ? 'doc' : item.page;
  (inspectItems[key] = inspectItems[key] || []).push(item);
}

function emptyNote(parent, text) {
  parent.append(el('p', 'ins-none', text));
}

// ---------- acting on a cover ----------
// A cover is the file's own content, not a zone: it cannot be moved, resized or
// deleted from here, and the pane still changes nothing. What it can do is hand
// you a zone over it — one click and it is an ordinary zone on the left, edited
// and undone like any other, which is what actually destroys the pixels.
const COVER_ZONE_MARGIN = 1;   // pt, so nothing peeks out from under the edge
const COVER_TIP = 'Click to redact this area for real: it becomes a zone on the '
  + 'left, movable and undoable like any other.';

// The same image can be placed several times on a page; what it carries belongs
// to the image, so it is listed once however often it is drawn.
function imageTraces(p) {
  const seen = new Set(), out = [];
  (p.images || []).forEach(im => {
    if (!(im.meta || []).length || seen.has(im.name)) return;
    seen.add(im.name);
    im.meta.forEach(m => out.push({ ...m, name: im.name }));
  });
  return out;
}

function colorName(rgb) {
  if (!rgb || rgb.length < 3) return 'opaque';
  if (rgb.every(c => c > 0.95)) return 'white';
  if (rgb.every(c => c < 0.05)) return 'black';
  return 'opaque';
}

function coverZone(c) {
  if (deletedPages.has(c.n) || coveredBy(c.n, c.rect)) return;   // nothing left to do
  const m = COVER_ZONE_MARGIN;
  const [x0, y0, x1, y1] = c.rect;
  addZone(c.n, {
    type: 'rect',
    points: [[x0 - m, y0 - m], [x1 + m, y0 - m], [x1 + m, y1 + m], [x0 - m, y1 + m]],
    mode: 'delete',
  });
}

function makeCoverAction(target, c) {
  target.setAttribute('role', 'button');
  target.setAttribute('tabindex', '0');
  target.setAttribute('aria-label', `Redact the covered area on page ${c.n + 1}`);
  target.style.cursor = 'pointer';
  target.addEventListener('click', () => coverZone(c));
  target.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); coverZone(c); }
  });
}

// ---------- the column: what has no position on any page ----------
function buildRail(d, pagesData) {
  insRail.textContent = '';

  const summary = el('div', 'ins-summary');
  summary.id = 'insSummary';
  insRail.append(summary);

  const head = el('div', 'ins-rail-head');
  head.append(el('h2', null, 'The document'));
  head.append(el('span', 'ins-sheet-sub', `${d.page_count} page(s)`));
  insRail.append(head);

  const meta = section(insRail, 'Metadata', d.metadata.length);
  if (d.metadata.length) {
    d.metadata.forEach(m => row(meta, m.label, m.value,
      { rule: m.key === 'format' ? 'info' : 'meta', page: null, notable: m.key !== 'format' }));
  } else emptyNote(meta, 'None.');

  const xmp = section(insRail, 'XMP', d.xmp ? 1 : 0);
  if (d.xmp) {
    row(xmp, 'XMP block', d.xmp.slice(0, 400) + (d.xmp.length > 400 ? '…' : ''),
      { rule: 'meta', page: null, notable: true });
  } else emptyNote(xmp, 'None.');

  // Listed, not only drawn on the pages: the summary says the kept items are
  // "marked below", and a cover is exactly the item you would go looking for.
  const covers = pagesData.flatMap(p => (p.covers || []).map(c => ({ ...c, n: p.n })));
  const cov = section(insRail, 'Opaque covers', covers.length);
  if (covers.length) {
    covers.forEach(c => {
      const [x0, y0, x1, y1] = c.rect;
      const r = row(cov, `p. ${c.n + 1}`,
        `${Math.round(x1 - x0)} × ${Math.round(y1 - y0)} pt, ${colorName(c.color)}`,
        { rule: 'cover', page: c.n, rect: c.rect, notable: true });
      r.title = COVER_TIP;
      makeCoverAction(r, c);
    });
  } else emptyNote(cov, 'None.');

  // Inside the image, not on the page: a scan photographed with a phone carries
  // the camera, its serial number, the date, sometimes a GPS fix — and a
  // thumbnail, which is a small copy of the picture before anything was drawn
  // over it. No zone reaches into an image stream, only the scrubbing does.
  const imgMeta = pagesData.flatMap(p => imageTraces(p).map(m => ({ ...m, n: p.n })));
  const imd = section(insRail, 'Image metadata', imgMeta.length);
  if (imgMeta.length) {
    imgMeta.forEach(m => {
      const r = row(imd, `p. ${m.n + 1} ${m.field}`, m.value,
        { rule: 'imagemeta', page: m.n, notable: true });
      r.title = `${m.kind} carried inside ${m.name}, not drawn on the page: `
        + 'removed by the scrubbing, not by a zone.';
    });
  } else emptyNote(imd, 'None.');

  // Attached to a page, but nowhere on it: it lives in the structure tree, so
  // it is listed here rather than drawn on the ghost. A scanned page whose only
  // text is this one reads as "no text" everywhere else.
  const struct = pagesData.flatMap(p => (p.struct || []).map(e => ({ ...e, n: p.n })));
  const stx = section(insRail, 'Text outside the pages', struct.length);
  if (struct.length) {
    struct.forEach(e => {
      const r = row(stx, `p. ${e.n + 1}`, e.text, { rule: 'struct', page: e.n, notable: true });
      r.title = `${e.kind}, read by readers and indexers but drawn nowhere on the page`;
    });
  } else emptyNote(stx, 'None.');

  const toc = section(insRail, 'Bookmarks', d.toc.length);
  if (d.toc.length) {
    d.toc.forEach(t => row(toc, `p. ${t.page}`, t.title,
      { rule: 'meta', page: null, notable: true }));
  } else emptyNote(toc, 'None.');

  const att = section(insRail, 'Attachments', d.attachments.length);
  if (d.attachments.length) {
    d.attachments.forEach(a => row(att, a.name,
      [a.filename, a.desc, `${a.size} bytes`].filter(Boolean).join(' — '),
      { rule: 'meta', page: null, notable: true }));
  } else emptyNote(att, 'None.');

  const lay = section(insRail, 'Layers', d.layers.length);
  if (d.layers.length) {
    d.layers.forEach(l => row(lay, l.on ? 'visible' : 'hidden', l.name,
      { rule: 'layer', page: null, notable: true }));
  } else emptyNote(lay, 'None.');

  const js = section(insRail, 'JavaScript', d.javascript.length);
  if (d.javascript.length) {
    d.javascript.forEach(j => row(js, j.name, j.code || '(empty script)',
      { rule: 'meta', page: null, notable: true }));
  } else emptyNote(js, 'None.');

  const fonts = section(insRail, 'Fonts', d.fonts.length);
  if (d.fonts.length) {
    d.fonts.forEach(f => row(fonts, f.embedded ? 'embedded' : 'referenced',
      `${f.name} (${f.type})`, { rule: 'info', page: null, notable: false }));
  } else emptyNote(fonts, 'None.');

  const leg = section(insRail, 'Legend');
  [['ig-legend-text', 'indexed text, at its real position'],
   ['ig-legend-hidden', 'text invisible on screen but indexed'],
   ['ig-legend-annot', 'annotation, field, link, image'],
   ['ig-legend-zone', 'zone drawn on the left'],
   ['ig-legend-cover', 'opaque cover: hidden, not removed'],
   ['ig-legend-gone', 'what will disappear on export']].forEach(([cls, text]) => {
    const line = el('div', 'ins-legend');
    line.append(el('span', `ins-legend-mark ${cls}`));
    line.append(el('span', null, text));
    leg.append(line);
  });
}

// ---------- the ghost pages: same size as on the left ----------
// A text fragment is drawn inside its own rectangle: same position, same width,
// same height as in the PDF. That is what lets the hidden layer be read "on top
// of" the page on the left.
function textNode(sp) {
  const [x0, y0, x1, y1] = sp.rect;
  const h = Math.max(y1 - y0, 0.1);
  const t = svgEl('text', 'ig-text' + (sp.hidden ? ' is-hidden' : ''));
  t.setAttribute('x', x0);
  t.setAttribute('y', y1 - h * 0.22);          // approximate baseline
  t.setAttribute('font-size', h * 0.82);
  if (x1 > x0) {
    t.setAttribute('textLength', x1 - x0);
    t.setAttribute('lengthAdjust', 'spacingAndGlyphs');
  }
  t.textContent = sp.text;
  return t;
}

function boxNode(rect, cls, title) {
  const [x0, y0, x1, y1] = rect;
  const g = svgEl('g', 'ig-item');
  const r = svgEl('rect', `ig-box ${cls}`);
  r.setAttribute('x', x0);
  r.setAttribute('y', y0);
  r.setAttribute('width', Math.max(x1 - x0, 0.5));
  r.setAttribute('height', Math.max(y1 - y0, 0.5));
  g.append(r);
  const tip = svgEl('title');
  tip.textContent = title;
  g.append(tip);
  return g;
}

// Items without a rectangle (an image whose position PyMuPDF cannot recover)
// have nothing to show on the ghost: they stay listed in the page footer.
function buildGhost(p) {
  const dim = pages[p.n] || { w: 595, h: 842, x0: 0, y0: 0 };
  const cont = el('div', 'ins-page');
  cont.style.aspectRatio = `${dim.w} / ${dim.h}`;
  cont.dataset.page = p.n;

  const tab = el('div', 'ins-page-tab', `${p.n + 1} / ${pages.length}`);
  const svg = svgEl('svg', 'ins-page-layer');
  svg.setAttribute('viewBox', `${dim.x0} ${dim.y0} ${dim.w} ${dim.h}`);
  svg.setAttribute('preserveAspectRatio', 'none');

  const zoneLayer = svgEl('g', 'ig-zones');
  svg.append(zoneLayer);

  let spanCount = 0;
  p.blocks.forEach(b => b.lines.forEach(line => line.spans.forEach(sp => {
    const t = textNode(sp);
    if (sp.hidden) t.append(Object.assign(svgEl('title'), {
      textContent: 'Text invisible on screen, but indexed and copyable',
    }));
    svg.append(t);
    addItem({ rule: 'text', page: p.n, rect: sp.rect, hidden: sp.hidden, el: t, chip: null, notable: sp.hidden });
    spanCount++;
  })));

  p.annots.forEach(a => {
    const g = boxNode(a.rect, 'ig-annot',
      `Annotation ${a.type} — ${[a.author, a.content, a.subject, a.date].filter(Boolean).join(' — ') || 'no content'}`);
    svg.append(g);
    addItem({ rule: 'annot', page: p.n, rect: a.rect, el: g, chip: null, notable: true });
  });
  p.widgets.forEach(w => {
    const g = boxNode(w.rect, 'ig-widget',
      `Field ${w.type || ''} ${w.name || ''} = ${w.value || '(empty)'}`);
    svg.append(g);
    addItem({ rule: 'widget', page: p.n, rect: w.rect, el: g, chip: null, notable: true });
  });
  p.links.forEach(l => {
    const g = boxNode(l.rect, 'ig-link', `Link (${l.kind}) → ${l.target}`);
    svg.append(g);
    addItem({ rule: 'link', page: p.n, rect: l.rect, el: g, chip: null, notable: true });
  });
  p.images.forEach(i => {
    if (!i.rect) return;
    const carried = (i.meta || []).map(m => `${m.field}: ${m.value}`).join(' · ');
    const g = boxNode(i.rect, 'ig-image', `Image ${i.name} — ${i.w} × ${i.h} px`
      + (carried ? ` — carries ${carried}` : ''));
    svg.append(g);
    addItem({ rule: 'image', page: p.n, rect: i.rect, el: g, chip: null, notable: false });
  });
  // An opaque rectangle over a scan looks like an erasure and is not one: the
  // area reads blank on the left, so nothing invites a zone there, while the
  // image still carries what it hides.
  (p.covers || []).forEach(c => {
    const g = boxNode(c.rect, 'ig-cover',
      'Opaque rectangle over the image: it hides what is underneath, it does not '
      + 'remove it. ' + COVER_TIP);
    svg.append(g);
    makeCoverAction(g, { ...c, n: p.n });
    // counted once, in the rail: the same cover has a row there
    addItem({ rule: 'cover', page: p.n, rect: c.rect, el: g, chip: null, notable: false });
  });

  cont.append(svg, tab);
  const struct = p.struct || [];
  const covers = p.covers || [];
  const traces = imageTraces(p);
  if (!spanCount) {
    // "only an image" was said even when the file described the page in its
    // structure tree — text no zone can reach, and the only one such a page has.
    cont.append(el('div', 'ins-page-note', struct.length
      ? 'No text on the page itself: it is only an image. But the file carries '
        + 'text for it outside the page, listed in the left-hand column.'
      : 'No text: this page is only an image, nothing on it is selectable or indexable.'));
  }
  const counts = [
    spanCount && `${spanCount} text fragment(s)`,
    struct.length && `${struct.length} text item(s) outside the page`,
    covers.length && `${covers.length} covered area(s)`,
    p.annots.length && `${p.annots.length} annotation(s)`,
    p.widgets.length && `${p.widgets.length} field(s)`,
    p.links.length && `${p.links.length} link(s)`,
    p.images.length && `${p.images.length} image(s)`,
    traces.length && `${traces.length} image metadata item(s)`,
    p.drawings && `${p.drawings} drawing(s)`,
  ].filter(Boolean);
  cont.append(el('div', 'ins-page-counts', counts.join(' · ') || 'Empty page'));

  ghosts[p.n] = { el: cont, zoneLayer };
  return cont;
}

// Mirror of the zones drawn on the left: without them you would see what the
// page hides, but not what is about to cover it.
function drawZones(n) {
  const g = ghosts[n];
  if (!g) return;
  g.zoneLayer.textContent = '';
  (zones[n] || []).forEach(z => {
    const poly = svgEl('polygon', `ig-zone ig-zone-${z.mode}`);
    poly.setAttribute('points', z.points.map(p => p.join(',')).join(' '));
    g.zoneLayer.append(poly);
  });
  g.el.classList.toggle('is-page-deleted', deletedPages.has(n));
}

function build(d) {
  inspectItems = {};
  keptCount = {};
  ghosts = [];
  dirty.clear();
  insPages.textContent = '';

  buildRail(d.doc, d.pages);
  d.pages.forEach(p => insPages.append(buildGhost(p)));

  if (d.truncated) {
    insRail.append(el('p', 'ins-none',
      'Very long document: the text layer has been truncated in this pane.'));
  }
  d.pages.forEach(p => drawZones(p.n));
  refreshAll();
  // inspection lands after the pages are rendered: if reading has already
  // started, the pane lines up on the current page rather than the first.
  requestAnimationFrame(() => syncFrom(stage, inspector));
}

// ---------- refreshing the statuses ----------
function paintItem(it, st) {
  if (it.chip) {
    it.chip.textContent = st.label;
    it.chip.className = `ins-chip ins-chip-${st.cls}`;
    it.el.className = `ins-row is-${st.cls}`;
    return;
  }
  // item drawn on the ghost page: no chip, what disappears is struck through
  const base = it.rule === 'text'
    ? 'ig-text' + (it.hidden ? ' is-hidden' : '')
    : it.el.firstChild.getAttribute('class').replace(' is-gone', '');
  if (it.rule === 'text') {
    it.el.setAttribute('class', base + (st.cls === 'gone' ? ' is-gone' : ''));
  } else {
    it.el.firstChild.setAttribute('class', base + (st.cls === 'gone' ? ' is-gone' : ''));
  }
}

function refreshGroup(key) {
  let kept = 0;
  for (const it of inspectItems[key] || []) {
    const st = statusOf(it);
    paintItem(it, st);
    if (it.notable && st.cls !== 'gone') kept++;
  }
  keptCount[key] = kept;
}

function refreshSummary() {
  const s = $('insSummary');
  if (!s) return;
  const kept = Object.values(keptCount).reduce((a, b) => a + b, 0);
  s.textContent = '';
  s.className = 'ins-summary ' + (kept ? 'is-warn' : 'is-ok');
  s.append(el('strong', null, kept
    ? `${kept} identifying item(s) will remain in the exported file.`
    : 'No identifying trace will survive the export.'));
  s.append(el('span', null, kept
    ? ' They are marked "kept" below.'
    : ' Everything listed here will be erased or covered.'));
}

function refreshAll() {
  Object.keys(inspectItems).forEach(refreshGroup);
  refreshSummary();
}

// Dragging a zone calls renderZones on every frame: recolour once per frame,
// and only for the pages that changed.
function scheduleRefresh() {
  if (refreshFrame) return;
  refreshFrame = requestAnimationFrame(() => {
    refreshFrame = 0;
    dirty.forEach(p => { refreshGroup(p); drawZones(p); });
    dirty.clear();
    refreshSummary();
  });
}

// ---------- scroll synchronisation ----------
// Both views show the same pages at the same size, so a reading position is
// "page n, x % down it" and transposes as-is from one pane to the other.
//
// The lock is not an arbitrary delay: the pane being scrolled takes ownership,
// holds it while it scrolls and releases it when it stops. The scrolling it
// induces in the other pane can therefore never bounce back.
function pageEllsOf(pane) {
  return pane === stage
    ? pageEls.map(pe => pe.container)
    : ghosts.map(g => g && g.el);
}

// Horizontal too, once zoomed in: a page wider than its pane is read at some
// offset, and the same offset has to hold on the other side.
function readPos(pane) {
  const els = pageEllsOf(pane);
  const box = pane.getBoundingClientRect();
  for (let i = 0; i < els.length; i++) {
    if (!els[i]) continue;
    const r = els[i].getBoundingClientRect();
    const top = r.top - box.top;
    if (top + r.height > 0) {
      return {
        i,
        frac: r.height ? -top / r.height : 0,
        fracX: r.width ? (box.left - r.left) / r.width : 0,
      };
    }
  }
  return null;
}

function applyPos(pane, pos) {
  const els = pageEllsOf(pane);
  const target = els[Math.min(pos.i, els.length - 1)];
  if (!target) return;
  const box = pane.getBoundingClientRect();
  const r = target.getBoundingClientRect();
  pane.scrollTop += (r.top - box.top) + pos.frac * r.height;
  pane.scrollLeft += (r.left - box.left) + pos.fracX * r.width;
}

let scrollOwner = null;
let ownerRelease = 0;

function syncFrom(pane, other) {
  if (!inspectData || !workspace.classList.contains('view-split')) return;
  if (scrollOwner && scrollOwner !== pane) return;   // this is our own echo
  scrollOwner = pane;
  const pos = readPos(pane);
  if (pos) applyPos(other, pos);
  // released when scrolling stops; the fallback covers browsers without the
  // 'scrollend' event (Safari), where only inactivity signals the end.
  clearTimeout(ownerRelease);
  ownerRelease = setTimeout(() => { scrollOwner = null; }, 150);
}

function release(pane) {
  if (scrollOwner === pane) scrollOwner = null;
}

stage.addEventListener('scroll', () => syncFrom(stage, inspector), { passive: true });
inspector.addEventListener('scroll', () => syncFrom(inspector, stage), { passive: true });
stage.addEventListener('scrollend', () => release(stage));
inspector.addEventListener('scrollend', () => release(inspector));

// ---------- hooks ----------
onDocumentOpened = async sid => {
  inspectData = null;
  ghosts = [];
  insRail.textContent = '';
  insPages.textContent = '';
  insPages.append(el('p', 'ins-empty', 'Reading the document content…'));
  try {
    const r = await fetch(`/api/inspect/${sid}`);
    if (!r.ok) throw new Error(await r.text());
    const d = await r.json();
    inspectData = d;
    build(d);
  } catch (err) {
    insPages.textContent = '';
    insPages.append(el('p', 'ins-empty',
      'The document content could not be read: ' + (err.message || 'error')));
  }
  applyView();
};

onZonesChanged = page => {
  if (!inspectData) return;
  dirty.add(page);
  scheduleRefresh();
};

// The reading position now travels through the scrolling itself: there is no
// page jump left to trigger.
onActivePageChanged = () => {};

// The zoom is a single factor applied to both panes through CSS, so there is
// nothing to scale here: only the reading position has to be carried over, from
// the pane that was zoomed to the other one.
onZoomChanged = pane => {
  if (!inspectData) return;
  scrollOwner = null;
  if (pane === inspector) syncFrom(inspector, stage);
  else syncFrom(stage, inspector);
};

// pages of the right-hand pane, for the zoom anchor (app.js)
ghostEls = () => ghosts.map(g => g && g.el);

// the checkbox changes the fate of items on every page at once
$('meta').addEventListener('change', () => { if (inspectData) refreshAll(); });
