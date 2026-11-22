/* Khayyam shared front-end helpers. Loaded in <head> of every page.
 *  - adds the CSRF token to every same-origin write request (fetch)
 *  - escapeHtml(): use for ANY user/AI text placed inside innerHTML
 *  - renderMarkdown(): markdown -> sanitized HTML for AI replies
 */
(function () {
  if (window.__khayyamMain) return;
  window.__khayyamMain = true;

  function csrfToken() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute('content') : '';
  }

  var nativeFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    init = init || {};
    var method = String(init.method || (input && input.method) || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS') {
      var url = typeof input === 'string' ? input : ((input && input.url) || '');
      var sameOrigin = (url.charAt(0) === '/' && url.charAt(1) !== '/') || url.indexOf(location.origin) === 0;
      if (sameOrigin) {
        var headers = new Headers(init.headers || (input && input.headers) || {});
        if (!headers.has('X-CSRFToken')) headers.set('X-CSRFToken', csrfToken());
        init.headers = headers;
      }
    }
    return nativeFetch(input, init);
  };

  window.escapeHtml = function (value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  };

  /* ───── formulas in their real order ─────
     In a right-to-left Dari line, maths, chemistry and English terms are reordered by the browser: «(5x + 6) - (2x + 1)» showed
     as «(6 + 5x) - (1 + 2x)», «-3» as «3-», «5x - 2x = 3x» backwards. MathDir finds those runs and keeps each one left to right
     (a <span dir="ltr"> chip on the page; separate left-to-right pieces on the board's canvas). */
  var MD_CH = /[A-Za-z0-9۰-۹٠-٩+\-−×÷*\/=<>≤≥≠≈^√±%°·.,()\[\]{}|!'_:⁰-₟²³¹←-⇿Ͱ-Ͽ∞′″ ]/;
  var MD_STRONG = /[A-Za-z0-9+\-−×÷*\/=<>≤≥≠≈^√±⁰-₟²³¹←-⇿Ͱ-Ͽ∞]/;
  var MD_FORMULA = /[0-9۰-۹+\-−×÷*\/=<>≤≥≠≈^√±⁰-₟²³¹←-⇿Ͱ-Ͽ∞]/;
  function mdRuns(s) {
    s = String(s == null ? '' : s);
    var out = [], i = 0, n = s.length, buf = '';
    while (i < n) {
      if (!MD_CH.test(s[i])) { buf += s[i]; i++; continue; }
      var j = i; while (j < n && MD_CH.test(s[j])) j++;
      var run = s.slice(i, j), lead = run.match(/^[\s.,:!']*/)[0], tail = run.match(/[\s.,:!']*$/)[0];
      var core = run.slice(lead.length, run.length - tail.length);
      // a lone bracket or punctuation, or plain Dari-digit numbers, need nothing
      if (core && MD_STRONG.test(core)) {
        buf += lead; if (buf) out.push({ t: buf, math: false }); buf = '';
        out.push({ t: core, math: true, formula: MD_FORMULA.test(core) });
        buf = tail;
      } else buf += run;
      i = j;
    }
    if (buf) out.push({ t: buf, math: false });
    return out;
  }
  window.MathDir = {
    runs: mdRuns,
    has: function (s) { return mdRuns(s).some(function (r) { return r.math; }); },
    // put text into an element: Dari as text, each formula as a left-to-right chip (never innerHTML: safe for any text)
    fill: function (el, s) {
      el.textContent = '';
      mdRuns(s).forEach(function (r) {
        if (!r.math) { el.appendChild(document.createTextNode(r.t)); return; }
        var sp = document.createElement('span'); sp.dir = 'ltr'; sp.className = r.formula ? 'mx' : 'mxw'; sp.textContent = r.t; el.appendChild(sp);
      });
      return el;
    },
    // the same for every text node under an element (markdown that is already sanitized); code is left alone
    wrapIn: function (root) {
      var w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null), nodes = [], t;
      while ((t = w.nextNode())) {
        var p = t.parentNode;
        if (p && p.closest && p.closest('code, pre, .mx, .mxw, a, script, style')) continue;
        if (MD_STRONG.test(t.nodeValue)) nodes.push(t);
      }
      nodes.forEach(function (node) {
        var runs = mdRuns(node.nodeValue); if (!runs.some(function (r) { return r.math; })) return;
        var frag = document.createDocumentFragment();
        runs.forEach(function (r) {
          if (!r.math) { frag.appendChild(document.createTextNode(r.t)); return; }
          var sp = document.createElement('span'); sp.dir = 'ltr'; sp.className = r.formula ? 'mx' : 'mxw'; sp.textContent = r.t; frag.appendChild(sp);
        });
        node.parentNode.replaceChild(frag, node);
      });
      return root;
    }
  };

  var hookInstalled = false;
  window.renderMarkdown = function (text) {
    text = String(text == null ? '' : text);
    if (!window.marked || !window.DOMPurify) return window.escapeHtml(text).replace(/\n/g, '<br>');
    if (!hookInstalled) {
      hookInstalled = true;
      DOMPurify.addHook('afterSanitizeAttributes', function (node) {
        if (node.tagName === 'A') {
          node.setAttribute('target', '_blank');
          node.setAttribute('rel', 'noopener noreferrer nofollow');
        }
      });
    }
    var clean = DOMPurify.sanitize(marked.parse(text), { USE_PROFILES: { html: true } });
    if (!/[\u0600-\u06FF]/.test(text)) return clean;                // no Dari: nothing to reorder
    try { var box = document.createElement('div'); box.innerHTML = clean; window.MathDir.wrapIn(box); return box.innerHTML; } catch (e) { return clean; }
  };
})();
