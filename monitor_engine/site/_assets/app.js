'use strict';

/* ═══════════════════════════════════════════════════════════════════
   State
   ═══════════════════════════════════════════════════════════════════ */
const state = {
  data: null,
  activeEdition: null,   // edition id string
  activeCategory: null,  // category string, null = all
  dateFilter: 'all',     // 'all' | 'week' | 'month'
  searchQuery: '',       // lowercased keyword filter, '' = no filter
  newIds: new Set(),     // item_ids that are new this run
};

/* ═══════════════════════════════════════════════════════════════════
   Boot
   ═══════════════════════════════════════════════════════════════════ */
document.addEventListener('DOMContentLoaded', () => {
  // Offline fallback: register the service worker (best-effort; ignored where
  // unsupported, e.g. the headless test harness has no navigator).
  if (typeof navigator !== 'undefined' && navigator.serviceWorker) {
    navigator.serviceWorker.register('sw.js').catch(() => {});
  }
  const url = window.__DATA_URL || 'run_output.json';
  fetch(url)
    .then(r => {
      if (!r.ok) throw new Error('HTTP ' + r.status + ' loading ' + url);
      return r.json();
    })
    .then(data => {
      state.data = data;
      boot();
    })
    .catch(err => {
      const scr = document.getElementById('loading-screen');
      scr.innerHTML =
        '<div class="loading-inner" style="color:var(--sig-deadline)">' +
        '<p>Could not load briefing data.</p>' +
        '<p style="font-size:.8rem;color:var(--text-muted)">' + esc(err.message) + '</p>' +
        '</div>';
    });
});

function boot() {
  const { data } = state;
  const wn = data.whats_new || {};
  state.newIds = new Set([...(wn.new_tier_1 || []), ...(wn.new_tier_2 || [])]);

  const cfg = data.site_config;
  if (cfg && cfg.editions && cfg.editions.length > 0) {
    state.activeEdition = cfg.editions[0].id;
  }

  applyBranding(cfg);
  buildEditionNav(cfg);
  buildFilterPanel(cfg);
  wireFilterToggle();
  renderRunMeta();
  render();

  document.getElementById('loading-screen').classList.add('hidden');
  document.getElementById('site-header').hidden = false;
  document.getElementById('main-layout').hidden = false;
}

/* ═══════════════════════════════════════════════════════════════════
   Branding — accent color and title from data
   ═══════════════════════════════════════════════════════════════════ */
function applyBranding(cfg) {
  if (!cfg) return;
  document.title = cfg.name + ' — Briefing';
  document.getElementById('site-title').textContent = cfg.name;
  if (cfg.accent_color) {
    const root = document.documentElement;
    root.style.setProperty('--accent', cfg.accent_color);
    root.style.setProperty('--accent-dark', shiftLightness(cfg.accent_color, -0.12));
    root.style.setProperty('--accent-bg', hexAlpha(cfg.accent_color, 0.08));
    const meta = document.getElementById('meta-theme-color');
    if (meta) meta.setAttribute('content', cfg.accent_color);
  }
}

function shiftLightness(hex, delta) {
  const r = clamp(parseInt(hex.slice(1, 3), 16) + Math.round(delta * 255));
  const g = clamp(parseInt(hex.slice(3, 5), 16) + Math.round(delta * 255));
  const b = clamp(parseInt(hex.slice(5, 7), 16) + Math.round(delta * 255));
  return '#' + [r, g, b].map(v => v.toString(16).padStart(2, '0')).join('');
}
function hexAlpha(hex, a) {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return 'rgba(' + r + ',' + g + ',' + b + ',' + a + ')';
}
function clamp(v) { return Math.max(0, Math.min(255, v)); }

/* ═══════════════════════════════════════════════════════════════════
   Edition nav
   ═══════════════════════════════════════════════════════════════════ */
