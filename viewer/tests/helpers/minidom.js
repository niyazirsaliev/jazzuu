// A very small HTML parser + DOM, just large enough to run the real app.js.
//
// Why hand-rolled: this repo ships no node_modules (the image is built from a
// pinned requirements.txt and the PWA has zero JS dependencies), so jsdom is
// not available and adding a dependency tree to run one test is a worse trade
// than 200 lines of DOM we fully control.
//
// It implements only what app/static/app.js actually touches: innerHTML get/set
// (round-tripping exactly, so "did this node change?" comparisons behave like a
// browser's), querySelector(All) over the handful of selector shapes the app
// uses, getElementById, closest, className/id, appendChild/remove, and bubbling
// addEventListener/dispatchEvent. Anything else throws rather than silently
// pretending, so a test can never pass on a fiction.
'use strict';

const VOID_TAGS = new Set([
  'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta',
  'param', 'source', 'track', 'wbr',
]);
const RAW_TEXT_TAGS = new Set(['script', 'style']);

const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' };
function decodeEntities(s) {
  return String(s).replace(/&(#x?[0-9a-fA-F]+|[a-zA-Z]+);/g, (m, body) => {
    if (body[0] === '#') {
      const code = body[1] === 'x' || body[1] === 'X'
        ? parseInt(body.slice(2), 16) : parseInt(body.slice(1), 10);
      return isFinite(code) ? String.fromCodePoint(code) : m;
    }
    return Object.prototype.hasOwnProperty.call(ENTITIES, body) ? ENTITIES[body] : m;
  });
}

// A stand-in for CSSStyleDeclaration in the one respect the app depends on:
// a property that was never set reads back as '', not undefined. Without this,
// "restore the overflow that was there before" would restore `undefined` and
// look like it had worked.
function createStyle() {
  return new Proxy({}, {
    get(target, prop) {
      if (typeof prop !== 'string') return target[prop];
      return prop in target ? target[prop] : '';
    },
    set(target, prop, value) {
      target[prop] = value == null ? '' : String(value);
      return true;
    },
  });
}

class TextNode {
  constructor(raw) { this.nodeType = 3; this.raw = raw; this.parentNode = null; }
  get textContent() { return decodeEntities(this.raw); }
  get outerHTML() { return this.raw; }
}

class Element {
  constructor(tagName) {
    this.nodeType = 1;
    this.tagName = tagName.toUpperCase();
    this.localName = tagName.toLowerCase();
    this.attrs = new Map();      // name -> raw (still-encoded) value
    this.childNodes = [];
    this.parentNode = null;
    this.listeners = new Map();
    this.style = createStyle();
    this.ownerDocument = null;
  }

  // ---- attributes ----
  getAttribute(name) {
    const raw = this.attrs.get(String(name).toLowerCase());
    return raw === undefined ? null : decodeEntities(raw);
  }
  setAttribute(name, value) { this.attrs.set(String(name).toLowerCase(), String(value)); }
  hasAttribute(name) { return this.attrs.has(String(name).toLowerCase()); }
  removeAttribute(name) { this.attrs.delete(String(name).toLowerCase()); }
  get id() { return this.getAttribute('id') || ''; }
  set id(v) { this.setAttribute('id', v); }
  // Form-field state, as a browser models it: the `value` ATTRIBUTE is the
  // default that markup carries, and the `value` PROPERTY is what the reader has
  // typed. They are deliberately separate — the app reads the property when it
  // saves, and a re-render that reset the property would silently discard input.
  get value() {
    return this._value === undefined ? (this.getAttribute('value') || '') : this._value;
  }
  set value(v) { this._value = v == null ? '' : String(v); }
  get className() { return this.getAttribute('class') || ''; }
  set className(v) { this.setAttribute('class', v); }
  get classList() { return new Set(this.className.split(/\s+/).filter(Boolean)); }

  // ---- tree ----
  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
  appendChild(node) {
    node.parentNode = this;
    if (node.nodeType === 1) node.ownerDocument = this.ownerDocument;
    this.childNodes.push(node);
    return node;
  }
  remove() {
    if (!this.parentNode) return;
    const i = this.parentNode.childNodes.indexOf(this);
    if (i >= 0) this.parentNode.childNodes.splice(i, 1);
    this.parentNode = null;
  }
  // True only while this node is still attached to the document that made it —
  // the check code uses before handing focus back to an element it captured
  // earlier, which a re-render may since have thrown away.
  get isConnected() {
    let node = this;
    while (node.parentNode) node = node.parentNode;
    return !!this.ownerDocument && node === this.ownerDocument.documentRoot;
  }

