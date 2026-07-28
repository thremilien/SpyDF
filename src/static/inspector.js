// Panneau d'inspection: le calque caché du document, à droite.
//
// Ouvrir un PDF dans un lecteur n'en montre qu'une image. Ce panneau montre ce
// que le fichier transporte réellement — sa couche de texte indexée, ses
// métadonnées, ses signets, ses annotations, ses champs, ses pièces jointes,
// ses calques, ses liens, son JavaScript — et dit, pour chaque élément, s'il
// disparaîtra à l'export ou s'il restera dans le fichier.
//
// Chaque page y est redessinée aux dimensions exactes de celle de gauche, et
// chaque élément à sa position réelle: les deux vues se lisent l'une sur
// l'autre, et le défilement se synchronise page pour page. Ce qui n'appartient
// à aucune page (métadonnées, signets, scripts…) n'a pas de position: cela vit
// dans la colonne de gauche du panneau.
//
// Lecture seule: rien ici ne modifie le document. Les zones et la case
// « Effacer les traces du document » restent le seul moyen d'agir.

const inspector = $('inspector');
const insRail = $('insRail');
const insPages = $('insPages');
const workspace = $('workspace');
const stage = $('stage');

const SVGNS = 'http://www.w3.org/2000/svg';

let inspectData = null;
// Les éléments sont groupés par page (clé 'doc' pour ce qui n'appartient à
// aucune): déplacer une zone ne recolorie alors que la page concernée.
let inspectItems = {};      // page|'doc' -> [{rule, page, rect, hidden, el, chip, notable}]
let keptCount = {};         // page|'doc' -> nombre d'éléments identifiants conservés
let ghosts = [];            // une par page: {el, zoneLayer}
let dirty = new Set();
let refreshFrame = 0;

const GONE = { cls: 'gone', label: 'effacé' };
const KEPT = { cls: 'kept', label: 'conservé' };

// ---------- vues ----------
let view = 'split';

function applyView() {
  workspace.className = `view-${view}` + (inspectData ? '' : ' no-doc');
  document.querySelectorAll('.view-btn').forEach(b => {
    const on = b.dataset.view === view;
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', on);
    b.disabled = !inspectData;   // rien à afficher tant qu'aucun PDF n'est ouvert
  });
}
// Un panneau masqué ne reçoit pas d'événement de défilement: en revenant aux
// deux vues, on rattrape l'écart accumulé plutôt que d'attendre le geste suivant.
function setView(v) {
  view = v;
  applyView();
  if (v === 'split') requestAnimationFrame(() => syncFrom(stage, inspector));
}
document.querySelectorAll('.view-btn').forEach(b => {
  b.onclick = () => setView(b.dataset.view);
});
applyView();

