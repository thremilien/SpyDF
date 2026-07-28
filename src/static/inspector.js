// Panneau d'inspection: la feuille de droite.
//
// Ouvrir un PDF dans un lecteur n'en montre qu'une image. Ce panneau écrit en
// clair ce que le fichier transporte réellement — sa couche de texte indexée,
// ses métadonnées, ses signets, ses annotations, ses champs, ses pièces
// jointes, ses calques, ses liens, son JavaScript — et dit, pour chaque
// élément, s'il disparaîtra à l'export ou s'il restera dans le fichier.
//
// Lecture seule: rien ici ne modifie le document. Les zones et la case
// « Effacer les traces du document » restent le seul moyen d'agir.

const inspectBody = $('inspectBody');
const workspace = $('workspace');

let inspectData = null;
// Les éléments sont groupés par page (clé 'doc' pour ce qui n'appartient à
// aucune): déplacer une zone ne recolorie alors que la page concernée.
let inspectItems = {};      // page|'doc' -> [{rule, page, rect, hidden, el, chip, notable}]
let keptCount = {};         // page|'doc' -> nombre d'éléments identifiants conservés
let inspectSheets = [];     // une par page, pour la synchronisation du défilement
let dirty = new Set();
let refreshFrame = 0;
let syncingScroll = false;

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
function setView(v) { view = v; applyView(); }
document.querySelectorAll('.view-btn').forEach(b => {
  b.onclick = () => setView(b.dataset.view);
});
applyView();

// ---------- géométrie ----------
function zoneBoxes(page) {
  return (zones[page] || []).map(z => {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const [x, y] of z.points) {
      if (x < x0) x0 = x; if (y < y0) y0 = y;
      if (x > x1) x1 = x; if (y > y1) y1 = y;
    }
    return [x0, y0, x1, y1];
  });
}
// La rédaction porte sur le rectangle englobant de la zone: c'est lui, et non
// le contour dessiné, qu'il faut confronter aux éléments de la page.
function coveredBy(page, rect) {
  if (!rect) return false;
  return zoneBoxes(page).some(b =>
    b[0] < rect[2] && b[2] > rect[0] && b[1] < rect[3] && b[3] > rect[1]);
}

function stripMeta() { return $('meta').checked; }

// ---------- statut d'un élément ----------
// Reflète exactement ce que fait /api/export: purge des annotations et champs
// touchant une zone, rédaction du rectangle englobant, puis scrub du document
// si la case est cochée (métadonnées, XMP, signets, pièces jointes, liens,
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

