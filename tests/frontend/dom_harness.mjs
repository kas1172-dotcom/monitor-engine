// Minimal zero-dependency DOM shim — just enough to execute site/_assets/app.js
// headlessly and inspect what it renders. No jsdom, no npm packages (the engine
// follows a boring/dependency-light rule). Implements only the DOM surface app.js
// actually touches; anything missing throws loudly rather than silently passing.

class FakeClassList {
  constructor(el) { this._el = el; this._set = new Set(); }
  add(...c) { c.forEach(x => this._set.add(x)); this._sync(); }
  remove(...c) { c.forEach(x => this._set.delete(x)); this._sync(); }
  contains(c) { return this._set.has(c); }
  toggle(c, force) {
    const has = this._set.has(c);
    const want = force === undefined ? !has : force;
    if (want) this._set.add(c); else this._set.delete(c);
    this._sync();
    return want;
  }
  _sync() { this._el._className = [...this._set].join(' '); }
}

class FakeStyle {
  setProperty(k, v) { this[k] = v; }
}

export class FakeElement {
  constructor(tag, doc) {
    this.tagName = (tag || 'div').toUpperCase();
    this._doc = doc;
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.style = new FakeStyle();
    this.classList = new FakeClassList(this);
    this.attributes = {};
    this._className = '';
    this._textContent = '';
    this._innerHTML = '';
    this.hidden = false;
    this._listeners = {};
  }
  get className() { return this._className; }
  set className(v) {
    this._className = v || '';
    this.classList._set = new Set(this._className.split(/\s+/).filter(Boolean));
  }
  get textContent() { return this._textContent; }
  set textContent(v) { this._textContent = String(v); this.children = []; this._innerHTML = ''; }
  get innerHTML() { return this._innerHTML; }
  set innerHTML(v) {
    // app.js only ever assigns '' (to clear) or trusted HTML fragments that do
    // not themselves contain elements we need to count. Clearing must drop
    // appended children so re-renders start clean.
    this._innerHTML = String(v);
    this.children = [];
  }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return this.attributes[k] ?? null; }
  addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }
  dispatchEvent(type) { (this._listeners[type] || []).forEach(fn => fn({ type })); }
  querySelectorAll() { return []; }   // only used inside click handlers, never on the initial render path
  _hasClass(cls) { return this._className.split(/\s+/).includes(cls); }
  // Recursively count descendants whose className contains `cls`.
  countByClass(cls) {
    let n = 0;
    for (const c of this.children) {
      if (c._hasClass(cls)) n++;
      n += c.countByClass(cls);
    }
    return n;
  }
  // Depth-first search for the first descendant whose className contains `cls`.
  findByClass(cls) {
    for (const c of this.children) {
      if (c._hasClass(cls)) return c;
      const found = c.findByClass(cls);
      if (found) return found;
    }
    return null;
  }
}

export class FakeDocument {
  constructor() {
    this._byId = new Map();
    this.documentElement = new FakeElement('html', this);
    this._listeners = {};
  }
  registerId(id) {
    if (!this._byId.has(id)) {
      const el = new FakeElement('div', this);
      el.attributes.id = id;
      this._byId.set(id, el);
    }
    return this._byId.get(id);
  }
  getElementById(id) { return this._byId.get(id) || this.registerId(id); }
  createElement(tag) { return new FakeElement(tag, this); }
  querySelectorAll() { return []; }
  addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }
  dispatchEvent(type) { (this._listeners[type] || []).forEach(fn => fn({ type })); }
}

// Build a document pre-populated with every id="..." declared in the template,
// so getElementById returns real, stable elements just like a browser would.
export function buildDocumentFromTemplate(templateHtml) {
  const doc = new FakeDocument();
  const idRe = /id="([^"]+)"/g;
  let m;
  while ((m = idRe.exec(templateHtml)) !== null) doc.registerId(m[1]);
  return doc;
}