// ---------- géométrie ----------
// La rédaction suit le contour dessiné (le serveur le découpe en bandes
// horizontales), et non son rectangle englobant: c'est donc au polygone lui-même
// qu'il faut confronter les éléments de la page.
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
  for (const [x, y] of pts) {                       // un sommet dans le rectangle
    if (x >= x0 && x <= x1 && y >= y0 && y <= y1) return true;
  }
  const corners = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]];
  for (const [x, y] of corners) {                   // un coin dans le contour
    if (pointInPoly(x, y, pts)) return true;
  }
  for (let i = 0; i < pts.length; i++) {            // ou deux arêtes qui se croisent
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

// ---------- statut d'un élément ----------
// Reflète exactement ce que fait /api/export: purge des annotations et champs
// touchant une zone, rédaction du contour dessiné, puis scrub du document si la
// case est cochée (métadonnées, XMP, signets, pièces jointes, liens,
// JavaScript, texte invisible, valeurs de champs, noms de calques).
function statusOf(it) {
  const dead = it.page != null && deletedPages.has(it.page);
  const hit = dead || coveredBy(it.page, it.rect);

  switch (it.rule) {
    case 'meta':
      return stripMeta() ? GONE : KEPT;
    case 'layer':
      return stripMeta() ? { cls: 'gone', label: 'renommé' } : KEPT;
    case 'link':
      return (dead || stripMeta()) ? GONE : KEPT;
    case 'annot':
    case 'image':
      return hit ? GONE : KEPT;
    case 'widget':
      if (hit) return GONE;
      return stripMeta() ? { cls: 'partial', label: 'valeur réinitialisée' } : KEPT;
    case 'text':
      if (hit) return GONE;
      if (it.hidden) return stripMeta() ? GONE : { cls: 'kept', label: 'texte invisible conservé' };
      return KEPT;
    default:
      return { cls: 'info', label: '' };
  }
}

// ---------- construction ----------
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;   // jamais innerHTML: tout vient du PDF
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

// Une ligne « libellé / valeur / statut ». C'est l'unité que l'on recolorie
// quand les zones ou la case à cocher changent.
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

// ---------- la colonne: ce qui n'a pas de position sur une page ----------
function buildRail(d) {
  insRail.textContent = '';

  const summary = el('div', 'ins-summary');
  summary.id = 'insSummary';
  insRail.append(summary);

  const head = el('div', 'ins-rail-head');
  head.append(el('h2', null, 'Le document'));
  head.append(el('span', 'ins-sheet-sub', `${d.page_count} page(s)`));
  insRail.append(head);

  const meta = section(insRail, 'Métadonnées', d.metadata.length);
  if (d.metadata.length) {
    d.metadata.forEach(m => row(meta, m.label, m.value,
      { rule: m.key === 'format' ? 'info' : 'meta', page: null, notable: m.key !== 'format' }));
  } else emptyNote(meta, 'Aucune.');

  const xmp = section(insRail, 'XMP', d.xmp ? 1 : 0);
  if (d.xmp) {
    row(xmp, 'Bloc XMP', d.xmp.slice(0, 400) + (d.xmp.length > 400 ? '…' : ''),
      { rule: 'meta', page: null, notable: true });
  } else emptyNote(xmp, 'Aucun.');

  const toc = section(insRail, 'Signets', d.toc.length);
  if (d.toc.length) {
    d.toc.forEach(t => row(toc, `p. ${t.page}`, t.title,
      { rule: 'meta', page: null, notable: true }));
  } else emptyNote(toc, 'Aucun.');

  const att = section(insRail, 'Pièces jointes', d.attachments.length);
  if (d.attachments.length) {
    d.attachments.forEach(a => row(att, a.name,
      [a.filename, a.desc, `${a.size} octets`].filter(Boolean).join(' — '),
      { rule: 'meta', page: null, notable: true }));
  } else emptyNote(att, 'Aucune.');

  const lay = section(insRail, 'Calques', d.layers.length);
  if (d.layers.length) {
    d.layers.forEach(l => row(lay, l.on ? 'visible' : 'masqué', l.name,
      { rule: 'layer', page: null, notable: true }));
  } else emptyNote(lay, 'Aucun.');

  const js = section(insRail, 'JavaScript', d.javascript.length);
  if (d.javascript.length) {
    d.javascript.forEach(j => row(js, j.name, j.code || '(script vide)',
      { rule: 'meta', page: null, notable: true }));
  } else emptyNote(js, 'Aucun.');

  const fonts = section(insRail, 'Polices', d.fonts.length);
  if (d.fonts.length) {
    d.fonts.forEach(f => row(fonts, f.embedded ? 'incorporée' : 'référencée',
      `${f.name} (${f.type})`, { rule: 'info', page: null, notable: false }));
  } else emptyNote(fonts, 'Aucune.');

  const leg = section(insRail, 'Légende');
  [['ig-legend-text', 'texte indexé, à sa place réelle'],
   ['ig-legend-hidden', 'texte invisible à l’écran mais indexé'],
   ['ig-legend-annot', 'annotation, champ, lien, image'],
   ['ig-legend-zone', 'zone dessinée à gauche'],
   ['ig-legend-gone', 'ce qui disparaîtra à l’export']].forEach(([cls, text]) => {
    const line = el('div', 'ins-legend');
    line.append(el('span', `ins-legend-mark ${cls}`));
    line.append(el('span', null, text));
    leg.append(line);
  });
}

// ---------- les pages fantômes: mêmes dimensions qu'à gauche ----------
// Un fragment de texte est dessiné dans son propre rectangle: même position,
// même largeur, même hauteur que dans le PDF. C'est ce qui permet de lire la
// couche cachée « par-dessus » la page de gauche.
function textNode(sp) {
  const [x0, y0, x1, y1] = sp.rect;
  const h = Math.max(y1 - y0, 0.1);
  const t = svgEl('text', 'ig-text' + (sp.hidden ? ' is-hidden' : ''));
  t.setAttribute('x', x0);
  t.setAttribute('y', y1 - h * 0.22);          // ligne de base approchée
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

// Les éléments sans rectangle (une image dont PyMuPDF ne retrouve pas la place)
// n'ont rien à montrer sur le fantôme: ils restent listés dans le pied de page.
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
      textContent: 'Texte invisible à l’écran, mais indexé et copiable',
    }));
    svg.append(t);
    addItem({ rule: 'text', page: p.n, rect: sp.rect, hidden: sp.hidden, el: t, chip: null, notable: sp.hidden });
    spanCount++;
  })));

  p.annots.forEach(a => {
    const g = boxNode(a.rect, 'ig-annot',
      `Annotation ${a.type} — ${[a.author, a.content, a.subject, a.date].filter(Boolean).join(' — ') || 'sans contenu'}`);
    svg.append(g);
    addItem({ rule: 'annot', page: p.n, rect: a.rect, el: g, chip: null, notable: true });
  });
  p.widgets.forEach(w => {
    const g = boxNode(w.rect, 'ig-widget',
      `Champ ${w.type || ''} ${w.name || ''} = ${w.value || '(vide)'}`);
    svg.append(g);
    addItem({ rule: 'widget', page: p.n, rect: w.rect, el: g, chip: null, notable: true });
  });
  p.links.forEach(l => {
    const g = boxNode(l.rect, 'ig-link', `Lien (${l.kind}) → ${l.target}`);
    svg.append(g);
    addItem({ rule: 'link', page: p.n, rect: l.rect, el: g, chip: null, notable: true });
  });
  p.images.forEach(i => {
    if (!i.rect) return;
    const g = boxNode(i.rect, 'ig-image', `Image ${i.name} — ${i.w} × ${i.h} px`);
    svg.append(g);
    addItem({ rule: 'image', page: p.n, rect: i.rect, el: g, chip: null, notable: false });
  });

  cont.append(svg, tab);
  if (!spanCount) {
    cont.append(el('div', 'ins-page-note',
      "Aucun texte : cette page n'est qu'une image, rien n'y est sélectionnable ni indexable."));
  }
  const counts = [
    spanCount && `${spanCount} fragment(s) de texte`,
    p.annots.length && `${p.annots.length} annotation(s)`,
    p.widgets.length && `${p.widgets.length} champ(s)`,
    p.links.length && `${p.links.length} lien(s)`,
    p.images.length && `${p.images.length} image(s)`,
    p.drawings && `${p.drawings} tracé(s)`,
  ].filter(Boolean);
  cont.append(el('div', 'ins-page-counts', counts.join(' · ') || 'Page vide'));

  ghosts[p.n] = { el: cont, zoneLayer };
  return cont;
}