  // ---- serialization ----
  // Raw in, raw out: attribute values and text keep the exact bytes they were
  // parsed from, so `el.innerHTML === generatedHtml` is true whenever the app
  // regenerated identical markup — the same property a browser gives us and the
  // one patchAsrSlots() relies on to avoid pointless DOM writes.
  get innerHTML() { return this.childNodes.map((n) => n.outerHTML).join(''); }
  set innerHTML(html) {
    // Detach what is being replaced. A browser leaves the discarded subtree
    // orphaned, and code that captured a node from it must be able to see that
    // it is no longer in the document.
    for (const node of this.childNodes) node.parentNode = null;
    this.childNodes = [];
    for (const node of parseFragment(String(html), this.ownerDocument)) {
      this.appendChild(node);
    }
  }
  get outerHTML() {
    let open = this.localName;
    for (const [name, value] of this.attrs) open += ` ${name}="${value}"`;
    if (VOID_TAGS.has(this.localName)) return `<${open}>`;
    return `<${open}>${this.innerHTML}</${this.localName}>`;
  }
  get textContent() {
    return this.childNodes.map((n) => n.textContent).join('');
  }

  // ---- queries ----
  matches(selector) { return matchesSelector(this, selector); }
  closest(selector) {
    let node = this;
    while (node && node.nodeType === 1) {
      if (node.matches(selector)) return node;
      node = node.parentNode;
    }
    return null;
  }
  querySelectorAll(selector) {
    const out = [];
    walk(this, (el) => { if (el !== this && el.matches(selector)) out.push(el); });
    return out;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }

  // ---- focus ----
  // renderSearch() focuses its input; the mind-map overlay moves focus to its
  // close button and back again. The options bag is recorded rather than
  // ignored so a test can assert on preventScroll, which is load-bearing on a
  // pan/zoom surface.
  focus(options) {
    this.focusOptions = options;
    this.focusCount = (this.focusCount || 0) + 1;
    if (this.ownerDocument) this.ownerDocument.activeElement = this;
  }
  blur() {
    if (this.ownerDocument && this.ownerDocument.activeElement === this) {
      this.ownerDocument.activeElement = null;
    }
  }
  scrollIntoView() {}

  // ---- events ----
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }
  removeEventListener(type, fn) {
    const list = this.listeners.get(type) || [];
    const i = list.indexOf(fn);
    if (i >= 0) list.splice(i, 1);
  }
  dispatchEvent(event) {
    const ev = Object.assign({ target: this, defaultPrevented: false }, event);
    ev.preventDefault = () => { ev.defaultPrevented = true; };
    let node = this;
    while (node) {
      ev.currentTarget = node;
      for (const fn of (node.listeners.get(ev.type) || []).slice()) fn.call(node, ev);
      node = node.parentNode;
    }
    return !ev.defaultPrevented;
  }
}

function walk(root, visit) {
  visit(root);
  for (const child of root.childNodes) if (child.nodeType === 1) walk(child, visit);
}

// Selector subset: a compound of #id / .class / tag / [attr] / [attr="v"].
const SIMPLE = /#([\w-]+)|\.([\w:./[\]-]+)|\[([\w-]+)(?:([~|^$*]?=)"?([^\]"]*)"?)?\]|([\w-]+)/g;
function matchesSelector(el, selector) {
  const sel = String(selector).trim();
  if (!sel) return false;
  // Only a SINGLE compound selector is supported. A descendant/child/sibling
  // combinator used to match nothing at all rather than raising, so a test
  // could assert against an element it had never actually found — precisely
  // the "passing on a fiction" this helper exists to prevent. Attribute values
  // are stripped first so [aria-label="две слова"] stays legal.
  if (/[\s>+~,]/.test(sel.replace(/\[[^\]]*\]/g, ''))) {
    throw new Error(
      `minidom: unsupported selector ${JSON.stringify(selector)} — only a single ` +
      'compound selector is supported; scope the query on the parent element ' +
      "instead, e.g. el.querySelector('img')");
  }
  SIMPLE.lastIndex = 0;
  let m;
  let consumed = 0;
  while ((m = SIMPLE.exec(sel))) {
    consumed = SIMPLE.lastIndex;
    const [, id, cls, attr, op, value, tag] = m;
    if (id !== undefined && el.getAttribute('id') !== id) return false;
    if (cls !== undefined && !el.classList.has(cls)) return false;
    if (tag !== undefined && el.localName !== tag.toLowerCase()) return false;
    if (attr !== undefined) {
      const actual = el.getAttribute(attr);
      if (actual === null) return false;
      if (op && actual !== value) return false;
    }
  }
  if (consumed !== sel.length) {
    throw new Error(`minidom: unsupported selector ${JSON.stringify(selector)}`);
  }
  return true;
}