function buildEditionNav(cfg) {
  const nav = document.getElementById('edition-nav');
  if (!cfg || cfg.editions.length < 2) { nav.hidden = true; return; }
  nav.innerHTML = '';
  cfg.editions.forEach(ed => {
    const btn = el('button', 'edition-btn' + (ed.id === state.activeEdition ? ' active' : ''));
    btn.textContent = ed.label;
    btn.dataset.id = ed.id;
    btn.addEventListener('click', () => {
      state.activeEdition = ed.id;
      state.activeCategory = null;
      nav.querySelectorAll('.edition-btn').forEach(b =>
        b.classList.toggle('active', b.dataset.id === ed.id));
      refreshCategoryPills(cfg);
      render();
    });
    nav.appendChild(btn);
  });
}

/* ═══════════════════════════════════════════════════════════════════
   Filter panel
   ═══════════════════════════════════════════════════════════════════ */
function buildFilterPanel(cfg) {
  refreshCategoryPills(cfg);
  buildDateButtons();
  const input = document.getElementById('search-input');
  if (input) {
    input.addEventListener('input', () => {
      state.searchQuery = input.value.trim().toLowerCase();
      render();
    });
  }
}

function refreshCategoryPills(cfg) {
  const wrap = document.getElementById('category-pills');
  const sec = document.getElementById('category-section');
  wrap.innerHTML = '';
  if (!cfg) return;

  const edition = cfg.editions.find(e => e.id === state.activeEdition);
  if (!edition || !edition.categories.length) {
    sec.hidden = true;
    return;
  }
  sec.hidden = false;

  // "All" pill
  wrap.appendChild(makePill('All topics', null));
  edition.categories.forEach(cat => wrap.appendChild(makePill(cat, cat)));
}

function makePill(label, value) {
  const btn = el('button', 'cat-pill' + (state.activeCategory === value ? ' active' : ''));
  btn.textContent = label;
  btn.addEventListener('click', () => {
    state.activeCategory = (state.activeCategory === value) ? null : value;
    syncPillStates(value);
    render();
  });
  return btn;
}

function syncPillStates(clicked) {
  document.querySelectorAll('.cat-pill').forEach(p => {
    const isAll = p.textContent === 'All topics';
    p.classList.toggle('active', isAll ? state.activeCategory === null : p.textContent === state.activeCategory);
  });
}

function buildDateButtons() {
  const wrap = document.getElementById('date-filters');
  [['All dates', 'all'], ['Past 7 days', 'week'], ['This month', 'month']].forEach(([label, val]) => {
    const btn = el('button', 'date-btn' + (state.dateFilter === val ? ' active' : ''));
    btn.textContent = label;
    btn.addEventListener('click', () => {
      state.dateFilter = val;
      document.querySelectorAll('.date-btn').forEach(b =>
        b.classList.toggle('active', b.textContent === label));
      render();
    });
    wrap.appendChild(btn);
  });
}

function wireFilterToggle() {
  const btn = document.getElementById('filter-toggle');
  const panel = document.getElementById('filter-panel');
  btn.addEventListener('click', () => {
    const open = panel.classList.toggle('open');
    btn.setAttribute('aria-expanded', String(open));
  });
}

/* ═══════════════════════════════════════════════════════════════════
   Filtering
   ═══════════════════════════════════════════════════════════════════ */