// Report des zones dessinées à gauche: sans elles on verrait ce que la page
// cache, mais pas ce qui va le recouvrir.
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

  buildRail(d.doc);
  d.pages.forEach(p => insPages.append(buildGhost(p)));

  if (d.truncated) {
    insRail.append(el('p', 'ins-none',
      'Document très long : la couche de texte a été tronquée dans ce panneau.'));
  }
  d.pages.forEach(p => drawZones(p.n));
  refreshAll();
  // l'inspection arrive après le rendu des pages: si l'on a déjà commencé à
  // lire, le panneau se cale sur la page en cours plutôt que sur la première.
  requestAnimationFrame(() => syncFrom(stage, inspector));
}

// ---------- mise à jour des statuts ----------
function paintItem(it, st) {
  if (it.chip) {
    it.chip.textContent = st.label;
    it.chip.className = `ins-chip ins-chip-${st.cls}`;
    it.el.className = `ins-row is-${st.cls}`;
    return;
  }
  // élément posé sur la page fantôme: pas de pastille, on barre ce qui disparaît
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
    ? `${kept} élément(s) identifiant(s) resteront dans le fichier exporté.`
    : 'Aucune trace identifiante ne subsistera à l’export.'));
  s.append(el('span', null, kept
    ? ' Ils sont marqués « conservé » ci-dessous.'
    : ' Tout ce qui est listé ici sera effacé ou couvert.'));
}

