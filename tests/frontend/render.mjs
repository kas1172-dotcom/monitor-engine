// Load app.js into the fake DOM, feed it a run_output.json, and return the
// rendered tier-bucket counts. Used by both the diagnostic script and the
// pytest-driven smoke test.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';
import { buildDocumentFromTemplate } from './dom_harness.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..', '..');
const APP_JS = join(ROOT, 'monitor_engine', 'site', '_assets', 'app.js');
const TEMPLATE = join(ROOT, 'monitor_engine', 'site', '_template', 'index.html');

// Render `data` (a parsed run_output.json object) and return bucket counts.
// If `searchQuery` is given, also types it into the search box and records the
// post-filter item count as `searchAfter`.
export function renderData(data, searchQuery = null) {
  const templateHtml = readFileSync(TEMPLATE, 'utf8');
  const doc = buildDocumentFromTemplate(templateHtml);

  const win = {
    __DATA_URL: 'run_output.json',
    fetch: () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(data) }),
  };

  const sandbox = {
    document: doc,
    window: win,
    fetch: win.fetch,
    console,
    Date, Set, Math, JSON, Array, Object, String, Number, parseInt, parseFloat,
    setTimeout, queueMicrotask,
  };
  vm.createContext(sandbox);
  vm.runInContext(readFileSync(APP_JS, 'utf8'), sandbox, { filename: 'app.js' });

  // Click the first deep-analysis button inside `container` and report whether
  // the panel populated with sections from the precomputed data.
  function probeDeep(container) {
    const card = container.children[0];
    if (!card) return { hasButton: false, sectionCount: 0 };
    const btn = card.findByClass('deep-btn');
    if (!btn) return { hasButton: false, sectionCount: 0 };
    btn.dispatchEvent('click');                 // populates the panel asynchronously
    return { btn, card };
  }

  // app.js wires its entry point to DOMContentLoaded.
  doc.dispatchEvent('DOMContentLoaded');

  // Two flushes: one for fetch+initial render, one for the deep-analysis
  // promises resolved by the button clicks.
  return new Promise(resolve => {
    setTimeout(() => {
      const containers = {
        tier1: doc.getElementById('tier1-cards'),
        tier2: doc.getElementById('tier2-cards'),
        tier3: doc.getElementById('tier3-list'),
      };
      const probes = {
        tier1: probeDeep(containers.tier1),
        tier2: probeDeep(containers.tier2),
        tier3: probeDeep(containers.tier3),
      };
      setTimeout(() => {
        const deep = {};
        for (const [tier, p] of Object.entries(probes)) {
          deep[tier] = {
            hasButton: p.btn !== undefined || p.hasButton === true,
            sectionCount: p.card ? p.card.countByClass('deep-section') : 0,
          };
        }
        const total = () => containers.tier1.children.length
          + containers.tier2.children.length + containers.tier3.children.length;
        // Tag name of the first tier-1 card's title node: "A" for a real link,
        // "SPAN" when the item URL is a bare reference (safeLink fallback).
        const titleNode = card => {
          const wrap = card && card.findByClass('card-title');
          return wrap && wrap.children[0];
        };
        let tier1TitleTag = null;
        const firstCard = containers.tier1.children[0];
        if (firstCard) {
          const node = titleNode(firstCard);
          tier1TitleTag = node ? node.tagName : null;
        }
        // Ordered tier-1 title texts (to verify within-tier sort), first card's
        // title text (to verify title clamping), and first category tag text
        // (to verify per-category icons).
        const tier1Titles = containers.tier1.children
          .map(c => { const n = titleNode(c); return n ? n.textContent : null; });
        const firstCatTag = firstCard
          ? (firstCard.findByClass('cat-tag') || {}).textContent ?? null
          : null;
        const result = {
          tier1: containers.tier1.children.length,
          tier2: containers.tier2.children.length,
          tier3: containers.tier3.children.length,
          tier3Count: doc.getElementById('tier3-count').textContent,
          loadingHidden: doc.getElementById('loading-screen').classList.contains('hidden'),
          title: doc.getElementById('site-title').textContent,
          tier1TitleTag,
          tier1Titles,
          firstCatTag,
          deep,
        };
        // Feedback probe: count per-item controls and verify the first card's
        // first feedback button toggles active on click (capture wiring works).
        const fbControls = containers.tier1.countByClass('fb-controls')
          + containers.tier2.countByClass('fb-controls')
          + containers.tier3.countByClass('fb-controls');
        const firstFbBtn = containers.tier1.findByClass('fb-btn');
        let fbToggled = false;
        if (firstFbBtn) {
          firstFbBtn.dispatchEvent('click');
          fbToggled = firstFbBtn.classList.contains('fb-active');
        }
        result.feedback = { controls: fbControls, toggledActive: fbToggled };
        // Search probe: type a query, re-render, record how many items remain.
        const search = doc.getElementById('search-input');
        if (search && typeof searchQuery === 'string') {
          search.value = searchQuery;
          search.dispatchEvent('input');
          result.searchAfter = total();
        }
        resolve(result);
      }, 0);
    }, 0);
  });
}

// CLI: `node render.mjs path/to/run_output.json [search-query]`
// Prints a single compact JSON line so callers (e.g. pytest) can parse it.
if (process.argv[2]) {
  const data = JSON.parse(readFileSync(process.argv[2], 'utf8'));
  renderData(data, process.argv[3] ?? null).then(r => {
    const out = {
      ...r,
      itemCount: data.items.length,
      totalRendered: r.tier1 + r.tier2 + r.tier3,
    };
    console.log(JSON.stringify(out));
  });
}