function sheet(title, sub) {
  const s = el('section', 'ins-sheet');
  const h = el('header', 'ins-sheet-head');
  h.append(el('h2', null, title));
  if (sub) h.append(el('span', 'ins-sheet-sub', sub));
  s.append(h);
  return s;
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
  const v = el('span', 'ins-value', value);
  r.append(v);
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

function buildDocSheet(d) {
  const s = sheet('Le document', `${d.page_count} page(s)`);

  const meta = section(s, 'Métadonnées', d.metadata.length);
  if (d.metadata.length) {
    d.metadata.forEach(m => row(meta, m.label, m.value,
      { rule: m.key === 'format' ? 'info' : 'meta', page: null, notable: m.key !== 'format' }));
  } else emptyNote(meta, 'Aucune.');

  const xmp = section(s, 'XMP', d.xmp ? 1 : 0);
  if (d.xmp) {
    row(xmp, 'Bloc XMP', d.xmp.slice(0, 400) + (d.xmp.length > 400 ? '…' : ''),
      { rule: 'meta', page: null, notable: true });
  } else emptyNote(xmp, 'Aucun.');

  const toc = section(s, 'Signets', d.toc.length);
  if (d.toc.length) {
    d.toc.forEach(t => row(toc, `p. ${t.page}`, t.title,
      { rule: 'meta', page: null, notable: true }));
  } else emptyNote(toc, 'Aucun.');

  const att = section(s, 'Pièces jointes', d.attachments.length);
  if (d.attachments.length) {
    d.attachments.forEach(a => row(att, a.name,
      [a.filename, a.desc, `${a.size} octets`].filter(Boolean).join(' — '),
      { rule: 'meta', page: null, notable: true }));
  } else emptyNote(att, 'Aucune.');

  const lay = section(s, 'Calques', d.layers.length);
  if (d.layers.length) {
    d.layers.forEach(l => row(lay, l.on ? 'visible' : 'masqué', l.name,
      { rule: 'layer', page: null, notable: true }));
  } else emptyNote(lay, 'Aucun.');

  const js = section(s, 'JavaScript', d.javascript.length);
  if (d.javascript.length) {
    d.javascript.forEach(j => row(js, j.name, j.code || '(script vide)',
      { rule: 'meta', page: null, notable: true }));
  } else emptyNote(js, 'Aucun.');

  const fonts = section(s, 'Polices', d.fonts.length);
  if (d.fonts.length) {
    d.fonts.forEach(f => row(fonts, f.embedded ? 'incorporée' : 'référencée',
      `${f.name} (${f.type})`, { rule: 'info', page: null, notable: false }));
  } else emptyNote(fonts, 'Aucune.');

  return s;
}

function buildTextSection(parent, page, blocks) {
  const sec = section(parent, 'Texte indexé');
  if (!blocks.length) {
    emptyNote(sec, "Aucun texte : cette page n'est qu'une image, rien n'y est "
      + 'sélectionnable ni indexable.');
    return;
  }
  const sheetEl = el('div', 'ins-text');
  blocks.forEach(b => {
    const p = el('p', 'ins-para');
    b.lines.forEach((line, li) => {
      if (li) p.append(document.createTextNode(' '));
      line.spans.forEach(sp => {
        const s = el('span', 'ins-span' + (sp.hidden ? ' is-hidden' : ''), sp.text);
        if (sp.hidden) s.title = 'Texte invisible à l’écran, mais indexé et copiable';
        addItem({
          rule: 'text', page, rect: sp.rect, hidden: sp.hidden,
          el: s, chip: null, notable: sp.hidden,
        });
        p.append(s);
      });
    });
    sheetEl.append(p);
  });
  sec.append(sheetEl);
}

function buildPageSheet(p) {
  const s = sheet(`Page ${p.n + 1}`, `${p.drawings} tracé(s) vectoriel(s)`);
  buildTextSection(s, p.n, p.blocks);

  const an = section(s, 'Annotations', p.annots.length);
  if (p.annots.length) {
    p.annots.forEach(a => row(an, a.author || a.type,
      [a.content, a.subject, a.date].filter(Boolean).join(' — ') || a.type,
      { rule: 'annot', page: p.n, rect: a.rect, notable: true }));
  } else emptyNote(an, 'Aucune.');

  const wd = section(s, 'Champs de formulaire', p.widgets.length);
  if (p.widgets.length) {
    p.widgets.forEach(w => row(wd, w.name || w.type, w.value || '(vide)',
      { rule: 'widget', page: p.n, rect: w.rect, notable: true }));
  } else emptyNote(wd, 'Aucun.');

  const lk = section(s, 'Liens', p.links.length);
  if (p.links.length) {
    p.links.forEach(l => row(lk, l.kind, l.target,
      { rule: 'link', page: p.n, rect: l.rect, notable: true }));
  } else emptyNote(lk, 'Aucun.');

  const im = section(s, 'Images', p.images.length);
  if (p.images.length) {
    p.images.forEach(i => row(im, i.name, `${i.w} × ${i.h} px`,
      { rule: 'image', page: p.n, rect: i.rect, notable: false }));
  } else emptyNote(im, 'Aucune.');

  return s;
}

function build(d) {
  inspectItems = {};
  keptCount = {};
  inspectSheets = [];
  dirty.clear();
  inspectBody.textContent = '';

  const summary = el('div', 'ins-summary');
  summary.id = 'insSummary';
  inspectBody.append(summary);

  inspectBody.append(buildDocSheet(d.doc));
  d.pages.forEach(p => {
    const s = buildPageSheet(p);
    inspectSheets[p.n] = s;
    inspectBody.append(s);
  });

  if (d.truncated) {
    inspectBody.append(el('p', 'ins-none',
      'Document très long : la couche de texte a été tronquée dans ce panneau.'));
  }
  refreshAll();
}

// ---------- mise à jour des statuts ----------
function refreshGroup(key) {
  let kept = 0;
  for (const it of inspectItems[key] || []) {
    const st = statusOf(it);
    if (it.chip) {
      it.chip.textContent = st.label;
      it.chip.className = `ins-chip ins-chip-${st.cls}`;
      it.el.className = `ins-row is-${st.cls}`;
    } else {
      // fragment de texte: pas de pastille, on barre ce qui disparaît
      it.el.className = 'ins-span'
        + (it.hidden ? ' is-hidden' : '')
        + (st.cls === 'gone' ? ' is-gone' : '');
    }
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
    : ' Tout ce qui est listé ci-dessous sera effacé ou couvert.'));
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
    dirty.forEach(p => refreshGroup(p));
    dirty.clear();
    refreshSummary();
  });
}

// ---------- synchronisation du défilement ----------
// Les deux vues montrent le même document: la feuille de droite suit la page
// lue à gauche, sinon la comparaison côte à côte ne sert à rien.
function scrollToPage(n) {
  if (!workspace.classList.contains('view-split')) return;
  const s = inspectSheets[n];
  if (!s || syncingScroll) return;
  syncingScroll = true;
  $('inspector').scrollTo({ top: s.offsetTop - 12, behavior: 'smooth' });
  setTimeout(() => { syncingScroll = false; }, 400);
}

// ---------- accroches ----------
onDocumentOpened = async sid => {
  inspectData = null;
  inspectBody.textContent = '';
  inspectBody.append(el('p', 'ins-empty', 'Lecture du contenu du document…'));
  try {
    const r = await fetch(`/api/inspect/${sid}`);
    if (!r.ok) throw new Error(await r.text());
    const d = await r.json();
    inspectData = d;
    build(d);
  } catch (err) {
    inspectBody.textContent = '';
    inspectBody.append(el('p', 'ins-empty',
      'Le contenu du document n’a pas pu être lu : ' + (err.message || 'erreur')));
  }
  applyView();
};

onZonesChanged = page => {
  if (!inspectData) return;
  dirty.add(page);
  scheduleRefresh();
};

onActivePageChanged = scrollToPage;

// la case change le sort des éléments de toutes les pages à la fois
$('meta').addEventListener('change', () => { if (inspectData) refreshAll(); });