function filteredItems() {
  const now = Date.now();
  const weekMs = 7 * 86400e3;
  return (state.data.items || []).filter(item => {
    // Edition: must have non-zero relevance in the active edition
    if (state.activeEdition) {
      const ea = item.per_edition && item.per_edition[state.activeEdition];
      if (!ea || ea.relevance_score === 0) return false;
    }
    // Category
    if (state.activeCategory) {
      const ea = item.per_edition && item.per_edition[state.activeEdition];
      if (!ea || !ea.categories.includes(state.activeCategory)) return false;
    }
    // Date
    if (state.dateFilter !== 'all') {
      const ts = new Date(item.published_at || item.collected_at).getTime();
      if (state.dateFilter === 'week' && now - ts > weekMs) return false;
      if (state.dateFilter === 'month') {
        const d = new Date(ts);
        const today = new Date();
        if (d.getFullYear() !== today.getFullYear() || d.getMonth() !== today.getMonth()) return false;
      }
    }
    // Keyword: substring match over title, source, and the active edition's analysis
    if (state.searchQuery) {
      const ea = item.per_edition && item.per_edition[state.activeEdition];
      const hay = [
        item.title,
        item.source_id,
        ea && ea.so_what,
        ea && ea.now_what,
        ...((ea && ea.categories) || []),
      ].filter(Boolean).join(' ').toLowerCase();
      if (!hay.includes(state.searchQuery)) return false;
    }
    return true;
  });
}

/* ═══════════════════════════════════════════════════════════════════
   Main render
   ═══════════════════════════════════════════════════════════════════ */
function render() {
  const items = filteredItems();
  // Within each tier, most relevant first (by the active edition's score,
  // falling back to importance_score), tie-broken by most recent.
  const score = it => {
    const ea = it.per_edition && it.per_edition[state.activeEdition];
    return ea ? ea.relevance_score : (it.importance_score || 0);
  };
  const byScore = (a, b) =>
    score(b) - score(a) ||
    new Date(b.published_at || b.collected_at) - new Date(a.published_at || a.collected_at);
  const t1 = items.filter(i => i.tier === 1).sort(byScore);
  const t2 = items.filter(i => i.tier === 2).sort(byScore);
  const t3 = items.filter(i => i.tier === 3).sort(byScore);

  renderWhatsNew(items);
  renderTier1(t1);
  renderTier2(t2);
  renderTier3(t3);
}

/* ═══════════════════════════════════════════════════════════════════
   What's-new zone
   ═══════════════════════════════════════════════════════════════════ */
function renderWhatsNew(visible) {
  const sec = document.getElementById('whats-new');
  sec.innerHTML = '';
  const wn = state.data.whats_new || {};
  const editorial = state.data.editorial;
  const visSet = new Set(visible.map(i => i.item_id));

  const n1    = (wn.new_tier_1  || []).filter(id => visSet.has(id)).length;
  const n2    = (wn.new_tier_2  || []).filter(id => visSet.has(id)).length;
  const nEsc  = (wn.escalated   || []).filter(e  => visSet.has(e.item_id)).length;
  const dl    = (wn.deadline_imminent || []).filter(id => visSet.has(id)).length;

  const chips = [];
  if (n1)   chips.push('<span class="sig-chip t1">▲ ' + n1 + ' new essential</span>');
  if (n2)   chips.push('<span class="sig-chip t2">+ ' + n2 + ' new important</span>');
  if (nEsc) chips.push('<span class="sig-chip esc">↑ ' + nEsc + ' escalated</span>');
  if (dl)   chips.push('<span class="sig-chip dl">⏰ ' + dl + ' deadline soon</span>');

  if (chips.length) {
    const bar = el('div', 'wn-bar');
    bar.innerHTML = '<span class="wn-label">What\'s new</span>' + chips.join('');
    sec.appendChild(bar);
  }

  if (editorial) {
    const card = el('div', 'editorial');
    card.innerHTML =
      '<p class="editorial-theme">' + esc(editorial.theme_of_week) + '</p>' +
      '<p class="editorial-note">' + esc(editorial.editors_note) + '</p>' +
      '<p class="editorial-digest">' + esc(editorial.whats_new_digest) + '</p>';
    sec.appendChild(card);
  }
}

/* ═══════════════════════════════════════════════════════════════════
   Tier 1 — full cards
   ═══════════════════════════════════════════════════════════════════ */
function renderTier1(items) {
  const grid = document.getElementById('tier1-cards');
  const empty = document.getElementById('tier1-empty');
  grid.innerHTML = '';
  empty.hidden = items.length > 0;
  items.forEach(item => grid.appendChild(fullCard(item)));
}