function refreshAll() {
  Object.keys(inspectItems).forEach(refreshGroup);
  refreshSummary();
}

// Un glissement de zone appelle renderZones à chaque image: on ne recolorie
// qu'une fois par frame, et seulement les pages touchées.
function scheduleRefresh() {
  if (refreshFrame) return;
  refreshFrame = requestAnimationFrame(() => {
    refreshFrame = 0;
    dirty.forEach(p => { refreshGroup(p); drawZones(p); });
    dirty.clear();
    refreshSummary();
  });
}

// ---------- synchronisation du défilement ----------
// Les deux vues montrent les mêmes pages, aux mêmes dimensions: une position de
// lecture s'exprime donc comme « page n, à x % de sa hauteur », et se transpose
// telle quelle d'un panneau à l'autre.
//
// Le verrou n'est pas une temporisation arbitraire: le panneau que l'on fait
// défiler prend la main, la garde tant qu'il défile, et la rend à l'arrêt. Le
// défilement induit dans l'autre panneau ne peut donc jamais rebondir.
function pageEllsOf(pane) {
  return pane === stage
    ? pageEls.map(pe => pe.container)
    : ghosts.map(g => g && g.el);
}

function readPos(pane) {
  const els = pageEllsOf(pane);
  const base = pane.getBoundingClientRect().top;
  for (let i = 0; i < els.length; i++) {
    if (!els[i]) continue;
    const r = els[i].getBoundingClientRect();
    const top = r.top - base;
    if (top + r.height > 0) return { i, frac: r.height ? -top / r.height : 0 };
  }
  return null;
}

function applyPos(pane, pos) {
  const els = pageEllsOf(pane);
  const target = els[Math.min(pos.i, els.length - 1)];
  if (!target) return;
  const r = target.getBoundingClientRect();
  const top = r.top - pane.getBoundingClientRect().top;
  pane.scrollTop += top + pos.frac * r.height;
}

let scrollOwner = null;
let ownerRelease = 0;

function syncFrom(pane, other) {
  if (!inspectData || !workspace.classList.contains('view-split')) return;
  if (scrollOwner && scrollOwner !== pane) return;   // c'est notre propre écho
  scrollOwner = pane;
  const pos = readPos(pane);
  if (pos) applyPos(other, pos);
  // relâché à l'arrêt du défilement; le repli couvre les navigateurs sans
  // l'événement 'scrollend' (Safari), où seule l'inactivité le signale.
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

// ---------- accroches ----------
onDocumentOpened = async sid => {
  inspectData = null;
  ghosts = [];
  insRail.textContent = '';
  insPages.textContent = '';
  insPages.append(el('p', 'ins-empty', 'Lecture du contenu du document…'));
  try {
    const r = await fetch(`/api/inspect/${sid}`);
    if (!r.ok) throw new Error(await r.text());
    const d = await r.json();
    inspectData = d;
    build(d);
  } catch (err) {
    insPages.textContent = '';
    insPages.append(el('p', 'ins-empty',
      'Le contenu du document n’a pas pu être lu : ' + (err.message || 'erreur')));
  }
  applyView();
};

onZonesChanged = page => {
  if (!inspectData) return;
  dirty.add(page);
  scheduleRefresh();
};

// La position de lecture se transmet maintenant par le défilement lui-même:
// il n'y a plus de saut de page à provoquer.
onActivePageChanged = () => {};

// la case change le sort des éléments de toutes les pages à la fois
$('meta').addEventListener('change', () => { if (inspectData) refreshAll(); });