// ---- parser ----
const TAG_RE = /<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>|<!DOCTYPE[^>]*>|<\/([A-Za-z][\w:-]*)\s*>|<([A-Za-z][\w:-]*)((?:\s+[^\s=/>]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]*))?)*)\s*(\/?)>/g;
const ATTR_RE = /([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]*)))?/g;

function parseAttrs(source) {
  const out = [];
  if (!source) return out;
  ATTR_RE.lastIndex = 0;
  let m;
  while ((m = ATTR_RE.exec(source))) {
    if (!m[0].trim()) { ATTR_RE.lastIndex++; continue; }
    const value = m[2] !== undefined ? m[2] : m[3] !== undefined ? m[3] : m[4] || '';
    out.push([m[1].toLowerCase(), value]);
  }
  return out;
}

function parseFragment(html, ownerDocument) {
  const root = new Element('#fragment');
  root.ownerDocument = ownerDocument;
  const stack = [root];
  const top = () => stack[stack.length - 1];
  let cursor = 0;
  TAG_RE.lastIndex = 0;
  let m;
  while ((m = TAG_RE.exec(html))) {
    if (m.index > cursor) top().appendChild(new TextNode(html.slice(cursor, m.index)));
    cursor = TAG_RE.lastIndex;
    if (m[0].startsWith('<!')) continue;            // comment / doctype: dropped
    if (m[1]) {                                      // closing tag
      for (let i = stack.length - 1; i > 0; i--) {
        if (stack[i].localName === m[1].toLowerCase()) { stack.length = i; break; }
      }
      continue;
    }
    const tag = m[2].toLowerCase();
    const el = new Element(tag);
    el.ownerDocument = ownerDocument;
    for (const [name, value] of parseAttrs(m[3])) el.attrs.set(name, value);
    top().appendChild(el);
    if (VOID_TAGS.has(tag) || m[4]) continue;        // void or self-closed
    if (RAW_TEXT_TAGS.has(tag)) {                    // <script>/<style>: raw text
      const close = html.toLowerCase().indexOf(`</${tag}`, cursor);
      const end = close < 0 ? html.length : close;
      if (end > cursor) el.appendChild(new TextNode(html.slice(cursor, end)));
      const gt = html.indexOf('>', end);
      cursor = gt < 0 ? html.length : gt + 1;
      TAG_RE.lastIndex = cursor;
      continue;
    }
    stack.push(el);
  }
  if (cursor < html.length) top().appendChild(new TextNode(html.slice(cursor)));
  const nodes = root.childNodes.slice();
  for (const n of nodes) n.parentNode = null;
  return nodes;
}

// ---- document ----
function createDocument(html) {
  const doc = {
    nodeType: 9,
    listeners: new Map(),
    visibilityState: 'visible',
  };
  const nodes = parseFragment(html, doc);
  const root = new Element('#document');
  root.ownerDocument = doc;
  for (const n of nodes) root.appendChild(n);

  doc.documentElement = root.querySelector('html') || root;
  doc.head = root.querySelector('head');
  doc.body = root.querySelector('body') || root;
  doc.getElementById = (id) => root.querySelector(`#${id}`);
  doc.querySelector = (s) => root.querySelector(s);
  doc.querySelectorAll = (s) => root.querySelectorAll(s);
  doc.createElement = (tag) => {
    const el = new Element(tag);
    el.ownerDocument = doc;
    return el;
  };
  doc.addEventListener = (type, fn) => {
    if (!doc.listeners.has(type)) doc.listeners.set(type, []);
    doc.listeners.get(type).push(fn);
  };
  doc.removeEventListener = (type, fn) => {
    const list = doc.listeners.get(type) || [];
    const i = list.indexOf(fn);
    if (i >= 0) list.splice(i, 1);
  };
  doc.dispatchEvent = (event) => {
    for (const fn of (doc.listeners.get(event.type) || []).slice()) {
      fn.call(doc, Object.assign({ target: doc }, event));
    }
    return true;
  };
  doc.documentRoot = root;
  return doc;
}

module.exports = { createDocument, parseFragment, Element, TextNode, decodeEntities };