function fullCard(item) {
  const ea = editionAnalysis(item);
  const isNew = state.newIds.has(item.item_id);
  const div = el('div', 'full-card');

  // NEW badge
  if (isNew) {
    const badge = el('span', 'new-badge');
    badge.textContent = 'NEW';
    div.appendChild(badge);
  }

  // Title
  const titleEl = el('div', 'card-title');
  const titleLink = safeLink(item.url, cleanTitle(item.title));
  titleLink.title = item.title;            // full title on hover
  titleEl.appendChild(titleLink);
  div.appendChild(titleEl);

  // Meta row
  const meta = el('div', 'card-meta');
  const src = el('span', 'source-tag');
  src.textContent = item.source_id;
  meta.appendChild(src);
  if (item.published_at) {
    const dt = el('span');
    dt.textContent = fmtDate(item.published_at);
    meta.appendChild(dt);
  }
  if (ea) {
    const sc = el('span', 'score-dot');
    sc.textContent = ea.relevance_score;
    sc.style.color = ea.relevance_score >= 80 ? 'var(--t1-accent)' : 'var(--t2-accent)';
    meta.appendChild(sc);
  }
  div.appendChild(meta);

  // Analysis
  if (ea) {
    const ab = el('div', 'analysis-block');
    ab.innerHTML =
      '<div class="so-what">' +
        '<span class="analysis-label">Why it matters</span>' +
        esc(ea.so_what) +
      '</div>' +
      '<div class="now-what">' +
        '<span class="analysis-label">What to do</span>' +
        esc(ea.now_what) +
      '</div>';
    div.appendChild(ab);
  }

  // Stat chips
  const chips = statChips(item);
  if (chips) div.appendChild(chips);

  // Category tags
  const fullTags = categoryTags(ea && ea.categories);
  if (fullTags) div.appendChild(fullTags);

  // Confidence note
  if (item.confidence_note) {
    const note = el('div', 'conf-note');
    note.textContent = '⚠ ' + item.confidence_note;
    div.appendChild(note);
  }

  div.appendChild(deepAnalysisBlock(item));
  return div;
}

/* ═══════════════════════════════════════════════════════════════════
   Tier 2 — compact expandable rows
   ═══════════════════════════════════════════════════════════════════ */
function renderTier2(items) {
  const list = document.getElementById('tier2-cards');
  const empty = document.getElementById('tier2-empty');
  list.innerHTML = '';
  empty.hidden = items.length > 0;
  items.forEach(item => list.appendChild(compactRow(item)));
}

function compactRow(item) {
  const ea = editionAnalysis(item);
  const isNew = state.newIds.has(item.item_id);
  const details = el('details', 'compact-row');

  // Summary (always visible)
  const summary = el('summary', 'compact-summary');
  const left = el('div', 'compact-left');

  const title = el('div', 'compact-title');
  title.textContent = cleanTitle(item.title);
  title.title = item.title;                 // full title on hover
  left.appendChild(title);

  const meta = el('div', 'compact-meta');
  const src = el('span', 'source-tag');
  src.textContent = item.source_id;
  meta.appendChild(src);
  if (item.published_at) {
    const dt = el('span');
    dt.textContent = fmtDate(item.published_at);
    meta.appendChild(dt);
  }
  if (isNew) {
    const nb = el('span');
    nb.textContent = 'New';
    nb.style.cssText = 'color:var(--sig-new);font-weight:700';
    meta.appendChild(nb);
  }
  if (ea) {
    const sc = el('span', 'score-dot');
    sc.textContent = ea.relevance_score;
    sc.style.color = 'var(--t2-accent)';
    meta.appendChild(sc);
  }
  left.appendChild(meta);
  summary.appendChild(left);

  const chev = el('span', 'chevron');
  chev.textContent = '▾';
  chev.setAttribute('aria-hidden', 'true');
  summary.appendChild(chev);
  details.appendChild(summary);

  // Detail (shown when open)
  if (ea) {
    const detail = el('div', 'compact-detail');

    const ab = el('div', 'analysis-block');
    ab.innerHTML =
      '<div class="so-what">' +
        '<span class="analysis-label">Why it matters</span>' +
        esc(ea.so_what) +
      '</div>' +
      '<div class="now-what">' +
        '<span class="analysis-label">What to do</span>' +
        esc(ea.now_what) +
      '</div>';
    detail.appendChild(ab);

    const chips = statChips(item);
    if (chips) detail.appendChild(chips);

    const compactTags = categoryTags(ea.categories);
    if (compactTags) detail.appendChild(compactTags);

    if (item.confidence_note) {
      const note = el('div', 'conf-note');
      note.textContent = '⚠ ' + item.confidence_note;
      detail.appendChild(note);
    }

    detail.appendChild(safeLink(item.url, 'Read source ↗', 'source-link'));

    details.appendChild(detail);
  }

  details.appendChild(deepAnalysisBlock(item));
  return details;
}

/* ═══════════════════════════════════════════════════════════════════
   Tier 3 — collapsed drawer
   ═══════════════════════════════════════════════════════════════════ */
function renderTier3(items) {
  const list   = document.getElementById('tier3-list');
  const count  = document.getElementById('tier3-count');
  const drawer = document.getElementById('tier3-drawer');
  list.innerHTML = '';
  count.textContent = items.length;
  drawer.style.display = items.length === 0 ? 'none' : '';
  items.forEach(item => {
    const li = el('li', 'tier3-item');
    const dot = el('span', 'tier3-dot');
    const a = safeLink(item.url, cleanTitle(item.title));
    a.title = item.title;
    li.appendChild(dot);
    li.appendChild(a);
    li.appendChild(deepAnalysisBlock(item));
    list.appendChild(li);
  });
}

/* ═══════════════════════════════════════════════════════════════════
   In-depth analysis
   ═══════════════════════════════════════════════════════════════════ */

// ── Live-call seam ───────────────────────────────────────────────────
// Returns a Promise of the deep-analysis payload for one item. Today it
// resolves with data precomputed at pipeline time and embedded in the loaded
// artifact. To move deep analysis to an on-demand backend later, change ONLY
// this function body — e.g.
//     return fetch('/api/deep/' + encodeURIComponent(itemId)).then(r => r.json());
// Every caller already awaits the returned promise and renders from its result,
// so no other frontend code needs to change.
function getDeepAnalysis(itemId) {
  const item = (state.data.items || []).find(i => i.item_id === itemId);
  return Promise.resolve((item && item.deep_analysis) || null);
}

// Button + lazily-populated panel, shared by every tier's card renderer.
function deepAnalysisBlock(item) {
  const wrap = el('div', 'deep-wrap');

  const btn = el('button', 'deep-btn');
  btn.textContent = 'In-depth analysis';
  btn.setAttribute('aria-expanded', 'false');

  const panel = el('div', 'deep-panel');
  panel.hidden = true;

  btn.addEventListener('click', () => {
    const willOpen = panel.hidden;
    panel.hidden = !willOpen;
    btn.setAttribute('aria-expanded', String(willOpen));
    // Populate once, on first open. getDeepAnalysis is the swap point for a
    // future remote call; we always treat its result as a promise.
    if (willOpen && !panel.dataset.loaded) {
      panel.dataset.loaded = '1';
      getDeepAnalysis(item.item_id).then(da => renderDeepSections(panel, da));
    }
  });

  wrap.appendChild(btn);
  wrap.appendChild(panel);
  return wrap;
}

function renderDeepSections(panel, da) {
  panel.innerHTML = '';
  if (!da || !da.sections) {
    const empty = el('p', 'deep-empty');
    empty.textContent = 'No in-depth analysis available for this item.';
    panel.appendChild(empty);
    return;
  }

  // Section order and labels come from site_config (presentation layer);
  // fall back to the data's own keys if config metadata is absent.
  const cfg = state.data.site_config;
  const meta = (cfg && cfg.deep_analysis_sections) ||
    Object.keys(da.sections).map(id => ({ id, label: id }));

  meta.forEach(m => {
    const value = da.sections[m.id];
    if (value == null) return;
    const sec = el('div', 'deep-section');
    const h = el('h4', 'deep-section-label');
    h.textContent = m.label;
    sec.appendChild(h);
    if (Array.isArray(value)) {
      const ul = el('ul', 'deep-list');
      value.forEach(v => { const li = el('li'); li.textContent = v; ul.appendChild(li); });
      sec.appendChild(ul);
    } else {
      const p = el('p', 'deep-text');
      p.textContent = value;
      sec.appendChild(p);
    }
    panel.appendChild(sec);
  });
}

/* ═══════════════════════════════════════════════════════════════════
   Run meta
   ═══════════════════════════════════════════════════════════════════ */
function renderRunMeta() {
  const wrap = document.getElementById('run-meta');
  const m = state.data.meta;
  if (!m) return;
  const lines = [];
  if (m.run_at) lines.push('Run: ' + fmtDate(m.run_at));
  if (m.items_analyzed != null) lines.push(m.items_analyzed + ' items analyzed');
  if (m.estimated_cost_usd != null) lines.push('Cost: $' + m.estimated_cost_usd.toFixed(4));
  if (m.engine_version) lines.push('Engine v' + m.engine_version);
  wrap.innerHTML = lines.map(l => '<p>' + esc(l) + '</p>').join('');
}

/* ═══════════════════════════════════════════════════════════════════
   Stat chips
   ═══════════════════════════════════════════════════════════════════ */
function statChips(item) {
  const chips = [];

  if (item.dollar_amount) {
    const txt = item.dollar_amount.value != null
      ? fmtMoney(item.dollar_amount.value, item.dollar_amount.currency)
      : item.dollar_amount.raw_text;
    const c = el('span', 'stat-chip money');
    c.textContent = '💰 ' + txt;
    chips.push(c);
  }

  if (item.affected_population) {
    const txt = item.affected_population.value != null
      ? fmtPop(item.affected_population.value, item.affected_population.unit)
      : item.affected_population.raw_text;
    const c = el('span', 'stat-chip population');
    c.textContent = '👥 ' + txt;
    chips.push(c);
  }

  if (item.action_deadline) {
    const imminent = deadlineImminent(item.action_deadline);
    const c = el('span', 'stat-chip deadline' + (imminent ? ' imminent' : ''));
    c.textContent = '📅 ' + fmtDeadline(item.action_deadline);
    chips.push(c);
  }

  if (!chips.length) return null;
  const wrap = el('div', 'stat-chips');
  chips.forEach(c => wrap.appendChild(c));
  return wrap;
}

/* ═══════════════════════════════════════════════════════════════════
   Helpers
   ═══════════════════════════════════════════════════════════════════ */
function editionAnalysis(item) {
  if (!state.activeEdition || !item.per_edition) return null;
  return item.per_edition[state.activeEdition] || null;
}

function deadlineImminent(dateStr) {
  const ms = new Date(dateStr + 'T00:00:00').getTime() - Date.now();
  return ms >= 0 && ms <= 7 * 86400e3;
}

function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const diffDays = Math.floor((Date.now() - d.getTime()) / 86400e3);
  if (diffDays === 0) return 'Today';
  if (diffDays === 1) return 'Yesterday';
  if (diffDays < 7)  return diffDays + 'd ago';
  return d.toLocaleDateString('en-US', {
    month: 'short', day: 'numeric',
    year: diffDays > 365 ? 'numeric' : undefined,
  });
}

function fmtDeadline(dateStr) {
  const d = new Date(dateStr + 'T00:00:00');
  const diff = Math.ceil((d.getTime() - Date.now()) / 86400e3);
  if (diff < 0)  return 'Due ' + d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) + ' (passed)';
  if (diff === 0) return 'Due today';
  if (diff === 1) return 'Due tomorrow';
  if (diff <= 7)  return 'Due in ' + diff + ' days';
  return 'Due ' + d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function fmtMoney(v, currency) {
  const sym = currency === 'EUR' ? '€' : currency === 'GBP' ? '£' : '$';
  if (v >= 1e12) return sym + (v / 1e12).toFixed(1) + 'T';
  if (v >= 1e9)  return sym + (v / 1e9).toFixed(1) + 'B';
  if (v >= 1e6)  return sym + (v / 1e6).toFixed(1) + 'M';
  if (v >= 1e3)  return sym + (v / 1e3).toFixed(0) + 'K';
  return sym + v.toFixed(0);
}

function fmtPop(v, unit) {
  const s = v >= 1e6 ? (v / 1e6).toFixed(1) + 'M'
          : v >= 1e3 ? (v / 1e3).toFixed(0) + 'K'
          : String(v);
  return unit ? s + ' ' + unit : s;
}

// DOM element factory
function el(tag, className) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  return e;
}

// Render an <a> for an http(s) URL, or a non-clickable <span> for a bare
// reference (e.g. an openFDA recall id that has no public page) — so item
// cards never emit dead links.
function safeLink(url, text, className) {
  const isUrl = /^https?:\/\//i.test(url || '');
  const cls = isUrl ? className : (className ? className + ' nonlink' : 'nonlink');
  const node = el(isUrl ? 'a' : 'span', cls);
  node.textContent = text;
  if (isUrl) {
    node.href = url;
    node.target = '_blank';
    node.rel = 'noopener';
  }
  return node;
}

// Tidy a source title for display: collapse whitespace and clamp to ~80 chars
// on a word boundary with an ellipsis. The full title is kept as the element's
// hover tooltip by callers. Cleaning happens at display only — stored data is
// untouched.
function cleanTitle(t) {
  const s = String(t || '').replace(/\s+/g, ' ').trim();
  if (s.length <= 80) return s;
  const clamped = s.slice(0, 80);
  const sp = clamped.lastIndexOf(' ');
  return (sp > 40 ? clamped.slice(0, sp) : clamped).replace(/[\s,;:.-]+$/, '') + '…';
}

// Generic icon for a category, by keyword. Keywords are generic category-type
// words (not client/industry terms), so this stays config-agnostic.
function categoryIcon(cat) {
  const c = String(cat || '').toLowerCase();
  const map = [
    [/enforc|litigat|legal|court|settlement/, '⚖️'],
    [/deadline|comment period/, '⏰'],
    [/payment|reimburs|pricing|cost|funding|budget|appropriat/, '💵'],
    [/clearance|approval|device|drug|biologic/, '✅'],
    [/market|m&a|merger|acquisition|moves/, '📈'],
    [/legislat|bill|congress|statut/, '🏛️'],
    [/rule|regulat|guidance|policy/, '📋'],
    [/coverage/, '🛡️'],
    [/oversight|report|audit/, '🔍'],
    [/safety|recall|risk/, '⚠️'],
  ];
  for (const [re, icon] of map) if (re.test(c)) return icon;
  return '🏷️';
}

// Clickable category tags for an item — shared by the full and compact cards.
// Returns the tags container, or null when there are no categories.
function categoryTags(categories) {
  if (!categories || !categories.length) return null;
  const tags = el('div', 'cat-tags');
  categories.forEach(cat => {
    const t = el('span', 'cat-tag');
    t.textContent = categoryIcon(cat) + ' ' + cat;
    t.addEventListener('click', () => { state.activeCategory = cat; syncPillStates(cat); render(); });
    tags.appendChild(t);
  });
  return tags;
}

// Minimal HTML escaping — use for all untrusted strings inserted as innerHTML
function esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
